"""Task-based model routing with quota-aware fallback and schema-validated outputs.

For each task there is an ordered chain of (provider, model). A model whose provider reports a long rate limit
or exhausted quota is skipped until its reset time (shared across workers via Redis). Every response is
validated against the caller's Pydantic model; one repair attempt is made per model before moving on.
The router never invents output: if every model fails, it raises `LLMUnavailable` with the earliest retry time.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, TypeVar

from prometheus_client import Counter, Histogram
from pydantic import BaseModel, ValidationError

from app.core.logging import get_logger
from app.core.metrics import registry
from app.integrations import http
from app.integrations.llm.base import LLMProvider, LLMUsage, json_schema_for, strip_code_fences

log = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)

llm_requests_total = Counter("llm_requests_total", "LLM calls", ["task", "provider", "model", "outcome"],
                             registry=registry)
llm_latency_seconds = Histogram("llm_latency_seconds", "LLM call latency", ["provider", "model"],
                                buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600), registry=registry)
llm_tokens_total = Counter("llm_tokens_total", "LLM tokens", ["provider", "model", "kind"], registry=registry)


class LLMUnavailable(Exception):
    def __init__(self, message: str, *, retry_after: float | None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ExhaustionStore:
    """Remembers exhausted (provider, model) pairs until their reset time. Redis-backed when available."""

    def __init__(self, redis: Any | None = None) -> None:
        self._redis = redis
        self._local: dict[str, float] = {}

    @staticmethod
    def _key(provider: str, model: str) -> str:
        return f"llm:exhausted:{provider}:{model}"

    async def mark(self, provider: str, model: str, seconds: float) -> None:
        seconds = max(1.0, min(seconds, 86400.0))
        self._local[self._key(provider, model)] = time.time() + seconds
        if self._redis is not None:
            try:
                await self._redis.set(self._key(provider, model), "1", ex=int(seconds))
            except Exception as exc:  # degraded mode: keep the in-process record
                log.warning("exhaustion_store_unavailable", error_type=type(exc).__name__)

    async def remaining(self, provider: str, model: str) -> float:
        key = self._key(provider, model)
        if self._redis is not None:
            try:
                ttl = await self._redis.ttl(key)
                if ttl and ttl > 0:
                    return float(ttl)
            except Exception:
                log.debug("exhaustion_store_read_failed")
        until = self._local.get(key, 0.0)
        return max(0.0, until - time.time())


class LLMRouter:
    def __init__(self, *, providers: dict[str, LLMProvider], chains: dict[str, list[tuple[str, str]]],
                 exhaustion: ExhaustionStore | None = None, concurrency: int = 2) -> None:
        self.providers = providers
        self.chains = chains
        self.exhaustion = exhaustion or ExhaustionStore()
        self._sem = asyncio.Semaphore(concurrency)

    def configured(self, task: str) -> list[tuple[str, str]]:
        return [(p, m) for p, m in self.chains.get(task, []) if p in self.providers]

    async def complete(self, *, task: str, system: str, user: str, output: type[T],
                       max_tokens: int = 1200) -> tuple[T, LLMUsage]:
        chain = self.configured(task)
        if not chain:
            raise LLMUnavailable(f"no LLM provider configured for task {task!r}", retry_after=None)
        schema = json_schema_for(output)
        errors: list[str] = []
        soonest: float | None = None
        for provider_name, model in chain:
            wait = await self.exhaustion.remaining(provider_name, model)
            if wait > 0:
                soonest = wait if soonest is None else min(soonest, wait)
                errors.append(f"{provider_name}:{model} exhausted for {int(wait)}s")
                continue
            provider = self.providers[provider_name]
            prompt = user
            for attempt in (1, 2):
                try:
                    async with self._sem:
                        text, usage = await provider.complete_json(model=model, system=system, user=prompt,
                                                                   schema=schema, max_tokens=max_tokens)
                except http.ProviderRateLimited as exc:
                    reset = exc.retry_after or 60.0
                    await self.exhaustion.mark(provider_name, model, reset)
                    soonest = reset if soonest is None else min(soonest, reset)
                    llm_requests_total.labels(task, provider_name, model, "rate_limited").inc()
                    errors.append(f"{provider_name}:{model} rate limited ({int(reset)}s)")
                    break
                except (http.ProviderUnavailable, http.ProviderAuthError) as exc:
                    llm_requests_total.labels(task, provider_name, model, "unavailable").inc()
                    errors.append(str(exc))
                    break
                except http.ProviderBadRequest as exc:
                    # e.g. the provider rejected output that failed its own schema check: try once more.
                    llm_requests_total.labels(task, provider_name, model, "bad_request").inc()
                    errors.append(str(exc)[:200])
                    if attempt == 1:
                        prompt = user + "\n\nReturn ONLY a JSON object that matches the required schema."
                        continue
                    break
                llm_latency_seconds.labels(provider_name, model).observe(usage.latency_ms / 1000)
                llm_tokens_total.labels(provider_name, model, "prompt").inc(usage.prompt_tokens)
                llm_tokens_total.labels(provider_name, model, "completion").inc(usage.completion_tokens)
                try:
                    parsed = output.model_validate_json(strip_code_fences(text))
                except ValidationError as exc:
                    llm_requests_total.labels(task, provider_name, model, "invalid_output").inc()
                    errors.append(f"{provider_name}:{model} invalid output: {exc.error_count()} errors")
                    if attempt == 1:
                        prompt = (user + "\n\nYour previous answer did not match the required JSON schema "
                                  f"({exc.errors()[0]['msg'][:120]}). Return only valid JSON.")
                        continue
                    break
                llm_requests_total.labels(task, provider_name, model, "ok").inc()
                return parsed, usage
        log.warning("llm_all_models_failed", task=task, errors=errors[-5:])
        raise LLMUnavailable("; ".join(errors[-5:]) or "all models failed", retry_after=soonest)
