"""SSRF-guarded, robots-respecting page fetcher.

Defense layers: scheme/port/credential/blocklist checks -> every resolved IP must be public -> redirects are
followed manually and each hop re-validated -> the connected peer address is re-checked (DNS-rebinding guard)
-> content type allowlist, byte cap, and total time limit. Fetched content is untrusted DATA: it is only ever
passed to extraction code and delimited LLM prompts, never interpreted as instructions.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib import robotparser
from urllib.parse import urlsplit

import httpx
from prometheus_client import Counter

from app.core.logging import get_logger
from app.core.metrics import registry
from app.integrations.web.urls import BlockedURL, check_url_shape, is_public_ip, normalize_url, resolve_public

log = get_logger(__name__)
fetches_total = Counter("web_fetches_total", "Page fetch outcomes", ["outcome"], registry=registry)

ALLOWED_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
MAX_REDIRECTS = 5
_ACCEPT = "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.5"


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    status: int
    content_type: str
    body: str | None
    fetched_at: datetime
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.body is not None and self.error is None and 200 <= self.status < 300


class WebFetcher:
    def __init__(self, *, user_agent: str, timeout: float, max_bytes: int, concurrency: int = 4,
                 per_host_delay: float = 1.0, client: httpx.AsyncClient | None = None,
                 resolve: Any = resolve_public, check_peer: bool = True) -> None:
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0),
                                                   follow_redirects=False,
                                                   headers={"User-Agent": user_agent, "Accept": _ACCEPT})
        self._user_agent = user_agent
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._sem = asyncio.Semaphore(concurrency)
        self._host_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._host_last: dict[str, float] = {}
        self._per_host_delay = per_host_delay
        self._robots: dict[str, tuple[float, robotparser.RobotFileParser | None]] = {}
        self._resolve = resolve
        self._check_peer = check_peer

    async def _validate(self, url: str) -> None:
        host = check_url_shape(url)
        parts = urlsplit(url)
        await self._resolve(host, parts.port or (443 if parts.scheme == "https" else 80))

    async def _get(self, url: str, accept_types: tuple[str, ...]) -> tuple[int, str, str, str | None]:
        """One validated GET following redirects manually. Returns (status, final_url, content_type, text)."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            await self._validate(current)
            async with self._client.stream("GET", current) as response:
                if self._check_peer:
                    stream = response.extensions.get("network_stream")
                    peer = stream.get_extra_info("server_addr") if stream is not None else None
                    if peer and not is_public_ip(str(peer[0])):
                        raise BlockedURL("connected to a non-public address")
                if response.status_code in (301, 302, 303, 307, 308) and "location" in response.headers:
                    current = str(response.url.join(response.headers["location"]))
                    continue
                ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if response.status_code >= 400 or not any(ctype.startswith(t) for t in accept_types):
                    return response.status_code, str(response.url), ctype, None
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self._max_bytes:
                        break
                    chunks.append(chunk)
                raw = b"".join(chunks)
                encoding = response.encoding or "utf-8"
                return response.status_code, str(response.url), ctype, raw.decode(encoding, errors="replace")
        raise BlockedURL("too many redirects")

    async def allowed_by_robots(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(origin)
        if cached is None or time.monotonic() - cached[0] > 86400:
            parser: robotparser.RobotFileParser | None
            try:
                status, _, _, text = await asyncio.wait_for(
                    self._get(origin + "/robots.txt", ("text/plain", "text/html", "")), self._timeout)
            except (BlockedURL, httpx.HTTPError, TimeoutError):
                status, text = 0, None
            if 400 <= status < 500 or (status < 300 and text is None):
                parser = None  # RFC 9309: unavailable robots.txt (4xx) means no restrictions
            elif status >= 500 or status == 0:
                parser = robotparser.RobotFileParser()
                parser.parse(["User-agent: *", "Disallow: /"])  # unreachable: assume complete disallow
            else:
                parser = robotparser.RobotFileParser()
                parser.parse((text or "").splitlines())
            self._robots[origin] = (time.monotonic(), parser)
            cached = self._robots[origin]
        parser = cached[1]
        return True if parser is None else parser.can_fetch(self._user_agent, url)

    async def fetch(self, url: str) -> FetchResult:
        now = datetime.now(UTC)
        try:
            normalized = normalize_url(url)
            check_url_shape(normalized)
            if not await self.allowed_by_robots(normalized):
                fetches_total.labels("robots_disallowed").inc()
                return FetchResult(url, normalized, 0, "", None, now, "disallowed by robots.txt")
            host = urlsplit(normalized).hostname or ""
            async with self._sem, self._host_locks[host]:
                wait = self._per_host_delay - (time.monotonic() - self._host_last.get(host, 0.0))
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    status, final_url, ctype, body = await asyncio.wait_for(self._get(normalized, ALLOWED_TYPES),
                                                                             self._timeout)
                finally:
                    self._host_last[host] = time.monotonic()
        except BlockedURL as exc:
            fetches_total.labels("blocked").inc()
            return FetchResult(url, url, 0, "", None, now, f"blocked: {exc}")
        except (httpx.HTTPError, TimeoutError) as exc:
            fetches_total.labels("error").inc()
            return FetchResult(url, url, 0, "", None, now, f"fetch failed: {type(exc).__name__}")
        outcome = "ok" if body is not None and status < 300 else "rejected"
        fetches_total.labels(outcome).inc()
        error = None if outcome == "ok" else f"HTTP {status} {ctype or 'unknown type'}"
        return FetchResult(url, final_url, status, ctype, body, now, error)
