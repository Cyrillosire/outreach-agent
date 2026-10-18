from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.domain.states import (
    DraftStatus,
    DraftType,
    EmailVerificationStatus,
    EnrollmentState,
    ImportRowStatus,
    ImportStatus,
    MessageDirection,
    OutboundStatus,
    ResearchStatus,
    ScheduledActionState,
    ScheduledActionType,
    SuppressionReason,
    SuppressionScope,
)
from app.schemas.common import ORMModel

_ATTRIBUTE_KEY = re.compile(r"^[a-z0-9_]{1,64}$")


def _check_attributes(value: dict[str, str] | None) -> dict[str, str] | None:
    if value is None:
        return None
    if len(value) > 50:
        raise ValueError("at most 50 custom attributes")
    for key, item in value.items():
        if not _ATTRIBUTE_KEY.match(key):
            raise ValueError(f"attribute key {key!r} must be snake_case letters, digits or underscores")
        if len(item) > 2000:
            raise ValueError(f"attribute {key!r} exceeds 2000 characters")
    return value


# --- Imports ------------------------------------------------------------------------------------

class TargetOption(BaseModel):
    value: str
    label: str


class ImportOut(ORMModel):
    id: uuid.UUID
    original_filename: str
    file_size_bytes: int
    encoding: str
    delimiter: str
    headers: list[str]
    row_count: int
    column_mapping: dict[str, str]
    validation_summary: dict[str, Any]
    status: ImportStatus
    error: str | None
    created_at: datetime
    committed_at: datetime | None


class ImportDetailOut(ImportOut):
    targets: list[TargetOption]
    sample_rows: list[dict[str, str]]


class MappingIn(BaseModel):
    mapping: dict[str, str] = Field(description="CSV header -> target field, 'attribute', or 'ignore'")

    model_config = {"json_schema_extra": {"examples": [{"mapping": {
        "Full Name": "full_name", "Individual Email": "email", "Job Title": "title", "Company Name": "company_name",
        "Location": "location", "Assigned To": "attribute", "Other Emails (Company / General)": "ignore"}}]}}


class ImportRowOut(ORMModel):
    row_number: int
    raw: dict[str, str]
    normalized: dict[str, Any] | None
    issues: list[dict[str, str]]
    status: ImportRowStatus | None
    duplicate_of_row: int | None
    existing_prospect_id: uuid.UUID | None
    prospect_id: uuid.UUID | None


# --- Prospects ----------------------------------------------------------------------------------

class ProspectListItem(BaseModel):
    id: uuid.UUID
    full_name: str | None
    email: str | None
    title: str | None
    company_id: uuid.UUID | None
    company_name: str | None
    company_domain: str | None
    research_status: ResearchStatus
    outreach_status: Literal["not_enrolled", "enrolled", "suppressed"]
    has_email: bool
    import_id: uuid.UUID | None
    last_activity_at: datetime | None
    next_action_at: datetime | None
    created_at: datetime


class CompanyBrief(ORMModel):
    id: uuid.UUID
    name: str
    normalized_domain: str | None
    website: str | None
    industry: str | None
    research_status: ResearchStatus


class SuppressionOut(ORMModel):
    id: uuid.UUID
    email_normalized: str | None
    domain: str | None
    scope: SuppressionScope
    campaign_id: uuid.UUID | None
    reason: SuppressionReason
    note: str | None
    is_permanent: bool
    created_at: datetime


class SourceOut(BaseModel):
    source: str
    import_id: uuid.UUID | None
    import_filename: str | None
    row_number: int | None
    raw: dict[str, Any] | None


class DraftBrief(ORMModel):
    id: uuid.UUID
    draft_type: DraftType
    version: int
    subject: str
    status: DraftStatus
    is_current: bool
    created_at: datetime


class MessageBrief(ORMModel):
    id: uuid.UUID
    direction: MessageDirection
    subject: str | None
    outbound_status: OutboundStatus | None
    sent_at: datetime | None
    received_at: datetime | None


