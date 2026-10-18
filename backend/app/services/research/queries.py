"""Read-side queries for the research API (workspace-scoped)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import Conflict, NotFound
from app.domain.states import JOB_ACTIVE, JobState, ResearchRunStatus, ResearchStatus, ResearchSubject
from app.jobs import queue
from app.models import (
    Company,
    Job,
    Prospect,
    ResearchFact,
    ResearchFactEvidence,
    ResearchRun,
    ResearchSource,
    User,
)
from app.schemas.research import (
    CompanyResearchOut,
    EvidenceOut,
    FactOut,
    ProspectResearchOut,
    RecentRunOut,
    ResearchOverviewOut,
    RunDetailOut,
    RunOut,
    SourceOut,
)
from app.services.research import store

_WORKING = {ResearchStatus.RESEARCHING_COMPANY, ResearchStatus.RESEARCHING_INDIVIDUAL, ResearchStatus.VERIFYING}


def research_phase(db: Session, prospect: Prospect) -> tuple[str, int | None]:
    """What the queue is doing for this prospect right now, from job state rather than the stored status.

    Returns (phase, queue position). Phases: queued, researching, waiting_for_company, finishing, idle.
    A prospect that was never queued is idle, so the UI offers "Start research" instead of a spinner.
    """
    keys = [store.prospect_job_key(prospect.id)]
    if prospect.company_id:
        keys.append(store.company_job_key(prospect.company_id))
    active = {j.dedupe_key: j for j in db.scalars(select(Job).where(Job.dedupe_key.in_(keys),
                                                                     Job.state.in_(JOB_ACTIVE))).all()}
    mine, company_job = active.get(keys[0]), active.get(keys[1]) if len(keys) > 1 else None
    if mine is not None and mine.state is JobState.RUNNING:
        return "researching", None
    if mine is not None:
        ahead = db.scalar(select(func.count()).select_from(Job).where(
            Job.workspace_id == prospect.workspace_id, Job.kind.like("research.%"), Job.state == mine.state,
            Job.created_at < mine.created_at)) or 0
        return "queued", ahead
    if company_job is not None:
        return "waiting_for_company", None
    if prospect.research_status in _WORKING:
        return "finishing", None  # results saved; finalization (or the reconciler) completes it shortly
    return "idle", None


def facts_for(db: Session, *, company_id: uuid.UUID | None = None,
              prospect_id: uuid.UUID | None = None) -> list[FactOut]:
    now = datetime.now(UTC)
    subject = ResearchFact.company_id == company_id if company_id else ResearchFact.prospect_id == prospect_id
    facts = db.scalars(select(ResearchFact).where(
        subject, or_(ResearchFact.expires_at.is_(None), ResearchFact.expires_at > now))
        .order_by(ResearchFact.relevance.desc(), ResearchFact.confidence.desc())).all()
    if not facts:
        return []
    rows = db.execute(select(ResearchFactEvidence, ResearchSource)
                      .join(ResearchSource, ResearchSource.id == ResearchFactEvidence.source_id)
                      .where(ResearchFactEvidence.fact_id.in_([f.id for f in facts]))).all()
    evidence: dict[uuid.UUID, list[EvidenceOut]] = {}
    for ev, src in rows:
        item = EvidenceOut(source=SourceOut.model_validate(src), excerpt=ev.excerpt)
        evidence.setdefault(ev.fact_id, []).append(item)
    return [FactOut(id=f.id, category=f.category, claim=f.claim, kind=f.kind, verification_status=f.verification_status,
                    confidence=float(f.confidence), relevance=float(f.relevance), conflict_note=f.relevance_note,
                    published_at=f.published_at, expires_at=f.expires_at, evidence=evidence.get(f.id, []))
            for f in facts]


def _latest_run(db: Session, **subject: uuid.UUID) -> ResearchRun | None:
    (column, value), = subject.items()
    return db.scalar(select(ResearchRun).where(getattr(ResearchRun, column) == value)
                     .order_by(ResearchRun.created_at.desc()).limit(1))


def company_research(db: Session, company: Company) -> CompanyResearchOut:
    run = _latest_run(db, company_id=company.id)
    return CompanyResearchOut(id=company.id, name=company.name, normalized_domain=company.normalized_domain,
                              research_status=company.research_status, last_researched_at=company.last_researched_at,
                              latest_run=RunOut.model_validate(run) if run else None,
                              facts=facts_for(db, company_id=company.id))


def prospect_research(db: Session, user: User, prospect_id: uuid.UUID) -> ProspectResearchOut:
    prospect = db.scalar(select(Prospect).where(Prospect.id == prospect_id, Prospect.workspace_id == user.workspace_id))
    if prospect is None:
        raise NotFound("Prospect not found")
    run = _latest_run(db, prospect_id=prospect.id)
    company = db.get(Company, prospect.company_id) if prospect.company_id else None
    reason = (run.sufficiency or {}).get("reason") if run and run.sufficiency else None
    phase, position = research_phase(db, prospect)
    return ProspectResearchOut(
        prospect_id=prospect.id, research_status=prospect.research_status, outcome_reason=reason,
        individual_run=RunOut.model_validate(run) if run else None,
        individual_facts=facts_for(db, prospect_id=prospect.id),
        company=company_research(db, company) if company else None,
        in_progress=phase != "idle", phase=phase, queue_position=position)


def run_detail(db: Session, user: User, run_id: uuid.UUID) -> RunDetailOut:
    run = db.scalar(select(ResearchRun).where(ResearchRun.id == run_id, ResearchRun.workspace_id == user.workspace_id))
    if run is None:
        raise NotFound("Research run not found")
    job = db.get(Job, run.job_id) if run.job_id else None
    count = db.scalar(select(func.count()).select_from(ResearchFact).where(ResearchFact.research_run_id == run.id)) or 0
    return RunDetailOut(**RunOut.model_validate(run).model_dump(), job_state=job.state if job else None,
                        job_last_error=job.last_error if job else None,
                        next_attempt_at=job.run_at if job and job.state in JOB_ACTIVE else None, fact_count=count)


_RETRYABLE_RUNS = {ResearchRunStatus.FAILED, ResearchRunStatus.FAILED_RETRYABLE,
                   ResearchRunStatus.INSUFFICIENT_EVIDENCE}


def retry_run(db: Session, user: User, run_id: uuid.UUID, settings: Settings) -> store.ResearchRequest:
    run = db.scalar(select(ResearchRun).where(ResearchRun.id == run_id, ResearchRun.workspace_id == user.workspace_id))
    if run is None:
        raise NotFound("Research run not found")
    if run.status not in _RETRYABLE_RUNS:
        raise Conflict(f"A {run.status.value} run can't be retried.", code="run_not_retryable")
    if run.subject_type is ResearchSubject.PROSPECT and run.prospect_id:
        prospect = db.get(Prospect, run.prospect_id)
        assert prospect is not None
        return store.request_prospect_research(db, prospect=prospect, settings=settings)
    company = db.get(Company, run.company_id) if run.company_id else None
    if company is None:
        raise NotFound("Company not found")
    prospects = db.scalars(select(Prospect).where(Prospect.company_id == company.id)).all()
    result = store.ResearchRequest(None, None, False, False)
    for p in prospects:  # re-research the company and refresh everyone who depends on it
        result = store.request_prospect_research(db, prospect=p, settings=settings, force=True)
    if not prospects:
        job_id, _ = queue.enqueue(db, kind=store.COMPANY_JOB, payload={"company_id": str(company.id),
                                                                              "force": True},
                                        workspace_id=company.workspace_id, dedupe_key=store.company_job_key(company.id))
        store.set_status(db, Company, company.id, ResearchStatus.PENDING, by_human=True)
        result = store.ResearchRequest(None, job_id, False, False)
    return result


def retry_failed(db: Session, user: User, settings: Settings, *, limit: int = 500) -> int:
    prospects = db.scalars(select(Prospect).where(Prospect.workspace_id == user.workspace_id,
                                                  Prospect.research_status == ResearchStatus.FAILED_RETRYABLE)
                           .limit(limit)).all()
    for p in prospects:
        store.request_prospect_research(db, prospect=p, settings=settings)
    return len(prospects)


def overview(db: Session, user: User, settings: Settings) -> ResearchOverviewOut:
    ws = user.workspace_id
    by_status = {s.value: 0 for s in ResearchStatus}
    by_status.update({status.value: n for status, n in db.execute(
        select(Prospect.research_status, func.count()).where(Prospect.workspace_id == ws)
        .group_by(Prospect.research_status)).all()})
    jobs = {s: 0 for s in ("queued", "running", "succeeded", "failed", "dead", "cancelled")}
    jobs.update({state.value: n for state, n in db.execute(
        select(Job.state, func.count()).where(Job.workspace_id == ws, Job.kind.like("research.%"))
        .group_by(Job.state)).all()})
    runs = db.execute(
        select(ResearchRun, func.coalesce(Prospect.full_name, Prospect.email, Company.name))
        .outerjoin(Prospect, Prospect.id == ResearchRun.prospect_id)
        .outerjoin(Company, Company.id == ResearchRun.company_id)
        .where(ResearchRun.workspace_id == ws).order_by(ResearchRun.created_at.desc()).limit(20)).all()
    chain = [f"{p}:{m}" for p, m in Settings.parse_chain(settings.llm_extraction_chain)]
    providers = {"groq": bool(settings.groq_api_key.get_secret_value()),
                 "ollama": any(c.startswith("ollama:") for c in chain),
                 "tavily": bool(settings.tavily_api_key.get_secret_value()),
                 "brave": bool(settings.brave_api_key.get_secret_value())}
    order = [n.strip() for n in settings.search_provider_order.split(",") if n.strip()]
    return ResearchOverviewOut(
        prospects_by_status=by_status, jobs_by_state=jobs, providers=providers, llm_extraction_chain=chain,
        search_order=order, auto_start=settings.research_auto_start,
        recent_runs=[RecentRunOut(**RunOut.model_validate(r).model_dump(), subject_name=name) for r, name in runs])
