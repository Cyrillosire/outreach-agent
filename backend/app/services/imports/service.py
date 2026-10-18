"""Import workflow: upload -> map & validate (repeatable) -> commit (idempotent) or cancel.

All functions flush but never commit; the API layer commits once per request.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, Conflict, NotFound
from app.db.transitions import StaleTransition, transition_row
from app.domain.states import (
    IMPORT,
    IMPORT_MAPPABLE,
    ActorType,
    AuditOutcome,
    EmailVerificationStatus,
    ImportRowStatus,
    ImportStatus,
)
from app.models import Company, Prospect, ProspectImport, ProspectImportRow, User
from app.services import audit
from app.services.companies import CompanyInput, plan_companies, resolve_companies, unify_batch
from app.services.imports.csv_parser import CsvLimits, ParsedCsv, parse_csv
from app.services.imports.mapping import MappingError, suggest_mapping, validate_mapping
from app.services.imports.normalize import Issue, company_name_key, name_key
from app.services.imports.validation import PersonIdentity, RowResult, person_identity, validate_rows

MAX_FILENAME = 255


def limits_from(settings: Settings) -> CsvLimits:
    return CsvLimits(max_bytes=settings.import_max_file_mb * 1024 * 1024, max_rows=settings.import_max_rows,
                     max_columns=settings.import_max_columns)


def get_import(db: Session, user: User, import_id: uuid.UUID, *, for_update: bool = False) -> ProspectImport:
    stmt = select(ProspectImport).where(ProspectImport.id == import_id,
                                        ProspectImport.workspace_id == user.workspace_id)
    if for_update:
        stmt = stmt.with_for_update()
    found = db.scalar(stmt)
    if found is None:
        raise NotFound("Import not found")
    return found


def create_import(db: Session, user: User, *, filename: str, data: bytes, settings: Settings) -> ProspectImport:
    """Parse and store the upload. Every original row is persisted verbatim before any interpretation."""
    parsed: ParsedCsv = parse_csv(data, limits_from(settings))
    mapping = suggest_mapping(parsed.headers)
    imp = ProspectImport(
        workspace_id=user.workspace_id, uploaded_by_id=user.id,
        original_filename=(filename or "upload.csv")[:MAX_FILENAME], file_sha256=parsed.sha256,
        file_size_bytes=parsed.size_bytes, encoding=parsed.encoding, delimiter=parsed.delimiter,
        headers=parsed.headers, row_count=len(parsed.rows), column_mapping={h: t.value for h, t in mapping.items()},
        validation_summary={"blank_rows_skipped": parsed.blank_rows_skipped, "ragged_rows": parsed.ragged_rows},
        status=ImportStatus.UPLOADED,
    )
    db.add(imp)
    db.flush()
    db.execute(pg_insert(ProspectImportRow).values([
        {"id": uuid.uuid4(), "import_id": imp.id, "row_number": n, "raw": raw, "issues": []}
        for n, raw in parsed.rows
    ]))
    audit.record(db, action="import.upload", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="prospect_import", entity_id=imp.id,
                 metadata={"rows": len(parsed.rows), "columns": len(parsed.headers), "sha256": parsed.sha256})
    return imp


def apply_mapping(db: Session, user: User, import_id: uuid.UUID, mapping: dict[str, str]) -> ProspectImport:
    imp = get_import(db, user, import_id, for_update=True)
    if imp.status not in IMPORT_MAPPABLE:
        raise Conflict(f"Mapping can't be changed once an import is {imp.status.value}.", code="import_not_mappable")
    try:
        resolved = validate_mapping(imp.headers, mapping)
    except MappingError as exc:
        raise AppError(str(exc), code="invalid_mapping") from exc

    rows = db.scalars(select(ProspectImportRow).where(ProspectImportRow.import_id == imp.id)
                      .order_by(ProspectImportRow.row_number)).all()
    ragged = set(imp.validation_summary.get("ragged_rows", []))
    results = validate_rows([(r.row_number, r.raw) for r in rows], resolved, ragged)
    existing_ids = _mark_existing(db, user.workspace_id, results)

    by_number = {r.row_number: r for r in rows}
    db.execute(update(ProspectImportRow), [{
        "id": by_number[res.row_number].id,
        "normalized": res.normalized,
        "issues": [i.as_dict() for i in res.issues],
        "status": res.status,
        "duplicate_of_row": res.duplicate_of_row,
        "existing_prospect_id": existing_ids.get(res.row_number),
    } for res in results])

    imp.column_mapping = {h: t.value for h, t in resolved.items()}
    imp.validation_summary = {
        **_summarize(results),
        "blank_rows_skipped": imp.validation_summary.get("blank_rows_skipped", 0),
        "ragged_rows": sorted(ragged),
        **_company_preview(db, user.workspace_id, results),
    }
    imp.error = None
    if imp.status is ImportStatus.UPLOADED:
        transition_row(db, ProspectImport, imp.id, IMPORT, ImportStatus.VALIDATED, column="status",
                       expected_from=ImportStatus.UPLOADED)
    db.flush()
    db.refresh(imp)
    audit.record(db, action="import.map", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="prospect_import", entity_id=imp.id,
                 metadata={"valid": imp.validation_summary["valid"], "invalid": imp.validation_summary["invalid"]})
    return imp


def _mark_existing(db: Session, workspace_id: uuid.UUID, results: list[RowResult]) -> dict[int, uuid.UUID]:
    """Mark rows matching an existing contact by email, or by exact name at the same company."""
    marked: dict[int, uuid.UUID] = {}
    emails = {r.normalized["email_normalized"]: r for r in results
              if r.status is ImportRowStatus.VALID and r.normalized and r.normalized["email_normalized"]}
    if emails:
        found = dict(db.execute(select(Prospect.email_normalized, Prospect.id).where(
            Prospect.workspace_id == workspace_id, Prospect.email_normalized.in_(list(emails)))).all())
        for email, prospect_id in found.items():
            row = emails[email]
            row.status = ImportRowStatus.ALREADY_EXISTS
            row.issues.append(Issue("already_exists", "email",
                                    "A contact with this email already exists; it will not be changed or duplicated."))
            marked[row.row_number] = prospect_id

    candidates = [(r, ident) for r in results if r.status is ImportRowStatus.VALID and r.normalized
                  and (ident := person_identity(r.normalized)) is not None]
    matches = match_existing_people(db, workspace_id, [(r.row_number, i) for r, i in candidates])
    for row_number, existing_id in matches.items():
        r = next(c for c, _ in candidates if c.row_number == row_number)
        r.status = ImportRowStatus.ALREADY_EXISTS
        r.issues.append(Issue("already_exists_name", "full_name",
                              "A contact with the same name at the same company already exists; it will not be "
                              "duplicated. Edit the existing contact if this row has newer details."))
        marked[row_number] = existing_id
    return marked


def match_existing_people(db: Session, workspace_id: uuid.UUID, people: list[tuple[int, PersonIdentity]],
                          exclude: uuid.UUID | None = None) -> dict[int, uuid.UUID]:
    """Map caller keys to existing prospects with the same name at the same company (see PersonIdentity)."""
    if not people:
        return {}
    names = sorted({p.name for _, p in people})
    norm_name = func.lower(func.regexp_replace(func.trim(Prospect.full_name), r"\s+", " ", "g"))
    rows = db.execute(select(Prospect.id, Prospect.full_name, Company.name, Company.normalized_domain)
                      .outerjoin(Company, Prospect.company_id == Company.id)
                      .where(Prospect.workspace_id == workspace_id, norm_name.in_(names),
                             Prospect.id != exclude if exclude else Prospect.id.is_not(None))
                      .order_by(Prospect.created_at)).all()
    existing = [(pid, PersonIdentity(name_key(full) or "", company_name_key(cname), domain))
                for pid, full, cname, domain in rows]
    out: dict[int, uuid.UUID] = {}
    for key, ident in people:
        hit = next((pid for pid, other in existing if ident.same_person(other)), None)
        if hit is not None:
            out[key] = hit
    return out


def _summarize(results: list[RowResult]) -> dict[str, Any]:
    statuses = Counter(r.status.value for r in results)
    issue_counts = Counter(i.code for r in results for i in r.issues)
    return {
        "total_rows": len(results),
        "valid": statuses.get(ImportRowStatus.VALID.value, 0),
        "invalid": statuses.get(ImportRowStatus.INVALID.value, 0),
        "duplicate_in_file": statuses.get(ImportRowStatus.DUPLICATE_IN_FILE.value, 0),
        "already_exists": statuses.get(ImportRowStatus.ALREADY_EXISTS.value, 0),
        "with_warnings": sum(1 for r in results if r.status is ImportRowStatus.VALID and r.issues),
        "without_email": sum(1 for r in results if r.status is ImportRowStatus.VALID and r.normalized
                             and not r.normalized["email_normalized"]),
        "issue_counts": dict(issue_counts.most_common()),
    }


def _company_inputs(results: list[RowResult]) -> list[CompanyInput]:
    return [CompanyInput(name=r.normalized["company_name"], domain=r.normalized["company_domain"],
                         website=r.normalized["company_website"], industry=r.normalized["industry"])
            for r in results if r.status is ImportRowStatus.VALID and r.normalized]


def _company_preview(db: Session, workspace_id: uuid.UUID, results: list[RowResult]) -> dict[str, int]:
    inputs = _company_inputs(results)
    unified = unify_batch(inputs)  # one entry per distinct company in the file
    plan = plan_companies(db, workspace_id, inputs)
    existing = len({plan[k] for k in unified if plan.get(k) is not None})
    new = sum(1 for k in unified if plan.get(k) is None)
    return {"companies_total": existing + new, "companies_existing": existing, "companies_new": new}


def commit_import(db: Session, user: User, import_id: uuid.UUID, settings: Settings | None = None) -> ProspectImport:
    """Create companies and prospects for every valid row, in one transaction.

    Idempotent: committing a completed import returns it unchanged. Concurrent commits serialize on the
    import row lock taken by the compare-and-set; the loser observes `completed` and returns it.
    """
    imp = get_import(db, user, import_id)
    if imp.status is ImportStatus.COMPLETED:
        return imp
    try:
        transition_row(db, ProspectImport, imp.id, IMPORT, ImportStatus.IMPORTING, column="status",
                       expected_from=ImportStatus.VALIDATED)
    except StaleTransition:
        current = db.scalar(select(ProspectImport.status).where(ProspectImport.id == imp.id))
        if current is ImportStatus.COMPLETED:
            db.refresh(imp)
            return imp
        raise Conflict(f"Only validated imports can be committed (current status: {current}).",
                       code="import_not_committable") from None

    rows = db.scalars(select(ProspectImportRow).where(
        ProspectImportRow.import_id == imp.id, ProspectImportRow.status == ImportRowStatus.VALID)
        .order_by(ProspectImportRow.row_number)).all()
    inputs = {r.row_number: CompanyInput(name=r.normalized["company_name"], domain=r.normalized["company_domain"],
                                         website=r.normalized["company_website"],
                                         industry=r.normalized["industry"])
              for r in rows if r.normalized}
    companies = resolve_companies(db, user.workspace_id, list(inputs.values()))

    created: dict[int, uuid.UUID] = {}
    if rows:
        values = []
        for r in rows:
            n = r.normalized or {}
            company_key = inputs[r.row_number].key
            attributes = dict(n.get("attributes") or {})
            if n.get("segment"):
                attributes["segment"] = n["segment"]
            if n.get("domain_source"):
                attributes["company_domain_source"] = n["domain_source"]
            values.append({
                "id": uuid.uuid4(), "workspace_id": user.workspace_id, "import_id": imp.id,
                "company_id": companies.ids.get(company_key) if company_key else None,
                "first_name": n.get("first_name"), "last_name": n.get("last_name"), "full_name": n.get("full_name"),
                "email": n.get("email"), "email_normalized": n.get("email_normalized"),
                "email_verification_status": (EmailVerificationStatus.SYNTAX_VALID if n.get("email_normalized")
                                              else EmailVerificationStatus.UNVERIFIED),
                "title": n.get("title"), "linkedin_url": n.get("linkedin_url"), "industry": n.get("industry"),
                "country": n.get("country"), "location": n.get("location"), "attributes": attributes,
                "source": "csv_import", "source_row": r.raw, "source_row_number": r.row_number,
            })
        # ON CONFLICT covers prospects created after validation (another import or manual entry).
        inserted = db.execute(
            pg_insert(Prospect).values(values)
            .on_conflict_do_nothing(index_elements=["workspace_id", "email_normalized"],
                                    index_where=Prospect.email_normalized.is_not(None))
            .returning(Prospect.id, Prospect.source_row_number)
        ).all()
        created = {row_number: pid for pid, row_number in inserted if row_number is not None}

    late_conflicts = [r for r in rows if r.row_number not in created]
    existing_by_email = {}
    if late_conflicts:
        existing_by_email = dict(db.execute(select(Prospect.email_normalized, Prospect.id).where(
            Prospect.workspace_id == user.workspace_id,
            Prospect.email_normalized.in_([(r.normalized or {}).get("email_normalized") for r in late_conflicts]),
        )).all())
    updates = []
    for r in rows:
        if r.row_number in created:
            updates.append({"id": r.id, "status": ImportRowStatus.IMPORTED, "prospect_id": created[r.row_number]})
        else:
            email = (r.normalized or {}).get("email_normalized")
            updates.append({"id": r.id, "status": ImportRowStatus.ALREADY_EXISTS,
                            "existing_prospect_id": existing_by_email.get(email),
                            "issues": [*r.issues, Issue("already_exists", "email",
                                       "Created by someone else after validation; not duplicated.").as_dict()]})
    if updates:
        db.execute(update(ProspectImportRow), updates)

    research_queued = 0
    if settings is not None and settings.research_auto_start and created:
        from app.services.research.store import request_prospect_research
        for prospect in db.scalars(select(Prospect).where(Prospect.id.in_(list(created.values())))).all():
            request_prospect_research(db, prospect=prospect, settings=settings)
            research_queued += 1

    transition_row(db, ProspectImport, imp.id, IMPORT, ImportStatus.COMPLETED, column="status",
                   expected_from=ImportStatus.IMPORTING,
                   extra_values={"committed_at": datetime.now(UTC), "validation_summary": {
                       **imp.validation_summary, "imported": len(created),
                       "already_exists": imp.validation_summary.get("already_exists", 0) + len(late_conflicts),
                       "companies_created": companies.created, "companies_reused": companies.reused,
                       "research_queued": research_queued}})
    db.flush()
    db.refresh(imp)
    audit.record(db, action="import.commit", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="prospect_import", entity_id=imp.id,
                 metadata={"imported": len(created), "companies_created": companies.created})
    return imp


def cancel_import(db: Session, user: User, import_id: uuid.UUID) -> ProspectImport:
    imp = get_import(db, user, import_id)
    if imp.status is ImportStatus.CANCELLED:
        return imp
    try:
        transition_row(db, ProspectImport, imp.id, IMPORT, ImportStatus.CANCELLED, column="status")
    except StaleTransition:
        raise Conflict(f"An import that is {imp.status.value} can't be cancelled.",
                       code="import_not_cancellable") from None
    db.refresh(imp)
    audit.record(db, action="import.cancel", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="prospect_import", entity_id=imp.id)
    return imp


def list_rows(db: Session, user: User, import_id: uuid.UUID, *, status: ImportRowStatus | None,
              with_issues: bool, page: int, page_size: int) -> tuple[list[ProspectImportRow], int]:
    imp = get_import(db, user, import_id)
    stmt = select(ProspectImportRow).where(ProspectImportRow.import_id == imp.id)
    if status is not None:
        stmt = stmt.where(ProspectImportRow.status == status)
    if with_issues:
        stmt = stmt.where(func.jsonb_array_length(ProspectImportRow.issues) > 0)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = db.scalars(stmt.order_by(ProspectImportRow.row_number).offset((page - 1) * page_size)
                       .limit(page_size)).all()
    return list(items), total


def list_imports(db: Session, user: User, *, limit: int = 20) -> list[ProspectImport]:
    return list(db.scalars(select(ProspectImport).where(ProspectImport.workspace_id == user.workspace_id)
                           .order_by(ProspectImport.created_at.desc()).limit(limit)).all())


__all__ = ["apply_mapping", "cancel_import", "commit_import", "create_import", "get_import",
           "list_imports", "list_rows"]
