"""Offerings, campaigns, sequences and enrollment. Functions flush; the API layer commits."""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, Conflict, NotFound
from app.db.transitions import StaleTransition, transition_row
from app.domain.states import (
    CAMPAIGN,
    ENROLLMENT,
    JOB_ACTIVE,
    SCHEDULED_ACTION,
    ActorType,
    AuditOutcome,
    CampaignStatus,
    ContentPolicy,
    DraftStatus,
    DraftType,
    EmailVerificationStatus,
    EnrollmentState,
    Fit,
    OfferingType,
    OutreachMotion,
    ResearchStatus,
    ScheduledActionState,
    SuppressionScope,
)
from app.jobs import queue
from app.models import (
    Campaign,
    CampaignProspect,
    Company,
    EmailAccount,
    EmailDraft,
    Job,
    Offering,
    Prospect,
    ScheduledAction,
    Sequence,
    SequenceStep,
    User,
)
from app.services import audit
from app.services.campaigns.brief import CampaignBrief, OfferingDetails, ReadinessItem, is_ready, readiness
from app.services.campaigns.presets import PLAYBOOKS, default_brief
from app.services.research import store as research_store
from app.services.suppression import active_suppressions, prospect_suppression_domains

DRAFT_JOB = "drafts.generate"
_ACTIVE_ENROLLMENT = [EnrollmentState.ENROLLED, EnrollmentState.AWAITING_DRAFT, EnrollmentState.AWAITING_APPROVAL,
                      EnrollmentState.SCHEDULED, EnrollmentState.IN_SEQUENCE, EnrollmentState.PAUSED]
_RESEARCH_BUSY = {ResearchStatus.PENDING, ResearchStatus.RESEARCHING_COMPANY, ResearchStatus.RESEARCHING_INDIVIDUAL,
                  ResearchStatus.VERIFYING}


def draft_job_key(enrollment_id: uuid.UUID) -> str:
    return f"drafts:{enrollment_id}"


def _validation_error(exc: ValidationError, what: str) -> AppError:
    details = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
    return AppError(f"The {what} is not valid.", code="invalid_" + what.replace(" ", "_"), details=details)


# --- Offerings ------------------------------------------------------------------------------------

def get_offering(db: Session, user: User, offering_id: uuid.UUID) -> Offering:
    o = db.scalar(select(Offering).where(Offering.id == offering_id, Offering.workspace_id == user.workspace_id))
    if o is None:
        raise NotFound("Offering not found")
    return o


def save_offering(db: Session, user: User, data: dict[str, Any], offering_id: uuid.UUID | None = None) -> Offering:
    try:
        details = OfferingDetails.model_validate(data.get("details") or {})
    except ValidationError as exc:
        raise _validation_error(exc, "offering details") from exc
    name = " ".join(str(data.get("name") or "").split())[:200]
    if offering_id is None:
        if not name or not data.get("summary"):
            raise AppError("An offering needs a name and a summary.", code="invalid_offering")
        offering = Offering(workspace_id=user.workspace_id, name=name, summary=str(data["summary"])[:4000],
                            offering_type=OfferingType(data.get("offering_type") or OfferingType.PRODUCT),
                            category=data.get("category"), details=details.model_dump(mode="json"))
        db.add(offering)
    else:
        offering = get_offering(db, user, offering_id)
        if name:
            offering.name = name
        for field in ("summary", "category"):
            if field in data and data[field] is not None:
                setattr(offering, field, str(data[field])[:4000])
        if data.get("offering_type"):
            offering.offering_type = OfferingType(data["offering_type"])
        if "details" in data:
            offering.details = details.model_dump(mode="json")
        if "archived" in data:
            offering.archived = bool(data["archived"])
    with db.no_autoflush:
        clash_q = select(Offering.id).where(Offering.workspace_id == user.workspace_id,
                                            func.lower(Offering.name) == offering.name.lower())
        if offering.id is not None:
            clash_q = clash_q.where(Offering.id != offering.id)
        clash = db.scalar(clash_q)
    if clash:
        if offering_id is None:
            db.expunge(offering)
        raise Conflict("Another offering already has this name.", code="offering_name_exists")
    db.flush()
    audit.record(db, action="offering.save", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER, actor_id=user.id,
                 workspace_id=user.workspace_id, entity_type="offering", entity_id=offering.id)
    return offering


def delete_offering(db: Session, user: User, offering_id: uuid.UUID) -> str:
    offering = get_offering(db, user, offering_id)
    in_use = db.scalar(select(func.count()).select_from(Campaign).where(Campaign.offering_id == offering.id)) or 0
    if in_use:
        offering.archived = True
        return "archived"
    db.delete(offering)
    return "deleted"


