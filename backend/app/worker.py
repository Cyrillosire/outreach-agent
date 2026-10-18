"""Worker process entrypoint:  python -m app.worker

Builds provider clients from configuration, registers job handlers, and runs until SIGINT/SIGTERM. Missing
provider keys are reported at startup; research jobs then fail with an actionable message instead of guessing.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

import redis.asyncio as aioredis
from prometheus_client import start_http_server

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.metrics import registry
from app.db.session import get_sessionmaker
from app.integrations.llm.base import LLMProvider
from app.integrations.llm.groq import GroqProvider
from app.integrations.llm.ollama import OllamaProvider
from app.integrations.llm.router import ExhaustionStore, LLMRouter
from app.integrations.search.providers import BraveSearch, SearchProvider, SearchRouter, TavilySearch
from app.integrations.web.fetcher import WebFetcher
from app.jobs.worker import Worker
from app.services.drafting.pipeline import HANDLERS as DRAFT_HANDLERS
from app.services.drafting.pipeline import DraftingServices
from app.services.research.pipeline import HANDLERS, ResearchServices
from app.services.research.store import reconcile_stuck

log = get_logger("worker")


def build_research_services(settings: Settings, redis: aioredis.Redis | None) -> ResearchServices:
    providers: dict[str, LLMProvider] = {}
    if settings.groq_api_key.get_secret_value():
        providers["groq"] = GroqProvider(api_key=settings.groq_api_key.get_secret_value(),
                                         base_url=settings.groq_base_url, timeout=settings.llm_timeout_seconds)
    if any(p == "ollama" for chain in (settings.llm_extraction_chain, settings.llm_reasoning_chain)
           for p, _ in Settings.parse_chain(chain)):
        providers["ollama"] = OllamaProvider(base_url=settings.ollama_base_url,
                                             timeout=settings.ollama_timeout_seconds)
    exhaustion = ExhaustionStore(redis)
    llm = LLMRouter(providers=providers, exhaustion=exhaustion, concurrency=settings.llm_concurrency, chains={
        "extract": Settings.parse_chain(settings.llm_extraction_chain),
        "reason": Settings.parse_chain(settings.llm_reasoning_chain),
    })
    search_providers: dict[str, SearchProvider] = {}
    if settings.tavily_api_key.get_secret_value():
        search_providers["tavily"] = TavilySearch(api_key=settings.tavily_api_key.get_secret_value())
    if settings.brave_api_key.get_secret_value():
        search_providers["brave"] = BraveSearch(api_key=settings.brave_api_key.get_secret_value())
    order = [n.strip() for n in settings.search_provider_order.split(",") if n.strip() in search_providers]
    search = SearchRouter([search_providers[n] for n in order], redis=redis,
                          cache_seconds=settings.search_cache_hours * 3600, exhaustion=exhaustion)
    fetcher = WebFetcher(user_agent=settings.fetch_user_agent, timeout=settings.fetch_timeout_seconds,
                         max_bytes=settings.fetch_max_bytes, concurrency=settings.fetch_concurrency)
    return ResearchServices(settings=settings, llm=llm, search=search, fetcher=fetcher)


def build_drafting_services(settings: Settings, research: ResearchServices) -> DraftingServices:
    """Drafting and review share provider clients and quota tracking with research, but never use local models:
    accuracy matters more than cost for anything a prospect will read."""
    def hosted(chain: str) -> list[tuple[str, str]]:
        return [c for c in Settings.parse_chain(chain) if c[0] != "ollama"]

    llm = LLMRouter(providers={k: v for k, v in research.llm.providers.items() if k != "ollama"},
                    exhaustion=research.llm.exhaustion, concurrency=settings.llm_concurrency,
                    chains={"write": hosted(settings.llm_drafting_chain), "review": hosted(settings.llm_review_chain)})
    return DraftingServices(settings=settings, llm=llm)


def provider_status(settings: Settings) -> dict[str, bool]:
    return {"groq": bool(settings.groq_api_key.get_secret_value()),
            "tavily": bool(settings.tavily_api_key.get_secret_value()),
            "brave": bool(settings.brave_api_key.get_secret_value())}


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    if settings.worker_metrics_port:
        start_http_server(settings.worker_metrics_port, registry=registry)
    redis = aioredis.Redis.from_url(settings.redis_url.get_secret_value(), socket_timeout=5)
    research = build_research_services(settings, redis)
    services = {"research": research, "drafting": build_drafting_services(settings, research)}
    status = provider_status(settings)
    log.info("providers_configured", **status,
             extraction_chain=settings.llm_extraction_chain, search_order=settings.search_provider_order)
    if not status["groq"]:
        log.warning("groq_not_configured", hint="set GROQ_API_KEY; extraction will use only local models if any")
    if not (status["tavily"] or status["brave"]):
        log.warning("search_not_configured", hint="set TAVILY_API_KEY and/or BRAVE_API_KEY")

    worker = Worker(sessions=get_sessionmaker(), settings=settings, handlers={**HANDLERS, **DRAFT_HANDLERS},
                    services=services,
                    maintenance=[reconcile_stuck])
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows event loops lack signal handlers; Ctrl+C then arrives as KeyboardInterrupt instead.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await worker.run(stop)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
