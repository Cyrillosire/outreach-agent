"""Resilient outbound HTTP for third-party APIs.

Retries transient failures (timeouts, connection errors, 5xx, short 429s) with exponential backoff and jitter,
honours Retry-After, and turns everything else into typed errors. Long rate-limit waits are *not* slept on:
they surface as `ProviderRateLimited` so callers can fall back to another provider or reschedule the job.
Request/response bodies and headers are never logged (they may carry API keys or personal data).
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.core.logging import get_logger

log = get_logger(__name__)


class ProviderError(Exception):
    def __init__(self, provider: str, message: str, *, status: int | None = None) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.status = status


class ProviderAuthError(ProviderError):
    """Missing/invalid credentials. Not retryable until configuration changes."""


class ProviderRateLimited(ProviderError):
    """Rate limited or quota exhausted for longer than we are willing to wait inline."""

    def __init__(self, provider: str, message: str, *, retry_after: float | None, status: int | None = 429) -> None:
        super().__init__(provider, message, status=status)
        self.retry_after = retry_after


class ProviderUnavailable(ProviderError):
    """Network failure or 5xx after retries."""


class ProviderBadRequest(ProviderError):
    """4xx other than auth/rate-limit: the request itself was rejected."""


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError):
        return None


def _backoff(attempt: int, base: float, cap: float) -> float:
    return float(min(cap, base * 2 ** (attempt - 1)) * (0.5 + random.random()))  # noqa: S311 - jitter only


async def request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    provider: str,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_inline_wait: float = 20.0,
    quota_statuses: frozenset[int] = frozenset(),
    **kwargs: Any,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            log.warning("provider_transport_error", provider=provider, attempt=attempt, error_type=type(exc).__name__)
            if attempt < max_attempts:
                await asyncio.sleep(_backoff(attempt, base_delay, max_inline_wait))
                continue
            raise ProviderUnavailable(provider, f"network error: {type(exc).__name__}") from exc

        status = response.status_code
        if status < 400:
            return response
        retry_after = parse_retry_after(response.headers.get("retry-after"))
        if status in quota_statuses:
            raise ProviderRateLimited(provider, f"quota exhausted (HTTP {status})", retry_after=retry_after or 86400,
                                      status=status)
        if status in (401, 403):
            raise ProviderAuthError(provider, f"authentication failed (HTTP {status})", status=status)
        if status == 429:
            wait = retry_after if retry_after is not None else _backoff(attempt, base_delay, max_inline_wait)
            if wait > max_inline_wait or attempt == max_attempts:
                raise ProviderRateLimited(provider, "rate limited", retry_after=wait)
            log.info("provider_rate_limited_waiting", provider=provider, wait_s=round(wait, 1))
            await asyncio.sleep(wait)
            continue
        if status >= 500:
            last_error = ProviderUnavailable(provider, f"HTTP {status}", status=status)
            if attempt < max_attempts:
                await asyncio.sleep(retry_after or _backoff(attempt, base_delay, max_inline_wait))
                continue
            raise last_error
        raise ProviderBadRequest(provider, f"HTTP {status}: {response.text[:300]}", status=status)
    raise ProviderUnavailable(provider, f"exhausted retries: {last_error}")
