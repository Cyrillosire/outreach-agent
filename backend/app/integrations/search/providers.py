"""Web search providers behind one interface, plus a caching, quota-aware router.

Search results are *leads*, not evidence: every claim must come from a page the fetcher retrieved itself, so its
excerpt can be verified verbatim. The one exception is the prospect's own LinkedIn profile and posts, which are
never fetched (LinkedIn's terms); there the indexed title and snippet are the source text, matched to the person by
the profile URL on their record (see services/research/linkedin.py).
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Protocol

import httpx
from prometheus_client import Counter

from app.core.logging import get_logger
from app.core.metrics import registry
from app.integrations import http

log = get_logger(__name__)
SNIPPET_CHARS = 1500
search_requests_total = Counter("search_requests_total", "Search API calls", ["provider", "outcome"],
                                registry=registry)


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str
    snippet: str
    provider: str
    rank: int
    published: str | None = None


class SearchProvider(Protocol):
    name: str

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]: ...


class TavilySearch:
    """https://docs.tavily.com — 1 credit per basic search. HTTP 432/433 signal exhausted plan credits."""

    name = "tavily"

    def __init__(self, *, api_key: str, timeout: float = 20, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(base_url="https://api.tavily.com", timeout=timeout)
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        response = await http.request(
            self._client, "POST", "/search", provider=self.name, headers=self._headers,
            quota_statuses=frozenset({402, 432, 433}),
            json={"query": query, "max_results": min(max_results, 20), "search_depth": "basic",
                  "include_answer": False, "include_raw_content": False, "include_images": False})
        return [SearchResult(url=r["url"], title=r.get("title") or "", snippet=(r.get("content") or "")[:SNIPPET_CHARS],
                             provider=self.name, rank=i, published=r.get("published_date"))
                for i, r in enumerate(response.json().get("results") or []) if r.get("url")]


class BraveSearch:
    """https://api-dashboard.search.brave.com — independent index; monthly free credit on paid plans."""

    name = "brave"

    def __init__(self, *, api_key: str, timeout: float = 20, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(base_url="https://api.search.brave.com", timeout=timeout)
        self._headers = {"X-Subscription-Token": api_key, "Accept": "application/json"}

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        response = await http.request(
            self._client, "GET", "/res/v1/web/search", provider=self.name, headers=self._headers,
            quota_statuses=frozenset({402}),
            params={"q": query, "count": min(max_results, 20), "extra_snippets": "true"})
        results = ((response.json().get("web") or {}).get("results")) or []
        return [SearchResult(url=r["url"], title=_plain(r.get("title") or ""),
                             snippet=_plain(" … ".join([r.get("description") or "", *(r.get("extra_snippets") or [])]))
                             [:SNIPPET_CHARS],
                             provider=self.name, rank=i, published=r.get("page_age") or r.get("age"))
                for i, r in enumerate(results) if r.get("url")]


def _plain(text: str) -> str:
    """Brave highlights matches with <strong> tags and HTML entities; evidence needs the plain words."""
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip(" …")


class SearchUnavailable(Exception):
    def __init__(self, message: str, *, retry_after: float | None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SearchRouter:
    def __init__(self, providers: list[SearchProvider], *, redis: Any | None = None, cache_seconds: int = 86400,
                 exhaustion: Any | None = None) -> None:
        from app.integrations.llm.router import ExhaustionStore  # shared reset-time bookkeeping

        self.providers = providers
        self._redis = redis
        self._cache_seconds = cache_seconds
        self._exhaustion = exhaustion or ExhaustionStore(redis)

    @property
    def configured(self) -> bool:
        return bool(self.providers)

    def _cache_key(self, query: str, max_results: int) -> str:
        digest = hashlib.sha256(f"{query.strip().lower()}|{max_results}".encode()).hexdigest()
        return f"search:v1:{digest}"

    async def search(self, query: str, *, max_results: int = 8) -> list[SearchResult]:
        if not self.providers:
            raise SearchUnavailable("no search provider configured (set TAVILY_API_KEY or BRAVE_API_KEY)",
                                    retry_after=None)
        key = self._cache_key(query, max_results)
        if self._redis is not None and self._cache_seconds:
            try:
                cached = await self._redis.get(key)
                if cached:
                    search_requests_total.labels("cache", "hit").inc()
                    return [SearchResult(**r) for r in json.loads(cached)]
            except Exception:
                log.debug("search_cache_read_failed")
        errors: list[str] = []
        soonest: float | None = None
        for provider in self.providers:
            wait = await self._exhaustion.remaining(provider.name, "search")
            if wait > 0:
                soonest = wait if soonest is None else min(soonest, wait)
                continue
            try:
                results = await provider.search(query, max_results=max_results)
            except http.ProviderRateLimited as exc:
                reset = exc.retry_after or 3600.0
                await self._exhaustion.mark(provider.name, "search", reset)
                soonest = reset if soonest is None else min(soonest, reset)
                search_requests_total.labels(provider.name, "rate_limited").inc()
                errors.append(f"{provider.name} rate limited")
                continue
            except http.ProviderError as exc:
                search_requests_total.labels(provider.name, "error").inc()
                errors.append(str(exc)[:200])
                continue
            search_requests_total.labels(provider.name, "ok").inc()
            if self._redis is not None and self._cache_seconds:
                try:
                    await self._redis.set(key, json.dumps([asdict(r) for r in results]), ex=self._cache_seconds)
                except Exception:
                    log.debug("search_cache_write_failed")
            return results
        raise SearchUnavailable("; ".join(errors) or "all search providers exhausted", retry_after=soonest)
