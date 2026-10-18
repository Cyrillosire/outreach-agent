"""Scheduled actions (durable outbox), suppressions, provider events, and the audit log."""

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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey, WorkspaceScoped, enum_type
from app.domain.states import (
    ActorType,
    AuditOutcome,
    EmailProvider,
    IdempotencyState,
    ProviderEventState,
    ScheduledActionState,
    ScheduledActionType,
    SuppressionReason,
    SuppressionScope,
    SuppressionSource,
)


class ScheduledAction(UUIDPrimaryKey, Timestamps, WorkspaceScoped, Base):
    __tablename__ = "scheduled_actions"
    __table_args__ = (
        Index("ix_scheduled_actions_due", "state", "due_at"),
        Index("ix_scheduled_actions_lease", "state", "lease_expires_at"),
        CheckConstraint("attempt_count >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
    )

    action_type: Mapped[ScheduledActionType] = mapped_column(enum_type(ScheduledActionType), nullable=False)
    # Deterministic key, e.g. "send:{campaign_prospect_id}:step:{n}". The unique constraint is the
    # last line of defense against duplicate logical sends.
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    campaign_prospect_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaign_prospects.id", ondelete="CASCADE"), index=True
    )
    prospect_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("prospects.id", ondelete="CASCADE"), index=True
    )
    draft_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("email_drafts.id", ondelete="CASCADE"))
    step_order: Mapped[int | None] = mapped_column(Integer)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[ScheduledActionState] = mapped_column(
        enum_type(ScheduledActionState), nullable=False, default=ScheduledActionState.PENDING
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    locked_by: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(200))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class Suppression(UUIDPrimaryKey, WorkspaceScoped, Base):
    __tablename__ = "suppressions"
    __table_args__ = (
        CheckConstraint("(email_normalized IS NOT NULL) <> (domain IS NOT NULL)", name="exactly_one_target"),
        CheckConstraint("(scope = 'campaign') = (campaign_id IS NOT NULL)", name="campaign_scope_matches"),
        Index("ix_suppressions_active_email", "workspace_id", "email_normalized",
              postgresql_where=text("lifted_at IS NULL")),
        Index("ix_suppressions_active_domain", "workspace_id", "domain",
              postgresql_where=text("lifted_at IS NULL")),
    )

    email_normalized: Mapped[str | None] = mapped_column(String(320))
    domain: Mapped[str | None] = mapped_column(String(253))
    scope: Mapped[SuppressionScope] = mapped_column(enum_type(SuppressionScope), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    reason: Mapped[SuppressionReason] = mapped_column(enum_type(SuppressionReason), nullable=False)
    source: Mapped[SuppressionSource] = mapped_column(enum_type(SuppressionSource), nullable=False)
    is_permanent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    review_policy: Mapped[str | None] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 nullable=False)
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lifted_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class ProviderEvent(UUIDPrimaryKey, Base):
    __tablename__ = "provider_events"
    __table_args__ = (
        UniqueConstraint("provider", "dedupe_key"),
        Index("ix_provider_events_state", "processing_state", "received_at"),
    )

    provider: Mapped[EmailProvider] = mapped_column(enum_type(EmailProvider), nullable=False)
    email_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_accounts.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(500), nullable=False)
    provider_resource_id: Mapped[str | None] = mapped_column(String(512))
    # Minimal, redacted payload: identifiers only, never tokens or message bodies.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    processing_state: Mapped[ProviderEventState] = mapped_column(
        enum_type(ProviderEventState), nullable=False, default=ProviderEventState.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                  nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(UUIDPrimaryKey, Base):
    """Append-only. Rows are never updated; metadata must never contain secrets or message bodies."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        Index("ix_audit_logs_workspace_created", "workspace_id", "created_at"),
    )

    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    actor_type: Mapped[ActorType] = mapped_column(enum_type(ActorType), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column()
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(60))
    entity_id: Mapped[uuid.UUID | None] = mapped_column()
    outcome: Mapped[AuditOutcome] = mapped_column(enum_type(AuditOutcome), nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    request_id: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 nullable=False)


class IdempotencyKey(UUIDPrimaryKey, Base):
    """Stored outcome of a mutating request sent with an `Idempotency-Key` header.

    The key row is inserted in the same transaction as the work it guards, so a concurrent duplicate
    blocks on the unique index until the first commits, then replays the stored response.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("user_id", "scope", "key"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    scope: Mapped[str] = mapped_column(String(120), nullable=False)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[IdempotencyState] = mapped_column(enum_type(IdempotencyState), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
