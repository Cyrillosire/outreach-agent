"""Queue operations. All functions are synchronous, take a Session, and commit nothing themselves unless
named `*_and_commit`; workers call them inside short transactions.

Correctness properties (each covered by integration tests):
- `claim` uses FOR UPDATE SKIP LOCKED, so concurrent workers never receive the same job.
- Every state change after claim is guarded by `locked_by = <worker>`: a worker whose lease was reclaimed
  cannot overwrite the new owner's result.
- `reap_expired` returns crashed workers' jobs to the queue (or dead-letters them when attempts are spent).
- An active dedupe key admits one queued/running job per logical unit of work.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.domain.states import JOB_ACTIVE, JobState
from app.models import Job

BACKOFF_BASE_SECONDS = 15
ACTIVE_DEDUPE_PREDICATE = text("dedupe_key IS NOT NULL AND state IN ('queued', 'running')")
BACKOFF_CAP_SECONDS = 3600


@dataclass(frozen=True)
class ClaimedJob:
    """Immutable snapshot handed to handlers (never a live ORM object shared across threads)."""

    id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    workspace_id: uuid.UUID | None
    attempts: int
    max_attempts: int


def backoff_seconds(attempt: int, *, base: float = BACKOFF_BASE_SECONDS, cap: float = BACKOFF_CAP_SECONDS,
                    rng: random.Random | None = None) -> float:
    """Exponential backoff with jitter: base*2^(attempt-1), capped, scaled by a random factor in [0.5, 1.5)."""
    exp = min(cap, base * (2 ** max(0, attempt - 1)))
    return float(exp * (0.5 + (rng or random).random()))


def enqueue(db: Session, *, kind: str, payload: dict[str, Any], workspace_id: uuid.UUID | None,
            dedupe_key: str | None = None, priority: int = 100, run_at: datetime | None = None,
            max_attempts: int = 4) -> tuple[uuid.UUID, bool]:
    """Insert a job. Returns (job_id, created). With a dedupe key, an already-active job is returned instead.

    All queue timing uses the database clock (`now()`), never the caller's: API, worker, and database may run
    on different hosts, and a few hundred milliseconds of skew would otherwise make new jobs look not yet due.
    """
    values = {"id": uuid.uuid4(), "kind": kind, "payload": payload, "workspace_id": workspace_id,
              "dedupe_key": dedupe_key, "priority": priority, "run_at": run_at if run_at is not None else func.now(),
              "max_attempts": max_attempts, "state": JobState.QUEUED, "attempts": 0}
    stmt = pg_insert(Job).values(values)
    if dedupe_key is not None:
        # The predicate must be a SQL literal identical to the partial index's. With bound parameters, Postgres
        # cannot prove the match once psycopg switches to server-side prepared statements (5th execution).
        stmt = stmt.on_conflict_do_nothing(index_elements=["dedupe_key"], index_where=ACTIVE_DEDUPE_PREDICATE)
    inserted = db.execute(stmt.returning(Job.id)).scalar_one_or_none()
    if inserted is not None:
        return inserted, True
    existing = db.scalar(select(Job.id).where(Job.dedupe_key == dedupe_key, Job.state.in_(JOB_ACTIVE)))
    if existing is None:  # the active job finished between our insert and select; enqueue again
        return enqueue(db, kind=kind, payload=payload, workspace_id=workspace_id, dedupe_key=dedupe_key,
                       priority=priority, run_at=run_at, max_attempts=max_attempts)
    return existing, False


def claim(db: Session, *, worker_id: str, limit: int, lease_seconds: int,
          kinds: list[str] | None = None) -> list[ClaimedJob]:
    now = func.now()
    candidates = (select(Job.id).where(Job.state == JobState.QUEUED, Job.run_at <= now)
                  .order_by(Job.priority, Job.run_at).limit(limit).with_for_update(skip_locked=True))
    if kinds:
        candidates = candidates.where(Job.kind.in_(kinds))
    rows = db.execute(
        update(Job).where(Job.id.in_(candidates.scalar_subquery()))
        .values(state=JobState.RUNNING, locked_by=worker_id, attempts=Job.attempts + 1,
                lease_expires_at=now + timedelta(seconds=lease_seconds))
        .returning(Job.id, Job.kind, Job.payload, Job.workspace_id, Job.attempts, Job.max_attempts)
        .execution_options(synchronize_session=False)
    ).all()
    return [ClaimedJob(*row) for row in rows]


def _owned(job_id: uuid.UUID, worker_id: str):  # type: ignore[no-untyped-def]
    return and_(Job.id == job_id, Job.state == JobState.RUNNING, Job.locked_by == worker_id)


def heartbeat(db: Session, *, job_id: uuid.UUID, worker_id: str, lease_seconds: int) -> bool:
    result = db.execute(update(Job).where(_owned(job_id, worker_id))
                        .values(lease_expires_at=func.now() + timedelta(seconds=lease_seconds))
                        .execution_options(synchronize_session=False))
    return bool(result.rowcount)  # type: ignore[attr-defined]


def complete(db: Session, *, job_id: uuid.UUID, worker_id: str, result: dict[str, Any] | None = None) -> bool:
    res = db.execute(update(Job).where(_owned(job_id, worker_id))
                     .values(state=JobState.SUCCEEDED, result=result, finished_at=func.now(), locked_by=None,
                             lease_expires_at=None, last_error=None)
                     .execution_options(synchronize_session=False))
    return bool(res.rowcount)  # type: ignore[attr-defined]


def fail(db: Session, *, job: ClaimedJob, worker_id: str, error: str, retryable: bool,
         retry_after_seconds: float | None = None) -> JobState | None:
    """Record a failure. Returns the new state, or None if this worker no longer owns the job."""
    error = error[:4000]
    if retryable and job.attempts < job.max_attempts:
        delay = retry_after_seconds if retry_after_seconds is not None else backoff_seconds(job.attempts)
        values: dict[str, Any] = {"state": JobState.QUEUED, "run_at": func.now() + timedelta(seconds=delay)}
        new_state = JobState.QUEUED
    else:
        new_state = JobState.DEAD if retryable else JobState.FAILED
        values = {"state": new_state, "finished_at": func.now()}
    res = db.execute(update(Job).where(_owned(job.id, worker_id))
                     .values(**values, last_error=error, locked_by=None, lease_expires_at=None)
                     .execution_options(synchronize_session=False))
    return new_state if res.rowcount else None  # type: ignore[attr-defined]


def reap_expired(db: Session) -> tuple[int, int]:
    """Reclaim jobs whose worker stopped heartbeating. Returns (requeued, dead_lettered)."""
    expired = and_(Job.state == JobState.RUNNING, Job.lease_expires_at < func.now())
    dead = db.execute(update(Job).where(expired, Job.attempts >= Job.max_attempts)
                      .values(state=JobState.DEAD, finished_at=func.now(), locked_by=None, lease_expires_at=None,
                              last_error="worker lease expired; attempts exhausted")
                      .execution_options(synchronize_session=False)).rowcount  # type: ignore[attr-defined]
    requeued = db.execute(update(Job).where(expired, Job.attempts < Job.max_attempts)
                          .values(state=JobState.QUEUED, locked_by=None, lease_expires_at=None,
                                  run_at=func.now() + timedelta(seconds=BACKOFF_BASE_SECONDS),
                                  last_error="worker lease expired; requeued")
                          .execution_options(synchronize_session=False)).rowcount  # type: ignore[attr-defined]
    return requeued, dead


def cancel_by_dedupe_key(db: Session, dedupe_key: str) -> int:
    return int(db.execute(update(Job).where(Job.dedupe_key == dedupe_key, Job.state == JobState.QUEUED)
                          .values(state=JobState.CANCELLED, finished_at=func.now())
                          .execution_options(synchronize_session=False)).rowcount)  # type: ignore[attr-defined]


def queue_depth(db: Session, workspace_id: uuid.UUID | None = None) -> dict[str, int]:
    stmt = select(Job.state, func.count()).group_by(Job.state)
    if workspace_id is not None:
        stmt = stmt.where(or_(Job.workspace_id == workspace_id))
    counts = {s.value: 0 for s in JobState}
    counts.update({state.value: n for state, n in db.execute(stmt).all()})
    return counts
