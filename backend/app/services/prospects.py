"""Prospect queries and edits. All reads are workspace-scoped; writes flush, the API commits."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.expression import Exists

from app.core.errors import AppError, Conflict, NotFound
from app.domain.states import (
    ActorType,
    AuditOutcome,
    EmailVerificationStatus,
    EnrollmentState,
    ResearchStatus,
    ScheduledActionState,
    SuppressionScope,
)
from app.models import (
    CampaignProspect,
    Company,
    EmailDraft,
    EmailMessage,
    Prospect,
    ProspectImport,
    ResearchFact,
    ScheduledAction,
    Suppression,
    User,
)
from app.services import audit
from app.services.companies import CompanyInput, resolve_companies
from app.services.imports.mapping import Target
from app.services.imports.normalize import (
    Issue,
    clean_text,
    normalize_domain,
    normalize_email,
    normalize_linkedin,
    normalize_website,
)
from app.services.imports.validation import validate_row
from app.services.suppression import active_suppressions, prospect_suppression_domains

SortKey = Literal["created", "-created", "name", "-name", "company", "-company", "research", "-research"]
_TERMINAL_ENROLLMENT = [EnrollmentState.COMPLETED, EnrollmentState.BOUNCED, EnrollmentState.UNSUBSCRIBED,
                        EnrollmentState.CANCELLED]


class ProspectValidationError(AppError):
    status_code = 422
    code = "prospect_invalid"


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _workspace_suppressed() -> Exists:
    """Correlated EXISTS: an active workspace-wide suppression matches the prospect's email or domain."""
    return exists().where(
        Suppression.workspace_id == Prospect.workspace_id,
        Suppression.lifted_at.is_(None),
        Suppression.scope == SuppressionScope.WORKSPACE,
        or_(
            and_(Prospect.email_normalized.is_not(None), Suppression.email_normalized == Prospect.email_normalized),
            Suppression.domain == func.split_part(Prospect.email_normalized, "@", 2),
            Suppression.domain == Company.normalized_domain,
        ),
    )


def _active_enrollment() -> Exists:
    return exists().where(CampaignProspect.prospect_id == Prospect.id,
                          CampaignProspect.state.not_in(_TERMINAL_ENROLLMENT))


@dataclass
class ProspectFilters:
    q: str | None = None
    research_status: list[ResearchStatus] = field(default_factory=list)
    company_id: uuid.UUID | None = None
    import_id: uuid.UUID | None = None
    has_email: bool | None = None
    suppressed: bool | None = None
    sort: SortKey = "-created"


# Row shape: (Prospect, company_name, company_domain, is_suppressed, is_enrolled, next_action_at)
ProspectRow = Select[Prospect, str, str | None, Any, Any, datetime]


def _base_query(user: User, f: ProspectFilters) -> ProspectRow:
    suppressed = _workspace_suppressed()
    next_action = (select(func.min(ScheduledAction.due_at))
                   .where(ScheduledAction.prospect_id == Prospect.id,
                          ScheduledAction.state == ScheduledActionState.PENDING)
                   .correlate(Prospect).scalar_subquery())
    stmt = (select(Prospect, Company.name.label("company_name"), Company.normalized_domain.label("company_domain"),
                   suppressed.label("is_suppressed"), _active_enrollment().label("is_enrolled"),
                   next_action.label("next_action_at"))
            .outerjoin(Company, Prospect.company_id == Company.id)
            .where(Prospect.workspace_id == user.workspace_id))
    if f.q:
        pattern = f"%{_escape_like(f.q.strip()[:200])}%"
        stmt = stmt.where(or_(*(col.ilike(pattern, escape="\\") for col in (
            Prospect.full_name, Prospect.email_normalized, Prospect.title, Company.name, Company.normalized_domain))))
    if f.research_status:
        stmt = stmt.where(Prospect.research_status.in_(f.research_status))
    if f.company_id:
        stmt = stmt.where(Prospect.company_id == f.company_id)
    if f.import_id:
        stmt = stmt.where(Prospect.import_id == f.import_id)
    if f.has_email is not None:
        has = Prospect.email_normalized.is_not(None)
        stmt = stmt.where(has if f.has_email else ~has)
    if f.suppressed is not None:
        stmt = stmt.where(suppressed if f.suppressed else ~suppressed)
    return stmt


_SORTS = {
    "created": Prospect.created_at, "name": func.lower(Prospect.full_name),
    "company": func.lower(Company.name), "research": Prospect.research_status,
}


def list_prospects(db: Session, user: User, f: ProspectFilters, *, page: int, page_size: int) -> tuple[list[Any], int]:
    stmt = _base_query(user, f)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    column = _SORTS[f.sort.lstrip("-")]
    order = column.desc().nulls_last() if f.sort.startswith("-") else column.asc().nulls_last()
    rows = db.execute(stmt.order_by(order, Prospect.id).offset((page - 1) * page_size).limit(page_size)).all()
    return list(rows), total


def outreach_status(is_suppressed: bool, is_enrolled: bool) -> Literal["not_enrolled", "enrolled", "suppressed"]:
    if is_suppressed:
        return "suppressed"
    return "enrolled" if is_enrolled else "not_enrolled"


