"""Fixed-window rate limiting.

`RedisRateLimiter` is the production implementation. `InMemoryRateLimiter` exists only for unit tests
and single-process local experiments; it is never selected automatically.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import redis


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class RateLimiter(Protocol):
    def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult: ...

    def reset(self, key: str) -> None: ...


class RedisRateLimiter:
    def __init__(self, client: redis.Redis) -> None:
        self._client = client

    def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        bucket = f"ratelimit:{key}"
        pipe = self._client.pipeline(transaction=True)
        pipe.incr(bucket)
        pipe.expire(bucket, window_seconds, nx=True)
        pipe.ttl(bucket)
        count, _, ttl = pipe.execute()
        ttl = int(ttl) if int(ttl) > 0 else window_seconds
        return RateLimitResult(allowed=int(count) <= limit, remaining=max(0, limit - int(count)),
                               retry_after_seconds=ttl)

    def reset(self, key: str) -> None:
        self._client.delete(f"ratelimit:{key}")


class InMemoryRateLimiter:
    """Test-only limiter. Not shared across processes."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[int, float]] = {}

    def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        now = self._clock()
        with self._lock:
            count, expires = self._buckets.get(key, (0, now + window_seconds))
            if now >= expires:
                count, expires = 0, now + window_seconds
            count += 1
            self._buckets[key] = (count, expires)
        return RateLimitResult(allowed=count <= limit, remaining=max(0, limit - count),
                               retry_after_seconds=max(1, int(expires - now)))

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)
