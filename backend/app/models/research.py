"""Research runs, sources, facts, and fact-to-source evidence links.

Company facts and individual facts live in the same table but are distinguished by exactly one of
`company_id` / `prospect_id` being set, so company research can be reused without implying the
individual has been researched.
"""

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
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey, WorkspaceScoped, enum_type
from app.domain.states import (
    ExtractionStatus,
    FactKind,
    FactVerification,
    ResearchRunStatus,
    ResearchSubject,
    SourceType,
)

_EXACTLY_ONE_SUBJECT = "(company_id IS NOT NULL) <> (prospect_id IS NOT NULL)"


class ResearchRun(UUIDPrimaryKey, Timestamps, WorkspaceScoped, Base):
    __tablename__ = "research_runs"
    __table_args__ = (
        CheckConstraint(_EXACTLY_ONE_SUBJECT, name="exactly_one_subject"),
        CheckConstraint(
            "(subject_type = 'company') = (company_id IS NOT NULL)", name="subject_type_matches"
        ),
        Index("ix_research_runs_status", "status"),
    )

    subject_type: Mapped[ResearchSubject] = mapped_column(enum_type(ResearchSubject), nullable=False)
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    prospect_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("prospects.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[ResearchRunStatus] = mapped_column(
        enum_type(ResearchRunStatus), nullable=False, default=ResearchRunStatus.QUEUED
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), index=True)
    stage: Mapped[str | None] = mapped_column(String(50))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    sufficiency: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResearchSource(UUIDPrimaryKey, Timestamps, WorkspaceScoped, Base):
    __tablename__ = "research_sources"
    __table_args__ = (
        # The same URL with different content is a new snapshot; identical content is reused.
        UniqueConstraint("workspace_id", "url_normalized", "content_sha256"),
    )

    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    url_normalized: Mapped[str] = mapped_column(String(2048), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(1000))
    publisher: Mapped[str | None] = mapped_column(String(300))
    source_type: Mapped[SourceType] = mapped_column(enum_type(SourceType), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    extraction_status: Mapped[ExtractionStatus] = mapped_column(
        enum_type(ExtractionStatus), nullable=False, default=ExtractionStatus.PENDING
    )
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    # Extracted plain text (untrusted). Never interpreted as instructions.
    extracted_text: Mapped[str | None] = mapped_column(Text)


class ResearchFact(UUIDPrimaryKey, Timestamps, WorkspaceScoped, Base):
    __tablename__ = "research_facts"
    __table_args__ = (
        CheckConstraint(_EXACTLY_ONE_SUBJECT, name="exactly_one_subject"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint("relevance >= 0 AND relevance <= 1", name="relevance_range"),
    )

    research_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_runs.id", ondelete="SET NULL"), index=True
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    prospect_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("prospects.id", ondelete="CASCADE"), index=True
    )
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[FactKind] = mapped_column(enum_type(FactKind), nullable=False)
    verification_status: Mapped[FactVerification] = mapped_column(
        enum_type(FactVerification), nullable=False, default=FactVerification.UNVERIFIED
    )
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    relevance: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False, default=0)
    relevance_note: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResearchFactEvidence(UUIDPrimaryKey, Base):
    """Links a fact to each source that supports (or contradicts) it, with the exact excerpt."""

    __tablename__ = "research_fact_evidence"
    __table_args__ = (UniqueConstraint("fact_id", "source_id"),)

    fact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_facts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_sources.id", ondelete="CASCADE"), index=True, nullable=False
    )
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    supports: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
