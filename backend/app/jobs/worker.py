"""Async worker runtime: claims jobs, runs handlers with bounded concurrency, heartbeats leases.

Handlers are `async def handler(ctx: JobContext) -> dict | None`. They signal outcomes by exception:
- PermanentJobError   -> job FAILED (no retry), e.g. bad input or missing configuration
- RetryableJobError   -> retried with backoff (or after `retry_after_seconds`), DEAD when attempts run out
- any other Exception -> treated as retryable and logged with its traceback
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import registry
from app.jobs import queue
from app.jobs.queue import ClaimedJob

log = get_logger("worker")

jobs_total = Counter("jobs_total", "Jobs finished by outcome", ["kind", "outcome"], registry=registry)
job_duration_seconds = Histogram("job_duration_seconds", "Job handler duration", ["kind"],
                                 buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800), registry=registry)
jobs_running = Gauge("jobs_running", "Jobs currently executing in this worker", registry=registry)
jobs_reaped_total = Counter("jobs_reaped_total", "Expired leases reclaimed", ["outcome"], registry=registry)


class PermanentJobError(Exception):
    pass


class RetryableJobError(Exception):
    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass
class JobContext:
    job: ClaimedJob
    sessions: sessionmaker[Session]
    services: dict[str, Any] = field(default_factory=dict)

    async def db(self, fn: Callable[[Session], Any]) -> Any:
        """Run `fn(session)` in a thread inside one transaction (committed on success)."""
        def _run() -> Any:
            with self.sessions() as s:
                out = fn(s)
                s.commit()
                return out
        return await asyncio.to_thread(_run)


Handler = Callable[[JobContext], Awaitable[dict[str, Any] | None]]


class Worker:
    def __init__(self, *, sessions: sessionmaker[Session], settings: Settings, handlers: dict[str, Handler],
                 services: dict[str, Any] | None = None, worker_id: str | None = None) -> None:
        self.sessions = sessions
        self.settings = settings
        self.handlers = handlers
        self.services = services or {}
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._tasks: set[asyncio.Task[None]] = set()
        self._last_reap = 0.0

    def _tx(self, fn: Callable[[Session], Any]) -> Any:
        with self.sessions() as s:
            out = fn(s)
            s.commit()
            return out

    async def run(self, stop: asyncio.Event, *, grace_seconds: float = 30) -> None:
        log.info("worker_started", worker_id=self.worker_id, concurrency=self.settings.worker_concurrency)
        while not stop.is_set():
            await self.tick()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.settings.worker_poll_seconds)
        if self._tasks:
            log.info("worker_draining", running=len(self._tasks))
            await asyncio.wait(self._tasks, timeout=grace_seconds)
        log.info("worker_stopped", worker_id=self.worker_id)

    async def tick(self) -> int:
        """One scheduling round: reap expired leases (periodically) and claim up to the free capacity."""
        if time.monotonic() - self._last_reap > 30:
            requeued, dead = await asyncio.to_thread(self._tx, queue.reap_expired)
            self._last_reap = time.monotonic()
            if requeued or dead:
                jobs_reaped_total.labels("requeued").inc(requeued)
                jobs_reaped_total.labels("dead").inc(dead)
                log.warning("leases_reaped", requeued=requeued, dead=dead)
        free = self.settings.worker_concurrency - len(self._tasks)
        if free <= 0:
            return 0
        claimed: list[ClaimedJob] = await asyncio.to_thread(
            self._tx, lambda s: queue.claim(s, worker_id=self.worker_id, limit=free,
                                            lease_seconds=self.settings.job_lease_seconds,
                                            kinds=list(self.handlers)))
        for job in claimed:
            task = asyncio.create_task(self._execute(job))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return len(claimed)

    async def drain(self) -> None:
        """Wait for all running tasks (used by tests and graceful shutdown)."""
        while self._tasks:
            await asyncio.wait(set(self._tasks))

    async def _heartbeat(self, job: ClaimedJob) -> None:
        interval = max(5.0, self.settings.job_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            owned = await asyncio.to_thread(self._tx, lambda s: queue.heartbeat(
                s, job_id=job.id, worker_id=self.worker_id, lease_seconds=self.settings.job_lease_seconds))
            if not owned:
                log.warning("lease_lost", job_id=str(job.id), kind=job.kind)
                return

    async def _execute(self, job: ClaimedJob) -> None:
        handler = self.handlers[job.kind]
        beat = asyncio.create_task(self._heartbeat(job))
        jobs_running.inc()
        start = time.perf_counter()
        bound = log.bind(job_id=str(job.id), kind=job.kind, attempt=job.attempts)
        try:
            result = await handler(JobContext(job=job, sessions=self.sessions, services=self.services))
            done = await asyncio.to_thread(self._tx, lambda s: queue.complete(
                s, job_id=job.id, worker_id=self.worker_id, result=result))
            outcome = "succeeded" if done else "lease_lost"
            bound.info("job_succeeded" if done else "job_result_discarded_lease_lost")
        # Errors are copied into plain locals before any closure: exception names are unbound when an
        # `except` block ends, so lambdas must never capture them.
        except PermanentJobError as exc:
            message = str(exc)
            await asyncio.to_thread(self._tx, lambda s: queue.fail(
                s, job=job, worker_id=self.worker_id, error=message, retryable=False))
            outcome = "failed"
            bound.warning("job_failed_permanently", error=message)
        except RetryableJobError as exc:
            message, retry_after = str(exc), exc.retry_after_seconds
            state = await asyncio.to_thread(self._tx, lambda s: queue.fail(
                s, job=job, worker_id=self.worker_id, error=message, retryable=True, retry_after_seconds=retry_after))
            outcome = state.value if state else "lease_lost"
            bound.warning("job_retry_scheduled" if state and state.value == "queued" else "job_dead", error=message)
        except Exception as exc:
            message, error_type = f"{type(exc).__name__}: {exc}", type(exc).__name__
            state = await asyncio.to_thread(self._tx, lambda s: queue.fail(
                s, job=job, worker_id=self.worker_id, error=message, retryable=True))
            outcome = state.value if state else "lease_lost"
            bound.exception("job_crashed", error_type=error_type)
        finally:
            beat.cancel()
            jobs_running.dec()
        jobs_total.labels(job.kind, outcome).inc()
        job_duration_seconds.labels(job.kind).observe(time.perf_counter() - start)
