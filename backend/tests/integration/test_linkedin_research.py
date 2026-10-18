"""LinkedIn evidence: search-index results tied to the profile URL, user captures, and research status phases."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.domain.states import JobState, ResearchStatus, SourceType
from app.integrations.llm.base import LLMUsage
from app.integrations.search.providers import SearchResult
from app.models import Company, Job, Prospect, ResearchFact, ResearchFactEvidence, ResearchSource
from app.services.research import store
from tests.conftest import AuthedClient
from tests.integration.test_research_pipeline import FakeLLM, run_worker, services, ws  # noqa: F401

pytestmark = pytest.mark.integration
SLUG = "priya-nair-7f"
PROFILE = f"https://www.linkedin.com/in/{SLUG}"


class LinkedInSearch:
    """Index results: her profile, her own post, a namesake's profile, and a namesake's post."""

    name = "tavily"

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        self.queries.append(query)
        if "linkedin" not in query:
            return []
        return [
            SearchResult(PROFILE, "Priya Nair - Head of Clinical Informatics - Northwind Regional Hospital Network",
                         "Priya Nair is Head of Clinical Informatics at Northwind Regional Hospital Network.",
                         "tavily", 0),
            SearchResult(f"https://www.linkedin.com/posts/{SLUG}_documentation-activity-7301-abc",
                         "Priya Nair on LinkedIn: Clinician documentation time",
                         "Priya Nair wrote that cutting clinician documentation time is her team's top goal for 2026.",
                         "tavily", 1),
            SearchResult("https://www.linkedin.com/in/priya-nair-99", "Priya Nair - Pastry Chef",
                         "Priya Nair is a pastry chef in Leeds.", "tavily", 2),
            SearchResult("https://www.linkedin.com/posts/priya-nair-99_cakes-activity-1",
                         "Priya Nair on LinkedIn: cakes", "Priya Nair shared a new cake recipe.", "tavily", 3),
        ]


class LinkedInAwareLLM(FakeLLM):
    """FakeLLM plus LinkedIn extraction: role from the headline, own posts, and engagement from captures."""

    async def complete_json(self, *, model: str, system: str, user: str, schema: dict[str, Any],
                            max_tokens: int) -> tuple[str, LLMUsage]:
        if "LinkedIn" not in system:
            return await super().complete_json(model=model, system=system, user=user, schema=schema,
                                               max_tokens=max_tokens)
        self.calls += 1
        body = user.split(">>>", 1)[1].rsplit("<<<END", 1)[0]
        facts = []
        for line in [ln.strip() for ln in body.split("\n") if len(ln.strip()) > 20]:
            sentence = re.split(r"(?<=[.!?])\s", line)[0]
            cat = ("current_role" if " is Head of" in sentence else "own_post" if "wrote that" in sentence
                   else "engagement" if sentence.startswith("Priya Nair commented") else None)
            if cat:
                facts.append({"claim": sentence[:200], "category": cat, "kind": "verified_fact", "excerpt": sentence,
                              "confidence": 0.9, "relevance": 0.9})
        return json.dumps({"facts": facts}), LLMUsage("groq", model, 100, 50, 10.0)


def _seed(sessions: sessionmaker[Session], wid: uuid.UUID, *, capture: str | None = None) -> uuid.UUID:
    with sessions() as s:
        company = Company(workspace_id=wid, name="Northwind Regional Hospital Network",
                          normalized_domain="northwind.example")
        s.add(company)
        s.flush()
        p = Prospect(workspace_id=wid, company_id=company.id, full_name="Priya Nair", email="priya@northwind.example",
                     email_normalized="priya@northwind.example", linkedin_url=PROFILE + "/")
        s.add(p)
        s.flush()
        if capture:
            store.save_capture(s, prospect=p, kind="activity", text=capture, url=PROFILE + "/recent-activity/all/")
        store.request_prospect_research(s, prospect=p, settings=services(FakeLLM(), None).settings)
        s.commit()
        return p.id


def test_linkedin_index_and_capture_become_first_party_evidence(ws: tuple[sessionmaker[Session], uuid.UUID]  # noqa: F811
                                                                ) -> None:
    sessions, wid = ws
    capture = ("Priya Nair commented on Dr. Rao's post about ambient scribes: \"We are piloting this in Q3.\"\n"
               "Like\nComment\nRepost\nAnita Shah posted: Our hospital went live with a new HIS.")
    pid = _seed(sessions, wid, capture=capture)
    requested: list[str] = []
    svc = services(LinkedInAwareLLM(), LinkedInSearch())  # type: ignore[arg-type]
    original = svc.fetcher._client._transport  # every fetch goes through the mock transport
    svc.fetcher._client._transport = httpx.MockTransport(
        lambda req: requested.append(str(req.url)) or original.handle_request(req))  # type: ignore[union-attr,func-returns-value]
    run_worker(sessions, svc)

    assert not any("linkedin.com" in u for u in requested)  # LinkedIn is never fetched
    with sessions() as s:
        rows = s.execute(select(ResearchFact, ResearchSource).join(
            ResearchFactEvidence, ResearchFactEvidence.fact_id == ResearchFact.id).join(
            ResearchSource, ResearchSource.id == ResearchFactEvidence.source_id).where(
            ResearchFact.prospect_id == pid)).all()
        by_cat = {f.category: src for f, src in rows}
        assert by_cat["current_role"].source_type is SourceType.LINKEDIN_PROFILE
        assert by_cat["own_post"].source_type is SourceType.LINKEDIN_POST
        assert by_cat["engagement"].source_type is SourceType.LINKEDIN_ACTIVITY
        assert by_cat["engagement"].captured_for_prospect_id == pid
        assert not any("pastry" in f.claim or "cake" in f.claim for f, _ in rows)  # namesake results ignored
        # Her own LinkedIn alone confirms her role: no second public source needed.
        assert s.get(Prospect, pid).research_status is ResearchStatus.READY  # type: ignore[union-attr]


def test_capture_api_lists_deletes_and_is_workspace_scoped(authed: AuthedClient, db: Session) -> None:
    p = authed.post("/api/v1/prospects", json={"email": "priya@northwind.example", "full_name": "Priya Nair",
                                               "linkedin_url": PROFILE}).json()["data"]["prospect"]
    text = "Priya Nair commented on a post about ambient AI scribes: we are piloting this in Q3 across two sites."
    r = authed.post(f"/api/v1/prospects/{p['id']}/linkedin-captures", json={"kind": "activity", "text": text,
                                                                           "url": PROFILE + "/recent-activity/all/"})
    assert r.status_code == 202, r.text
    captures = authed.get(f"/api/v1/prospects/{p['id']}/linkedin-captures").json()["data"]
    assert len(captures) == 1 and captures[0]["kind"] == "activity"
    assert authed.post(f"/api/v1/prospects/{p['id']}/linkedin-captures",
                       json={"kind": "activity", "text": text, "url": "https://evil.example/x"}).status_code == 400

    # A fact that rests only on the capture disappears with it.
    src = db.get(ResearchSource, uuid.UUID(captures[0]["id"]))
    assert src is not None
    fact = ResearchFact(workspace_id=src.workspace_id, prospect_id=uuid.UUID(p["id"]), category="engagement",
                        claim="Commented on a post about ambient AI scribes", kind="verified_fact",
                        verification_status="single_source", confidence=0.9, relevance=0.9,
                        research_run_id=_run(db, src.workspace_id, uuid.UUID(p["id"])))
    db.add(fact)
    db.flush()
    db.add(ResearchFactEvidence(fact_id=fact.id, source_id=src.id, excerpt=text[:60], accessed_at=src.accessed_at))
    db.commit()
    r = authed.client.delete(f"/api/v1/prospects/{p['id']}/linkedin-captures/{src.id}",
                             headers={"X-CSRF-Token": authed.csrf})
    assert r.json()["data"]["facts_removed"] == 1
    db.expire_all()
    assert db.get(ResearchFact, fact.id).expires_at is not None  # type: ignore[union-attr]
    assert authed.get(f"/api/v1/prospects/{p['id']}/linkedin-captures").json()["data"] == []


def _run(db: Session, wid: uuid.UUID, pid: uuid.UUID) -> uuid.UUID:
    from app.models import ResearchRun
    run = ResearchRun(workspace_id=wid, subject_type="prospect", prospect_id=pid)  # type: ignore[arg-type]
    db.add(run)
    db.flush()
    return run.id


def test_research_phase_reflects_the_queue_not_the_stored_status(authed: AuthedClient, db: Session) -> None:
    p = authed.post("/api/v1/prospects", json={"email": "wen@acme.example", "full_name": "Wen Li",
                                               "company_name": "Acme Labs"}).json()["data"]["prospect"]
    r = authed.get(f"/api/v1/prospects/{p['id']}/research").json()["data"]
    assert r["phase"] == "queued" and r["in_progress"] is True  # auto-started on create

    for job in db.scalars(select(Job)).all():  # the queue finished (or never ran) but the status says pending
        job.state = JobState.CANCELLED
    db.commit()
    r = authed.get(f"/api/v1/prospects/{p['id']}/research").json()["data"]
    assert r["research_status"] == "pending" and r["phase"] == "idle" and r["in_progress"] is False

    company_job = db.scalar(select(Job).where(Job.kind == "research.company"))
    assert company_job is not None
    company_job.state = JobState.RUNNING
    db.commit()
    assert authed.get(f"/api/v1/prospects/{p['id']}/research").json()["data"]["phase"] == "waiting_for_company"


def test_finalize_does_not_wait_for_an_abandoned_company(ws: tuple[sessionmaker[Session], uuid.UUID]  # noqa: F811
                                                        ) -> None:
    """Regression: a company left 'pending' with no job kept its prospects 'researching' forever."""
    sessions, wid = ws
    pid = _seed(sessions, wid)
    run_worker(sessions, services(LinkedInAwareLLM(), LinkedInSearch()))  # type: ignore[arg-type]
    with sessions() as s:
        prospect = s.get(Prospect, pid)
        assert prospect is not None
        company = s.get(Company, prospect.company_id)
        assert company is not None
        company.research_status = ResearchStatus.PENDING  # e.g. its job was cancelled
        prospect.research_status = ResearchStatus.RESEARCHING_INDIVIDUAL
        s.commit()
        assert store.finalize_prospect(s, pid) is not None
        s.commit()
