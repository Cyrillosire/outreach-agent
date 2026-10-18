from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse

from app.api.deps import AppSettings, CurrentUser, DbSession
from app.domain.states import ResearchStatus
from app.schemas.common import ERROR_RESPONSES, Envelope, PageMeta
from app.schemas.prospects import (
    ActionBrief,
    Blocker,
    CompanyBrief,
    CompanyDetailOut,
    DraftBrief,
    EnrollmentBrief,
    MessageBrief,
    ProspectCreateIn,
    ProspectDetailOut,
    ProspectListItem,
    ProspectOut,
    ProspectPatchIn,
    ProspectWriteResult,
    SourceOut,
    SuppressIn,
    SuppressionOut,
    SuppressionResult,
)
from app.services import idempotency
from app.services import prospects as prospect_service
from app.services.research.queries import facts_for
from app.services.research.store import request_prospect_research
from app.services.suppression import suppress_prospect

router = APIRouter(tags=["prospects"], responses=ERROR_RESPONSES)


@router.get("/prospects", response_model=Envelope[list[ProspectListItem]], summary="Search and filter prospects")
def list_prospects(
    user: CurrentUser,
    db: DbSession,
    q: Annotated[str | None, Query(max_length=200, description="Name, email, title, company or domain")] = None,
    research_status: Annotated[list[ResearchStatus], Query()] = [],  # noqa: B006 (FastAPI copies defaults)
    company_id: uuid.UUID | None = None,
    import_id: uuid.UUID | None = None,
    has_email: bool | None = None,
    suppressed: bool | None = None,
    sort: prospect_service.SortKey = "-created",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Envelope[list[ProspectListItem]]:
    filters = prospect_service.ProspectFilters(q=q, research_status=research_status, company_id=company_id,
                                               import_id=import_id, has_email=has_email, suppressed=suppressed,
                                               sort=sort)
    rows, total = prospect_service.list_prospects(db, user, filters, page=page, page_size=page_size)
    items = [ProspectListItem(
        id=p.id, full_name=p.full_name, email=p.email, title=p.title, company_id=p.company_id,
        company_name=company_name, company_domain=company_domain, research_status=p.research_status,
        outreach_status=prospect_service.outreach_status(is_suppressed, is_enrolled),
        has_email=p.email_normalized is not None, import_id=p.import_id, last_activity_at=p.last_activity_at,
        next_action_at=next_action_at, created_at=p.created_at,
    ) for p, company_name, company_domain, is_suppressed, is_enrolled, next_action_at in rows]
    return Envelope(data=items, meta=PageMeta(page=page, page_size=page_size, total=total).model_dump())


@router.post("/prospects", status_code=201, response_model=Envelope[ProspectWriteResult],
             summary="Create a prospect manually")
def create_prospect(
    body: ProspectCreateIn,
    user: CurrentUser,
    db: DbSession,
    settings: AppSettings,
    idempotency_key: Annotated[str | None, Header(alias=idempotency.HEADER)] = None,
) -> JSONResponse:
    def action() -> tuple[int, Envelope[ProspectWriteResult]]:
        prospect, issues = prospect_service.create_prospect(db, user, body.model_dump(exclude={"attributes"}),
                                                            body.attributes)
        if settings.research_auto_start:
            request_prospect_research(db, prospect=prospect, settings=settings)
            db.refresh(prospect)
        return 201, Envelope(data=ProspectWriteResult(prospect=ProspectOut.model_validate(prospect),
                                                      warnings=[i.as_dict() for i in issues]))

    return idempotency.run_idempotent(db, user=user, key=idempotency_key, scope="POST /prospects",
                                      fingerprint=idempotency.request_hash(body.model_dump_json()), action=action)


@router.get("/prospects/{prospect_id}", response_model=Envelope[ProspectDetailOut], summary="Prospect detail")
def get_prospect(prospect_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Envelope[ProspectDetailOut]:
    d = prospect_service.get_detail(db, user, prospect_id)
    p = d.prospect
    return Envelope(data=ProspectDetailOut(
        **ProspectOut.model_validate(p).model_dump(),
        company=CompanyBrief.model_validate(d.company) if d.company else None,
        provenance=SourceOut(source=p.source, import_id=p.import_id,
                             import_filename=d.source_import.original_filename if d.source_import else None,
                             row_number=p.source_row_number, raw=p.source_row),
        suppressions=[SuppressionOut.model_validate(s) for s in d.suppressions],
        blockers=[Blocker(**b) for b in d.blockers],
        research_fact_count=d.research_fact_count,
        drafts=[DraftBrief.model_validate(x) for x in d.drafts],
        messages=[MessageBrief.model_validate(x) for x in d.messages],
        scheduled_actions=[ActionBrief.model_validate(x) for x in d.scheduled_actions],
        enrollments=[EnrollmentBrief.model_validate(x) for x in d.enrollments],
    ))


@router.patch("/prospects/{prospect_id}", response_model=Envelope[ProspectWriteResult], summary="Edit a prospect")
def patch_prospect(prospect_id: uuid.UUID, body: ProspectPatchIn, user: CurrentUser,
                   db: DbSession) -> Envelope[ProspectWriteResult]:
    prospect, issues = prospect_service.update_prospect(db, user, prospect_id, body.model_dump(exclude_unset=True))
    db.commit()
    db.refresh(prospect)
    return Envelope(data=ProspectWriteResult(prospect=ProspectOut.model_validate(prospect),
                                             warnings=[i.as_dict() for i in issues]))


@router.post("/prospects/{prospect_id}/suppress", response_model=Envelope[SuppressionResult],
             summary="Suppress outreach to this prospect's email or domain (idempotent)")
def suppress(prospect_id: uuid.UUID, body: SuppressIn, user: CurrentUser, db: DbSession) -> JSONResponse:
    prospect = prospect_service.get_prospect(db, user, prospect_id)
    outcome = suppress_prospect(db, user, prospect, reason=body.reason, target=body.target, scope=body.scope,
                                campaign_id=body.campaign_id, note=body.note)
    db.commit()
    result: Envelope[SuppressionResult] = Envelope(data=SuppressionResult(
        suppression=SuppressionOut.model_validate(outcome.suppression), created=outcome.created,
        enrollments_stopped=outcome.enrollments_stopped, actions_cancelled=outcome.actions_cancelled))
    return JSONResponse(result.model_dump(mode="json"), status_code=201 if outcome.created else 200)


@router.get("/companies/{company_id}", response_model=Envelope[CompanyDetailOut], summary="Company detail",
            tags=["companies"])
def get_company(company_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Envelope[CompanyDetailOut]:
    company, prospects = prospect_service.get_company(db, user, company_id)
    return Envelope(data=CompanyDetailOut(
        **CompanyBrief.model_validate(company).model_dump(), description=company.description,
        location=company.location, created_at=company.created_at,
        prospects=[ProspectOut.model_validate(p) for p in prospects],
        facts=facts_for(db, company_id=company.id),
        last_researched_at=company.last_researched_at))
