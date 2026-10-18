"""Suppression: the deterministic control that stops outreach to an address or domain.

Creating a suppression immediately (same transaction):
- moves every affected, still-active campaign enrollment to `suppressed` (compare-and-set), and
- cancels every cancellable scheduled action for those enrollments.
Actions already `executing` cannot be cancelled (the provider call may be in flight). The send pipeline
re-checks suppression immediately before every send, which closes that window.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.errors import AppError, Conflict, NotFound
from app.db.transitions import StaleTransition, transition_row
from app.domain.states import (
    ENROLLMENT,
    SCHEDULED_ACTION,
    ActorType,
    AuditOutcome,
    EnrollmentState,
    ScheduledActionState,
    SuppressionReason,
    SuppressionScope,
    SuppressionSource,
)
from app.models import Campaign, CampaignProspect, Company, Prospect, ScheduledAction, Suppression, User
from app.services import audit
from app.services.imports.normalize import FREEMAIL_DOMAINS

# Reasons a user may record manually. hard_bounce / replied are set only by the system from provider data.
USER_REASONS = frozenset({SuppressionReason.MANUAL, SuppressionReason.UNSUBSCRIBE,
                          SuppressionReason.COMPLAINT, SuppressionReason.LEGAL})


@dataclass
class SuppressionOutcome:
    suppression: Suppression
    created: bool
    enrollments_stopped: int
    actions_cancelled: int


def email_domain(email_normalized: str | None) -> str | None:
    return email_normalized.rpartition("@")[2] if email_normalized else None


def active_suppressions(db: Session, workspace_id: uuid.UUID, *, email: str | None,
                        domains: list[str]) -> list[Suppression]:
    conditions = []
    if email:
        conditions.append(Suppression.email_normalized == email)
    domains = [d for d in domains if d]
    if domains:
        conditions.append(Suppression.domain.in_(domains))
    if not conditions:
        return []
    return list(db.scalars(select(Suppression).where(
        Suppression.workspace_id == workspace_id, Suppression.lifted_at.is_(None), or_(*conditions))
        .order_by(Suppression.created_at)).all())


def prospect_suppression_domains(prospect: Prospect, company: Company | None) -> list[str]:
    return [d for d in {email_domain(prospect.email_normalized), company.normalized_domain if company else None} if d]


def suppress_prospect(
    db: Session, user: User, prospect: Prospect, *, reason: SuppressionReason, target: str,
    scope: SuppressionScope, campaign_id: uuid.UUID | None, note: str | None,
) -> SuppressionOutcome:
    if reason not in USER_REASONS:
        raise AppError(f"Reason {reason.value!r} can only be recorded by the system.", code="invalid_reason")
    if scope is SuppressionScope.CAMPAIGN:
        if campaign_id is None:
            raise AppError("campaign_id is required for campaign-scoped suppression.", code="campaign_required")
        if db.scalar(select(Campaign.id).where(Campaign.id == campaign_id,
                                               Campaign.workspace_id == user.workspace_id)) is None:
            raise NotFound("Campaign not found")
    elif campaign_id is not None:
        raise AppError("campaign_id is only valid with scope 'campaign'.", code="campaign_not_allowed")

    email_value: str | None = None
    domain_value: str | None = None
    if target == "email":
        if not prospect.email_normalized:
            raise Conflict("This prospect has no email address to suppress. Suppress the domain instead, "
                           "or add an email first.", code="no_email")
        email_value = prospect.email_normalized
    else:
        company = db.get(Company, prospect.company_id) if prospect.company_id else None
        domain_value = (company.normalized_domain if company else None) or email_domain(prospect.email_normalized)
        if not domain_value:
            raise Conflict("No company domain is known for this prospect.", code="no_domain")
        if domain_value in FREEMAIL_DOMAINS:
            raise AppError(f"{domain_value} is a public mailbox provider; suppressing it would block unrelated "
                           "people. Suppress the email address instead.", code="freemail_domain")

    existing = db.scalar(select(Suppression).where(
        Suppression.workspace_id == user.workspace_id, Suppression.lifted_at.is_(None),
        Suppression.reason == reason, Suppression.scope == scope,
        Suppression.campaign_id.is_(None) if campaign_id is None else Suppression.campaign_id == campaign_id,
        Suppression.email_normalized == email_value if email_value else Suppression.domain == domain_value,
    ))
    created = existing is None
    suppression = existing or Suppression(
        workspace_id=user.workspace_id, email_normalized=email_value, domain=domain_value, scope=scope,
        campaign_id=campaign_id, reason=reason, source=SuppressionSource.USER, is_permanent=True,
        note=note, created_by_id=user.id,
    )
    if created:
        db.add(suppression)
        db.flush()

    stopped, cancelled = _stop_outreach(db, user.workspace_id, email=email_value, domain=domain_value,
                                        campaign_id=campaign_id, reason=reason)
    audit.record(db, action="suppression.create", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="prospect", entity_id=prospect.id,
                 metadata={"suppression_id": str(suppression.id), "reason": reason.value, "scope": scope.value,
                           "target": target, "created": created, "enrollments_stopped": stopped,
                           "actions_cancelled": cancelled})
    return SuppressionOutcome(suppression, created, stopped, cancelled)


def _stop_outreach(db: Session, workspace_id: uuid.UUID, *, email: str | None, domain: str | None,
                   campaign_id: uuid.UUID | None, reason: SuppressionReason) -> tuple[int, int]:
    prospect_filter = (Prospect.email_normalized == email) if email else or_(
        func.split_part(Prospect.email_normalized, "@", 2) == domain, Company.normalized_domain == domain)
    stmt = (select(CampaignProspect.id, CampaignProspect.state)
            .join(Prospect, CampaignProspect.prospect_id == Prospect.id)
            .outerjoin(Company, Prospect.company_id == Company.id)
            .where(Prospect.workspace_id == workspace_id, prospect_filter,
                   CampaignProspect.state.in_(ENROLLMENT.predecessors(EnrollmentState.SUPPRESSED))))
    if campaign_id is not None:
        stmt = stmt.where(CampaignProspect.campaign_id == campaign_id)
    enrollments = db.execute(stmt).all()

    stopped = 0
    for enrollment_id, _ in enrollments:
        try:
            transition_row(db, CampaignProspect, enrollment_id, ENROLLMENT, EnrollmentState.SUPPRESSED,
                           extra_values={"stop_reason": reason, "next_action_at": None,
                                         "version": CampaignProspect.version + 1})
            stopped += 1
        except StaleTransition:
            pass  # moved to another stop state concurrently (e.g. replied); outreach is stopped either way

    cancelled = 0
    if enrollments:
        result = db.execute(
            update(ScheduledAction)
            .where(ScheduledAction.campaign_prospect_id.in_([e[0] for e in enrollments]),
                   ScheduledAction.state.in_(SCHEDULED_ACTION.predecessors(ScheduledActionState.CANCELLED)))
            .values(state=ScheduledActionState.CANCELLED, cancelled_at=func.now(),
                    cancel_reason=f"suppressed:{reason.value}")
            .execution_options(synchronize_session=False)
        )
        cancelled = result.rowcount  # type: ignore[attr-defined]
    return stopped, cancelled
