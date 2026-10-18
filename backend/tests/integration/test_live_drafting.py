"""Opt-in end-to-end drafting against the real hosted models. Run with LIVE_LLM=1 (uses GROQ_API_KEY from ../.env).

Checks that real model output satisfies the drafting contract (schema, citations, rules), not just the fakes.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.domain.states import DraftStatus, JobState
from app.integrations.llm.groq import GroqProvider
from app.integrations.llm.router import LLMRouter
from app.jobs.queue import ClaimedJob
from app.jobs.worker import JobContext
from app.models import EmailDraft, Job
from app.services.drafting.pipeline import DraftingServices, handle_draft
from tests.conftest import AuthedClient
from tests.integration.test_campaigns_drafts import ready_campaign, researched_prospect

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(os.environ.get("LIVE_LLM") != "1", reason="set LIVE_LLM=1 to call real models")]


def test_real_models_produce_a_valid_grounded_draft(authed: AuthedClient, db: Session) -> None:
    keys = Settings(_env_file=Path(__file__).parents[3] / ".env")  # type: ignore[call-arg]
    if not keys.groq_api_key.get_secret_value():
        pytest.skip("GROQ_API_KEY is not configured")
    settings = get_settings()
    provider = GroqProvider(api_key=keys.groq_api_key.get_secret_value(), base_url=keys.groq_base_url,
                            timeout=keys.llm_timeout_seconds)
    hosted = lambda chain: [c for c in Settings.parse_chain(chain) if c[0] == "groq"]  # noqa: E731
    router = LLMRouter(providers={"groq": provider}, chains={"write": hosted(settings.llm_drafting_chain),
                                                              "review": hosted(settings.llm_review_chain)})

    c = ready_campaign(authed)
    p = researched_prospect(authed, db)
    authed.post(f"/api/v1/campaigns/{c['id']}/enroll", json={"prospect_ids": [str(p.id)]})
    job = db.scalar(select(Job).where(Job.kind == "drafts.generate", Job.state == JobState.QUEUED))
    assert job is not None
    sessions = sessionmaker(bind=db.connection(), join_transaction_mode="create_savepoint", expire_on_commit=False)
    ctx = JobContext(job=ClaimedJob(job.id, job.kind, job.payload, job.workspace_id, 1, 3), sessions=sessions,
                     services={"drafting": DraftingServices(settings=settings, llm=router)})
    result = asyncio.run(handle_draft(ctx))
    db.expire_all()

    draft = db.scalar(select(EmailDraft).where(EmailDraft.prospect_id == p.id, EmailDraft.is_current))
    print("\nRESULT:", result)
    if draft is not None:
        print("SCORES:", draft.quality_scores)
        print("ISSUES:", draft.validation_issues)
        print("SUBJECT:", draft.subject, "\n" + draft.body_text)
    assert draft is not None, result
    assert draft.status is DraftStatus.PENDING_REVIEW
    # Deterministic rules must pass; the AI reviewer's judgement may still flag a draft for a human, by design.
    assert not [i for i in draft.validation_issues if i["severity"] == "error" and i["source"] == "rules"]
    assert draft.body_text.startswith("Hi Lena,") and draft.body_text.count("Hi Lena") == 1
