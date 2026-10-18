"""Overview counts. Every number is a live query scoped to the caller's workspace; nothing is estimated."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession
from app.domain.states import (
    DraftStatus,
    EmailAccountStatus,
    EmailProvider,
    EnrollmentState,
    MessageDirection,
    OutboundStatus,
    ResearchStatus,
    ScheduledActionState,
)
from app.models import (
    Campaign,
    CampaignProspect,
    EmailAccount,
    EmailDraft,
    EmailMessage,
    Prospect,
    ScheduledAction,
)
from app.schemas.common import ERROR_RESPONSES, Envelope

router = APIRouter(prefix="/dashboard", tags=["dashboard"], responses=ERROR_RESPONSES)


class EmailAccountStatusOut(BaseModel):
    id: uuid.UUID
    provider: EmailProvider
    email_address: str
    status: EmailAccountStatus
    last_synced_at: datetime | None


class DashboardSummary(BaseModel):
    prospects_total: int
    research_by_status: dict[str, int]
    drafts_awaiting_approval: int
    emails_provider_accepted: int
    replies_received: int
    active_sequences: int
    scheduled_actions_pending: int
    actions_needing_attention: int
    email_accounts: list[EmailAccountStatusOut]


@router.get("/summary", response_model=Envelope[DashboardSummary], summary="Overview counts")
def summary(user: CurrentUser, db: DbSession) -> Envelope[DashboardSummary]:
    ws = user.workspace_id

    research_rows = db.execute(
        select(Prospect.research_status, func.count()).where(Prospect.workspace_id == ws)
        .group_by(Prospect.research_status)
    ).all()
    research = {s.value: 0 for s in ResearchStatus}
    research.update({status.value: count for status, count in research_rows})

    def count(stmt: object) -> int:
        return int(db.scalar(stmt) or 0)  # type: ignore[call-overload]

    drafts = count(select(func.count()).select_from(EmailDraft).where(
        EmailDraft.workspace_id == ws, EmailDraft.is_current, EmailDraft.status == DraftStatus.PENDING_REVIEW))
    sent = count(select(func.count()).select_from(EmailMessage).where(
        EmailMessage.workspace_id == ws, EmailMessage.direction == MessageDirection.OUTBOUND,
        EmailMessage.outbound_status.in_([OutboundStatus.PROVIDER_ACCEPTED, OutboundStatus.BOUNCED])))
    replies = count(select(func.count()).select_from(EmailMessage).where(
        EmailMessage.workspace_id == ws, EmailMessage.direction == MessageDirection.INBOUND,
        EmailMessage.prospect_id.is_not(None)))
    active = count(select(func.count()).select_from(CampaignProspect).join(Campaign).where(
        Campaign.workspace_id == ws,
        CampaignProspect.state.in_([EnrollmentState.SCHEDULED, EnrollmentState.IN_SEQUENCE])))
    pending = count(select(func.count()).select_from(ScheduledAction).where(
        ScheduledAction.workspace_id == ws, ScheduledAction.state == ScheduledActionState.PENDING))
    attention = count(select(func.count()).select_from(ScheduledAction).where(
        ScheduledAction.workspace_id == ws,
        ScheduledAction.state.in_([ScheduledActionState.AMBIGUOUS, ScheduledActionState.DEAD])))
    accounts = db.scalars(
        select(EmailAccount).where(EmailAccount.workspace_id == ws).order_by(EmailAccount.email_address)
    ).all()

    return Envelope(data=DashboardSummary(
        prospects_total=sum(research.values()),
        research_by_status=research,
        drafts_awaiting_approval=drafts,
        emails_provider_accepted=sent,
        replies_received=replies,
        active_sequences=active,
        scheduled_actions_pending=pending,
        actions_needing_attention=attention,
        email_accounts=[
            EmailAccountStatusOut(id=a.id, provider=a.provider, email_address=a.email_address,
                                  status=a.status, last_synced_at=a.last_synced_at)
            for a in accounts
        ],
    ))
