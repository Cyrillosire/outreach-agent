from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.domain.states import (
    FactKind,
    FactVerification,
    JobState,
    ResearchRunStatus,
    ResearchStatus,
    ResearchSubject,
    SourceType,
)
from app.schemas.common import ORMModel


class ResearchRequestIn(BaseModel):
    force: bool = False


class ResearchRequestOut(BaseModel):
    research_status: ResearchStatus
    prospect_job_id: uuid.UUID | None
    company_job_id: uuid.UUID | None
    company_research_reused: bool
    already_running: bool


class SourceOut(ORMModel):
    id: uuid.UUID
    url: str
    title: str | None
    publisher: str | None
    source_type: SourceType
    published_at: datetime | None
    accessed_at: datetime


class EvidenceOut(BaseModel):
    source: SourceOut
    excerpt: str


class FactOut(BaseModel):
    id: uuid.UUID
    category: str
    claim: str
    kind: FactKind
    verification_status: FactVerification
    confidence: float
    relevance: float
    conflict_note: str | None
    published_at: datetime | None
    expires_at: datetime | None
    evidence: list[EvidenceOut]


class RunOut(ORMModel):
    id: uuid.UUID
    subject_type: ResearchSubject
    company_id: uuid.UUID | None
    prospect_id: uuid.UUID | None
    status: ResearchRunStatus
    stage: str | None
    attempt_count: int
    sufficiency: dict[str, Any] | None
    error: dict[str, Any] | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class RunDetailOut(RunOut):
    job_state: JobState | None
    job_last_error: str | None
    next_attempt_at: datetime | None
    fact_count: int


class CompanyResearchOut(BaseModel):
    id: uuid.UUID
    name: str
    normalized_domain: str | None
    research_status: ResearchStatus
    last_researched_at: datetime | None
    latest_run: RunOut | None
    facts: list[FactOut]


class ProspectResearchOut(BaseModel):
    prospect_id: uuid.UUID
    research_status: ResearchStatus
    outcome_reason: str | None
    individual_run: RunOut | None
    individual_facts: list[FactOut]
    company: CompanyResearchOut | None
    in_progress: bool


class RecentRunOut(RunOut):
    subject_name: str | None


class ResearchOverviewOut(BaseModel):
    prospects_by_status: dict[str, int]
    jobs_by_state: dict[str, int]
    providers: dict[str, bool]
    llm_extraction_chain: list[str]
    search_order: list[str]
    auto_start: bool
    recent_runs: list[RecentRunOut]
