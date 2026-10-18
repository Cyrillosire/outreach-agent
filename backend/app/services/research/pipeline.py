"""Async research pipeline and its job handlers.

Stages (recorded on the run as `stage`): collecting -> extracting -> verifying -> done.
Budgets (queries, sources, wall time) are configurable; when evidence stays thin the outcome is
"insufficient_evidence" or "needs_review" -- never a guess.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter, Histogram
from sqlalchemy import update

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import registry
from app.domain.states import ResearchRunStatus, ResearchStatus, ResearchSubject
from app.integrations.llm.router import LLMRouter, LLMUnavailable
from app.integrations.search.providers import SearchRouter, SearchUnavailable
from app.integrations.web.extract import extract_page
from app.integrations.web.fetcher import WebFetcher
from app.integrations.web.urls import host_of, is_blocked_domain, normalize_url, same_site
from app.jobs.worker import JobContext, PermanentJobError, RetryableJobError
from app.models import Company, Prospect
from app.services.research import store
from app.services.research.evidence import (
    Candidate,
    CompanyFacts,
    PersonFacts,
    company_prompt,
    company_sufficiency,
    consolidate,
    excerpt_supported,
    is_sensitive_person_fact,
    normalize_for_match,
    person_prompt,
    person_sufficiency,
    select_passages,
    source_block,
)

log = get_logger(__name__)
stage_seconds = Histogram("research_stage_seconds", "Research stage duration", ["subject", "stage"],
                          buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1200), registry=registry)
sources_total = Counter("research_sources_total", "Pages considered", ["subject", "outcome"], registry=registry)
facts_total = Counter("research_facts_total", "Extracted facts", ["subject", "outcome"], registry=registry)

# External pages need some substance to be worth a source record. Pages on the company's own site are always kept
# (listing and "about" pages are often short but lead to articles), and a page too thin to yield a passage simply
# costs no LLM call.
MIN_TEXT_CHARS = 80
_SITE_KEYWORDS = ("about", "company", "who-we-are", "overview", "news", "press", "media", "newsroom", "blog",
                  "insights", "leadership", "management", "team", "board", "services", "solutions", "projects",
                  "what-we-do", "investors", "annual-report", "careers", "story", "mission")
_ARTICLE_PATH = re.compile(r"/(news|press|media|newsroom|blog|insights)/.+|/20\d\d/", re.IGNORECASE)


@dataclass
class ResearchServices:
    settings: Settings
    llm: LLMRouter
    search: SearchRouter
    fetcher: WebFetcher
    # domain -> (monotonic time, homepage links). Lets individual research find "leadership"/"team" pages without
    # re-fetching the homepage for every colleague at the same company.
    site_links: dict[str, tuple[float, list[tuple[str, str]]]] = field(default_factory=dict)


@dataclass
class Page:
    key: str
    url: str
    host: str
    title: str | None
    text: str
    published_at: datetime | None
    links: list[tuple[str, str]]


class Budget:
    def __init__(self, seconds: int) -> None:
        self.deadline = time.monotonic() + seconds

    @property
    def exhausted(self) -> bool:
        return time.monotonic() > self.deadline


# --- Collection ---------------------------------------------------------------------------------

async def load_page(ctx: JobContext, svc: ResearchServices, url: str, *, company_domain: str | None,
                    use_cache: bool = True, min_chars: int = MIN_TEXT_CHARS) -> tuple[Page | None, uuid.UUID | None]:
    """Fetch (or reuse a fresh cached copy of) a page, extract its main text, and persist it as a source.

    Homepages are loaded with `min_chars=0`: they often carry little main text but are the map to the site.
    """
    try:
        key = normalize_url(url)
    except ValueError:
        return None, None
    ws = ctx.job.workspace_id
    if use_cache:
        cached = await ctx.db(lambda s: store.cached_source(s, ws, key, svc.settings.source_cache_hours))  # type: ignore[arg-type]
        if cached is not None:
            sources_total.labels("any", "cache_hit").inc()
            return Page(key, cached.url, host_of(cached.url), cached.title, cached.extracted_text or "",
                        cached.published_at, []), cached.id
    result = await svc.fetcher.fetch(url)
    if not result.ok or not result.body:
        sources_total.labels("any", "fetch_failed").inc()
        return None, None
    page = extract_page(result.body, result.final_url)
    if len(page.text) < min_chars or not page.text:
        sources_total.labels("any", "too_short").inc()
        return None, None
    final_key = normalize_url(result.final_url)
    host = host_of(result.final_url)
    source_id = await ctx.db(lambda s: store.save_source(
        s, workspace_id=ws, url=result.final_url, url_normalized=final_key, title=page.title, text=page.text,  # type: ignore[arg-type]
        published_at=page.published_at, http_status=result.status, publisher=host,
        source_type=store.classify_source(result.final_url, host, company_domain)))
    sources_total.labels("any", "fetched").inc()
    return Page(final_key, result.final_url, host, page.title, page.text, page.published_at, page.links), source_id


def safe_key(url: str) -> str | None:
    """Normalized URL, or None for malformed links (one bad href must never fail a research job)."""
    try:
        return normalize_url(url)
    except ValueError:
        return None


def _site_link_score(url: str, anchor: str) -> int:
    text = f"{url} {anchor}".lower()
    return sum(1 for k in _SITE_KEYWORDS if k in text)


async def collect_company_pages(ctx: JobContext, svc: ResearchServices, company: Company,
                                notes: list[str]) -> list[tuple[Page, uuid.UUID]]:
    settings = svc.settings
    domain = company.normalized_domain
    pages: dict[str, tuple[Page, uuid.UUID]] = {}
    max_sources = settings.research_company_max_sources

    if domain:
        home = None
        for root in (f"https://{domain}/", f"https://www.{domain}/"):
            home, sid = await load_page(ctx, svc, root, company_domain=domain, use_cache=False, min_chars=0)
            if home and sid:
                pages[home.key] = (home, sid)
                break
        if home is None:
            notes.append(f"Company website {domain} could not be fetched.")
        else:
            svc.site_links[domain] = (time.monotonic(), home.links)
            internal = sorted({(u, a) for u, a in home.links if same_site(host_of(u), domain)
                               and (k := safe_key(u)) is not None and k not in pages and _site_link_score(u, a) > 0},
                              key=lambda la: -_site_link_score(*la))
            picks = list(dict.fromkeys(safe_key(u) for u, _ in internal))[: max(2, max_sources // 2)]
            loaded = await asyncio.gather(*(load_page(ctx, svc, u, company_domain=domain, min_chars=0)
                                            for u in picks if u))
            for page, sid in loaded:
                if page and sid:
                    pages.setdefault(page.key, (page, sid))
            # Follow up to two dated articles from a news/press listing.
            articles = [k for p, _ in list(pages.values()) for u, _a in p.links
                        if same_site(host_of(u), domain) and _ARTICLE_PATH.search(u)
                        and (k := safe_key(u)) is not None and k not in pages]
            for page, sid in await asyncio.gather(*(load_page(ctx, svc, u, company_domain=domain, min_chars=0)
                                                    for u in list(dict.fromkeys(articles))[:2])):
                if page and sid:
                    pages.setdefault(page.key, (page, sid))
    else:
        notes.append("No company domain known; only web search can be used.")

    if svc.search.configured and settings.research_company_max_queries > 0 and len(pages) < max_sources:
        name = company.name
        queries = [f'"{name}" news', f'"{name}" announcement OR partnership OR expansion OR launch',
                   f'"{name}" strategy OR technology OR digital OR tender'][: settings.research_company_max_queries]
        urls: list[str] = []
        for q in queries:
            try:
                for r in await svc.search.search(q, max_results=8):
                    if not is_blocked_domain(host_of(r.url)) and safe_key(r.url) not in pages:
                        urls.append(r.url)
            except SearchUnavailable as exc:
                notes.append(f"Web search unavailable: {exc}")
                break
        # Prefer independent coverage over more pages from the company's own site.
        urls = sorted(dict.fromkeys(urls), key=lambda u: same_site(host_of(u), domain) if domain else False)
        remaining = max(0, max_sources - len(pages))
        for page, sid in await asyncio.gather(*(load_page(ctx, svc, u, company_domain=domain)
                                                for u in urls[:remaining])):
            if page and sid:
                pages.setdefault(page.key, (page, sid))
    elif not svc.search.configured:
        notes.append("Web search is not configured; used the company website only.")
    return list(pages.values())[:max_sources]


_PEOPLE = re.compile(r"leadership|management|team|board|people|directors|executives|about|who-we-are|our-story",
                     re.IGNORECASE)


async def company_site_links(ctx: JobContext, svc: ResearchServices, domain: str) -> list[tuple[str, str]]:
    cached = svc.site_links.get(domain)
    if cached and time.monotonic() - cached[0] < 6 * 3600:
        return cached[1]
    for root in (f"https://{domain}/", f"https://www.{domain}/"):
        home, sid = await load_page(ctx, svc, root, company_domain=domain, use_cache=False, min_chars=0)
        if home and sid:
            svc.site_links[domain] = (time.monotonic(), home.links)
            return home.links
    return []


def mentions_person(text: str, full_name: str, company_name: str | None, domain: str | None) -> bool:
    """Identity guard against namesakes: the page must name the person AND their organization."""
    norm = normalize_for_match(text)
    if normalize_for_match(full_name) not in norm:
        return False
    anchors = [a for a in (company_name, domain.split(".")[0] if domain else None) if a]
    return any(normalize_for_match(a) in norm for a in anchors) if anchors else False


async def collect_person_pages(ctx: JobContext, svc: ResearchServices, prospect: Prospect, company: Company | None,
                               notes: list[str]) -> list[tuple[Page, uuid.UUID]]:
    settings = svc.settings
    full_name = prospect.full_name or ""
    company_name = company.name if company else None
    domain = company.normalized_domain if company else None
    pages: dict[str, tuple[Page, uuid.UUID]] = {}

    if domain:  # pages already collected from the company's own site that name this person
        for src in await ctx.db(lambda s: store.sources_mentioning(s, ctx.job.workspace_id, host_suffix=domain,  # type: ignore[arg-type]
                                                                   phrase=full_name)):
            pages[src.url_normalized] = (Page(src.url_normalized, src.url, host_of(src.url), src.title,
                                              src.extracted_text or "", src.published_at, []), src.id)

    if domain and not pages:
        # Company and individual research run concurrently, so the company's people pages may not be cached yet.
        links = await company_site_links(ctx, svc, domain)
        people_pages = list(dict.fromkeys(
            k for u, a in links if same_site(host_of(u), domain) and _PEOPLE.search(f"{u} {a}")
            and (k := safe_key(u)) is not None))[:3]
        for page, sid in await asyncio.gather(*(load_page(ctx, svc, u, company_domain=domain) for u in people_pages)):
            if page and sid and mentions_person(page.text, full_name, company_name, domain):
                pages.setdefault(page.key, (page, sid))

    if not svc.search.configured:
        notes.append("Web search is not configured; individual research limited to the company website.")
    elif settings.research_person_max_queries > 0:
        surname = full_name.split()[-1].lower() if full_name.split() else ""
        queries = [f'"{full_name}" "{company_name}"' if company_name else f'"{full_name}"',
                   f'"{full_name}" {prospect.title}' if prospect.title else None,
                   f'"{full_name}" {domain}' if domain else None]
        urls: list[str] = []
        for q in [q for q in queries if q][: settings.research_person_max_queries]:
            try:
                for r in await svc.search.search(q, max_results=8):
                    if (not is_blocked_domain(host_of(r.url)) and surname
                            and surname in f"{r.title} {r.snippet}".lower() and safe_key(r.url) not in pages):
                        urls.append(r.url)
            except SearchUnavailable as exc:
                if exc.retry_after and not pages:
                    raise RetryableJobError(f"search unavailable: {exc}", retry_after_seconds=exc.retry_after) from exc
                notes.append(f"Web search unavailable: {exc}")
                break
        remaining = max(0, settings.research_person_max_sources - len(pages))
        for page, sid in await asyncio.gather(*(load_page(ctx, svc, u, company_domain=domain)
                                                for u in list(dict.fromkeys(urls))[:remaining])):
            if page and sid:
                if mentions_person(page.text, full_name, company_name, domain):
                    pages.setdefault(page.key, (page, sid))
                else:
                    sources_total.labels("prospect", "namesake_rejected").inc()
    return list(pages.values())


# --- Extraction ---------------------------------------------------------------------------------

async def extract_company(svc: ResearchServices, page: Page, company: Company) -> list[Candidate]:
    keywords = [company.name, (company.normalized_domain or "").split(".")[0]]
    passages = select_passages(page.text, keywords=keywords)
    if not passages:
        return []
    result, _ = await svc.llm.complete(
        task="extract", system=company_prompt(name=company.name, domain=company.normalized_domain),
        user=source_block(title=page.title, url=page.url, published=page.published_at, passages=passages),
        output=CompanyFacts, max_tokens=1000)
    return _accept(result.facts, passages, page, subject="company")


async def extract_person(svc: ResearchServices, page: Page, prospect: Prospect,
                         company: Company | None) -> list[Candidate]:
    full_name = prospect.full_name or ""
    surname = full_name.split()[-1] if full_name.split() else full_name
    passages = select_passages(page.text, keywords=[full_name, prospect.title or "", company.name if company else ""],
                               must_mention=[surname])
    if not passages:
        return []
    result, _ = await svc.llm.complete(
        task="extract", system=person_prompt(full_name=full_name, company=company.name if company else None),
        user=source_block(title=page.title, url=page.url, published=page.published_at, passages=passages),
        output=PersonFacts, max_tokens=800)
    return _accept(result.facts, passages, page, subject="prospect")


def _accept(facts: Any, passages: list[str], page: Page, *, subject: str) -> list[Candidate]:
    seen_text = "\n".join(passages)
    out: list[Candidate] = []
    for f in facts:
        if not excerpt_supported(f.excerpt, seen_text):
            facts_total.labels(subject, "rejected_unverifiable_excerpt").inc()
            continue
        if subject == "prospect" and is_sensitive_person_fact(f.claim, f.excerpt):
            facts_total.labels(subject, "rejected_sensitive").inc()
            continue
        cap = 0.95 if f.kind == "verified_fact" else 0.6
        out.append(Candidate(claim=f.claim.strip(), category=f.category, kind=f.kind, excerpt=f.excerpt.strip(),
                             confidence=min(f.confidence, cap), relevance=f.relevance, source_key=page.key,
                             source_host=page.host.removeprefix("www."), published_at=page.published_at))
        facts_total.labels(subject, "accepted").inc()
    return out


async def extract_all(svc: ResearchServices, pages: list[tuple[Page, uuid.UUID]], extractor: Any, budget: Budget,
                      notes: list[str]) -> list[Candidate]:
    candidates: list[Candidate] = []
    retry_after: float | None = None
    failures = 0

    async def one(page: Page) -> None:
        nonlocal retry_after, failures
        if budget.exhausted:
            return
        try:
            candidates.extend(await extractor(page))
        except LLMUnavailable as exc:
            failures += 1
            retry_after = exc.retry_after if retry_after is None else min(retry_after, exc.retry_after or retry_after)

    await asyncio.gather(*(one(p) for p, _ in pages))
    if failures and not candidates:
        # Nothing extracted because every model is unavailable: retry later rather than report "no evidence".
        raise RetryableJobError(f"LLM unavailable for {failures} page(s)", retry_after_seconds=retry_after)
    if failures:
        notes.append(f"{failures} page(s) could not be analysed (LLM unavailable); results may be incomplete.")
    if budget.exhausted:
        notes.append("Research time budget reached; some pages were not analysed.")
    return candidates


# --- Handlers -----------------------------------------------------------------------------------

def _services(ctx: JobContext) -> ResearchServices:
    svc = ctx.services.get("research")
    if svc is None:
        raise PermanentJobError("research services are not configured in this worker")
    return svc  # type: ignore[no-any-return]


async def _guard_llm(svc: ResearchServices) -> None:
    if not svc.llm.configured("extract"):
        raise PermanentJobError("No LLM provider configured. Set GROQ_API_KEY (or run Ollama) and retry research.")


async def handle_company(ctx: JobContext) -> dict[str, Any]:
    svc = _services(ctx)
    company_id = uuid.UUID(ctx.job.payload["company_id"])
    company = await ctx.db(lambda s: s.get(Company, company_id))
    if company is None:
        raise PermanentJobError("company no longer exists")
    is_final = ctx.job.attempts >= ctx.job.max_attempts
    run_id = await ctx.db(lambda s: store.start_run(s, workspace_id=company.workspace_id,
                                                    subject=ResearchSubject.COMPANY, subject_id=company_id,
                                                    job_id=ctx.job.id, attempt=ctx.job.attempts,
                                                    config={"max_sources": svc.settings.research_company_max_sources}))

    def begin(s: Any) -> None:
        store.set_status(s, Company, company_id, ResearchStatus.RESEARCHING_COMPANY)
        store.mark_company_prospects(s, company_id, ResearchStatus.RESEARCHING_COMPANY)

    await ctx.db(begin)
    notes: list[str] = []
    try:
        await _guard_llm(svc)
        t0 = time.perf_counter()
        pages = await collect_company_pages(ctx, svc, company, notes)
        stage_seconds.labels("company", "collect").observe(time.perf_counter() - t0)
        await ctx.db(lambda s: store.update_run(s, run_id, stage="extracting"))
        t0 = time.perf_counter()
        candidates = await extract_all(svc, pages, lambda p: extract_company(svc, p, company),
                                       Budget(svc.settings.research_time_budget_seconds), notes)
        stage_seconds.labels("company", "extract").observe(time.perf_counter() - t0)
    except (RetryableJobError, PermanentJobError) as exc:
        message, final = str(exc), is_final or isinstance(exc, PermanentJobError)
        await ctx.db(lambda s: _record_failure(s, run_id, company_id, message, final=final))
        raise
    consolidated = consolidate(candidates)
    source_ids = {p.key: sid for p, sid in pages}

    def persist(s: Any) -> dict[str, Any]:
        store.set_status(s, Company, company_id, ResearchStatus.VERIFYING)
        saved = store.save_facts(s, workspace_id=company.workspace_id, run_id=run_id, company_id=company_id,
                                 prospect_id=None, candidates=consolidated, source_ids=source_ids)
        suff = company_sufficiency(store.fact_views(s, company_id=company_id))
        outcome = ResearchStatus.READY if suff.level == "sufficient" else (
            ResearchStatus.NEEDS_REVIEW if suff.level == "weak" else ResearchStatus.INSUFFICIENT_EVIDENCE)
        store.set_status(s, Company, company_id, outcome)
        s.execute(update(Company).where(Company.id == company_id).values(last_researched_at=datetime.now(UTC)))
        store.update_run(s, run_id, status=ResearchRunStatus.SUCCEEDED if suff.level != "none"
                         else ResearchRunStatus.INSUFFICIENT_EVIDENCE, stage="done", finished_at=datetime.now(UTC),
                         sufficiency={**suff.as_dict(), "notes": notes, "sources": len(pages)})
        waiting = store.prospects_awaiting_company(s, company_id)
        finalized = {str(pid): store.finalize_prospect(s, pid) for pid in waiting}
        return {"facts": saved, "sources": len(pages), "sufficiency": suff.level, "finalized": finalized}

    return await ctx.db(persist)  # type: ignore[no-any-return]


def _record_failure(s: Any, run_id: uuid.UUID, company_id: uuid.UUID | None, error: str, *, final: bool,
                    prospect_id: uuid.UUID | None = None) -> None:
    store.update_run(s, run_id, error={"message": error[:1000], "final": final},
                     status=ResearchRunStatus.FAILED if final else ResearchRunStatus.FAILED_RETRYABLE,
                     finished_at=datetime.now(UTC) if final else None)
    if not final:
        return
    if company_id is not None:
        store.set_status(s, Company, company_id, ResearchStatus.FAILED_RETRYABLE)
        for pid in store.prospects_awaiting_company(s, company_id):
            store.finalize_prospect(s, pid)
    if prospect_id is not None:
        store.finalize_prospect(s, prospect_id)


async def handle_prospect(ctx: JobContext) -> dict[str, Any]:
    svc = _services(ctx)
    prospect_id = uuid.UUID(ctx.job.payload["prospect_id"])
    prospect = await ctx.db(lambda s: s.get(Prospect, prospect_id))
    if prospect is None:
        raise PermanentJobError("prospect no longer exists")
    company = await ctx.db(lambda s: s.get(Company, prospect.company_id)) if prospect.company_id else None
    is_final = ctx.job.attempts >= ctx.job.max_attempts
    run_id = await ctx.db(lambda s: store.start_run(s, workspace_id=prospect.workspace_id,
                                                    subject=ResearchSubject.PROSPECT, subject_id=prospect_id,
                                                    job_id=ctx.job.id, attempt=ctx.job.attempts,
                                                    config={"max_sources": svc.settings.research_person_max_sources}))
    await ctx.db(lambda s: store.set_status(s, Prospect, prospect_id, ResearchStatus.RESEARCHING_INDIVIDUAL))
    notes: list[str] = []
    candidates: list[Candidate] = []
    pages: list[tuple[Page, uuid.UUID]] = []
    try:
        await _guard_llm(svc)
        if not prospect.full_name:
            notes.append("No name on file; individual research needs a name.")
        else:
            t0 = time.perf_counter()
            pages = await collect_person_pages(ctx, svc, prospect, company, notes)
            stage_seconds.labels("prospect", "collect").observe(time.perf_counter() - t0)
            await ctx.db(lambda s: store.update_run(s, run_id, stage="extracting"))
            candidates = await extract_all(svc, pages, lambda p: extract_person(svc, p, prospect, company),
                                           Budget(svc.settings.research_time_budget_seconds), notes)
    except (RetryableJobError, PermanentJobError) as exc:
        message, final = str(exc), is_final or isinstance(exc, PermanentJobError)
        await ctx.db(lambda s: _record_failure(s, run_id, None, message, prospect_id=prospect_id, final=final))
        raise
    consolidated = consolidate(candidates)
    source_ids = {p.key: sid for p, sid in pages}

    def persist(s: Any) -> dict[str, Any]:
        saved = store.save_facts(s, workspace_id=prospect.workspace_id, run_id=run_id, company_id=None,
                                 prospect_id=prospect_id, candidates=consolidated, source_ids=source_ids)
        suff = person_sufficiency(store.fact_views(s, prospect_id=prospect_id))
        store.update_run(s, run_id, status=ResearchRunStatus.SUCCEEDED if suff.level != "none"
                         else ResearchRunStatus.INSUFFICIENT_EVIDENCE, stage="done", finished_at=datetime.now(UTC),
                         sufficiency={**suff.as_dict(), "notes": notes, "sources": len(pages)})
        return {"facts": saved, "sources": len(pages), "sufficiency": suff.level,
                "outcome": store.finalize_prospect(s, prospect_id)}

    return await ctx.db(persist)  # type: ignore[no-any-return]


HANDLERS = {store.COMPANY_JOB: handle_company, store.PROSPECT_JOB: handle_prospect}