def get_prospect(db: Session, user: User, prospect_id: uuid.UUID) -> Prospect:
    prospect = db.scalar(select(Prospect).where(Prospect.id == prospect_id, Prospect.workspace_id == user.workspace_id))
    if prospect is None:
        raise NotFound("Prospect not found")
    return prospect


@dataclass
class ProspectDetail:
    prospect: Prospect
    company: Company | None
    source_import: ProspectImport | None
    suppressions: list[Suppression]
    blockers: list[dict[str, str]]
    research_fact_count: int
    drafts: list[EmailDraft]
    messages: list[EmailMessage]
    scheduled_actions: list[ScheduledAction]
    enrollments: list[CampaignProspect]


def get_detail(db: Session, user: User, prospect_id: uuid.UUID) -> ProspectDetail:
    p = get_prospect(db, user, prospect_id)
    company = db.get(Company, p.company_id) if p.company_id else None
    source_import = db.get(ProspectImport, p.import_id) if p.import_id else None
    suppressions = active_suppressions(db, user.workspace_id, email=p.email_normalized,
                                       domains=prospect_suppression_domains(p, company))
    facts = db.scalar(select(func.count()).select_from(ResearchFact).where(ResearchFact.prospect_id == p.id)) or 0
    drafts = db.scalars(select(EmailDraft).where(EmailDraft.prospect_id == p.id)
                        .order_by(EmailDraft.created_at.desc())).all()
    messages = db.scalars(select(EmailMessage).where(EmailMessage.prospect_id == p.id)
                          .order_by(EmailMessage.created_at.desc())).all()
    actions = db.scalars(select(ScheduledAction).where(ScheduledAction.prospect_id == p.id)
                         .order_by(ScheduledAction.due_at)).all()
    enrollments = db.scalars(select(CampaignProspect).where(CampaignProspect.prospect_id == p.id)).all()
    return ProspectDetail(p, company, source_import, suppressions, blockers(p, suppressions, list(enrollments)),
                          facts, list(drafts), list(messages), list(actions), list(enrollments))


def blockers(p: Prospect, suppressions: list[Suppression], enrollments: list[CampaignProspect]) -> list[dict[str, str]]:
    """Every reason this prospect cannot currently be emailed, in plain language. Empty means nothing blocks
    *at the prospect level*; campaign, approval, and mailbox checks still run before any send."""
    out: list[dict[str, str]] = []
    if not p.email_normalized:
        out.append({"code": "no_email", "message": "No email address on file."})
    elif p.email_verification_status in {EmailVerificationStatus.INVALID, EmailVerificationStatus.BOUNCED}:
        out.append({"code": "email_unusable", "message": f"Email is marked {p.email_verification_status.value}."})
    for s in suppressions:
        scope = "this campaign" if s.scope is SuppressionScope.CAMPAIGN else "all campaigns"
        target = s.email_normalized or f"the domain {s.domain}"
        out.append({"code": "suppressed", "message": f"Suppressed ({s.reason.value}) for {target} in {scope}."})
    if p.research_status is not ResearchStatus.READY:
        out.append({"code": "research_incomplete",
                    "message": f"Research is not complete (status: {p.research_status.value.replace('_', ' ')})."})
    if not any(e.state not in _TERMINAL_ENROLLMENT for e in enrollments):
        out.append({"code": "not_enrolled", "message": "Not enrolled in an active campaign."})
    return out


# --- Writes -------------------------------------------------------------------------------------

_FIELD_TARGETS = {
    "first_name": Target.FIRST_NAME, "last_name": Target.LAST_NAME, "full_name": Target.FULL_NAME,
    "email": Target.EMAIL, "title": Target.TITLE, "company_name": Target.COMPANY_NAME,
    "company_domain": Target.COMPANY_DOMAIN, "company_website": Target.COMPANY_WEBSITE,
    "linkedin_url": Target.LINKEDIN_URL, "industry": Target.INDUSTRY, "country": Target.COUNTRY,
    "location": Target.LOCATION,
}


def _issues_payload(issues: list[Issue]) -> list[dict[str, str]]:
    return [i.as_dict() for i in issues]


def create_prospect(
    db: Session, user: User, data: dict[str, Any], attributes: dict[str, str]
) -> tuple[Prospect, list[Issue]]:
    """Manual entry runs through exactly the same validation as CSV rows."""
    raw = {k: (v or "") for k, v in data.items() if k in _FIELD_TARGETS}
    result = validate_row(0, raw, {k: _FIELD_TARGETS[k] for k in raw})
    if result.has_errors or result.normalized is None:
        raise ProspectValidationError("The prospect could not be created.", details=_issues_payload(result.issues))
    n = result.normalized
    if n["email_normalized"]:
        existing = db.scalar(select(Prospect.id).where(Prospect.workspace_id == user.workspace_id,
                                                       Prospect.email_normalized == n["email_normalized"]))
        if existing:
            raise Conflict("A prospect with this email already exists.", code="prospect_exists",
                           details={"prospect_id": str(existing)})
    company_id = _resolve_company(db, user, n)
    merged_attributes = {**attributes}
    if n.get("domain_source"):
        merged_attributes["company_domain_source"] = n["domain_source"]
    p = Prospect(
        workspace_id=user.workspace_id, company_id=company_id, first_name=n["first_name"], last_name=n["last_name"],
        full_name=n["full_name"], email=n["email"], email_normalized=n["email_normalized"],
        email_verification_status=(EmailVerificationStatus.SYNTAX_VALID if n["email_normalized"]
                                   else EmailVerificationStatus.UNVERIFIED),
        title=n["title"], linkedin_url=n["linkedin_url"], industry=n["industry"], country=n["country"],
        location=n["location"], attributes=merged_attributes, source="manual",
    )
    db.add(p)
    db.flush()
    audit.record(db, action="prospect.create", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="prospect", entity_id=p.id)
    return p, result.issues


