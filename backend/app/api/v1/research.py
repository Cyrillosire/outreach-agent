from __future__ import annotations

import uuid

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession
from app.core.errors import NotFound
from app.domain.states import ActorType, AuditOutcome, ResearchStatus
from app.models import Prospect
from app.schemas.common import ERROR_RESPONSES, Envelope
from app.schemas.research import (
    ProspectResearchOut,
    ResearchOverviewOut,
    ResearchRequestIn,
    ResearchRequestOut,
    RunDetailOut,
)
from app.services import audit
from app.services.research import queries, store

router = APIRouter(tags=["research"], responses=ERROR_RESPONSES)


def _accepted(prospect: Prospect, req: store.ResearchRequest) -> JSONResponse:
    body: Envelope[ResearchRequestOut] = Envelope(data=ResearchRequestOut(
        research_status=prospect.research_status, prospect_job_id=req.prospect_job_id,
        company_job_id=req.company_job_id, company_research_reused=req.company_reused,
        already_running=req.already_running))
    return JSONResponse(body.model_dump(mode="json"), status_code=202)


@router.post("/prospects/{prospect_id}/research", response_model=Envelope[ResearchRequestOut], status_code=202,
             summary="Start or refresh research for a prospect (asynchronous)")
def start_research(prospect_id: uuid.UUID, body: ResearchRequestIn, user: CurrentUser, db: DbSession,
                   settings: AppSettings) -> JSONResponse:
    """Returns immediately with job ids; poll GET /prospects/{id}/research for progress. Idempotent while running."""
    prospect = db.scalar(select(Prospect).where(Prospect.id == prospect_id, Prospect.workspace_id == user.workspace_id))
    if prospect is None:
        raise NotFound("Prospect not found")
    req = store.request_prospect_research(db, prospect=prospect, settings=settings, force=body.force)
    audit.record(db, action="research.request", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="prospect", entity_id=prospect.id,
                 metadata={"force": body.force, "already_running": req.already_running})
    db.commit()
    db.refresh(prospect)
    return _accepted(prospect, req)


@router.get("/prospects/{prospect_id}/research", response_model=Envelope[ProspectResearchOut],
            summary="Research status, evidence-backed facts, and sufficiency for a prospect and their company")
def get_research(prospect_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Envelope[ProspectResearchOut]:
    return Envelope(data=queries.prospect_research(db, user, prospect_id))


@router.get("/research-runs/{run_id}", response_model=Envelope[RunDetailOut], summary="Research run detail")
def get_run(run_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Envelope[RunDetailOut]:
    return Envelope(data=queries.run_detail(db, user, run_id))


@router.post("/research-runs/{run_id}/retry", response_model=Envelope[ResearchRequestOut], status_code=202,
             summary="Retry a failed or insufficient research run")
def retry_run(run_id: uuid.UUID, user: CurrentUser, db: DbSession, settings: AppSettings) -> JSONResponse:
    req = queries.retry_run(db, user, run_id, settings)
    run = queries.run_detail(db, user, run_id)
    audit.record(db, action="research.retry", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="research_run", entity_id=run_id)
    db.commit()
    prospect = db.get(Prospect, run.prospect_id) if run.prospect_id else None
    body: Envelope[ResearchRequestOut] = Envelope(data=ResearchRequestOut(
        research_status=prospect.research_status if prospect else ResearchStatus.PENDING,
        prospect_job_id=req.prospect_job_id, company_job_id=req.company_job_id,
        company_research_reused=req.company_reused, already_running=req.already_running))
    return JSONResponse(body.model_dump(mode="json"), status_code=202)


@router.get("/research/overview", response_model=Envelope[ResearchOverviewOut], summary="Research workspace summary")
def research_overview(user: CurrentUser, db: DbSession, settings: AppSettings) -> Envelope[ResearchOverviewOut]:
    return Envelope(data=queries.overview(db, user, settings))


@router.post("/research/retry-failed", response_model=Envelope[dict[str, int]], status_code=202,
             summary="Re-queue research for every prospect whose research failed")
def retry_failed(user: CurrentUser, db: DbSession, settings: AppSettings) -> JSONResponse:
    count = queries.retry_failed(db, user, settings)
    audit.record(db, action="research.retry_failed", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, metadata={"count": count})
    db.commit()
    return JSONResponse(Envelope(data={"requeued": count}).model_dump(mode="json"), status_code=202)
