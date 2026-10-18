from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession
from app.core.errors import AppError, NotFound
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
from app.services.research import linkedin, queries, store

router = APIRouter(tags=["research"], responses=ERROR_RESPONSES)


class LinkedInCaptureIn(BaseModel):
    kind: Literal["profile", "post", "activity"] = Field(
        description="profile = their profile page; post = posts/articles they wrote; activity = their activity feed "
                    "(posts, comments, reposts, reactions)")
    text: str = Field(min_length=40, max_length=200_000, description="Page text copied from your own browser")
    url: str | None = Field(default=None, max_length=2048, description="The LinkedIn page the text came from")


class LinkedInCaptureOut(BaseModel):
    id: uuid.UUID
    kind: str
    url: str
    characters: int
    captured_at: datetime


def _prospect(db: DbSession, user: Any, prospect_id: uuid.UUID) -> Prospect:
    prospect = db.scalar(select(Prospect).where(Prospect.id == prospect_id, Prospect.workspace_id == user.workspace_id))
    if prospect is None:
        raise NotFound("Prospect not found")
    return prospect


@router.get("/prospects/{prospect_id}/linkedin-captures", response_model=Envelope[list[LinkedInCaptureOut]],
            summary="LinkedIn text captured for this prospect")
def list_captures(prospect_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Envelope[list[LinkedInCaptureOut]]:
    prospect = _prospect(db, user, prospect_id)
    return Envelope(data=[LinkedInCaptureOut(id=s.id, kind=s.source_type.value.removeprefix("linkedin_"), url=s.url,
                                             characters=len(s.extracted_text or ""), captured_at=s.accessed_at)
                          for s in store.captured_sources(db, prospect.id)])


@router.post("/prospects/{prospect_id}/linkedin-captures", response_model=Envelope[ResearchRequestOut],
             status_code=202, summary="Add LinkedIn text you copied (profile, posts, or activity) and re-research")
def add_capture(prospect_id: uuid.UUID, body: LinkedInCaptureIn, user: CurrentUser, db: DbSession,
                settings: AppSettings) -> JSONResponse:
    """The text is stored as first-party evidence about this person and research is refreshed to extract from it.
    If research is already running, refresh it once it finishes to include this capture."""
    prospect = _prospect(db, user, prospect_id)
    if body.url and linkedin.profile_slug(body.url) is None and "linkedin.com/" not in body.url.lower():
        raise AppError("The URL should be a linkedin.com page.", code="invalid_linkedin_url")
    source_id = store.save_capture(db, prospect=prospect, kind=body.kind, text=body.text, url=body.url)
    if not prospect.linkedin_url and body.url and linkedin.profile_slug(body.url):
        prospect.linkedin_url = body.url
    req = store.request_prospect_research(db, prospect=prospect, settings=settings, force=True)
    audit.record(db, action="research.linkedin_capture", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="prospect", entity_id=prospect.id,
                 metadata={"kind": body.kind, "source_id": str(source_id), "characters": len(body.text)})
    db.commit()
    db.refresh(prospect)
    return _accepted(prospect, req)


@router.delete("/prospects/{prospect_id}/linkedin-captures/{source_id}", response_model=Envelope[dict[str, int]],
               summary="Remove a capture and the facts that relied only on it")
def delete_capture(prospect_id: uuid.UUID, source_id: uuid.UUID, user: CurrentUser,
                   db: DbSession) -> Envelope[dict[str, int]]:
    prospect = _prospect(db, user, prospect_id)
    if source_id not in {s.id for s in store.captured_sources(db, prospect.id)}:
        raise NotFound("Capture not found")
    removed = store.delete_capture(db, prospect=prospect, source_id=source_id)
    audit.record(db, action="research.linkedin_capture_deleted", outcome=AuditOutcome.SUCCESS,
                 actor_type=ActorType.USER, actor_id=user.id, workspace_id=user.workspace_id, entity_type="prospect",
                 entity_id=prospect.id, metadata={"source_id": str(source_id), "facts_removed": removed})
    db.commit()
    return Envelope(data={"facts_removed": removed})


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