# --- Campaigns ------------------------------------------------------------------------------------

def get_campaign(db: Session, user: User, campaign_id: uuid.UUID, *, for_update: bool = False) -> Campaign:
    stmt = select(Campaign).where(Campaign.id == campaign_id, Campaign.workspace_id == user.workspace_id)
    c = db.scalar(stmt.with_for_update() if for_update else stmt)
    if c is None:
        raise NotFound("Campaign not found")
    return c


def brief_of(campaign: Campaign) -> CampaignBrief:
    return CampaignBrief.model_validate(campaign.brief or {})


def steps_of(db: Session, campaign: Campaign) -> list[SequenceStep]:
    if campaign.sequence_id is None:
        return []
    return list(db.scalars(select(SequenceStep).where(SequenceStep.sequence_id == campaign.sequence_id,
                                                      SequenceStep.is_active)
                           .order_by(SequenceStep.step_order)).all())


def campaign_readiness(db: Session, campaign: Campaign) -> list[ReadinessItem]:
    offering = db.get(Offering, campaign.offering_id) if campaign.offering_id else None
    details = OfferingDetails.model_validate(offering.details) if offering else None
    return readiness(brief_of(campaign), details, offering_set=offering is not None and not offering.archived,
                     steps=len(steps_of(db, campaign)))


def create_campaign(db: Session, user: User, *, name: str, motion: OutreachMotion,
                    offering_id: uuid.UUID | None) -> Campaign:
    name = " ".join(name.split())[:200]
    if not name:
        raise AppError("A campaign needs a name.", code="invalid_campaign")
    if offering_id:
        get_offering(db, user, offering_id)
    sequence = Sequence(workspace_id=user.workspace_id, name=f"{name} sequence")
    db.add(sequence)
    db.flush()
    _write_steps(db, sequence.id, [0, *PLAYBOOKS[motion].followup_days])
    campaign = Campaign(workspace_id=user.workspace_id, name=name, motion=motion, offering_id=offering_id,
                        brief=default_brief(motion).model_dump(mode="json"), sequence_id=sequence.id,
                        created_by_id=user.id, status=CampaignStatus.DRAFT)
    db.add(campaign)
    db.flush()
    audit.record(db, action="campaign.create", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="campaign", entity_id=campaign.id,
                 metadata={"motion": motion.value})
    return campaign


def update_campaign(db: Session, user: User, campaign_id: uuid.UUID, changes: dict[str, Any]) -> Campaign:
    campaign = get_campaign(db, user, campaign_id, for_update=True)
    if campaign.status in (CampaignStatus.CANCELLED, CampaignStatus.COMPLETED):
        raise Conflict(f"A {campaign.status.value} campaign can't be edited.", code="campaign_closed")
    if changes.get("name"):
        campaign.name = " ".join(str(changes["name"]).split())[:200]
    if "offering_id" in changes:
        offering_id = changes["offering_id"]
        if offering_id is not None:
            get_offering(db, user, uuid.UUID(str(offering_id)))
        campaign.offering_id = offering_id
    if changes.get("motion"):
        campaign.motion = OutreachMotion(changes["motion"])
    if "brief" in changes and changes["brief"] is not None:
        try:
            brief = CampaignBrief.model_validate(changes["brief"])
        except ValidationError as exc:
            raise _validation_error(exc, "campaign brief") from exc
        new = brief.model_dump(mode="json")
        if new != campaign.brief:
            campaign.brief = new
            campaign.brief_version += 1
    if changes.get("timezone"):
        campaign.timezone = str(changes["timezone"])[:64]
    db.flush()
    audit.record(db, action="campaign.update", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="campaign", entity_id=campaign.id,
                 metadata={"fields": sorted(changes), "brief_version": campaign.brief_version})
    return campaign


# --- Sequences ------------------------------------------------------------------------------------

def _write_steps(db: Session, sequence_id: uuid.UUID, days: list[int]) -> None:
    """Replace a sequence's steps from absolute days (0 = initial). Stored as delays relative to the previous step."""
    if not days or days[0] != 0 or any(b <= a for a, b in itertools.pairwise(days)):
        raise AppError("Steps must start at day 0 and use increasing days (e.g. 0, 3, 7, 12).", code="invalid_steps")
    if len(days) > 8:
        raise AppError("At most 8 steps per sequence.", code="invalid_steps")
    # Drafts keep their step_order; their sequence_step_id link is cleared (ON DELETE SET NULL).
    db.execute(delete(SequenceStep).where(SequenceStep.sequence_id == sequence_id))
    previous = 0
    for order, day in enumerate(days):
        db.add(SequenceStep(sequence_id=sequence_id, step_order=order, delay_minutes=(day - previous) * 1440,
                            message_type=DraftType.INITIAL if order == 0 else DraftType.FOLLOW_UP,
                            content_policy=ContentPolicy.GENERATED_REQUIRES_APPROVAL, requires_approval=True))
        previous = day
    db.flush()