class ActionBrief(ORMModel):
    id: uuid.UUID
    action_type: ScheduledActionType
    due_at: datetime
    state: ScheduledActionState
    cancel_reason: str | None


class EnrollmentBrief(ORMModel):
    id: uuid.UUID
    campaign_id: uuid.UUID
    state: EnrollmentState
    next_action_at: datetime | None


class Blocker(BaseModel):
    code: str
    message: str


class ProspectOut(ORMModel):
    id: uuid.UUID
    first_name: str | None
    last_name: str | None
    full_name: str | None
    email: str | None
    email_normalized: str | None
    email_verification_status: EmailVerificationStatus
    title: str | None
    linkedin_url: str | None
    industry: str | None
    country: str | None
    location: str | None
    attributes: dict[str, Any]
    research_status: ResearchStatus
    last_activity_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ProspectDetailOut(ProspectOut):
    company: CompanyBrief | None
    provenance: SourceOut
    suppressions: list[SuppressionOut]
    blockers: list[Blocker]
    research_fact_count: int
    drafts: list[DraftBrief]
    messages: list[MessageBrief]
    scheduled_actions: list[ActionBrief]
    enrollments: list[EnrollmentBrief]


class ProspectWriteResult(BaseModel):
    prospect: ProspectOut
    warnings: list[dict[str, str]]


class ProspectCreateIn(BaseModel):
    email: str | None = Field(default=None, max_length=320)
    first_name: str | None = Field(default=None, max_length=150)
    last_name: str | None = Field(default=None, max_length=150)
    full_name: str | None = Field(default=None, max_length=300)
    title: str | None = Field(default=None, max_length=300)
    company_name: str | None = Field(default=None, max_length=300)
    company_domain: str | None = Field(default=None, max_length=253)
    company_website: str | None = Field(default=None, max_length=2048)
    linkedin_url: str | None = Field(default=None, max_length=2048)
    industry: str | None = Field(default=None, max_length=200)
    country: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=300)
    attributes: dict[str, str] = Field(default_factory=dict)

    _attrs = field_validator("attributes")(_check_attributes)

    model_config = {"json_schema_extra": {"examples": [{
        "email": "priya.sharma@example-hospital.in", "full_name": "Priya Sharma", "title": "Head of IT",
        "company_name": "Example Hospital", "company_domain": "example-hospital.in", "country": "India"}]}}


class ProspectPatchIn(BaseModel):
    """Only fields present in the request are changed; send null to clear a field."""

    email: str | None = Field(default=None, max_length=320)
    first_name: str | None = Field(default=None, max_length=150)
    last_name: str | None = Field(default=None, max_length=150)
    full_name: str | None = Field(default=None, max_length=300)
    title: str | None = Field(default=None, max_length=300)
    company_name: str | None = Field(default=None, max_length=300)
    company_domain: str | None = Field(default=None, max_length=253)
    company_website: str | None = Field(default=None, max_length=2048)
    linkedin_url: str | None = Field(default=None, max_length=2048)
    industry: str | None = Field(default=None, max_length=200)
    country: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=300)
    attributes: dict[str, str] | None = None

    _attrs = field_validator("attributes")(_check_attributes)


class SuppressIn(BaseModel):
    reason: SuppressionReason = SuppressionReason.MANUAL
    target: Literal["email", "domain"] = "email"
    scope: SuppressionScope = SuppressionScope.WORKSPACE
    campaign_id: uuid.UUID | None = None
    note: str | None = Field(default=None, max_length=1000)


class SuppressionResult(BaseModel):
    suppression: SuppressionOut
    created: bool
    enrollments_stopped: int
    actions_cancelled: int


class CompanyDetailOut(CompanyBrief):
    description: str | None
    location: str | None
    created_at: datetime
    prospects: list[ProspectOut]
