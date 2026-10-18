"""Durable background jobs (PostgreSQL-backed queue).

Claimed with `SELECT ... FOR UPDATE SKIP LOCKED` under a time-bounded lease. A crashed worker's lease
expires and the job is reclaimed, so no job is lost and no in-memory timer is ever the only record of work.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey, enum_type
from app.domain.states import JobState


class Job(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        # At most one active job per logical unit of work (e.g. "research:company:<id>").
        # Keep this predicate byte-identical to app.jobs.queue.ACTIVE_DEDUPE_PREDICATE (used by ON CONFLICT).
        Index("uq_jobs_active_dedupe_key", "dedupe_key", unique=True,
              postgresql_where=text("dedupe_key IS NOT NULL AND state IN ('queued', 'running')")),
        Index("ix_jobs_claim", "state", "priority", "run_at"),
        Index("ix_jobs_lease", "state", "lease_expires_at"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
    )

    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    state: Mapped[JobState] = mapped_column(enum_type(JobState), nullable=False, default=JobState.QUEUED)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)  # lower runs first
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    locked_by: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