def step_days(steps: list[SequenceStep]) -> list[int]:
    days, total = [], 0
    for s in steps:
        total += s.delay_minutes // 1440
        days.append(total)
    return days


def set_campaign_steps(db: Session, user: User, campaign_id: uuid.UUID, days: list[int]) -> Campaign:
    campaign = get_campaign(db, user, campaign_id, for_update=True)
    if campaign.status not in (CampaignStatus.DRAFT, CampaignStatus.PAUSED):
        raise Conflict("Pause the campaign before changing its sequence.", code="campaign_not_editable")
    if campaign.sequence_id is None:
        seq = Sequence(workspace_id=user.workspace_id, name=f"{campaign.name} sequence")
        db.add(seq)
        db.flush()
        campaign.sequence_id = seq.id
    _write_steps(db, campaign.sequence_id, days)
    # Drafts for steps that no longer exist are withdrawn.
    db.execute(update(EmailDraft).where(EmailDraft.campaign_id == campaign.id, EmailDraft.step_order >= len(days),
                                        EmailDraft.is_current)
               .values(status=DraftStatus.SUPERSEDED, is_current=False))
    return campaign


# --- Status ---------------------------------------------------------------------------------------

def change_status(db: Session, user: User, campaign_id: uuid.UUID, target: CampaignStatus) -> Campaign:
    campaign = get_campaign(db, user, campaign_id, for_update=True)
    if target is CampaignStatus.ACTIVE and campaign.status is CampaignStatus.DRAFT:
        items = campaign_readiness(db, campaign)
        if not is_ready(items):
            raise Conflict("Complete the campaign brief before activating.", code="campaign_not_ready",
                           details=[i.model_dump() for i in items if i.severity == "blocker"])
        mailbox = db.get(EmailAccount, campaign.sending_account_id) if campaign.sending_account_id else None
        if mailbox is None:
            raise Conflict("Connect a sending mailbox and choose it for this campaign before activating. Mailbox "
                           "connections arrive in Milestone 5; drafting and approval already work.",
                           code="no_sending_mailbox")
    try:
        CAMPAIGN.assert_transition(campaign.status, target, by_human=True)
    except Exception as exc:
        raise Conflict(f"Can't change a {campaign.status.value} campaign to {target.value}.",
                       code="invalid_campaign_transition") from exc
    campaign.status = target
    if target is CampaignStatus.CANCELLED:
        enrollments = db.scalars(select(CampaignProspect.id).where(
            CampaignProspect.campaign_id == campaign.id, CampaignProspect.state.in_(_ACTIVE_ENROLLMENT))).all()
        for eid in enrollments:
            _stop_enrollment(db, eid, "Campaign cancelled.")
    db.flush()
    audit.record(db, action=f"campaign.{target.value}", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="campaign", entity_id=campaign.id)
    return campaign


def _stop_enrollment(db: Session, enrollment_id: uuid.UUID, reason: str) -> None:
    try:
        transition_row(db, CampaignProspect, enrollment_id, ENROLLMENT, EnrollmentState.CANCELLED,
                       extra_values={"blocker": reason, "next_action_at": None})
    except StaleTransition:
        return
    db.execute(update(ScheduledAction).where(
        ScheduledAction.campaign_prospect_id == enrollment_id,
        ScheduledAction.state.in_(SCHEDULED_ACTION.predecessors(ScheduledActionState.CANCELLED)))
        .values(state=ScheduledActionState.CANCELLED, cancelled_at=func.now(), cancel_reason=reason[:200]))
    queue.cancel_by_dedupe_key(db, draft_job_key(enrollment_id))


# --- Enrollment -----------------------------------------------------------------------------------

@dataclass
class EnrollResult:
    prospect_id: uuid.UUID
    outcome: str  # enrolled | already_enrolled | skipped
    reason: str | None = None


