"""Durable queue guarantees, against real PostgreSQL with committed transactions (cleaned up per test)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.domain.states import JobState
from app.jobs import queue
from app.jobs.worker import PermanentJobError, RetryableJobError, Worker
from app.models import Job

pytestmark = pytest.mark.integration


@pytest.fixture
def sessions(engine: Engine) -> Iterator[sessionmaker[Session]]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    with factory() as s:
        s.execute(delete(Job).where(Job.kind.like("test.%")))
        s.commit()


def kind() -> str:
    return f"test.{uuid.uuid4().hex[:8]}"


def tx(sessions: sessionmaker[Session], fn):  # type: ignore[no-untyped-def]
    with sessions() as s:
        out = fn(s)
        s.commit()
        return out


def state_of(sessions: sessionmaker[Session], job_id: uuid.UUID) -> Job:
    with sessions() as s:
        return s.get(Job, job_id)  # type: ignore[return-value]


def test_dedupe_key_admits_one_active_job(sessions: sessionmaker[Session]) -> None:
    k, key = kind(), f"test:{uuid.uuid4()}"
    a = tx(sessions, lambda s: queue.enqueue(s, kind=k, payload={}, workspace_id=None, dedupe_key=key))
    b = tx(sessions, lambda s: queue.enqueue(s, kind=k, payload={}, workspace_id=None, dedupe_key=key))
    assert a[1] is True and b == (a[0], False)
    # Once finished, the same key can be enqueued again.
    claimed = tx(sessions, lambda s: queue.claim(s, worker_id="w", limit=1, lease_seconds=60, kinds=[k]))
    tx(sessions, lambda s: queue.complete(s, job_id=claimed[0].id, worker_id="w"))
    c = tx(sessions, lambda s: queue.enqueue(s, kind=k, payload={}, workspace_id=None, dedupe_key=key))
    assert c[1] is True and c[0] != a[0]


def test_skip_locked_claims_are_disjoint(sessions: sessionmaker[Session]) -> None:
    k = kind()
    for _ in range(6):
        tx(sessions, lambda s: queue.enqueue(s, kind=k, payload={}, workspace_id=None))
    s1, s2 = sessions(), sessions()
    try:
        first = queue.claim(s1, worker_id="w1", limit=4, lease_seconds=60, kinds=[k])   # s1 holds row locks
        second = queue.claim(s2, worker_id="w2", limit=4, lease_seconds=60, kinds=[k])  # skips them
        s1.commit()
        s2.commit()
    finally:
        s1.close()
        s2.close()
    assert len(first) == 4 and len(second) == 2
    assert not {j.id for j in first} & {j.id for j in second}


def test_crashed_worker_lease_is_reclaimed_then_dead_lettered(sessions: sessionmaker[Session]) -> None:
    k = kind()
    job_id, _ = tx(sessions, lambda s: queue.enqueue(s, kind=k, payload={}, workspace_id=None, max_attempts=2))
    for attempt in (1, 2):
        claimed = tx(sessions, lambda s: queue.claim(s, worker_id="crashy", limit=1, lease_seconds=60, kinds=[k]))
        assert claimed and claimed[0].attempts == attempt
        # Simulate a crash: the lease simply runs out without heartbeat or completion.
        tx(sessions, lambda s: s.execute(update(Job).where(Job.id == job_id).values(
            lease_expires_at=func.now() - timedelta(seconds=1), run_at=func.now())))
        tx(sessions, queue.reap_expired)
        if attempt == 1:
            assert state_of(sessions, job_id).state is JobState.QUEUED
            tx(sessions, lambda s: s.execute(update(Job).where(Job.id == job_id).values(run_at=func.now())))
    job = state_of(sessions, job_id)
    assert job.state is JobState.DEAD and "lease expired" in (job.last_error or "")


def test_stale_owner_cannot_overwrite_reclaimed_job(sessions: sessionmaker[Session]) -> None:
    k = kind()
    job_id, _ = tx(sessions, lambda s: queue.enqueue(s, kind=k, payload={}, workspace_id=None))
    tx(sessions, lambda s: queue.claim(s, worker_id="old", limit=1, lease_seconds=60, kinds=[k]))
    tx(sessions, lambda s: s.execute(update(Job).where(Job.id == job_id).values(
        lease_expires_at=func.now() - timedelta(seconds=1))))
    tx(sessions, queue.reap_expired)
    tx(sessions, lambda s: s.execute(update(Job).where(Job.id == job_id).values(run_at=func.now())))
    tx(sessions, lambda s: queue.claim(s, worker_id="new", limit=1, lease_seconds=60, kinds=[k]))
    assert tx(sessions, lambda s: queue.complete(s, job_id=job_id, worker_id="old")) is False
    assert state_of(sessions, job_id).locked_by == "new"


def test_retryable_failure_backs_off_and_is_not_claimable_early(sessions: sessionmaker[Session]) -> None:
    k = kind()
    tx(sessions, lambda s: queue.enqueue(s, kind=k, payload={}, workspace_id=None))
    job = tx(sessions, lambda s: queue.claim(s, worker_id="w", limit=1, lease_seconds=60, kinds=[k]))[0]
    state = tx(sessions, lambda s: queue.fail(s, job=job, worker_id="w", error="boom", retryable=True,
                                              retry_after_seconds=120))
    assert state is JobState.QUEUED
    assert tx(sessions, lambda s: queue.claim(s, worker_id="w", limit=1, lease_seconds=60, kinds=[k])) == []
    assert state_of(sessions, job.id).run_at > datetime.now(UTC) + timedelta(seconds=100)


def test_backoff_grows_and_is_capped() -> None:
    import random
    rng = random.Random(1)  # noqa: S311 - deterministic jitter for the test
    values = [queue.backoff_seconds(a, rng=rng) for a in range(1, 12)]
    assert values[0] < 25 and max(values) <= queue.BACKOFF_CAP_SECONDS * 1.5
    assert values[4] > values[0]


def test_worker_runs_handlers_and_records_outcomes(sessions: sessionmaker[Session]) -> None:
    ok, perm, flaky, crash = kind(), kind(), kind(), kind()
    calls: dict[str, int] = {}

    async def handler(ctx):  # type: ignore[no-untyped-def]
        calls[ctx.job.kind] = calls.get(ctx.job.kind, 0) + 1
        if ctx.job.kind == perm:
            raise PermanentJobError("bad input")
        if ctx.job.kind == flaky:
            raise RetryableJobError("provider busy", retry_after_seconds=300)
        if ctx.job.kind == crash:
            raise RuntimeError("unexpected")
        return {"echo": ctx.job.payload["n"]}

    ids = {k: tx(sessions, lambda s, k=k: queue.enqueue(s, kind=k, payload={"n": 7}, workspace_id=None))[0]
           for k in (ok, perm, flaky, crash)}
    settings = get_settings().model_copy(update={"worker_concurrency": 4})
    worker = Worker(sessions=sessions, settings=settings, handlers={k: handler for k in ids})

    async def go() -> None:
        await worker.tick()
        await worker.drain()

    asyncio.run(go())
    assert state_of(sessions, ids[ok]).state is JobState.SUCCEEDED
    assert state_of(sessions, ids[ok]).result == {"echo": 7}
    assert state_of(sessions, ids[perm]).state is JobState.FAILED
    assert state_of(sessions, ids[flaky]).state is JobState.QUEUED  # scheduled retry
    crashed = state_of(sessions, ids[crash])
    assert crashed.state is JobState.QUEUED and "RuntimeError" in (crashed.last_error or "")
    assert calls == {ok: 1, perm: 1, flaky: 1, crash: 1}


def test_worker_restart_does_not_rerun_completed_jobs(sessions: sessionmaker[Session]) -> None:
    k = kind()
    runs: list[uuid.UUID] = []

    async def handler(ctx):  # type: ignore[no-untyped-def]
        runs.append(ctx.job.id)
        return None

    for _ in range(3):
        tx(sessions, lambda s: queue.enqueue(s, kind=k, payload={}, workspace_id=None))
    settings = get_settings().model_copy(update={"worker_concurrency": 2})

    async def one_round(worker: Worker) -> None:
        while await worker.tick():
            await worker.drain()

    asyncio.run(one_round(Worker(sessions=sessions, settings=settings, handlers={k: handler})))
    asyncio.run(one_round(Worker(sessions=sessions, settings=settings, handlers={k: handler})))  # "restart"
    assert len(runs) == 3 and len(set(runs)) == 3
    with sessions() as s:
        assert set(s.scalars(select(Job.state).where(Job.kind == k)).all()) == {JobState.SUCCEEDED}


def test_dedupe_survives_server_side_prepared_statements(sessions: sessionmaker[Session]) -> None:
    """Regression: psycopg prepares statements after 5 executions; ON CONFLICT must still match the partial index."""
    k = kind()
    with sessions() as s:
        ids = [queue.enqueue(s, kind=k, payload={}, workspace_id=None, dedupe_key=f"test:{k}:{i % 3}")
               for i in range(12)]
        s.commit()
    assert sum(created for _, created in ids) == 3
    assert len({job_id for job_id, _ in ids}) == 3
