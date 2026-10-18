"""Connected mailboxes, versioned drafts, and message records."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey, WorkspaceScoped, enum_type
from app.domain.states import (
    DraftStatus,
    DraftType,
    EmailAccountStatus,
    EmailProvider,
    MessageDirection,
    OutboundStatus,
    ReplyClassification,
)


class EmailAccount(UUIDPrimaryKey, Timestamps, WorkspaceScoped, Base):
    __tablename__ = "email_accounts"
    __table_args__ = (UniqueConstraint("workspace_id", "provider", "provider_account_id"),)

    connected_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    provider: Mapped[EmailProvider] = mapped_column(enum_type(EmailProvider), nullable=False)
    # Google `sub` / Microsoft `oid`: stable even if the mailbox address changes.
    provider_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    email_address: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    granted_scopes: Mapped[list[str]] = mapped_column(ARRAY(String(255)), nullable=False, default=list)
    # AES-GCM ciphertext (see app.core.crypto); bound to this row id. Access tokens are never persisted.
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text)
    sync_cursor: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    subscription: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[EmailAccountStatus] = mapped_column(
        enum_type(EmailAccountStatus), nullable=False, default=EmailAccountStatus.CONNECTED
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailDraft(UUIDPrimaryKey, Timestamps, WorkspaceScoped, Base):
    """One row per draft version. Edits and regenerations create a new version; approval is bound to
    a specific version *and* the hash of its content, so any change invalidates it."""

    __tablename__ = "email_drafts"
    __table_args__ = (
        UniqueConstraint("campaign_id", "prospect_id", "step_order", "version"),
        Index(
            "uq_email_drafts_current", "campaign_id", "prospect_id", "step_order",
            unique=True, postgresql_where=text("is_current"),
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "status <> 'approved' OR (approved_by_id IS NOT NULL AND approved_at IS NOT NULL "
            "AND approved_content_sha256 IS NOT NULL)",
            name="approval_complete",
        ),
    )

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), index=True, nullable=False
    )
    prospect_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("prospects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sequence_step_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sequence_steps.id", ondelete="SET NULL"), index=True
    )
    step_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    draft_type: Mapped[DraftType] = mapped_column(enum_type(DraftType), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    personalization_rationale: Mapped[str | None] = mapped_column(Text)
    quality_scores: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    validation_issues: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    generated_by: Mapped[str] = mapped_column(String(20), nullable=False, default="ai")  # ai | user
    generation_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[DraftStatus] = mapped_column(
        enum_type(DraftStatus), nullable=False, default=DraftStatus.PENDING_REVIEW
    )
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_content_sha256: Mapped[str | None] = mapped_column(String(64))
    rejected_reason: Mapped[str | None] = mapped_column(Text)


class EmailDraftEvidence(UUIDPrimaryKey, Base):
    """Maps a material claim in a draft to the research fact that supports it."""

    __tablename__ = "email_draft_evidence"
    __table_args__ = (UniqueConstraint("draft_id", "fact_id", "claim_text"),)

    draft_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("email_drafts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # CASCADE so deleting a contact/company removes its evidence links in the same statement. Services invalidate
    # (supersede) any draft whose evidence is deleted, so approval never survives losing its evidence.
    fact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_facts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)


class EmailMessage(UUIDPrimaryKey, Timestamps, WorkspaceScoped, Base):
    __tablename__ = "email_messages"
    __table_args__ = (
        Index(
            "uq_email_messages_provider_id", "email_account_id", "provider_message_id",
            unique=True, postgresql_where=text("provider_message_id IS NOT NULL"),
        ),
        Index(
            "uq_email_messages_internet_message_id", "email_account_id", "internet_message_id",
            unique=True, postgresql_where=text("internet_message_id IS NOT NULL"),
        ),
        # One logical send == one row, no matter how many times a worker retries.
        Index(
            "uq_email_messages_send_key", "send_idempotency_key",
            unique=True, postgresql_where=text("send_idempotency_key IS NOT NULL"),
        ),
        Index("ix_email_messages_thread", "email_account_id", "provider_thread_id"),
        CheckConstraint(
            "(direction = 'outbound') = (outbound_status IS NOT NULL)", name="outbound_status_iff_outbound"
        ),
    )

    email_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("email_accounts.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    prospect_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("prospects.id", ondelete="SET NULL"), index=True
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), index=True
    )
    draft_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_drafts.id", ondelete="SET NULL"), index=True
    )
    direction: Mapped[MessageDirection] = mapped_column(enum_type(MessageDirection), nullable=False)
    send_idempotency_key: Mapped[str | None] = mapped_column(String(200))
    provider_message_id: Mapped[str | None] = mapped_column(String(512))
    provider_thread_id: Mapped[str | None] = mapped_column(String(512))
    internet_message_id: Mapped[str | None] = mapped_column(String(998))
    in_reply_to: Mapped[str | None] = mapped_column(String(998))
    references: Mapped[str | None] = mapped_column(Text)
    from_address: Mapped[str] = mapped_column(String(320), nullable=False)
    to_addresses: Mapped[list[str]] = mapped_column(ARRAY(String(320)), nullable=False, default=list)
    subject: Mapped[str | None] = mapped_column(String(998))
    snippet: Mapped[str | None] = mapped_column(String(500))
    # Subject to retention policy; may be nulled by retention jobs while metadata is kept.
    body_text: Mapped[str | None] = mapped_column(Text)
    outbound_status: Mapped[OutboundStatus | None] = mapped_column(enum_type(OutboundStatus))
    classification: Mapped[ReplyClassification | None] = mapped_column(enum_type(ReplyClassification))
    classification_confidence: Mapped[float | None] = mapped_column()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
