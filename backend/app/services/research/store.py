"""Research persistence and orchestration (synchronous DB code; callers own the transaction)."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import get_logger
from app.domain.states import (
    JOB_ACTIVE,
    RESEARCH,
    ExtractionStatus,
    FactKind,
    FactVerification,
    ResearchRunStatus,
    ResearchStatus,
    ResearchSubject,
    SourceType,
)
from app.jobs import queue
from app.models import Company, Job, Prospect, ResearchFact, ResearchFactEvidence, ResearchRun, ResearchSource
from app.services.research.evidence import (
    Candidate,
    FactView,
    Sufficiency,
    company_sufficiency,
    expires_at,
    person_sufficiency,
    prospect_outcome,
)
from app.services.research.linkedin import SOURCE_TYPES, LinkedInKind, clean_capture

log = get_logger(__name__)
COMPANY_JOB = "research.company"
PROSPECT_JOB = "research.prospect"
_IN_PROGRESS = {ResearchStatus.PENDING, ResearchStatus.RESEARCHING_COMPANY, ResearchStatus.RESEARCHING_INDIVIDUAL,
                ResearchStatus.VERIFYING}


def company_job_key(company_id: uuid.UUID) -> str:
    return f"research:company:{company_id}"


def prospect_job_key(prospect_id: uuid.UUID) -> str:
    return f"research:prospect:{prospect_id}"


# --- Status transitions (always via the RESEARCH state machine) ----------------------------------

def set_status(db: Session, model: Any, row_id: uuid.UUID, target: ResearchStatus, *, by_human: bool = False) -> bool:
    current = db.execute(select(model.research_status).where(model.id == row_id).with_for_update()).scalar_one_or_none()
    if current is None:
        return False
    if current == target:
        return True
    if not RESEARCH.can_transition(current, target, by_human=by_human):
        log.info("research_status_transition_skipped", model=model.__tablename__, current=current.value,
                 target=target.value)
        return False
    db.execute(update(model).where(model.id == row_id).values(research_status=target))
    return True


def set_path(db: Session, model: Any, row_id: uuid.UUID, *targets: ResearchStatus) -> bool:
    return all(set_status(db, model, row_id, t) for t in targets)


# --- Requesting research ------------------------------------------------------------------------

@dataclass
class ResearchRequest:
    prospect_job_id: uuid.UUID | None
    company_job_id: uuid.UUID | None
    company_reused: bool
    already_running: bool


def company_is_fresh(company: Company, settings: Settings) -> bool:
    cutoff = datetime.now(UTC) - timedelta(days=settings.research_company_freshness_days)
    return (company.research_status == ResearchStatus.READY and company.last_researched_at is not None
            and company.last_researched_at > cutoff)


def request_prospect_research(db: Session, *, prospect: Prospect, settings: Settings, force: bool = False,
                              priority: int = 100) -> ResearchRequest:
    """Enqueue individual research and (unless fresh or already queued) company research, transactionally."""
    active = db.scalar(select(Job.id).where(Job.dedupe_key == prospect_job_key(prospect.id), Job.state.in_(JOB_ACTIVE)))
    if active and prospect.research_status in _IN_PROGRESS:
        return ResearchRequest(active, None, False, True)

    company = db.get(Company, prospect.company_id) if prospect.company_id else None
    company_job_id = None
    reused = False
    if company is not None:
        if company_is_fresh(company, settings) and not force:
            reused = True
        elif company.normalized_domain or company.name:
            company_job_id, _ = queue.enqueue(
                db, kind=COMPANY_JOB, payload={"company_id": str(company.id), "force": force},
                # Slightly higher priority so the company's pages are usually cached before colleagues need them.
                workspace_id=company.workspace_id, dedupe_key=company_job_key(company.id), priority=priority - 10,
                max_attempts=settings.job_max_attempts)
            if company.research_status not in _IN_PROGRESS:
                set_status(db, Company, company.id, ResearchStatus.PENDING, by_human=True)
    prospect_job_id, _ = queue.enqueue(
        db, kind=PROSPECT_JOB, payload={"prospect_id": str(prospect.id)}, workspace_id=prospect.workspace_id,
        dedupe_key=prospect_job_key(prospect.id), priority=priority, max_attempts=settings.job_max_attempts)
    set_status(db, Prospect, prospect.id, ResearchStatus.PENDING, by_human=True)
    return ResearchRequest(prospect_job_id, company_job_id, reused, False)


# --- Runs ---------------------------------------------------------------------------------------

def start_run(db: Session, *, workspace_id: uuid.UUID, subject: ResearchSubject, subject_id: uuid.UUID,
              job_id: uuid.UUID, attempt: int, config: dict[str, Any]) -> uuid.UUID:
    run = db.scalar(select(ResearchRun).where(ResearchRun.job_id == job_id))
    if run is None:
        run = ResearchRun(workspace_id=workspace_id, subject_type=subject, job_id=job_id, config=config,
                          company_id=subject_id if subject is ResearchSubject.COMPANY else None,
                          prospect_id=subject_id if subject is ResearchSubject.PROSPECT else None)
        db.add(run)
    run.status = ResearchRunStatus.RUNNING
    run.stage = "collecting"
    run.attempt_count = attempt
    run.started_at = run.started_at or datetime.now(UTC)
    run.error = None
    db.flush()
    return run.id


def update_run(db: Session, run_id: uuid.UUID, **values: Any) -> None:
    db.execute(update(ResearchRun).where(ResearchRun.id == run_id).values(**values))


# --- Sources ------------------------------------------------------------------------------------

def cached_source(db: Session, workspace_id: uuid.UUID, url_normalized: str,
                  max_age_hours: int) -> ResearchSource | None:
    if max_age_hours <= 0:
        return None
    return db.scalar(select(ResearchSource).where(
        ResearchSource.workspace_id == workspace_id, ResearchSource.url_normalized == url_normalized,
        ResearchSource.extraction_status == ExtractionStatus.EXTRACTED,
        ResearchSource.accessed_at > datetime.now(UTC) - timedelta(hours=max_age_hours),
    ).order_by(ResearchSource.accessed_at.desc()).limit(1))


def classify_source(url: str, host: str, company_domain: str | None) -> SourceType:
    path = url.lower()
    if host.endswith((".gov", ".gov.in", ".nic.in", ".gov.uk", ".gc.ca", ".europa.eu")):
        return SourceType.GOVERNMENT
    official = bool(company_domain) and (host == company_domain or host.endswith("." + (company_domain or "")))
    if official:
        if any(p in path for p in ("/press", "/news", "/media", "/newsroom")):
            return SourceType.PRESS_RELEASE
        if "annual-report" in path or "annual_report" in path:
            return SourceType.ANNUAL_REPORT
        return SourceType.OFFICIAL_WEBSITE
    if any(p in path for p in ("/news/", "/article", "/story/", "/business/")):
        return SourceType.NEWS
    return SourceType.OTHER


def save_source(db: Session, *, workspace_id: uuid.UUID, url: str, url_normalized: str, title: str | None,
                text: str, published_at: datetime | None, http_status: int | None, source_type: SourceType,
                publisher: str | None, captured_for_prospect_id: uuid.UUID | None = None) -> uuid.UUID:
    digest = hashlib.sha256(text.encode()).hexdigest()
    now = datetime.now(UTC)
    inserted = db.execute(pg_insert(ResearchSource).values(
        id=uuid.uuid4(), workspace_id=workspace_id, url=url[:2048], url_normalized=url_normalized[:2048],
        title=(title or "")[:1000] or None, publisher=publisher, source_type=source_type, published_at=published_at,
        accessed_at=now, http_status=http_status, extraction_status=ExtractionStatus.EXTRACTED,
        content_sha256=digest, extracted_text=text, captured_for_prospect_id=captured_for_prospect_id,
    ).on_conflict_do_nothing(index_elements=["workspace_id", "url_normalized", "content_sha256"])
        .returning(ResearchSource.id)).scalar_one_or_none()
    if inserted is not None:
        return inserted
    existing = db.scalar(select(ResearchSource).where(
        ResearchSource.workspace_id == workspace_id, ResearchSource.url_normalized == url_normalized,
        ResearchSource.content_sha256 == digest))
    assert existing is not None
    existing.accessed_at = now  # same content re-confirmed today
    return existing.id


def captured_sources(db: Session, prospect_id: uuid.UUID) -> list[ResearchSource]:
    """Text users captured for this prospect (LinkedIn profile, posts, activity), newest first."""
    return list(db.scalars(select(ResearchSource).where(ResearchSource.captured_for_prospect_id == prospect_id)
                           .order_by(ResearchSource.accessed_at.desc())).all())


def save_capture(db: Session, *, prospect: Prospect, kind: LinkedInKind, text: str, url: str | None) -> uuid.UUID:
    """Store pasted LinkedIn text as a source owned by this prospect. The key is per prospect and kind, so two
    contacts never share a capture and re-pasting identical text is a no-op."""
    cleaned = clean_capture(text)
    page_url = url or prospect.linkedin_url or "https://www.linkedin.com/"
    titles = {"profile": "LinkedIn profile", "post": "LinkedIn posts", "activity": "LinkedIn activity"}
    return save_source(db, workspace_id=prospect.workspace_id, url=page_url,
                       url_normalized=f"capture:{prospect.id}:{kind}", title=f"{titles[kind]} (captured)",
                       text=cleaned, published_at=None, http_status=None, source_type=SOURCE_TYPES[kind],
                       publisher="linkedin.com (captured by user)", captured_for_prospect_id=prospect.id)


def delete_capture(db: Session, *, prospect: Prospect, source_id: uuid.UUID) -> int:
    """Remove a capture and every fact that rested only on it (drafts citing those facts are superseded)."""
    src = db.scalar(select(ResearchSource).where(ResearchSource.id == source_id,
                                                 ResearchSource.captured_for_prospect_id == prospect.id))
    if src is None:
        return 0
    fact_ids = list(db.scalars(select(ResearchFactEvidence.fact_id).where(ResearchFactEvidence.source_id == src.id)))
    db.delete(src)
    db.flush()
    orphaned = [fid for fid in fact_ids if not db.scalar(
        select(ResearchFactEvidence.id).where(ResearchFactEvidence.fact_id == fid).limit(1))]
    if orphaned:
        from app.services.records import supersede_drafts_citing
        supersede_drafts_citing(db, orphaned, "A LinkedIn capture this draft relied on was removed.")
        db.execute(update(ResearchFact).where(ResearchFact.id.in_(orphaned)).values(expires_at=datetime.now(UTC)))
    return len(orphaned)


def sources_mentioning(db: Session, workspace_id: uuid.UUID, *, host_suffix: str, phrase: str,
                       limit: int = 5) -> list[ResearchSource]:
    like = "%" + phrase.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return list(db.scalars(select(ResearchSource).where(
        ResearchSource.workspace_id == workspace_id,
        ResearchSource.extraction_status == ExtractionStatus.EXTRACTED,
        or_(ResearchSource.url_normalized.like(f"%://{host_suffix}/%"),
            ResearchSource.url_normalized.like(f"%.{host_suffix}/%")),
        ResearchSource.extracted_text.ilike(like, escape="\\"),
    ).order_by(ResearchSource.accessed_at.desc()).limit(limit)).all())


# --- Facts --------------------------------------------------------------------------------------

def save_facts(db: Session, *, workspace_id: uuid.UUID, run_id: uuid.UUID, company_id: uuid.UUID | None,
               prospect_id: uuid.UUID | None, candidates: list[Candidate], source_ids: dict[str, uuid.UUID]) -> int:
    """Persist consolidated facts and expire the subject's facts from earlier runs (kept for audit/drafts)."""
    now = datetime.now(UTC)
    subject = ResearchFact.company_id == company_id if company_id else ResearchFact.prospect_id == prospect_id
    db.execute(update(ResearchFact).where(subject, ResearchFact.research_run_id != run_id,
                                          or_(ResearchFact.expires_at.is_(None), ResearchFact.expires_at > now))
               .values(expires_at=now))
    saved = 0
    for c in candidates:
        if c.source_key not in source_ids:
            continue
        kind = FactKind.CONFLICTING if c.conflict_with else FactKind(c.kind)
        verification = (FactVerification.CONTRADICTED if c.conflict_with else
                        FactVerification.CORROBORATED if c.corroborated else
                        FactVerification.SINGLE_SOURCE if c.kind == "verified_fact" else FactVerification.UNVERIFIED)
        fact = ResearchFact(
            workspace_id=workspace_id, research_run_id=run_id, company_id=company_id, prospect_id=prospect_id,
            category=c.category, claim=c.claim, kind=kind, verification_status=verification,
            confidence=round(c.confidence, 3), relevance=round(c.relevance, 3),
            relevance_note=f"Conflicts with: {c.conflict_with}"[:1000] if c.conflict_with else None,
            published_at=c.published_at, expires_at=expires_at(c.category, now))
        db.add(fact)
        db.flush()
        evidence = [(c.source_key, c.excerpt), *c.extra_evidence]
        for key, excerpt in evidence:
            if key in source_ids:
                db.execute(pg_insert(ResearchFactEvidence).values(
                    id=uuid.uuid4(), fact_id=fact.id, source_id=source_ids[key], excerpt=excerpt[:2000],
                    supports=True, accessed_at=now).on_conflict_do_nothing())
        saved += 1
    return saved