def enroll(db: Session, user: User, campaign_id: uuid.UUID, prospect_ids: list[uuid.UUID],
           settings: Settings) -> list[EnrollResult]:
    campaign = get_campaign(db, user, campaign_id)
    if campaign.status in (CampaignStatus.CANCELLED, CampaignStatus.COMPLETED):
        raise Conflict(f"Can't add contacts to a {campaign.status.value} campaign.", code="campaign_closed")
    prospects = {p.id: p for p in db.scalars(select(Prospect).where(
        Prospect.workspace_id == user.workspace_id, Prospect.id.in_(prospect_ids))).all()}
    existing = {cp.prospect_id: cp for cp in db.scalars(select(CampaignProspect).where(
        CampaignProspect.campaign_id == campaign.id, CampaignProspect.prospect_id.in_(prospect_ids))).all()}
    results: list[EnrollResult] = []
    for pid in dict.fromkeys(prospect_ids):
        p = prospects.get(pid)
        if p is None:
            results.append(EnrollResult(pid, "skipped", "Contact not found."))
            continue
        if pid in existing and existing[pid].state in _ACTIVE_ENROLLMENT:
            results.append(EnrollResult(pid, "already_enrolled"))
            continue
        reason = _ineligible(db, p, campaign)
        if reason:
            results.append(EnrollResult(pid, "skipped", reason))
            continue
        if pid in existing:  # re-enrol a previously removed contact
            cp = existing[pid]
            cp.state, cp.blocker, cp.completed_at = EnrollmentState.ENROLLED, None, None
            cp.enrolled_at = datetime.now(UTC)
        else:
            cp = CampaignProspect(campaign_id=campaign.id, prospect_id=pid, state=EnrollmentState.ENROLLED,
                                  enrolled_at=datetime.now(UTC))
            db.add(cp)
        db.flush()
        advance(db, cp, campaign, p, settings)
        results.append(EnrollResult(pid, "enrolled"))
    audit.record(db, action="campaign.enroll", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="campaign", entity_id=campaign.id,
                 metadata={"enrolled": sum(r.outcome == "enrolled" for r in results), "requested": len(prospect_ids)})
    return results


def _ineligible(db: Session, p: Prospect, campaign: Campaign) -> str | None:
    if not p.email_normalized:
        return "No email address."
    if p.email_verification_status in (EmailVerificationStatus.INVALID, EmailVerificationStatus.BOUNCED):
        return f"Email is marked {p.email_verification_status.value}."
    company = db.get(Company, p.company_id) if p.company_id else None
    for s in active_suppressions(db, p.workspace_id, email=p.email_normalized,
                                 domains=prospect_suppression_domains(p, company)):
        if s.scope is SuppressionScope.WORKSPACE or s.campaign_id == campaign.id:
            return f"Suppressed ({s.reason.value})."
    return None


def _research_eligible(status: ResearchStatus, brief: CampaignBrief) -> bool:
    return status is ResearchStatus.READY or (status is ResearchStatus.NEEDS_REVIEW
                                              and brief.allow_company_only_personalization)


def advance(db: Session, cp: CampaignProspect, campaign: Campaign, prospect: Prospect, settings: Settings,
            *, force: bool = False) -> str:
    """Move an enrollment forward as far as currently possible; records the blocker otherwise."""
    if cp.state not in (EnrollmentState.ENROLLED, EnrollmentState.AWAITING_DRAFT, EnrollmentState.AWAITING_APPROVAL):
        return cp.state.value
    if not is_ready(campaign_readiness(db, campaign)):
        cp.blocker = "Campaign brief is incomplete; finish it to start drafting."
        return "brief_incomplete"
    brief = brief_of(campaign)
    status = prospect.research_status
    if not force and not _research_eligible(status, brief):
        if status in _RESEARCH_BUSY:
            cp.blocker = "Waiting for research to finish."
            active = db.scalar(select(Job.id).where(Job.dedupe_key == research_store.prospect_job_key(prospect.id),
                                                    Job.state.in_(JOB_ACTIVE)))
            if not active:
                research_store.request_prospect_research(db, prospect=prospect, settings=settings)
            return "waiting_for_research"
        cp.blocker = {
            ResearchStatus.NEEDS_REVIEW: "Research needs review: the person's role isn't confirmed by a source. "
                                         "Review it, or allow company-only personalization in the brief.",
            ResearchStatus.INSUFFICIENT_EVIDENCE: "Not enough verifiable evidence to personalize. Refresh research "
                                                  "or draft anyway.",
            ResearchStatus.FAILED_RETRYABLE: "Research failed. Retry it from the Research page.",
        }.get(status, f"Research status: {status.value}.")
        return "research_not_ready"
    queue_drafting(db, cp, force=force)
    return "drafting_queued"


