"""Offerings, campaigns (briefs, readiness, enrollment, status) and sequences."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import AppSettings, CurrentUser, DbSession
from app.domain.states import CampaignStatus, EnrollmentState, OfferingType, OutreachMotion, ResearchStatus
from app.models import Campaign, CampaignProspect, Company, EmailDraft, Offering, Prospect, Sequence, SequenceStep
from app.schemas.common import ERROR_RESPONSES, Envelope, ORMModel, PageMeta
from app.services.campaigns import service
from app.services.campaigns.brief import CampaignBrief, OfferingDetails, ReadinessItem
from app.services.campaigns.presets import PLAYBOOKS

router = APIRouter(tags=["campaigns"], responses=ERROR_RESPONSES)


# --- Schemas --------------------------------------------------------------------------------------

class OfferingIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    offering_type: OfferingType = OfferingType.PRODUCT
    category: str | None = Field(default=None, max_length=200)
    summary: str = Field(min_length=10, max_length=4000)
    details: OfferingDetails = Field(default_factory=OfferingDetails)

    model_config = {"json_schema_extra": {"examples": [{
        "name": "Penthus Clinical Documentation", "offering_type": "product",
        "category": "Clinical documentation / ambient AI scribe",
        "summary": "Ambient AI that drafts clinical notes from doctor-patient conversations inside the HIS.",
        "details": {"problems_solved": ["Doctors spend hours on documentation"],
                    "capabilities": ["Drafts structured notes in under a minute"],
                    "proof_points": [{"claim": "Cut documentation time by 40% in a 3-hospital pilot (2025)",
                                      "kind": "metric"}],
                    "forbidden_claims": ["replaces doctors"]}}]}}


class OfferingPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    offering_type: OfferingType | None = None
    category: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, min_length=10, max_length=4000)
    details: OfferingDetails | None = None
    archived: bool | None = None


class OfferingOut(ORMModel):
    id: uuid.UUID
    name: str
    offering_type: OfferingType
    category: str | None
    summary: str
    details: OfferingDetails
    archived: bool
    created_at: datetime
    updated_at: datetime


class PlaybookOut(BaseModel):
    motion: OutreachMotion
    label: str
    description: str
    guidance: str
    default_brief: CampaignBrief
    followup_days: list[int]


class CampaignCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    motion: OutreachMotion
    offering_id: uuid.UUID | None = None


class CampaignPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    motion: OutreachMotion | None = None
    offering_id: uuid.UUID | None = None
    brief: CampaignBrief | None = None
    timezone: str | None = Field(default=None, max_length=64)


class CampaignOut(BaseModel):
    id: uuid.UUID
    name: str
    status: CampaignStatus
    motion: OutreachMotion
    offering_id: uuid.UUID | None
    offering_name: str | None
    brief: CampaignBrief
    brief_version: int
    timezone: str
    step_days: list[int]
    readiness: list[ReadinessItem]
    ready: bool
    enrollment_counts: dict[str, int]
    draft_counts: dict[str, int]
    created_at: datetime
    updated_at: datetime


class StepsIn(BaseModel):
    days: Annotated[list[int], Field(min_length=1, max_length=8)]


class EnrollIn(BaseModel):
    prospect_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=500)]


class GenerateIn(BaseModel):
    prospect_ids: list[uuid.UUID] | None = Field(default=None, max_length=500)
    force: bool = False


class EnrollmentOut(BaseModel):
    id: uuid.UUID
    prospect_id: uuid.UUID
    full_name: str | None
    email: str | None
    title: str | None
    company_name: str | None
    research_status: ResearchStatus
    state: EnrollmentState
    blocker: str | None
    fit: str | None
    fit_assessment: dict[str, Any] | None
    draft_id: uuid.UUID | None
    draft_status: str | None
    draft_score: float | None
    draft_issues: int
    enrolled_at: datetime


class SequenceIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    days: Annotated[list[int], Field(min_length=1, max_length=8)]


class SequencePatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    days: Annotated[list[int], Field(min_length=1, max_length=8)] | None = None


class SequenceOut(BaseModel):
    id: uuid.UUID
    name: str
    days: list[int]


def _campaign_out(db: DbSession, c: Campaign) -> CampaignOut:
    items = service.campaign_readiness(db, c)
    offering = db.get(Offering, c.offering_id) if c.offering_id else None
    return CampaignOut(
        id=c.id, name=c.name, status=c.status, motion=c.motion, offering_id=c.offering_id,
        offering_name=offering.name if offering else None, brief=service.brief_of(c), brief_version=c.brief_version,
        timezone=c.timezone, step_days=service.step_days(service.steps_of(db, c)), readiness=items,
        ready=not any(i.severity == "blocker" for i in items), enrollment_counts=service.enrollment_counts(db, c.id),
        draft_counts=service.draft_counts(db, c.id), created_at=c.created_at, updated_at=c.updated_at)


# --- Offerings ------------------------------------------------------------------------------------

@router.get("/offerings", response_model=Envelope[list[OfferingOut]], summary="What you sell")
def list_offerings(user: CurrentUser, db: DbSession, include_archived: bool = False) -> Envelope[list[OfferingOut]]:
    stmt = select(Offering).where(Offering.workspace_id == user.workspace_id)
    if not include_archived:
        stmt = stmt.where(Offering.archived.is_(False))
    return Envelope(data=[OfferingOut.model_validate(o) for o in db.scalars(stmt.order_by(Offering.name)).all()])


@router.post("/offerings", status_code=201, response_model=Envelope[OfferingOut], summary="Define an offering")
def create_offering(body: OfferingIn, user: CurrentUser, db: DbSession) -> Envelope[OfferingOut]:
    o = service.save_offering(db, user, body.model_dump(mode="json"))
    db.commit()
    return Envelope(data=OfferingOut.model_validate(o))


@router.get("/offerings/{offering_id}", response_model=Envelope[OfferingOut], summary="Offering detail")
def get_offering(offering_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Envelope[OfferingOut]:
    return Envelope(data=OfferingOut.model_validate(service.get_offering(db, user, offering_id)))


@router.patch("/offerings/{offering_id}", response_model=Envelope[OfferingOut], summary="Edit an offering")
def patch_offering(offering_id: uuid.UUID, body: OfferingPatch, user: CurrentUser,
                   db: DbSession) -> Envelope[OfferingOut]:
    o = service.save_offering(db, user, body.model_dump(mode="json", exclude_unset=True), offering_id)
    db.commit()
    return Envelope(data=OfferingOut.model_validate(o))


@router.delete("/offerings/{offering_id}", response_model=Envelope[dict[str, str]],
               summary="Delete an offering (archived instead if a campaign uses it)")
def delete_offering(offering_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Envelope[dict[str, str]]:
    outcome = service.delete_offering(db, user, offering_id)
    db.commit()
    return Envelope(data={"outcome": outcome})


# --- Campaigns ------------------------------------------------------------------------------------

@router.get("/campaigns/playbooks", response_model=Envelope[list[PlaybookOut]], summary="Campaign playbooks")
def playbooks(_: CurrentUser) -> Envelope[list[PlaybookOut]]:
    from app.services.campaigns.presets import default_brief
    return Envelope(data=[PlaybookOut(motion=p.motion, label=p.label, description=p.description, guidance=p.guidance,
                                      default_brief=default_brief(p.motion), followup_days=list(p.followup_days))
                          for p in PLAYBOOKS.values()])


@router.post("/campaigns", status_code=201, response_model=Envelope[CampaignOut], summary="Create a campaign")
def create_campaign(body: CampaignCreate, user: CurrentUser, db: DbSession) -> Envelope[CampaignOut]:
    c = service.create_campaign(db, user, name=body.name, motion=body.motion, offering_id=body.offering_id)
    db.commit()
    return Envelope(data=_campaign_out(db, c))


@router.get("/campaigns", response_model=Envelope[list[CampaignOut]], summary="Campaigns")
def list_campaigns(user: CurrentUser, db: DbSession) -> Envelope[list[CampaignOut]]:
    rows = db.scalars(select(Campaign).where(Campaign.workspace_id == user.workspace_id)
                      .order_by(Campaign.created_at.desc())).all()
    return Envelope(data=[_campaign_out(db, c) for c in rows])


@router.get("/campaigns/{campaign_id}", response_model=Envelope[CampaignOut], summary="Campaign detail")
def get_campaign(campaign_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Envelope[CampaignOut]:
    return Envelope(data=_campaign_out(db, service.get_campaign(db, user, campaign_id)))


@router.patch("/campaigns/{campaign_id}", response_model=Envelope[CampaignOut], summary="Edit campaign and brief")
def patch_campaign(campaign_id: uuid.UUID, body: CampaignPatch, user: CurrentUser,
                   db: DbSession) -> Envelope[CampaignOut]:
    changes = body.model_dump(mode="json", exclude_unset=True)
    c = service.update_campaign(db, user, campaign_id, changes)
    db.commit()
    return Envelope(data=_campaign_out(db, c))


@router.put("/campaigns/{campaign_id}/steps", response_model=Envelope[CampaignOut],
            summary="Set the follow-up schedule (days from the first email)")
def set_steps(campaign_id: uuid.UUID, body: StepsIn, user: CurrentUser, db: DbSession) -> Envelope[CampaignOut]:
    c = service.set_campaign_steps(db, user, campaign_id, body.days)
    db.commit()
    return Envelope(data=_campaign_out(db, c))


def _status(campaign_id: uuid.UUID, user: Any, db: Any, target: CampaignStatus) -> Envelope[CampaignOut]:
    c = service.change_status(db, user, campaign_id, target)
    db.commit()
    return Envelope(data=_campaign_out(db, c))


@router.post("/campaigns/{campaign_id}/activate", response_model=Envelope[CampaignOut], summary="Activate")
def activate(campaign_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Envelope[CampaignOut]:
    return _status(campaign_id, user, db, CampaignStatus.ACTIVE)


@router.post("/campaigns/{campaign_id}/pause", response_model=Envelope[CampaignOut], summary="Pause")
def pause(campaign_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Envelope[CampaignOut]:
    return _status(campaign_id, user, db, CampaignStatus.PAUSED)


@router.post("/campaigns/{campaign_id}/resume", response_model=Envelope[CampaignOut], summary="Resume")
def resume(campaign_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Envelope[CampaignOut]:
    return _status(campaign_id, user, db, CampaignStatus.ACTIVE)


@router.post("/campaigns/{campaign_id}/cancel", response_model=Envelope[CampaignOut], summary="Cancel")
def cancel(campaign_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Envelope[CampaignOut]:
    return _status(campaign_id, user, db, CampaignStatus.CANCELLED)


@router.post("/campaigns/{campaign_id}/enroll", response_model=Envelope[list[dict[str, Any]]],
             summary="Add contacts to a campaign")
def enroll(campaign_id: uuid.UUID, body: EnrollIn, user: CurrentUser, db: DbSession,
           settings: AppSettings) -> Envelope[list[dict[str, Any]]]:
    results = service.enroll(db, user, campaign_id, body.prospect_ids, settings)
    db.commit()
    return Envelope(data=[{"prospect_id": str(r.prospect_id), "outcome": r.outcome, "reason": r.reason}
                          for r in results],
                    meta={"enrolled": sum(r.outcome == "enrolled" for r in results)})


@router.post("/campaigns/{campaign_id}/remove", response_model=Envelope[dict[str, int]],
             summary="Remove contacts from a campaign")
def remove(campaign_id: uuid.UUID, body: EnrollIn, user: CurrentUser, db: DbSession) -> Envelope[dict[str, int]]:
    count = service.remove_from_campaign(db, user, campaign_id, body.prospect_ids)
    db.commit()
    return Envelope(data={"removed": count})


@router.post("/campaigns/{campaign_id}/generate", status_code=202, response_model=Envelope[dict[str, int]],
             summary="Queue drafting for enrolled contacts whose research is ready")
def generate(campaign_id: uuid.UUID, body: GenerateIn, user: CurrentUser, db: DbSession,
             settings: AppSettings) -> JSONResponse:
    counts = service.generate_for_campaign(db, user, campaign_id, settings, body.prospect_ids, force=body.force)
    db.commit()
    return JSONResponse(Envelope(data=counts).model_dump(mode="json"), status_code=202)


@router.get("/campaigns/{campaign_id}/prospects", response_model=Envelope[list[EnrollmentOut]],
            summary="Contacts in a campaign with research and draft status")
def campaign_prospects(campaign_id: uuid.UUID, user: CurrentUser, db: DbSession,
                       page: Annotated[int, Query(ge=1)] = 1,
                       page_size: Annotated[int, Query(ge=1, le=200)] = 100) -> Envelope[list[EnrollmentOut]]:
    c = service.get_campaign(db, user, campaign_id)
    draft = (select(EmailDraft.prospect_id, EmailDraft.id, EmailDraft.status, EmailDraft.overall_score,
                    func.jsonb_array_length(EmailDraft.validation_issues).label("issues"))
             .where(EmailDraft.campaign_id == c.id, EmailDraft.is_current, EmailDraft.step_order == 0).subquery())
    stmt = (select(CampaignProspect, Prospect, Company.name, draft.c.id, draft.c.status, draft.c.overall_score,
                   draft.c.issues)
            .join(Prospect, Prospect.id == CampaignProspect.prospect_id)
            .outerjoin(Company, Company.id == Prospect.company_id)
            .outerjoin(draft, draft.c.prospect_id == Prospect.id)
            .where(CampaignProspect.campaign_id == c.id))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(stmt.order_by(CampaignProspect.enrolled_at.desc()).offset((page - 1) * page_size)
                      .limit(page_size)).all()
    items = [EnrollmentOut(
        id=cp.id, prospect_id=p.id, full_name=p.full_name, email=p.email, title=p.title, company_name=cname,
        research_status=p.research_status, state=cp.state, blocker=cp.blocker, fit=cp.fit.value if cp.fit else None,
        fit_assessment=cp.fit_assessment, draft_id=did, draft_status=dstatus.value if dstatus else None,
        draft_score=float(dscore) if dscore is not None else None, draft_issues=int(issues or 0),
        enrolled_at=cp.enrolled_at) for cp, p, cname, did, dstatus, dscore, issues in rows]
    return Envelope(data=items, meta=PageMeta(page=page, page_size=page_size, total=total).model_dump())


# --- Sequences ------------------------------------------------------------------------------------

def _sequence_out(db: DbSession, seq: Sequence) -> SequenceOut:
    steps = db.scalars(select(SequenceStep).where(SequenceStep.sequence_id == seq.id, SequenceStep.is_active)
                       .order_by(SequenceStep.step_order)).all()
    return SequenceOut(id=seq.id, name=seq.name, days=service.step_days(list(steps)))


@router.get("/sequences", response_model=Envelope[list[SequenceOut]], summary="Sequences")
def list_sequences(user: CurrentUser, db: DbSession) -> Envelope[list[SequenceOut]]:
    rows = db.scalars(select(Sequence).where(Sequence.workspace_id == user.workspace_id).order_by(Sequence.name)).all()
    return Envelope(data=[_sequence_out(db, s) for s in rows])


@router.post("/sequences", status_code=201, response_model=Envelope[SequenceOut], summary="Create a sequence")
def create_sequence(body: SequenceIn, user: CurrentUser, db: DbSession) -> Envelope[SequenceOut]:
    seq = Sequence(workspace_id=user.workspace_id, name=body.name)
    db.add(seq)
    db.flush()
    service._write_steps(db, seq.id, body.days)
    db.commit()
    return Envelope(data=_sequence_out(db, seq))


@router.patch("/sequences/{sequence_id}", response_model=Envelope[SequenceOut], summary="Edit a sequence")
def patch_sequence(sequence_id: uuid.UUID, body: SequencePatch, user: CurrentUser,
                   db: DbSession) -> Envelope[SequenceOut]:
    from app.core.errors import Conflict, NotFound
    seq = db.scalar(select(Sequence).where(Sequence.id == sequence_id, Sequence.workspace_id == user.workspace_id))
    if seq is None:
        raise NotFound("Sequence not found")
    if body.name:
        seq.name = body.name
    if body.days is not None:
        busy = db.scalar(select(func.count()).select_from(Campaign).where(
            Campaign.sequence_id == seq.id, Campaign.status == CampaignStatus.ACTIVE)) or 0
        if busy:
            raise Conflict("Pause the campaigns using this sequence before changing its steps.",
                           code="sequence_in_use")
        service._write_steps(db, seq.id, body.days)
    db.commit()
    return Envelope(data=_sequence_out(db, seq))
