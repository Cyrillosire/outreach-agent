"""Draft review operations: listing, detail, editing (new version), approval, rejection, bulk approval, regeneration.

Approval rules:
- Only a human can approve, only the current version, and only the exact content they saw (content hash).
- Any edit or regeneration creates a new version, superseding the old one (and its approval).
- Drafts with error-level issues need an explicit acknowledgement; bulk approval skips them.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, Conflict, NotFound
from app.domain.states import (
    DRAFT,
    ActorType,
    AuditOutcome,
    DraftStatus,
    EnrollmentState,
)
from app.models import (
    Campaign,
    CampaignProspect,
    Company,
    EmailDraft,
    EmailDraftEvidence,
    Prospect,
    ResearchFact,
    ResearchFactEvidence,
    ResearchSource,
    User,
)
from app.services import audit
from app.services.campaigns.service import brief_of, get_campaign, queue_drafting, steps_of
from app.services.drafting.core import (
    SECTION_ORDER,
    DraftClaim,
    EvidenceItem,
    FactInput,
    OfferingInput,
    OfferingRefs,
    ProspectInput,
    assemble_body,
    content_hash,
    greeting_for,
    score,
    tidy_text,
    validate,
    word_count,
)
from app.services.drafting.pipeline import load_facts

MAX_BULK_APPROVE = 200


def _draft(db: Session, user: User, draft_id: uuid.UUID, *, for_update: bool = False) -> EmailDraft:
    stmt = select(EmailDraft).where(EmailDraft.id == draft_id, EmailDraft.workspace_id == user.workspace_id)
    d = db.scalar(stmt.with_for_update() if for_update else stmt)
    if d is None:
        raise NotFound("Draft not found")
    return d


DraftRow = Select[EmailDraft, str | None, str | None, str | None, str, bool]


def drafts_query(user: User, *, campaign_id: uuid.UUID | None, status: DraftStatus | None, flagged: bool | None,
                 step: int | None, prospect_id: uuid.UUID | None) -> DraftRow:
    has_errors = EmailDraft.validation_issues.contains([{"severity": "error"}])
    stmt: DraftRow = (select(EmailDraft, Prospect.full_name, Prospect.email, Company.name, Campaign.name,
                                has_errors.label("err"))
            .join(Prospect, Prospect.id == EmailDraft.prospect_id)
            .outerjoin(Company, Company.id == Prospect.company_id)
            .join(Campaign, Campaign.id == EmailDraft.campaign_id)
            .where(EmailDraft.workspace_id == user.workspace_id, EmailDraft.is_current))
    if campaign_id:
        stmt = stmt.where(EmailDraft.campaign_id == campaign_id)
    if status:
        stmt = stmt.where(EmailDraft.status == status)
    if flagged is not None:
        stmt = stmt.where(has_errors if flagged else ~has_errors)
    if step is not None:
        stmt = stmt.where(EmailDraft.step_order == step)
    if prospect_id:
        stmt = stmt.where(EmailDraft.prospect_id == prospect_id)
    return stmt


def list_drafts(db: Session, user: User, *, page: int, page_size: int, **filters: Any) -> tuple[list[Any], int]:
    stmt = drafts_query(user, **filters)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(stmt.order_by(EmailDraft.overall_score.desc().nulls_last(), EmailDraft.created_at)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return list(rows), total


@dataclass
class DraftDetail:
    draft: EmailDraft
    prospect: Prospect
    company: Company | None
    campaign: Campaign
    enrollment: CampaignProspect | None
    evidence: list[dict[str, Any]]
    versions: list[dict[str, Any]]
    total_steps: int


def get_detail(db: Session, user: User, draft_id: uuid.UUID) -> DraftDetail:
    d = _draft(db, user, draft_id)
    prospect = db.get(Prospect, d.prospect_id)
    campaign = db.get(Campaign, d.campaign_id)
    assert prospect is not None and campaign is not None
    company = db.get(Company, prospect.company_id) if prospect.company_id else None
    enrollment = db.scalar(select(CampaignProspect).where(CampaignProspect.campaign_id == campaign.id,
                                                          CampaignProspect.prospect_id == prospect.id))
    refs = {e["fact_id"]: e["ref"] for e in (d.generation_metadata or {}).get("evidence", [])}
    rows = db.execute(select(ResearchFact, ResearchFactEvidence, ResearchSource)
                      .join(ResearchFactEvidence, ResearchFactEvidence.fact_id == ResearchFact.id)
                      .join(ResearchSource, ResearchSource.id == ResearchFactEvidence.source_id)
                      .where(ResearchFact.id.in_([uuid.UUID(f) for f in refs]))).all() if refs else []
    facts: dict[str, dict[str, Any]] = {}
    for fact, ev, src in rows:
        item = facts.setdefault(str(fact.id), {
            "ref": refs.get(str(fact.id)), "fact_id": str(fact.id), "claim": fact.claim, "category": fact.category,
            "kind": fact.kind.value, "verification_status": fact.verification_status.value,
            "subject": "person" if fact.prospect_id else "company", "sources": []})
        item["sources"].append({"url": src.url, "title": src.title, "publisher": src.publisher, "excerpt": ev.excerpt,
                                "published_at": src.published_at.isoformat() if src.published_at else None})
    cited = {e.fact_id for e in db.scalars(select(EmailDraftEvidence).where(EmailDraftEvidence.draft_id == d.id)).all()}
    evidence = sorted(facts.values(), key=lambda f: (uuid.UUID(f["fact_id"]) not in cited, f["ref"] or ""))
    for f in evidence:
        f["cited"] = uuid.UUID(f["fact_id"]) in cited
    versions = [{"id": v.id, "version": v.version, "status": v.status.value, "generated_by": v.generated_by,
                 "overall_score": float(v.overall_score) if v.overall_score is not None else None,
                 "created_at": v.created_at, "is_current": v.is_current}
                for v in db.scalars(select(EmailDraft).where(EmailDraft.campaign_id == d.campaign_id,
                                                             EmailDraft.prospect_id == d.prospect_id,
                                                             EmailDraft.step_order == d.step_order)
                                    .order_by(EmailDraft.version.desc())).all()]
    return DraftDetail(d, prospect, company, campaign, enrollment, evidence, versions, len(steps_of(db, campaign)))


# --- Editing -----------------------------------------------------------------------------------------

def _evidence_items(db: Session, draft: EmailDraft, prospect: Prospect) -> list[EvidenceItem]:
    meta = (draft.generation_metadata or {}).get("evidence", [])
    by_id = {f.fact_id: f for f in load_facts(db, company_id=prospect.company_id, prospect_id=prospect.id)}
    items = []
    for entry in meta:
        fact: FactInput | None = by_id.get(entry["fact_id"])
        if fact is not None:
            items.append(EvidenceItem(ref=entry["ref"], fact=fact, score=0.0))
    return items


def edit_draft(db: Session, user: User, draft_id: uuid.UUID, *, base_version: int, subject: str | None,
               sections: dict[str, str] | None) -> EmailDraft:
    d = _draft(db, user, draft_id, for_update=True)
    if not d.is_current:
        raise Conflict("This isn't the current version of the draft.", code="draft_not_current")
    if d.version != base_version:
        raise Conflict("The draft changed since you opened it. Reload to see the latest version.",
                       code="draft_version_conflict")
    campaign = db.get(Campaign, d.campaign_id)
    prospect = db.get(Prospect, d.prospect_id)
    assert campaign is not None and prospect is not None
    brief = brief_of(campaign)
    new_sections = {k: tidy_text((sections or {}).get(k, d.sections.get(k, ""))) for k in SECTION_ORDER}
    new_subject = tidy_text(subject if subject is not None else d.subject)[:150]
    if new_subject == d.subject and new_sections == {k: d.sections.get(k, "") for k in SECTION_ORDER}:
        return d
    company = db.get(Company, prospect.company_id) if prospect.company_id else None
    pinput = ProspectInput(first_name=prospect.first_name, full_name=prospect.full_name, title=prospect.title,
                           company_name=company.name if company else None, location=prospect.location)
    greeting = greeting_for(brief, pinput)
    body = assemble_body(greeting=greeting, sections=new_sections, brief=brief)
    meta = d.generation_metadata or {}
    oref = meta.get("offering_refs", {})
    refs = OfferingRefs(items=dict(oref.get("items", {})),
                        proofs={k: (v[0], v[1]) for k, v in oref.get("proofs", {}).items()})
    text = " ".join(new_sections.values()).lower()
    claims = [DraftClaim.model_validate(c) for c in (d.plan or {}).get("claims", [])
              if " ".join(c.get("text", "").lower().split())[:60] in " ".join(text.split())]
    evidence = _evidence_items(db, d, prospect)
    max_words = brief.style.max_words if d.step_order == 0 else max(50, int(brief.style.max_words * 0.7))
    from app.models import Offering
    from app.services.campaigns.brief import OfferingDetails  # local: avoids import cycle at module load
    offering_row = db.get(Offering, campaign.offering_id) if campaign.offering_id else None
    offering = OfferingInput(name=offering_row.name if offering_row else "", offering_type="other", category=None,
                             summary=offering_row.summary if offering_row else "",
                             details=OfferingDetails.model_validate(offering_row.details if offering_row else {}))
    issues = validate(subject=new_subject, sections=new_sections, claims=claims, evidence=evidence, refs=refs,
                      brief=brief, offering=offering, prospect=pinput, max_words=max_words)
    used = [e for e in evidence if any(e.ref in c.refs for c in claims)]
    quality = score(issues=issues, review=None, evidence_used=used, words=word_count(new_sections),
                    max_words=max_words, min_score=0.0)
    quality["edited_by_user"] = True
    d.is_current = False
    DRAFT.assert_transition(d.status, DraftStatus.SUPERSEDED)
    d.status = DraftStatus.SUPERSEDED
    db.flush()
    new = EmailDraft(
        workspace_id=d.workspace_id, campaign_id=d.campaign_id, prospect_id=d.prospect_id,
        sequence_step_id=d.sequence_step_id, step_order=d.step_order, draft_type=d.draft_type, version=d.version + 1,
        is_current=True, subject=new_subject, body_text=body, content_sha256=content_hash(new_subject, body),
        personalization_rationale=d.personalization_rationale, sections=new_sections,
        plan={**(d.plan or {}), "claims": [c.model_dump() for c in claims]}, quality_scores=quality,
        validation_issues=[i.as_dict() for i in issues], generated_by="user",
        generation_metadata={**meta, "edited_from_version": d.version, "edited_at": datetime.now(UTC).isoformat()},
        status=DraftStatus.PENDING_REVIEW, overall_score=quality["overall"], fit=d.fit)
    db.add(new)
    db.flush()
    kept = {c.text for c in claims}
    for ev in db.scalars(select(EmailDraftEvidence).where(EmailDraftEvidence.draft_id == d.id)).all():
        if ev.claim_text in kept:
            db.add(EmailDraftEvidence(draft_id=new.id, fact_id=ev.fact_id, claim_text=ev.claim_text))
    db.flush()
    audit.record(db, action="draft.edit", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER, actor_id=user.id,
                 workspace_id=user.workspace_id, entity_type="email_draft", entity_id=new.id,
                 metadata={"from_version": d.version})
    return new


# --- Approval -----------------------------------------------------------------------------------------

def _errors(d: EmailDraft) -> list[dict[str, Any]]:
    return [i for i in (d.validation_issues or []) if i.get("severity") == "error"]


def approve(db: Session, user: User, draft_id: uuid.UUID, *, content_sha256: str, acknowledge_issues: bool,
            settings: Settings) -> EmailDraft:
    d = _draft(db, user, draft_id, for_update=True)
    if not d.is_current or d.status is not DraftStatus.PENDING_REVIEW:
        raise Conflict(f"Only the current, pending version can be approved (this one is {d.status.value}).",
                       code="draft_not_approvable")
    if content_sha256 != d.content_sha256:
        raise Conflict("The draft content changed since you reviewed it. Reload and review again.",
                       code="draft_content_changed")
    errors = _errors(d)
    if errors and not acknowledge_issues:
        raise Conflict("This draft has unresolved issues. Fix them, or approve with acknowledgement.",
                       code="draft_has_issues", details=errors)
    DRAFT.assert_transition(d.status, DraftStatus.APPROVED, by_human=True)
    d.status = DraftStatus.APPROVED
    d.approved_by_id = user.id
    d.approved_at = datetime.now(UTC)
    d.approved_content_sha256 = d.content_sha256
    db.flush()
    audit.record(db, action="draft.approve", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="email_draft", entity_id=d.id,
                 metadata={"version": d.version, "content_sha256": d.content_sha256,
                           "acknowledged_issues": len(errors) if acknowledge_issues else 0})
    _queue_followups_after_initial(db, d)
    return d


def _queue_followups_after_initial(db: Session, d: EmailDraft) -> None:
    if d.step_order != 0:
        return
    campaign = db.get(Campaign, d.campaign_id)
    assert campaign is not None
    if brief_of(campaign).sequence.draft_followups != "after_initial_approved":
        return
    total = len(steps_of(db, campaign))
    existing = set(db.scalars(select(EmailDraft.step_order).where(
        EmailDraft.campaign_id == d.campaign_id, EmailDraft.prospect_id == d.prospect_id, EmailDraft.is_current)).all())
    missing = [s for s in range(1, total) if s not in existing]
    cp = db.scalar(select(CampaignProspect).where(CampaignProspect.campaign_id == d.campaign_id,
                                                  CampaignProspect.prospect_id == d.prospect_id))
    if missing and cp is not None and cp.state is EnrollmentState.AWAITING_APPROVAL:
        with contextlib.suppress(Conflict):  # a drafting job is already running for this contact
            queue_drafting(db, cp, steps=missing, force=True)


def reject(db: Session, user: User, draft_id: uuid.UUID, *, reason: str) -> EmailDraft:
    d = _draft(db, user, draft_id, for_update=True)
    if not d.is_current or d.status is not DraftStatus.PENDING_REVIEW:
        raise Conflict("Only the current, pending version can be rejected.", code="draft_not_rejectable")
    DRAFT.assert_transition(d.status, DraftStatus.REJECTED, by_human=True)
    d.status = DraftStatus.REJECTED
    d.rejected_reason = reason.strip()[:1000] or None
    cp = db.scalar(select(CampaignProspect).where(CampaignProspect.campaign_id == d.campaign_id,
                                                  CampaignProspect.prospect_id == d.prospect_id))
    if cp is not None and d.step_order == 0:
        cp.blocker = f"Initial draft rejected{': ' + d.rejected_reason if d.rejected_reason else ''}. Regenerate it."
    db.flush()
    audit.record(db, action="draft.reject", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER, actor_id=user.id,
                 workspace_id=user.workspace_id, entity_type="email_draft", entity_id=d.id)
    return d


@dataclass
class BulkApproval:
    approved: list[uuid.UUID]
    skipped: list[dict[str, str]]


def bulk_approve(db: Session, user: User, draft_ids: list[uuid.UUID], settings: Settings) -> BulkApproval:
    if not draft_ids:
        raise AppError("Select at least one draft.", code="empty_selection")
    if len(draft_ids) > MAX_BULK_APPROVE:
        raise AppError(f"At most {MAX_BULK_APPROVE} drafts per bulk approval.", code="selection_too_large")
    result = BulkApproval([], [])
    for did in dict.fromkeys(draft_ids):
        try:
            with db.begin_nested():
                d = _draft(db, user, did, for_update=True)
                if _errors(d):
                    result.skipped.append({"id": str(did), "reason": "Has unresolved issues; review it individually."})
                    continue
                approve(db, user, did, content_sha256=d.content_sha256, acknowledge_issues=False, settings=settings)
            result.approved.append(did)
        except (Conflict, NotFound) as exc:
            result.skipped.append({"id": str(did), "reason": exc.message})
    return result


def regenerate(db: Session, user: User, draft_id: uuid.UUID, *, sections: list[str],
               instruction: str | None) -> uuid.UUID:
    d = _draft(db, user, draft_id)
    if not d.is_current:
        raise Conflict("Regenerate from the current version.", code="draft_not_current")
    invalid = [s for s in sections if s not in SECTION_ORDER]
    if invalid:
        raise AppError(f"Unknown sections: {invalid}", code="invalid_sections")
    cp = db.scalar(select(CampaignProspect).where(CampaignProspect.campaign_id == d.campaign_id,
                                                  CampaignProspect.prospect_id == d.prospect_id))
    if cp is None:
        raise Conflict("This contact is no longer in the campaign.", code="not_enrolled")
    job_id = queue_drafting(db, cp, steps=[d.step_order], force=True, mode="regenerate", draft_id=d.id,
                            sections=sections, instruction=(instruction or "").strip()[:500] or None)
    audit.record(db, action="draft.regenerate", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="email_draft", entity_id=d.id,
                 metadata={"sections": sections or "all"})
    return job_id


def generate_for_prospect(db: Session, user: User, prospect_id: uuid.UUID, campaign_id: uuid.UUID, settings: Settings,
                          *, force: bool) -> dict[str, Any]:
    from app.services.campaigns.service import advance, enroll

    campaign = get_campaign(db, user, campaign_id)
    cp = db.scalar(select(CampaignProspect).where(CampaignProspect.campaign_id == campaign.id,
                                                  CampaignProspect.prospect_id == prospect_id))
    if cp is None or cp.state not in (EnrollmentState.ENROLLED, EnrollmentState.AWAITING_DRAFT,
                                      EnrollmentState.AWAITING_APPROVAL):
        results = enroll(db, user, campaign.id, [prospect_id], settings)
        if results[0].outcome == "skipped":
            raise Conflict(f"Can't draft for this contact: {results[0].reason}", code="not_eligible")
        cp = db.scalar(select(CampaignProspect).where(CampaignProspect.campaign_id == campaign.id,
                                                      CampaignProspect.prospect_id == prospect_id))
        assert cp is not None
        return {"enrollment_id": str(cp.id), "outcome": "enrolled", "blocker": cp.blocker}
    prospect = db.get(Prospect, prospect_id)
    assert prospect is not None
    outcome = advance(db, cp, campaign, prospect, settings, force=force)
    return {"enrollment_id": str(cp.id), "outcome": outcome, "blocker": cp.blocker}
