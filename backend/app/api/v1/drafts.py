"""Draft generation, review queue, editing, approval."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession
from app.api.v1.mailboxes import MessageOut
from app.domain.states import DraftStatus, DraftType
from app.models import EmailMessage
from app.schemas.common import ERROR_RESPONSES, Envelope, PageMeta
from app.services.campaigns.service import brief_of
from app.services.drafting import service

router = APIRouter(tags=["drafts"], responses=ERROR_RESPONSES)


class GenerateIn(BaseModel):
    campaign_id: uuid.UUID
    force: bool = Field(default=False, description="Draft even if research isn't ready or the fit is weak")


class DraftListItem(BaseModel):
    id: uuid.UUID
    prospect_id: uuid.UUID
    prospect_name: str | None
    prospect_email: str | None
    company_name: str | None
    campaign_id: uuid.UUID
    campaign_name: str
    step_order: int
    draft_type: DraftType
    version: int
    subject: str
    status: DraftStatus
    overall_score: float | None
    fit: str | None
    has_errors: bool
    issue_count: int
    created_at: datetime


class DraftOut(BaseModel):
    id: uuid.UUID
    prospect_id: uuid.UUID
    prospect_name: str | None
    prospect_title: str | None
    company_name: str | None
    campaign_id: uuid.UUID
    campaign_name: str
    step_order: int
    total_steps: int
    draft_type: DraftType
    version: int
    is_current: bool
    subject: str
    body_text: str
    sections: dict[str, str]
    content_sha256: str
    plan: dict[str, Any]
    quality_scores: dict[str, Any]
    validation_issues: list[dict[str, Any]]
    status: DraftStatus
    approved_at: datetime | None
    rejected_reason: str | None
    generated_by: str
    generation_metadata: dict[str, Any]
    evidence: list[dict[str, Any]]
    versions: list[dict[str, Any]]
    max_words: int
    enrollment_state: str | None
    sending_account_id: uuid.UUID | None
    sends: list[dict[str, Any]]
    created_at: datetime


class EditIn(BaseModel):
    base_version: int = Field(ge=1)
    subject: str | None = Field(default=None, max_length=150)
    sections: dict[str, str] | None = None


class ApproveIn(BaseModel):
    content_sha256: str = Field(min_length=64, max_length=64)
    acknowledge_issues: bool = False


class RejectIn(BaseModel):
    reason: str = Field(default="", max_length=1000)


class RegenerateIn(BaseModel):
    sections: list[str] = Field(default_factory=list, max_length=5,
                                description="Sections to rewrite; empty rewrites the whole email")
    instruction: str | None = Field(default=None, max_length=500)


class BulkApproveIn(BaseModel):
    draft_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=service.MAX_BULK_APPROVE)]
    confirm_reviewed: bool = Field(description="Must be true: you have read each selected draft")


def _out(db: DbSession, user: Any, draft_id: uuid.UUID) -> DraftOut:
    d = service.get_detail(db, user, draft_id)
    draft = d.draft
    brief = brief_of(d.campaign)
    return DraftOut(
        id=draft.id, prospect_id=d.prospect.id, prospect_name=d.prospect.full_name, prospect_title=d.prospect.title,
        company_name=d.company.name if d.company else None, campaign_id=d.campaign.id, campaign_name=d.campaign.name,
        step_order=draft.step_order, total_steps=d.total_steps, draft_type=draft.draft_type, version=draft.version,
        is_current=draft.is_current, subject=draft.subject, body_text=draft.body_text, sections=draft.sections,
        content_sha256=draft.content_sha256, plan=draft.plan, quality_scores=draft.quality_scores,
        validation_issues=draft.validation_issues, status=draft.status, approved_at=draft.approved_at,
        rejected_reason=draft.rejected_reason, generated_by=draft.generated_by,
        generation_metadata={k: v for k, v in (draft.generation_metadata or {}).items() if k != "offering_refs"},
        evidence=d.evidence, versions=d.versions,
        max_words=brief.style.max_words if draft.step_order == 0 else max(50, int(brief.style.max_words * 0.7)),
        enrollment_state=d.enrollment.state.value if d.enrollment else None,
        sending_account_id=d.campaign.sending_account_id,
        sends=[MessageOut.of(m).model_dump(mode="json") for m in db.scalars(
            select(EmailMessage).where(EmailMessage.draft_id == draft.id).order_by(EmailMessage.created_at.desc()))],
        created_at=draft.created_at)


@router.post("/prospects/{prospect_id}/drafts/generate", status_code=202, response_model=Envelope[dict[str, Any]],
             summary="Generate a draft for a contact in a campaign (asynchronous)")
def generate(prospect_id: uuid.UUID, body: GenerateIn, user: CurrentUser, db: DbSession,
             settings: AppSettings) -> JSONResponse:
    result = service.generate_for_prospect(db, user, prospect_id, body.campaign_id, settings, force=body.force)
    db.commit()
    return JSONResponse(Envelope(data=result).model_dump(mode="json"), status_code=202)


@router.get("/drafts", response_model=Envelope[list[DraftListItem]], summary="Draft review queue")
def list_drafts(user: CurrentUser, db: DbSession, campaign_id: uuid.UUID | None = None,
                status: DraftStatus | None = None, flagged: bool | None = None, step: int | None = None,
                prospect_id: uuid.UUID | None = None, page: Annotated[int, Query(ge=1)] = 1,
                page_size: Annotated[int, Query(ge=1, le=200)] = 50) -> Envelope[list[DraftListItem]]:
    rows, total = service.list_drafts(db, user, page=page, page_size=page_size, campaign_id=campaign_id,
                                      status=status, flagged=flagged, step=step, prospect_id=prospect_id)
    items = [DraftListItem(
        id=d.id, prospect_id=d.prospect_id, prospect_name=pname, prospect_email=pemail, company_name=cname,
        campaign_id=d.campaign_id, campaign_name=campaign_name, step_order=d.step_order, draft_type=d.draft_type,
        version=d.version, subject=d.subject, status=d.status,
        overall_score=float(d.overall_score) if d.overall_score is not None else None,
        fit=d.fit.value if d.fit else None, has_errors=bool(err), issue_count=len(d.validation_issues or []),
        created_at=d.created_at) for d, pname, pemail, cname, campaign_name, err in rows]
    return Envelope(data=items, meta=PageMeta(page=page, page_size=page_size, total=total).model_dump())


@router.get("/drafts/{draft_id}", response_model=Envelope[DraftOut], summary="Draft with evidence and scores")
def get_draft(draft_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Envelope[DraftOut]:
    return Envelope(data=_out(db, user, draft_id))


@router.patch("/drafts/{draft_id}", response_model=Envelope[DraftOut],
              summary="Edit a draft (creates a new version and removes any approval)")
def edit(draft_id: uuid.UUID, body: EditIn, user: CurrentUser, db: DbSession) -> Envelope[DraftOut]:
    new = service.edit_draft(db, user, draft_id, base_version=body.base_version, subject=body.subject,
                             sections=body.sections)
    db.commit()
    return Envelope(data=_out(db, user, new.id))


@router.post("/drafts/{draft_id}/approve", response_model=Envelope[DraftOut], summary="Approve this exact version")
def approve(draft_id: uuid.UUID, body: ApproveIn, user: CurrentUser, db: DbSession,
            settings: AppSettings) -> Envelope[DraftOut]:
    service.approve(db, user, draft_id, content_sha256=body.content_sha256,
                    acknowledge_issues=body.acknowledge_issues, settings=settings)
    db.commit()
    return Envelope(data=_out(db, user, draft_id))


@router.post("/drafts/{draft_id}/reject", response_model=Envelope[DraftOut], summary="Reject")
def reject(draft_id: uuid.UUID, body: RejectIn, user: CurrentUser, db: DbSession) -> Envelope[DraftOut]:
    service.reject(db, user, draft_id, reason=body.reason)
    db.commit()
    return Envelope(data=_out(db, user, draft_id))


@router.post("/drafts/{draft_id}/regenerate", status_code=202, response_model=Envelope[dict[str, str]],
             summary="Regenerate the whole draft or selected sections (asynchronous)")
def regenerate(draft_id: uuid.UUID, body: RegenerateIn, user: CurrentUser, db: DbSession) -> JSONResponse:
    job_id = service.regenerate(db, user, draft_id, sections=body.sections, instruction=body.instruction)
    db.commit()
    return JSONResponse(Envelope(data={"job_id": str(job_id)}).model_dump(mode="json"), status_code=202)


@router.post("/drafts/bulk-approve", response_model=Envelope[dict[str, Any]],
             summary="Approve several reviewed drafts (drafts with issues are skipped)")
def bulk_approve(body: BulkApproveIn, user: CurrentUser, db: DbSession,
                 settings: AppSettings) -> Envelope[dict[str, Any]]:
    from app.core.errors import AppError
    if not body.confirm_reviewed:
        raise AppError("Confirm that you've reviewed each selected draft.", code="confirmation_required")
    result = service.bulk_approve(db, user, body.draft_ids, settings)
    db.commit()
    return Envelope(data={"approved": [str(i) for i in result.approved], "skipped": result.skipped})