def queue_drafting(db: Session, cp: CampaignProspect, *, steps: list[int] | None = None, force: bool = False,
                   mode: str = "new", draft_id: uuid.UUID | None = None, sections: list[str] | None = None,
                   instruction: str | None = None) -> uuid.UUID:
    campaign = db.get(Campaign, cp.campaign_id)
    assert campaign is not None
    if db.scalar(select(Job.id).where(Job.dedupe_key == draft_job_key(cp.id), Job.state.in_(JOB_ACTIVE))):
        raise Conflict("Drafting is already in progress for this contact.", code="drafting_in_progress")
    job_id, _ = queue.enqueue(db, kind=DRAFT_JOB, workspace_id=campaign.workspace_id, dedupe_key=draft_job_key(cp.id),
                              payload={"campaign_prospect_id": str(cp.id), "steps": steps or [0], "force": force,
                                       "mode": mode, "draft_id": str(draft_id) if draft_id else None,
                                       "sections": sections or [], "instruction": instruction})
    if cp.state is EnrollmentState.ENROLLED:
        transition_row(db, CampaignProspect, cp.id, ENROLLMENT, EnrollmentState.AWAITING_DRAFT,
                       expected_from=EnrollmentState.ENROLLED)
    cp.blocker = None
    db.flush()
    return job_id


def remove_from_campaign(db: Session, user: User, campaign_id: uuid.UUID, prospect_ids: list[uuid.UUID]) -> int:
    campaign = get_campaign(db, user, campaign_id)
    rows = db.scalars(select(CampaignProspect).where(CampaignProspect.campaign_id == campaign.id,
                                                     CampaignProspect.prospect_id.in_(prospect_ids),
                                                     CampaignProspect.state.in_(_ACTIVE_ENROLLMENT))).all()
    for cp in rows:
        _stop_enrollment(db, cp.id, "Removed from campaign.")
        db.execute(update(EmailDraft).where(EmailDraft.campaign_id == campaign.id,
                                            EmailDraft.prospect_id == cp.prospect_id, EmailDraft.is_current)
                   .values(status=DraftStatus.SUPERSEDED, is_current=False))
    return len(rows)


def on_research_finalized(db: Session, prospect_id: uuid.UUID, settings: Settings) -> int:
    """Called after a prospect's research outcome is decided: start drafting for waiting enrollments."""
    prospect = db.get(Prospect, prospect_id)
    if prospect is None:
        return 0
    started = 0
    for cp in db.scalars(select(CampaignProspect).where(CampaignProspect.prospect_id == prospect_id,
                                                        CampaignProspect.state == EnrollmentState.ENROLLED)).all():
        campaign = db.get(Campaign, cp.campaign_id)
        if campaign is None or campaign.status in (CampaignStatus.CANCELLED, CampaignStatus.COMPLETED):
            continue
        if advance(db, cp, campaign, prospect, settings) == "drafting_queued":
            started += 1
    return started


def generate_for_campaign(db: Session, user: User, campaign_id: uuid.UUID, settings: Settings,
                          prospect_ids: list[uuid.UUID] | None = None, *, force: bool = False) -> dict[str, int]:
    campaign = get_campaign(db, user, campaign_id)
    stmt = select(CampaignProspect).where(CampaignProspect.campaign_id == campaign.id,
                                          CampaignProspect.state.in_([EnrollmentState.ENROLLED,
                                                                      EnrollmentState.AWAITING_DRAFT]))
    if prospect_ids:
        stmt = stmt.where(CampaignProspect.prospect_id.in_(prospect_ids))
    counts: dict[str, int] = {}
    for cp in db.scalars(stmt).all():
        prospect = db.get(Prospect, cp.prospect_id)
        assert prospect is not None
        if not force and cp.fit in (Fit.WEAK, Fit.DISQUALIFIED) and cp.state is EnrollmentState.AWAITING_DRAFT:
            counts["held_for_review"] = counts.get("held_for_review", 0) + 1
            continue
        try:
            outcome = advance(db, cp, campaign, prospect, settings, force=force)
        except Conflict:
            outcome = "already_drafting"
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def enrollment_counts(db: Session, campaign_id: uuid.UUID) -> dict[str, int]:
    rows = db.execute(select(CampaignProspect.state, func.count()).where(CampaignProspect.campaign_id == campaign_id)
                      .group_by(CampaignProspect.state)).all()
    return {state.value: n for state, n in rows}


def draft_counts(db: Session, campaign_id: uuid.UUID) -> dict[str, int]:
    rows = db.execute(select(EmailDraft.status, func.count()).where(EmailDraft.campaign_id == campaign_id,
                                                                    EmailDraft.is_current)
                      .group_by(EmailDraft.status)).all()
    return {status.value: n for status, n in rows}