def fact_views(db: Session, *, company_id: uuid.UUID | None = None, prospect_id: uuid.UUID | None = None,
               company_domain: str | None = None) -> list[FactView]:
    now = datetime.now(UTC)
    subject = ResearchFact.company_id == company_id if company_id else ResearchFact.prospect_id == prospect_id
    rows = db.execute(
        select(ResearchFact.category, ResearchFact.kind, ResearchFact.verification_status, ResearchFact.published_at,
               func.bool_or(ResearchSource.source_type.in_([SourceType.OFFICIAL_WEBSITE, SourceType.PRESS_RELEASE,
                                                             SourceType.ANNUAL_REPORT])))
        .join(ResearchFactEvidence, ResearchFactEvidence.fact_id == ResearchFact.id)
        .join(ResearchSource, ResearchSource.id == ResearchFactEvidence.source_id)
        .where(subject, or_(ResearchFact.expires_at.is_(None), ResearchFact.expires_at > now))
        .group_by(ResearchFact.id)
    ).all()
    return [FactView(cat, kind.value, ver.value, bool(official), pub) for cat, kind, ver, pub, official in rows]


# --- Finalization ---------------------------------------------------------------------------------

def finalize_prospect(db: Session, prospect_id: uuid.UUID) -> str | None:
    """Decide the prospect's research outcome once its individual run and its company research are both done.

    Safe to call repeatedly and concurrently; returns the outcome or None while still waiting.

    Concurrency: both research jobs write their own result (run status / company status) and THEN call this,
    which first locks the prospect row. The second caller therefore always sees the first caller's committed
    result (READ COMMITTED takes a fresh snapshot per statement). Decisions use only state written in the same
    transaction as the facts -- never the queue's job state, which is committed separately afterwards.
    """
    prospect = db.scalar(select(Prospect).where(Prospect.id == prospect_id).with_for_update())
    if prospect is None:
        return None
    person_run = db.scalar(select(ResearchRun).where(ResearchRun.prospect_id == prospect_id)
                           .order_by(ResearchRun.created_at.desc()).limit(1))
    if person_run is None or person_run.status in (ResearchRunStatus.QUEUED, ResearchRunStatus.RUNNING):
        return None
    company = db.scalar(select(Company).where(Company.id == prospect.company_id)) if prospect.company_id else None
    if company is not None and company.research_status in _IN_PROGRESS and db.scalar(
            select(Job.id).where(Job.dedupe_key == company_job_key(company.id), Job.state.in_(JOB_ACTIVE))):
        # Wait for the company job. It writes its final status in the same transaction as its facts (before its job
        # row is marked done), so a company still "in progress" with NO active job was abandoned (cancelled, or a
        # status left over from an older run): decide from the company facts that exist instead of waiting forever.
        return None

    if person_run.status is ResearchRunStatus.FAILED or (
            company is not None and company.research_status is ResearchStatus.FAILED_RETRYABLE):
        set_status(db, Prospect, prospect.id, ResearchStatus.FAILED_RETRYABLE)
        return ResearchStatus.FAILED_RETRYABLE.value

    company_suff: Sufficiency | None = None
    if company is not None:
        company_suff = company_sufficiency(fact_views(db, company_id=company.id))
    person_suff = person_sufficiency(fact_views(db, prospect_id=prospect.id))
    outcome, reason = prospect_outcome(company_suff, person_suff)
    if prospect.research_status not in (ResearchStatus.RESEARCHING_COMPANY, ResearchStatus.RESEARCHING_INDIVIDUAL,
                                        ResearchStatus.VERIFYING):
        set_status(db, Prospect, prospect.id, ResearchStatus.RESEARCHING_INDIVIDUAL)
    set_path(db, Prospect, prospect.id, ResearchStatus.VERIFYING, ResearchStatus(outcome))
    person_run.sufficiency = {"outcome": outcome, "reason": reason, "person": person_suff.as_dict(),
                              "company": company_suff.as_dict() if company_suff else None}
    # Research is done: let any campaign enrollments that were waiting on it move on to drafting.
    from app.core.config import get_settings
    from app.services.campaigns.service import on_research_finalized
    db.flush()
    on_research_finalized(db, prospect.id, get_settings())
    return outcome


