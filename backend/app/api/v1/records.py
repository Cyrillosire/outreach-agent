"""Record management endpoints: deletion, bulk actions, companies list/edit, duplicates and merging."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession
from app.core.errors import AppError, NotFound
from app.models import Prospect
from app.schemas.common import ERROR_RESPONSES, Envelope, PageMeta
from app.schemas.prospects import CompanyBrief
from app.services import prospects as prospect_service
from app.services import records
from app.services.research.store import request_prospect_research

router = APIRouter(tags=["records"], responses=ERROR_RESPONSES)

Ids = Annotated[list[uuid.UUID], Field(min_length=1, max_length=records.MAX_BULK)]


class IdsIn(BaseModel):
    ids: Ids


class CompanyDeleteIn(IdsIn):
    delete_contacts: bool = False


class BulkResearchIn(IdsIn):
    force: bool = False


class ProspectBulkChanges(BaseModel):
    """Fields applied to every selected contact. Omitted fields are left unchanged; null clears a field."""

    title: str | None = Field(default=None, max_length=300)
    industry: str | None = Field(default=None, max_length=200)
    country: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=300)
    company_name: str | None = Field(default=None, max_length=300)
    company_domain: str | None = Field(default=None, max_length=253)
    attributes_set: dict[str, str] | None = None
    attributes_remove: list[str] | None = None

    @field_validator("attributes_set")
    @classmethod
    def _attrs(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value and (len(value) > 50 or any(len(k) > 64 or len(v) > 2000 for k, v in value.items())):
            raise ValueError("at most 50 attributes; keys up to 64 and values up to 2000 characters")
        return value


class ProspectBulkUpdateIn(IdsIn):
    changes: ProspectBulkChanges


class CompanyBulkChanges(BaseModel):
    industry: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=300)


class CompanyBulkUpdateIn(IdsIn):
    changes: CompanyBulkChanges


class CompanyPatchIn(BaseModel):
    name: str | None = Field(default=None, max_length=300)
    normalized_domain: str | None = Field(default=None, max_length=253)
    website: str | None = Field(default=None, max_length=2048)
    industry: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=5000)


class MergeIn(BaseModel):
    survivor_id: uuid.UUID
    duplicate_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=50)]


class BulkResult(BaseModel):
    affected: int
    failed: list[dict[str, str]] = []


class CompanyListItem(CompanyBrief):
    contacts: int
    location: str | None
    last_researched_at: datetime | None
    created_at: datetime


class DuplicatesOut(BaseModel):
    companies: list[dict[str, Any]]
    contacts: list[dict[str, Any]]


# --- Contacts ------------------------------------------------------------------------------------

@router.delete("/prospects/{prospect_id}", response_model=Envelope[BulkResult], summary="Delete a contact")
def delete_prospect(prospect_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Envelope[BulkResult]:
    if not records.delete_prospects(db, user, [prospect_id]):
        raise NotFound("Prospect not found")
    db.commit()
    return Envelope(data=BulkResult(affected=1))


@router.post("/prospects/bulk-delete", response_model=Envelope[BulkResult], summary="Delete selected contacts")
def bulk_delete_prospects(body: IdsIn, user: CurrentUser, db: DbSession) -> Envelope[BulkResult]:
    count = records.delete_prospects(db, user, records._check_bulk(body.ids), audit_action="prospect.bulk_delete")
    db.commit()
    return Envelope(data=BulkResult(affected=count))


@router.post("/prospects/bulk-update", response_model=Envelope[BulkResult], summary="Edit fields on selected contacts")
def bulk_update_prospects(body: ProspectBulkUpdateIn, user: CurrentUser, db: DbSession) -> Envelope[BulkResult]:
    ids = records._check_bulk(body.ids)
    base = body.changes.model_dump(exclude_unset=True, exclude={"attributes_set", "attributes_remove"})
    if "company_name" in base or "company_domain" in base:
        base.setdefault("company_name", None)
        base.setdefault("company_domain", None)
    attrs_set, attrs_remove = body.changes.attributes_set or {}, set(body.changes.attributes_remove or [])
    if not base and not attrs_set and not attrs_remove:
        raise AppError("Choose at least one field to change.", code="no_changes")
    affected, failed = 0, []
    for pid in ids:
        try:
            with db.begin_nested():
                changes = dict(base)
                if attrs_set or attrs_remove:
                    prospect = prospect_service.get_prospect(db, user, pid)
                    merged = {**(prospect.attributes or {}), **attrs_set}
                    changes["attributes"] = {k: v for k, v in merged.items() if k not in attrs_remove}
                prospect_service.update_prospect(db, user, pid, changes)
            affected += 1
        except AppError as exc:
            failed.append({"id": str(pid), "error": exc.message})
    db.commit()
    return Envelope(data=BulkResult(affected=affected, failed=failed))


@router.post("/prospects/bulk-research", response_model=Envelope[BulkResult], summary="Start research for contacts")
def bulk_research(body: BulkResearchIn, user: CurrentUser, db: DbSession,
                  settings: AppSettings) -> Envelope[BulkResult]:
    prospects = db.scalars(select(Prospect).where(Prospect.workspace_id == user.workspace_id,
                                                  Prospect.id.in_(records._check_bulk(body.ids)))).all()
    for p in prospects:
        request_prospect_research(db, prospect=p, settings=settings, force=body.force)
    db.commit()
    return Envelope(data=BulkResult(affected=len(prospects)))


@router.post("/prospects/merge", response_model=Envelope[dict[str, str]], summary="Merge duplicate contacts")
def merge_prospects(body: MergeIn, user: CurrentUser, db: DbSession) -> Envelope[dict[str, str]]:
    survivor = records.merge_prospects(db, user, body.survivor_id, body.duplicate_ids)
    db.commit()
    return Envelope(data={"id": str(survivor.id)})


# --- Companies ------------------------------------------------------------------------------------

@router.get("/companies", response_model=Envelope[list[CompanyListItem]], summary="Search companies")
def list_companies(
    user: CurrentUser, db: DbSession,
    q: Annotated[str | None, Query(max_length=200)] = None,
    has_domain: bool | None = None,
    sort: Literal["name", "-created", "-contacts"] = "name",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Envelope[list[CompanyListItem]]:
    rows, total = records.list_companies(db, user, q=q, has_domain=has_domain, sort=sort, page=page,
                                         page_size=page_size)
    items = [CompanyListItem(**CompanyBrief.model_validate(c).model_dump(), contacts=n, location=c.location,
                             last_researched_at=c.last_researched_at, created_at=c.created_at) for c, n in rows]
    return Envelope(data=items, meta=PageMeta(page=page, page_size=page_size, total=total).model_dump())


@router.patch("/companies/{company_id}", response_model=Envelope[CompanyBrief], summary="Edit a company")
def patch_company(company_id: uuid.UUID, body: CompanyPatchIn, user: CurrentUser,
                  db: DbSession) -> Envelope[CompanyBrief]:
    company = records.update_company(db, user, company_id, body.model_dump(exclude_unset=True))
    db.commit()
    return Envelope(data=CompanyBrief.model_validate(company))


@router.delete("/companies/{company_id}", response_model=Envelope[BulkResult], summary="Delete a company")
def delete_company(company_id: uuid.UUID, user: CurrentUser, db: DbSession,
                   delete_contacts: bool = False) -> Envelope[BulkResult]:
    companies, contacts = records.delete_companies(db, user, [company_id], delete_contacts=delete_contacts)
    if not companies:
        raise NotFound("Company not found")
    db.commit()
    return Envelope(data=BulkResult(affected=companies), meta={"contacts_deleted": contacts})


@router.post("/companies/bulk-delete", response_model=Envelope[BulkResult], summary="Delete selected companies")
def bulk_delete_companies(body: CompanyDeleteIn, user: CurrentUser, db: DbSession) -> Envelope[BulkResult]:
    companies, contacts = records.delete_companies(db, user, records._check_bulk(body.ids),
                                                   delete_contacts=body.delete_contacts)
    db.commit()
    return Envelope(data=BulkResult(affected=companies), meta={"contacts_deleted": contacts})


@router.post("/companies/bulk-update", response_model=Envelope[BulkResult], summary="Edit selected companies")
def bulk_update_companies(body: CompanyBulkUpdateIn, user: CurrentUser, db: DbSession) -> Envelope[BulkResult]:
    changes = body.changes.model_dump(exclude_unset=True)
    if not changes:
        raise AppError("Choose at least one field to change.", code="no_changes")
    affected, failed = 0, []
    for cid in records._check_bulk(body.ids):
        try:
            with db.begin_nested():
                records.update_company(db, user, cid, changes)
            affected += 1
        except AppError as exc:
            failed.append({"id": str(cid), "error": exc.message})
    db.commit()
    return Envelope(data=BulkResult(affected=affected, failed=failed))


@router.post("/companies/merge", response_model=Envelope[dict[str, str]], summary="Merge duplicate companies")
def merge_companies(body: MergeIn, user: CurrentUser, db: DbSession) -> Envelope[dict[str, str]]:
    survivor = records.merge_companies(db, user, body.survivor_id, body.duplicate_ids)
    db.commit()
    return Envelope(data={"id": str(survivor.id)})


# --- Duplicates -----------------------------------------------------------------------------------

@router.get("/duplicates", response_model=Envelope[DuplicatesOut],
            summary="Companies with the exact same name, and same-named contacts at the same company")
def duplicates(user: CurrentUser, db: DbSession) -> Envelope[DuplicatesOut]:
    groups = records.find_duplicates(db, user)
    return Envelope(data=DuplicatesOut(companies=groups.companies, contacts=groups.contacts))