def _resolve_company(db: Session, user: User, n: dict[str, Any]) -> uuid.UUID | None:
    item = CompanyInput(name=n.get("company_name"), domain=n.get("company_domain"),
                        website=n.get("company_website"), industry=n.get("industry"))
    if not item.key:
        return None
    return resolve_companies(db, user.workspace_id, [item]).ids.get(item.key)


def update_prospect(
    db: Session, user: User, prospect_id: uuid.UUID, changes: dict[str, Any]
) -> tuple[Prospect, list[Issue]]:
    """Apply only the supplied fields, each normalized with the same rules as imports."""
    p = get_prospect(db, user, prospect_id)
    issues: list[Issue] = []
    changed: list[str] = []

    if "email" in changes:
        email = normalize_email(changes["email"])
        if email.original and not email.is_valid:
            raise ProspectValidationError("Invalid email address.", details=_issues_payload(list(email.issues)))
        if email.normalized and email.normalized != p.email_normalized:
            clash = db.scalar(select(Prospect.id).where(Prospect.workspace_id == user.workspace_id,
                                                        Prospect.email_normalized == email.normalized,
                                                        Prospect.id != p.id))
            if clash:
                raise Conflict("Another prospect already uses this email.", code="prospect_exists",
                               details={"prospect_id": str(clash)})
        if email.normalized != p.email_normalized:
            p.email, p.email_normalized = (email.original if email.is_valid else None), email.normalized
            p.email_verification_status = (EmailVerificationStatus.SYNTAX_VALID if email.normalized
                                           else EmailVerificationStatus.UNVERIFIED)
            changed.append("email")
        issues.extend(email.issues)

    text_limits = {"first_name": 150, "last_name": 150, "full_name": 300, "title": 300, "industry": 200,
                   "country": 100, "location": 300}
    for k, limit in text_limits.items():
        if k in changes:
            value = clean_text(changes[k], limit)
            if getattr(p, k) != value:
                setattr(p, k, value)
                changed.append(k)
    if "linkedin_url" in changes:
        linkedin, linkedin_issue = normalize_linkedin(changes["linkedin_url"])
        if linkedin_issue:
            raise ProspectValidationError(linkedin_issue.message, details=_issues_payload([linkedin_issue]))
        if p.linkedin_url != linkedin:
            p.linkedin_url = linkedin
            changed.append("linkedin_url")

    if any(k in changes for k in ("company_name", "company_domain", "company_website")):
        website, website_issue = normalize_website(changes.get("company_website"))
        explicit = clean_text(changes.get("company_domain"))
        domain = normalize_domain(explicit) if explicit else None
        if website_issue or (explicit and domain is None):
            bad = [website_issue] if website_issue else [Issue("company_domain_invalid", "company_domain",
                                                             f"Not a valid domain: {explicit}")]
            raise ProspectValidationError("Invalid company details.", details=_issues_payload(bad))
        domain = domain or (normalize_domain(website) if website else None)
        name = clean_text(changes.get("company_name"), 300)
        company_id = _resolve_company(db, user, {"company_name": name, "company_domain": domain,
                                                 "company_website": website, "industry": p.industry})
        if company_id != p.company_id:
            p.company_id = company_id
            changed.append("company")

    if not p.full_name and not p.email_normalized:
        raise ProspectValidationError("A prospect needs at least a name or an email address.",
                                      details=[{"code": "no_person", "field": "row", "severity": "error",
                                                "message": "A prospect needs at least a name or an email address."}])

    if "attributes" in changes and changes["attributes"] is not None and changes["attributes"] != p.attributes:
        p.attributes = changes["attributes"]
        changed.append("attributes")

    if changed:
        db.flush()
        audit.record(db, action="prospect.update", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                     actor_id=user.id, workspace_id=user.workspace_id, entity_type="prospect", entity_id=p.id,
                     metadata={"fields": changed})
    return p, issues


def get_company(db: Session, user: User, company_id: uuid.UUID) -> tuple[Company, list[Prospect]]:
    company = db.scalar(select(Company).where(Company.id == company_id, Company.workspace_id == user.workspace_id))
    if company is None:
        raise NotFound("Company not found")
    prospects = db.scalars(select(Prospect).where(Prospect.company_id == company.id)
                           .order_by(func.lower(Prospect.full_name))).all()
    return company, list(prospects)
