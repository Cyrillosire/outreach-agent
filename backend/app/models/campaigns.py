"""Sequences, campaigns, and per-prospect campaign membership."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey, WorkspaceScoped, enum_type
from app.domain.states import (
    CampaignStatus,
    ContentPolicy,
    DraftType,
    EnrollmentState,
    Fit,
    OfferingType,
    OutreachMotion,
    SuppressionReason,
)


class Sequence(UUIDPrimaryKey, Timestamps, WorkspaceScoped, Base):
    __tablename__ = "sequences"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


class SequenceStep(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "sequence_steps"
    __table_args__ = (
        UniqueConstraint("sequence_id", "step_order"),
        CheckConstraint("step_order >= 0", name="step_order_non_negative"),
        CheckConstraint("delay_minutes >= 0", name="delay_non_negative"),
        # The initial email must always be human-approved.
        CheckConstraint(
            "message_type <> 'initial' OR (requires_approval AND content_policy <> 'generated_auto_policy')",
            name="initial_requires_approval",
        ),
    )

    sequence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sequences.id", ondelete="CASCADE"), index=True, nullable=False
    )
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    # Delay relative to the previous step's send time (step 0 is relative to scheduling).
    delay_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message_type: Mapped[DraftType] = mapped_column(enum_type(DraftType, "message_type"), nullable=False)
    content_policy: Mapped[ContentPolicy] = mapped_column(
        enum_type(ContentPolicy), nullable=False, default=ContentPolicy.GENERATED_REQUIRES_APPROVAL
    )
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Offering(UUIDPrimaryKey, Timestamps, WorkspaceScoped, Base):
    """What we sell (product, service, partnership program, pilot, event...), defined once and reused by campaigns.

    `details` holds the structured, seller-vouched facts the drafts may use (validated by
    app.services.campaigns.brief.OfferingDetails): problems solved, capabilities, differentiators, proof points,
    and claims that must never be made.
    """

    __tablename__ = "offerings"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    offering_type: Mapped[OfferingType] = mapped_column(enum_type(OfferingType), nullable=False)
    category: Mapped[str | None] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Campaign(UUIDPrimaryKey, Timestamps, WorkspaceScoped, Base):
    __tablename__ = "campaigns"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    offering_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("offerings.id", ondelete="RESTRICT"), index=True
    )
    motion: Mapped[OutreachMotion] = mapped_column(
        enum_type(OutreachMotion), nullable=False, default=OutreachMotion.PRODUCT_SALE
    )
    # Structured campaign brief (app.services.campaigns.brief.CampaignBrief): audience and personas, fit signals,
    # disqualifiers, call to action, style, sender and compliance footer. Versioned so drafts record what they used.
    brief: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    brief_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sequence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sequences.id", ondelete="RESTRICT"), index=True
    )
    sending_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_accounts.id", ondelete="RESTRICT"), index=True
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    # Daily limits, sending windows, etc. Validated by a Pydantic schema at the service layer.
    sending_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[CampaignStatus] = mapped_column(
        enum_type(CampaignStatus), nullable=False, default=CampaignStatus.DRAFT
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class CampaignProspect(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "campaign_prospects"
    __table_args__ = (
        UniqueConstraint("campaign_id", "prospect_id"),
        Index("ix_campaign_prospects_state_next_action", "state", "next_action_at"),
    )

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), index=True, nullable=False
    )
    prospect_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("prospects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    state: Mapped[EnrollmentState] = mapped_column(
        enum_type(EnrollmentState), nullable=False, default=EnrollmentState.ENROLLED
    )
    current_step_order: Mapped[int | None] = mapped_column(Integer)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_reason: Mapped[SuppressionReason | None] = mapped_column(enum_type(SuppressionReason, "stop_reason"))
    # Why this contact isn't progressing (e.g. "Waiting for research", "Not drafted: disqualified - ...").
    blocker: Mapped[str | None] = mapped_column(String(1000))
    fit: Mapped[Fit | None] = mapped_column(enum_type(Fit))
    fit_assessment: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Incremented on every state change; used for optimistic concurrency in multi-field updates.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
