"""Record management: deletion, duplicate detection, merging, companies list, and bulk edits.

Deletion rules:
- Deleting a contact removes their research, drafts, scheduled actions, campaign memberships, message history and
  the raw import rows holding their data. Suppressions are kept on purpose (they are keyed by email address), so
  a deleted-then-reimported contact who opted out stays suppressed.
- Deleting a company either deletes its contacts too, or unlinks them (their research outcome drops to "needs
  review" because the company evidence is gone).
- Any draft that cited deleted evidence is superseded, so an approval can never outlive its evidence.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.core.errors import AppError, Conflict, NotFound
from app.domain.states import ActorType, AuditOutcome, DraftStatus, ResearchStatus
from app.jobs import queue
from app.models import (
    CampaignProspect,
    Company,
    EmailDraft,
    EmailDraftEvidence,
    EmailMessage,
    Prospect,
    ProspectImportRow,
    ResearchFact,
    ScheduledAction,
    User,
)
from app.services import audit
from app.services.imports.normalize import clean_text, company_name_key, normalize_domain, normalize_website
from app.services.research import store as research_store

MAX_BULK = 500


def _norm_sql(column: Any) -> Any:
    """SQL twin of the Python name keys: lower-case, trimmed, internal whitespace collapsed."""
    return func.lower(func.regexp_replace(func.trim(column), r"\s+", " ", "g"))


def _check_bulk(ids: list[uuid.UUID]) -> list[uuid.UUID]:
    unique = list(dict.fromkeys(ids))
    if not unique:
        raise AppError("Select at least one record.", code="empty_selection")
    if len(unique) > MAX_BULK:
        raise AppError(f"At most {MAX_BULK} records per bulk action.", code="selection_too_large")
    return unique


# --- Draft invalidation ---------------------------------------------------------------------------

def supersede_drafts_citing(db: Session, fact_ids: list[uuid.UUID], reason: str) -> int:
    """Supersede every live draft that cites any of these facts (evidence is about to disappear)."""
    if not fact_ids:
        return 0
    draft_ids = db.scalars(select(EmailDraftEvidence.draft_id).where(EmailDraftEvidence.fact_id.in_(fact_ids))
                           .distinct()).all()
    if not draft_ids:
        return 0
    result = db.execute(update(EmailDraft).where(
        EmailDraft.id.in_(draft_ids), EmailDraft.is_current,
        EmailDraft.status.in_([DraftStatus.PENDING_REVIEW, DraftStatus.APPROVED]))
        .values(status=DraftStatus.SUPERSEDED, is_current=False,
                validation_issues=func.coalesce(EmailDraft.validation_issues, func.jsonb("[]")).op("||")(
                    func.jsonb_build_array(func.jsonb_build_object(
                        "code", "evidence_deleted", "severity", "error", "message", reason))))
        .execution_options(synchronize_session=False))
    return int(result.rowcount)  # type: ignore[attr-defined]


# --- Deletion -------------------------------------------------------------------------------------

def _owned_prospects(db: Session, user: User, ids: list[uuid.UUID]) -> list[uuid.UUID]:
    return list(db.scalars(select(Prospect.id).where(Prospect.workspace_id == user.workspace_id,
                                                     Prospect.id.in_(ids))).all())


def delete_prospects(db: Session, user: User, ids: list[uuid.UUID], *, audit_action: str = "prospect.delete") -> int:
    pids = _owned_prospects(db, user, ids)
    if not pids:
        return 0
    for pid in pids:
        queue.cancel_by_dedupe_key(db, research_store.prospect_job_key(pid))
    db.execute(delete(EmailMessage).where(EmailMessage.prospect_id.in_(pids)))
    db.execute(delete(ProspectImportRow).where(or_(ProspectImportRow.prospect_id.in_(pids),
                                                   ProspectImportRow.existing_prospect_id.in_(pids))))
    db.execute(delete(Prospect).where(Prospect.id.in_(pids)))  # cascades research, drafts, actions, enrollments
    audit.record(db, action=audit_action, outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, metadata={"count": len(pids)})
    return len(pids)


def delete_companies(db: Session, user: User, ids: list[uuid.UUID], *, delete_contacts: bool) -> tuple[int, int]:
    cids = list(db.scalars(select(Company.id).where(Company.workspace_id == user.workspace_id,
                                                    Company.id.in_(ids))).all())
    if not cids:
        return 0, 0
    contacts = list(db.scalars(select(Prospect.id).where(Prospect.company_id.in_(cids))).all())
    removed_contacts = 0
    if delete_contacts and contacts:
        removed_contacts = delete_prospects(db, user, contacts, audit_action="prospect.delete_with_company")
    else:
        facts = list(db.scalars(select(ResearchFact.id).where(ResearchFact.company_id.in_(cids))).all())
        supersede_drafts_citing(db, facts, "The company record (and its research) was deleted.")
        for pid in db.scalars(select(Prospect.id).where(Prospect.company_id.in_(cids),
                                                        Prospect.research_status == ResearchStatus.READY)).all():
            research_store.set_status(db, Prospect, pid, ResearchStatus.NEEDS_REVIEW)
    for cid in cids:
        queue.cancel_by_dedupe_key(db, research_store.company_job_key(cid))
    db.execute(delete(Company).where(Company.id.in_(cids)))  # contacts are unlinked (company_id SET NULL)
    audit.record(db, action="company.delete", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id,
                 metadata={"count": len(cids), "delete_contacts": delete_contacts,
                           "contacts_removed": removed_contacts})
    return len(cids), removed_contacts


# --- Duplicates -----------------------------------------------------------------------------------

@dataclass
class DuplicateGroups:
    companies: list[dict[str, Any]]
    contacts: list[dict[str, Any]]


def find_duplicates(db: Session, user: User) -> DuplicateGroups:
    ws = user.workspace_id
    name = _norm_sql(Company.name)
    company_keys = db.scalars(select(name).where(Company.workspace_id == ws).group_by(name)
                              .having(func.count() > 1)).all()
    contact_counts = (select(Prospect.company_id, func.count().label("n")).group_by(Prospect.company_id).subquery())
    companies = []
    for key in company_keys:
        rows = db.execute(select(Company, func.coalesce(contact_counts.c.n, 0))
                          .outerjoin(contact_counts, contact_counts.c.company_id == Company.id)
                          .where(Company.workspace_id == ws, name == key).order_by(Company.created_at)).all()
        domains = {c.normalized_domain for c, _ in rows if c.normalized_domain}
        companies.append({"key": key, "domain_conflict": len(domains) > 1, "records": [
            {"id": c.id, "name": c.name, "normalized_domain": c.normalized_domain, "industry": c.industry,
             "research_status": c.research_status, "contacts": int(n), "created_at": c.created_at} for c, n in rows]})

    person = _norm_sql(Prospect.full_name)
    company = func.coalesce(_norm_sql(Company.name), "")
    contact_keys = db.execute(select(person, company).select_from(Prospect)
                              .join(Company, Prospect.company_id == Company.id)
                              .where(Prospect.workspace_id == ws, Prospect.full_name.is_not(None))
                              .group_by(person, company).having(func.count() > 1)).all()
    contacts = []
    for pkey, ckey in contact_keys:
        rows = db.execute(select(Prospect, Company.name).join(Company, Prospect.company_id == Company.id)
                          .where(Prospect.workspace_id == ws, person == pkey, company == ckey)
                          .order_by(Prospect.created_at)).all()
        contacts.append({"key": f"{pkey} @ {ckey}", "records": [
            {"id": p.id, "full_name": p.full_name, "email": p.email, "title": p.title, "company_id": p.company_id,
             "company_name": cname, "research_status": p.research_status, "created_at": p.created_at,
             "source": p.source} for p, cname in rows]})
    return DuplicateGroups(companies, contacts)


def duplicate_counts(db: Session, user: User) -> dict[str, int]:
    groups = find_duplicates(db, user)
    return {"company_groups": len(groups.companies), "contact_groups": len(groups.contacts)}


# --- Merging ----------------------------------------------------------------------------------------

def _load_group(db: Session, user: User, model: Any, survivor_id: uuid.UUID,
                duplicate_ids: list[uuid.UUID]) -> tuple[Any, list[Any]]:
    dup_ids = [d for d in dict.fromkeys(duplicate_ids) if d != survivor_id]
    if not dup_ids:
        raise AppError("Choose at least one duplicate to merge into the kept record.", code="nothing_to_merge")
    rows = db.scalars(select(model).where(model.workspace_id == user.workspace_id,
                                          model.id.in_([survivor_id, *dup_ids])).with_for_update()).all()
    by_id = {r.id: r for r in rows}
    if survivor_id not in by_id or any(d not in by_id for d in dup_ids):
        raise NotFound("One or more records were not found")
    return by_id[survivor_id], [by_id[d] for d in dup_ids]


def merge_companies(db: Session, user: User, survivor_id: uuid.UUID, duplicate_ids: list[uuid.UUID]) -> Company:
    survivor, dups = _load_group(db, user, Company, survivor_id, duplicate_ids)
    for dup in dups:
        db.execute(update(Prospect).where(Prospect.company_id == dup.id).values(company_id=survivor.id))
        if not survivor.normalized_domain and dup.normalized_domain:
            domain, dup.normalized_domain = dup.normalized_domain, None
            db.flush()
            survivor.normalized_domain = domain
        for field in ("website", "industry", "description", "location"):
            if not getattr(survivor, field) and getattr(dup, field):
                setattr(survivor, field, getattr(dup, field))
        facts = list(db.scalars(select(ResearchFact.id).where(ResearchFact.company_id == dup.id)).all())
        supersede_drafts_citing(db, facts, "Merged into another company record; regenerate with its research.")
        queue.cancel_by_dedupe_key(db, research_store.company_job_key(dup.id))
        db.delete(dup)
    db.flush()
    audit.record(db, action="company.merge", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="company", entity_id=survivor.id,
                 metadata={"merged": [str(d.id) for d in dups]})
    return survivor  # type: ignore[no-any-return]


_PERSON_FIELDS = ("first_name", "last_name", "full_name", "title", "linkedin_url", "industry", "country", "location",
                  "timezone")


def merge_prospects(db: Session, user: User, survivor_id: uuid.UUID, duplicate_ids: list[uuid.UUID]) -> Prospect:
    survivor, dups = _load_group(db, user, Prospect, survivor_id, duplicate_ids)
    for dup in dups:
        for field in _PERSON_FIELDS:
            if not getattr(survivor, field) and getattr(dup, field):
                setattr(survivor, field, getattr(dup, field))
        if not survivor.email_normalized and dup.email_normalized:
            email, norm, status = dup.email, dup.email_normalized, dup.email_verification_status
            dup.email, dup.email_normalized = None, None
            db.flush()
            survivor.email, survivor.email_normalized, survivor.email_verification_status = email, norm, status
        if not survivor.company_id and dup.company_id:
            survivor.company_id = dup.company_id
        survivor.attributes = {**(dup.attributes or {}), **(survivor.attributes or {})}
        # Keep outreach history on the survivor.
        db.execute(update(EmailMessage).where(EmailMessage.prospect_id == dup.id).values(prospect_id=survivor.id))
        enrolled = set(db.scalars(select(CampaignProspect.campaign_id).where(
            CampaignProspect.prospect_id == survivor.id)).all())
        for cp in db.scalars(select(CampaignProspect).where(CampaignProspect.prospect_id == dup.id)).all():
            if cp.campaign_id in enrolled:
                db.delete(cp)
                continue
            cp.prospect_id = survivor.id
            db.execute(update(EmailDraft).where(EmailDraft.prospect_id == dup.id,
                                                EmailDraft.campaign_id == cp.campaign_id)
                       .values(prospect_id=survivor.id))
            db.execute(update(ScheduledAction).where(ScheduledAction.campaign_prospect_id == cp.id)
                       .values(prospect_id=survivor.id))
        db.execute(update(ProspectImportRow).where(ProspectImportRow.prospect_id == dup.id)
                   .values(prospect_id=survivor.id))
        queue.cancel_by_dedupe_key(db, research_store.prospect_job_key(dup.id))
        db.flush()
        db.delete(dup)
    db.flush()
    audit.record(db, action="prospect.merge", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="prospect", entity_id=survivor.id,
                 metadata={"merged": [str(d.id) for d in dups]})
    return survivor  # type: ignore[no-any-return]


# --- Companies list & edit ------------------------------------------------------------------------

def companies_query(user: User, *, q: str | None, has_domain: bool | None) -> Select[Company, Any]:
    contacts = (select(Prospect.company_id, func.count().label("contacts")).group_by(Prospect.company_id).subquery())
    stmt = (select(Company, func.coalesce(contacts.c.contacts, 0).label("contacts"))
            .outerjoin(contacts, contacts.c.company_id == Company.id)
            .where(Company.workspace_id == user.workspace_id))
    if q:
        pattern = "%" + q.strip()[:200].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(or_(Company.name.ilike(pattern, escape="\\"),
                              Company.normalized_domain.ilike(pattern, escape="\\")))
    if has_domain is not None:
        has = Company.normalized_domain.is_not(None)
        stmt = stmt.where(has if has_domain else ~has)
    return stmt


def list_companies(db: Session, user: User, *, q: str | None, has_domain: bool | None, sort: str, page: int,
                   page_size: int) -> tuple[list[Any], int]:
    stmt = companies_query(user, q=q, has_domain=has_domain)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    order = {"name": func.lower(Company.name).asc(), "-created": Company.created_at.desc(),
             "-contacts": func.coalesce(stmt.selected_columns.contacts, 0).desc()}.get(sort, func.lower(Company.name))
    rows = db.execute(stmt.order_by(order, Company.id).offset((page - 1) * page_size).limit(page_size)).all()
    return list(rows), total


def update_company(db: Session, user: User, company_id: uuid.UUID, changes: dict[str, Any]) -> Company:
    company = db.scalar(select(Company).where(Company.id == company_id, Company.workspace_id == user.workspace_id))
    if company is None:
        raise NotFound("Company not found")
    changed: list[str] = []
    if "name" in changes:
        name = clean_text(changes["name"], 300)
        if not name:
            raise AppError("A company needs a name.", code="invalid_company")
        clash = db.scalar(select(Company.id).where(Company.workspace_id == user.workspace_id, Company.id != company.id,
                                                   _norm_sql(Company.name) == company_name_key(name)))
        if clash:
            raise Conflict("Another company already has this exact name. Merge them from the Duplicates page instead.",
                           code="company_name_exists", details={"company_id": str(clash)})
        if name != company.name:
            company.name, changed = name, [*changed, "name"]
    if "normalized_domain" in changes or "website" in changes:
        raw_domain = changes.get("normalized_domain")
        domain = normalize_domain(raw_domain) if raw_domain else None
        if raw_domain and domain is None:
            raise AppError(f"Not a valid domain: {raw_domain}", code="invalid_domain")
        website, issue = normalize_website(changes.get("website")) if changes.get("website") else (None, None)
        if issue:
            raise AppError(issue.message, code="invalid_website")
        domain = domain or (normalize_domain(website) if website else None)
        if "normalized_domain" in changes and domain != company.normalized_domain:
            if domain and db.scalar(select(Company.id).where(Company.workspace_id == user.workspace_id,
                                                             Company.normalized_domain == domain,
                                                             Company.id != company.id)):
                raise Conflict("Another company already uses this domain.", code="company_domain_exists")
            company.normalized_domain, changed = domain, [*changed, "domain"]
        if "website" in changes and website != company.website:
            company.website, changed = website, [*changed, "website"]
    for field, limit in (("industry", 200), ("location", 300), ("description", 5000)):
        if field in changes:
            value = clean_text(changes[field], limit)
            if value != getattr(company, field):
                setattr(company, field, value)
                changed.append(field)
    if changed:
        db.flush()
        audit.record(db, action="company.update", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                     actor_id=user.id, workspace_id=user.workspace_id, entity_type="company", entity_id=company.id,
                     metadata={"fields": changed})
    return company