def reconcile_stuck(db: Session, *, limit: int = 200) -> int:
    """Finalize prospects still marked in-progress although no research job is active for them or their company.

    Recovers from crashes between saving results and finalizing, and from any missed finalization.
    """
    active_keys = select(Job.dedupe_key).where(Job.state.in_(JOB_ACTIVE), Job.kind.in_([COMPANY_JOB, PROSPECT_JOB]))
    candidates = db.scalars(select(Prospect.id).where(
        Prospect.research_status.in_(_IN_PROGRESS),
        func.concat("research:prospect:", Prospect.id).not_in(active_keys),
        or_(Prospect.company_id.is_(None), func.concat("research:company:", Prospect.company_id).not_in(active_keys)),
    ).limit(limit)).all()
    finalized = 0
    for pid in candidates:
        if finalize_prospect(db, pid) is not None:
            finalized += 1
    return finalized


def prospects_awaiting_company(db: Session, company_id: uuid.UUID) -> list[uuid.UUID]:
    return list(db.scalars(select(Prospect.id).where(
        Prospect.company_id == company_id, Prospect.research_status.in_(_IN_PROGRESS))).all())


def mark_company_prospects(db: Session, company_id: uuid.UUID, target: ResearchStatus) -> None:
    for pid in db.scalars(select(Prospect.id).where(and_(Prospect.company_id == company_id,
                                                         Prospect.research_status == ResearchStatus.PENDING))).all():
        set_status(db, Prospect, pid, target)
