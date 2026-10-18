"""Campaigns, enrollment and the drafting pipeline (fake LLM) through the API and job handler."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.domain.states import (
    DraftStatus,
    EnrollmentState,
    FactKind,
    FactVerification,
    JobState,
    ResearchStatus,
    ResearchSubject,
    SourceType,
)
from app.integrations.llm.base import LLMUsage
from app.integrations.llm.router import LLMRouter
from app.jobs.queue import ClaimedJob
from app.jobs.worker import JobContext, PermanentJobError
from app.models import (
    CampaignProspect,
    Company,
    EmailDraft,
    EmailDraftEvidence,
    Job,
    Prospect,
    ResearchFact,
    ResearchFactEvidence,
    ResearchRun,
    ResearchSource,
    Workspace,
)
from app.services.drafting.pipeline import DraftingServices, handle_draft
from tests.conftest import AuthedClient

pytestmark = pytest.mark.integration

OFFERING = {
    "name": "Penthus Clinical Documentation", "offering_type": "product", "category": "Ambient AI scribe",
    "summary": "Drafts clinical notes from consultations inside the hospital information system.",
    "details": {"problems_solved": ["Clinicians lose hours to documentation"],
                "capabilities": ["Drafts structured notes in under a minute"],
                "proof_points": [{"id": "pp_abc1", "claim": "Cut documentation time by 40% in a 3-hospital pilot",
                                  "kind": "metric"}],
                "forbidden_claims": ["replaces doctors"]},
}
BRIEF = {
    "goal": "Book discovery calls with hospital digital leaders",
    "fit_signals": ["hospital information system rollout"], "disqualifiers": ["uses a competing scribe"],
    "audience": {"personas": [{"name": "Digital leader", "title_keywords": ["digital", "CIO"],
                               "pains": ["documentation burden"], "priorities": ["clinician adoption"]}]},
    "cta": {"type": "book_meeting", "details": "a 20-minute call"},
    "style": {"max_words": 120},
    "sender": {"name": "Asha Rao", "signature": "Partnerships, Penthus",
               "compliance_footer": "Penthus Health Pvt Ltd, 12 MG Road, Bengaluru 560001. "
                                    "Reply 'unsubscribe' to opt out."},
}


class FakeDraftLLM:
    """Writer + reviewer double. Scenarios: 'good', 'bad_then_good' (unsupported number first), 'disqualified'."""

    name = "groq"

    def __init__(self, scenario: str = "good") -> None:
        self.scenario = scenario
        self.writes = 0
        self.reviews = 0
        self.prompts: list[str] = []

    async def complete_json(self, *, model: str, system: str, user: str, schema: dict[str, Any],
                            max_tokens: int) -> tuple[str, LLMUsage]:
        usage = LLMUsage("groq", model, 500, 200, 5.0)
        if "strict reviewer" in system:
            self.reviews += 1
            return json.dumps({"scores": {k: 5 for k in ("evidence_strength", "individual_relevance",
                                                         "company_relevance", "offering_alignment", "naturalness",
                                                         "conciseness", "value_clarity", "cta_quality")},
                               "unsupported_claims": [], "issues": [],
                               "summary": "Specific and well supported."}), usage
        self.writes += 1
        self.prompts.append(user)
        fact_ref = re.search(r"^(F\d+) \[company", user, re.MULTILINE)
        ref = fact_ref.group(1) if fact_ref else "F1"
        follow_up = "FOLLOW-UP #" in user
        bad = self.scenario == "bad_then_good" and self.writes == 1
        value = ("Penthus cut documentation time by 70% for 12 hospitals." if bad else
                 "Penthus drafts structured notes in under a minute; a 3-hospital pilot cut documentation time by 40%.")
        out = {
            "fit": "disqualified" if self.scenario == "disqualified" else "strong",
            "fit_reason": ("Already uses a competing scribe." if self.scenario == "disqualified"
                           else "Active HIS rollout."),
            "disqualifier_hits": ["uses a competing scribe"] if self.scenario == "disqualified" else [],
            "persona": "Digital leader",
            "opportunity": "Documentation load during the rollout (hypothesis).",
            "rationale": {"why_organization": "HIS rollout under way.", "why_person": "Leads digital programmes.",
                          "why_offering": "Reduces documentation time.", "why_now": "Rollout is in progress.",
                          "next_step": "20-minute call."},
            "subject": "Re: Northwind's HIS rollout" if follow_up else "Northwind's HIS rollout",
            "sections": {
                "opening": "Following up briefly." if follow_up else
                "Saw that Northwind is rolling out a hospital information system across six hospitals.",
                "observation": "Rollouts like that usually put new pressure on clinicians' time.",
                "problem": "Is documentation load something your team is trying to reduce as part of it?",
                "value": value,
                "cta": "Would a 20-minute call be useful?",
            },
            "claims": [
                {"text": "Northwind is rolling out a hospital information system across six hospitals", "refs": [ref],
                 "kind": "research"},
                {"text": "Is documentation load something your team is trying to reduce", "refs": [],
                 "kind": "hypothesis"},
                {"text": "drafts structured notes in under a minute", "refs": ["C2"], "kind": "offering"},
                {"text": "a 3-hospital pilot cut documentation time by 40%", "refs": ["P1"], "kind": "offering"},
            ],
        }
        return json.dumps(out), usage


def create(a: AuthedClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
    r = a.post(path, json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()["data"]


def ready_campaign(a: AuthedClient) -> dict[str, Any]:
    offering = create(a, "/api/v1/offerings", OFFERING)
    campaign = create(a, "/api/v1/campaigns", {"name": "Delhi hospitals", "motion": "product_sale",
                                               "offering_id": offering["id"]})
    r = a.patch(f"/api/v1/campaigns/{campaign['id']}", json={"brief": BRIEF})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["ready"] is True, r.json()["data"]["readiness"]
    return r.json()["data"]


def researched_prospect(a: AuthedClient, db: Session, *, email: str = "lena@northwind.example",
                        name: str = "Lena Okafor", status: ResearchStatus = ResearchStatus.READY) -> Prospect:
    p = create(a, "/api/v1/prospects", {"email": email, "full_name": name, "title": "Chief Digital Officer",
                                        "company_name": "Northwind Health", "company_domain": "northwind.example"})
    prospect = db.get(Prospect, uuid.UUID(p["prospect"]["id"] if "prospect" in p else p["id"]))
    assert prospect is not None
    for job in db.scalars(select(Job)).all():  # research jobs from auto-start are not run in these tests
        job.state = JobState.SUCCEEDED
    now = datetime.now(UTC)
    src = ResearchSource(workspace_id=prospect.workspace_id, url="https://northwind.example/news",
                         url_normalized=f"https://northwind.example/news?{uuid.uuid4().hex}", title="News",
                         publisher="northwind.example", source_type=SourceType.PRESS_RELEASE, accessed_at=now,
                         extracted_text="x", content_sha256=uuid.uuid4().hex * 2)
    db.add(src)
    db.flush()
    for subject, claim, category in (
            ("company", "Northwind is rolling out a hospital information system across six hospitals", "technology"),
            ("person", "Lena Okafor leads clinical informatics at Northwind", "current_role")):
        run = ResearchRun(workspace_id=prospect.workspace_id, subject_type=ResearchSubject(
            "company" if subject == "company" else "prospect"),
            company_id=prospect.company_id if subject == "company" else None,
            prospect_id=prospect.id if subject == "person" else None)
        db.add(run)
        db.flush()
        f = ResearchFact(workspace_id=prospect.workspace_id, research_run_id=run.id,
                         company_id=prospect.company_id if subject == "company" else None,
                         prospect_id=prospect.id if subject == "person" else None, category=category, claim=claim,
                         kind=FactKind.VERIFIED_FACT, verification_status=FactVerification.SINGLE_SOURCE,
                         confidence=0.9, relevance=0.9)
        db.add(f)
        db.flush()
        db.add(ResearchFactEvidence(fact_id=f.id, source_id=src.id, excerpt=claim, accessed_at=now))
    prospect.research_status = status
    db.commit()
    return prospect


def run_draft_job(db: Session, llm: FakeDraftLLM) -> Any:
    """Run the oldest queued drafting job in-process against the test transaction."""
    job = db.scalar(select(Job).where(Job.kind == "drafts.generate", Job.state == JobState.QUEUED)
                    .order_by(Job.created_at))
    assert job is not None, "no drafting job queued"
    sessions = sessionmaker(bind=db.connection(), join_transaction_mode="create_savepoint", expire_on_commit=False)
    router = LLMRouter(providers={"groq": llm}, chains={"write": [("groq", "openai/gpt-oss-120b")],
                                                       "review": [("groq", "openai/gpt-oss-20b")]})
    ctx = JobContext(job=ClaimedJob(job.id, job.kind, job.payload, job.workspace_id, 1, 3), sessions=sessions,
                     services={"drafting": DraftingServices(settings=get_settings(), llm=router)})
    try:
        return asyncio.run(handle_draft(ctx))
    finally:
        job.state = JobState.SUCCEEDED
        db.commit()
        db.expire_all()


# --- Offerings & campaigns ---------------------------------------------------------------------------

def test_offering_crud_and_archive_when_in_use(authed: AuthedClient) -> None:
    o = create(authed, "/api/v1/offerings", OFFERING)
    assert o["details"]["proof_points"][0]["id"] == "pp_abc1"
    assert authed.post("/api/v1/offerings", json=OFFERING).status_code == 409  # duplicate name
    r = authed.patch(f"/api/v1/offerings/{o['id']}", json={"category": "Clinical documentation"})
    assert r.json()["data"]["category"] == "Clinical documentation"
    create(authed, "/api/v1/campaigns", {"name": "Clinics", "motion": "product_sale", "offering_id": o["id"]})
    r = authed.client.delete(f"/api/v1/offerings/{o['id']}", headers={"X-CSRF-Token": authed.csrf})
    assert r.json()["data"]["outcome"] == "archived"


def test_campaign_from_playbook_brief_versioning_and_steps(authed: AuthedClient) -> None:
    playbooks = authed.get("/api/v1/campaigns/playbooks").json()["data"]
    assert {p["motion"] for p in playbooks} >= {"product_sale", "partnership_proposal", "pilot_offer"}
    c = create(authed, "/api/v1/campaigns", {"name": "Partners", "motion": "partnership_proposal"})
    assert c["step_days"] == [0, 3, 7, 12] and c["ready"] is False
    assert c["brief"]["cta"]["details"] == "an exploratory 30-minute conversation"
    assert {i["field"] for i in c["readiness"] if i["severity"] == "blocker"} >= {"offering", "goal"}
    r = authed.patch(f"/api/v1/campaigns/{c['id']}", json={"brief": {**c["brief"], "goal": "Find partners"}})
    assert r.json()["data"]["brief_version"] == 2
    r = authed.client.put(f"/api/v1/campaigns/{c['id']}/steps", json={"days": [0, 4, 10]},
                          headers={"X-CSRF-Token": authed.csrf})
    assert r.json()["data"]["step_days"] == [0, 4, 10]
    bad = authed.client.put(f"/api/v1/campaigns/{c['id']}/steps", json={"days": [1, 3]},
                            headers={"X-CSRF-Token": authed.csrf})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_steps"
    r = authed.patch(f"/api/v1/campaigns/{c['id']}", json={"brief": {"style": {"max_words": 5}}})
    assert r.status_code == 422


def test_activation_requires_complete_brief_and_mailbox(authed: AuthedClient) -> None:
    c = create(authed, "/api/v1/campaigns", {"name": "Clinics", "motion": "product_sale"})
    r = authed.post(f"/api/v1/campaigns/{c['id']}/activate")
    assert r.status_code == 409 and r.json()["error"]["code"] == "campaign_not_ready"
    ready = ready_campaign(authed)
    r = authed.post(f"/api/v1/campaigns/{ready['id']}/activate")
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_sending_mailbox"
    r = authed.post(f"/api/v1/campaigns/{ready['id']}/cancel")
    assert r.json()["data"]["status"] == "cancelled"
    assert authed.post(f"/api/v1/campaigns/{ready['id']}/resume").status_code == 409


# --- Enrollment ------------------------------------------------------------------------------------

def test_enrollment_eligibility_and_blockers(authed: AuthedClient, db: Session) -> None:
    c = ready_campaign(authed)
    no_email = create(authed, "/api/v1/prospects", {"full_name": "Priya Nair", "company_name": "Acme Labs"})["prospect"]
    suppressed = create(authed, "/api/v1/prospects", {"email": "s@acme.example", "full_name": "Sam Iyer"})["prospect"]
    authed.post(f"/api/v1/prospects/{suppressed['id']}/suppress", json={"reason": "unsubscribe"})
    waiting = create(authed, "/api/v1/prospects", {"email": "w@acme.example", "full_name": "Wen Li"})["prospect"]
    ready = researched_prospect(authed, db)
    r = authed.post(f"/api/v1/campaigns/{c['id']}/enroll", json={"prospect_ids": [
        no_email["id"], suppressed["id"], waiting["id"], str(ready.id)]})
    outcomes = {x["prospect_id"]: (x["outcome"], x["reason"]) for x in r.json()["data"]}
    assert outcomes[no_email["id"]] == ("skipped", "No email address.")
    assert outcomes[suppressed["id"]][0] == "skipped" and "Suppressed" in outcomes[suppressed["id"]][1]
    assert outcomes[waiting["id"]][0] == outcomes[str(ready.id)][0] == "enrolled"
    rows = {e["prospect_id"]: e for e in authed.get(f"/api/v1/campaigns/{c['id']}/prospects").json()["data"]}
    assert rows[waiting["id"]]["blocker"] == "Waiting for research to finish."
    assert rows[str(ready.id)]["state"] == "awaiting_draft"
    again = authed.post(f"/api/v1/campaigns/{c['id']}/enroll", json={"prospect_ids": [str(ready.id)]})
    assert again.json()["data"][0]["outcome"] == "already_enrolled"


def test_incomplete_brief_blocks_drafting(authed: AuthedClient, db: Session) -> None:
    c = create(authed, "/api/v1/campaigns", {"name": "Clinics", "motion": "product_sale"})
    p = researched_prospect(authed, db)
    authed.post(f"/api/v1/campaigns/{c['id']}/enroll", json={"prospect_ids": [str(p.id)]})
    row = authed.get(f"/api/v1/campaigns/{c['id']}/prospects").json()["data"][0]
    assert row["state"] == "enrolled" and "brief is incomplete" in row["blocker"]


# --- Drafting pipeline ------------------------------------------------------------------------------

def enrolled(authed: AuthedClient, db: Session) -> tuple[dict[str, Any], Prospect]:
    c = ready_campaign(authed)
    p = researched_prospect(authed, db)
    authed.post(f"/api/v1/campaigns/{c['id']}/enroll", json={"prospect_ids": [str(p.id)]})
    return c, p


def test_draft_generated_with_evidence_and_awaits_approval(authed: AuthedClient, db: Session) -> None:
    _, p = enrolled(authed, db)
    llm = FakeDraftLLM("good")
    result = run_draft_job(db, llm)
    assert result["0"]["passed"] is True and llm.writes == 1 and llm.reviews == 1
    assert "<<<RESEARCH " in llm.prompts[0] and "Penthus Clinical Documentation" in llm.prompts[0]
    draft = db.scalar(select(EmailDraft).where(EmailDraft.prospect_id == p.id))
    assert draft is not None and draft.status is DraftStatus.PENDING_REVIEW and draft.version == 1
    assert draft.body_text.startswith("Hi Lena,") and "Reply 'unsubscribe' to opt out." in draft.body_text
    assert draft.plan["rationale"]["why_now"] == "Rollout is in progress."
    assert draft.quality_scores["passed"] and float(draft.overall_score) > 0.8  # type: ignore[arg-type]
    links = db.scalars(select(EmailDraftEvidence).where(EmailDraftEvidence.draft_id == draft.id)).all()
    assert len(links) == 1 and "hospital information system" in links[0].claim_text
    cp = db.scalar(select(CampaignProspect).where(CampaignProspect.prospect_id == p.id))
    assert cp is not None and cp.state is EnrollmentState.AWAITING_APPROVAL and cp.fit.value == "strong"  # type: ignore[union-attr]
    detail = authed.get(f"/api/v1/drafts/{draft.id}").json()["data"]
    assert detail["evidence"][0]["cited"] is True and detail["evidence"][0]["sources"][0]["url"]


def test_unsupported_number_triggers_regeneration(authed: AuthedClient, db: Session) -> None:
    _, p = enrolled(authed, db)
    llm = FakeDraftLLM("bad_then_good")
    result = run_draft_job(db, llm)
    assert llm.writes == 2 and result["0"]["passed"] is True
    assert "YOUR PREVIOUS ATTEMPT FAILED REVIEW" in llm.prompts[1] and '"70%"' in llm.prompts[1]
    draft = db.scalar(select(EmailDraft).where(EmailDraft.prospect_id == p.id))
    assert draft is not None and "70%" not in draft.body_text


def test_disqualified_fit_is_held_for_a_human(authed: AuthedClient, db: Session) -> None:
    c, p = enrolled(authed, db)
    llm = FakeDraftLLM("disqualified")
    result = run_draft_job(db, llm)
    assert result["0"] == {"held": "disqualified"} and llm.reviews == 0  # no tokens spent reviewing
    assert db.scalar(select(EmailDraft).where(EmailDraft.prospect_id == p.id)) is None
    row = authed.get(f"/api/v1/campaigns/{c['id']}/prospects").json()["data"][0]
    assert row["fit"] == "disqualified" and "Draft anyway" in row["blocker"]
    counts = authed.post(f"/api/v1/campaigns/{c['id']}/generate", json={}).json()["data"]
    assert counts == {"held_for_review": 1}
    r = authed.post(f"/api/v1/prospects/{p.id}/drafts/generate", json={"campaign_id": c["id"], "force": True})
    assert r.status_code == 202 and r.json()["data"]["outcome"] == "drafting_queued"
    assert run_draft_job(db, FakeDraftLLM("disqualified"))["0"]["draft_id"]  # human override drafts anyway


def test_drafting_without_groq_fails_with_actionable_blocker(authed: AuthedClient, db: Session) -> None:
    c, _ = enrolled(authed, db)
    job = db.scalar(select(Job).where(Job.kind == "drafts.generate"))
    assert job is not None
    sessions = sessionmaker(bind=db.connection(), join_transaction_mode="create_savepoint", expire_on_commit=False)
    ctx = JobContext(job=ClaimedJob(job.id, job.kind, job.payload, job.workspace_id, 1, 3), sessions=sessions,
                     services={"drafting": DraftingServices(settings=get_settings(),
                                                            llm=LLMRouter(providers={}, chains={}))})
    with pytest.raises(PermanentJobError):
        asyncio.run(handle_draft(ctx))
    db.expire_all()
    row = authed.get(f"/api/v1/campaigns/{c['id']}/prospects").json()["data"][0]
    assert "GROQ_API_KEY" in row["blocker"]


# --- Review, edit, approval --------------------------------------------------------------------------

def drafted(authed: AuthedClient, db: Session, scenario: str = "good") -> tuple[dict[str, Any], dict[str, Any]]:
    c, p = enrolled(authed, db)
    run_draft_job(db, FakeDraftLLM(scenario))
    d = db.scalar(select(EmailDraft).where(EmailDraft.prospect_id == p.id, EmailDraft.is_current))
    assert d is not None
    return c, authed.get(f"/api/v1/drafts/{d.id}").json()["data"]


def test_approval_is_bound_to_exact_content_and_edits_invalidate_it(authed: AuthedClient, db: Session) -> None:
    _, d = drafted(authed, db)
    stale = authed.post(f"/api/v1/drafts/{d['id']}/approve", json={"content_sha256": "0" * 64})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "draft_content_changed"
    ok = authed.post(f"/api/v1/drafts/{d['id']}/approve", json={"content_sha256": d["content_sha256"]})
    assert ok.status_code == 200 and ok.json()["data"]["status"] == "approved"

    edited = authed.patch(f"/api/v1/drafts/{d['id']}", json={"base_version": 1, "sections": {
        "cta": "Open to a 20-minute call next week?"}})
    assert edited.status_code == 200, edited.text
    new = edited.json()["data"]
    assert new["version"] == 2 and new["status"] == "pending_review" and new["generated_by"] == "user"
    assert "Open to a 20-minute call next week?" in new["body_text"]
    old = db.get(EmailDraft, uuid.UUID(d["id"]))
    assert old is not None and old.status is DraftStatus.SUPERSEDED and not old.is_current  # approval gone
    conflict = authed.patch(f"/api/v1/drafts/{new['id']}", json={"base_version": 1, "subject": "x"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "draft_version_conflict"


def test_edit_introducing_unsupported_number_is_flagged_and_needs_acknowledgement(authed: AuthedClient,
                                                                                    db: Session) -> None:
    _, d = drafted(authed, db)
    new = authed.patch(f"/api/v1/drafts/{d['id']}", json={"base_version": 1, "sections": {
        "value": "We cut documentation time by 90%."}}).json()["data"]
    assert any(i["code"] == "unsupported_number" for i in new["validation_issues"])
    r = authed.post(f"/api/v1/drafts/{new['id']}/approve", json={"content_sha256": new["content_sha256"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "draft_has_issues"
    r = authed.post(f"/api/v1/drafts/{new['id']}/approve", json={"content_sha256": new["content_sha256"],
                                                                 "acknowledge_issues": True})
    assert r.status_code == 200


def test_bulk_approve_skips_flagged_and_requires_confirmation(authed: AuthedClient, db: Session) -> None:
    c, d = drafted(authed, db)
    p2 = researched_prospect(authed, db, email="arjun@northwind.example", name="Arjun Mehta")
    authed.post(f"/api/v1/campaigns/{c['id']}/enroll", json={"prospect_ids": [str(p2.id)]})
    run_draft_job(db, FakeDraftLLM("good"))
    d2 = db.scalar(select(EmailDraft).where(EmailDraft.prospect_id == p2.id))
    assert d2 is not None
    flagged = authed.patch(f"/api/v1/drafts/{d2.id}", json={"base_version": 1, "sections": {
        "value": "It saves 99% of time."}}).json()["data"]
    assert authed.post("/api/v1/drafts/bulk-approve", json={"draft_ids": [d["id"]], "confirm_reviewed": False}
                       ).status_code == 400
    r = authed.post("/api/v1/drafts/bulk-approve", json={"draft_ids": [d["id"], flagged["id"]],
                                                         "confirm_reviewed": True}).json()["data"]
    assert r["approved"] == [d["id"]] and r["skipped"][0]["id"] == flagged["id"]


def test_follow_ups_are_drafted_after_initial_approval(authed: AuthedClient, db: Session) -> None:
    c, d = drafted(authed, db)
    authed.post(f"/api/v1/drafts/{d['id']}/approve", json={"content_sha256": d["content_sha256"]})
    job = db.scalar(select(Job).where(Job.kind == "drafts.generate", Job.state == JobState.QUEUED))
    assert job is not None and job.payload["steps"] == [1, 2, 3]
    llm = FakeDraftLLM("good")
    result = run_draft_job(db, llm)
    assert set(result) == {"1", "2", "3"} and "FOLLOW-UP #1" in llm.prompts[0]
    queue = authed.get("/api/v1/drafts", params={"campaign_id": c["id"], "step": 1}).json()
    assert queue["meta"]["total"] == 1 and queue["data"][0]["subject"].startswith("Re:")


def test_reject_records_reason_and_regenerate_queues_a_new_version(authed: AuthedClient, db: Session) -> None:
    c, d = drafted(authed, db)
    r = authed.post(f"/api/v1/drafts/{d['id']}/reject", json={"reason": "Too salesy"})
    assert r.json()["data"]["status"] == "rejected"
    row = authed.get(f"/api/v1/campaigns/{c['id']}/prospects").json()["data"][0]
    assert "Too salesy" in row["blocker"]
    r = authed.post(f"/api/v1/drafts/{d['id']}/regenerate", json={"sections": ["cta"], "instruction": "Softer ask"})
    assert r.status_code == 202
    busy = authed.post(f"/api/v1/drafts/{d['id']}/regenerate", json={})
    assert busy.json()["error"]["code"] == "drafting_in_progress"
    llm = FakeDraftLLM("good")
    run_draft_job(db, llm)
    assert "REWRITE ONLY these sections: cta" in llm.prompts[0] and "Softer ask" in llm.prompts[0]
    versions = authed.get(f"/api/v1/drafts/{d['id']}").json()["data"]["versions"]
    assert [v["version"] for v in versions] == [2, 1]


def test_research_completion_starts_drafting_for_waiting_enrollment(authed: AuthedClient, db: Session) -> None:
    from app.services.research import store
    c = ready_campaign(authed)
    p = researched_prospect(authed, db, status=ResearchStatus.RESEARCHING_INDIVIDUAL)
    authed.post(f"/api/v1/campaigns/{c['id']}/enroll", json={"prospect_ids": [str(p.id)]})
    cp = db.scalar(select(CampaignProspect).where(CampaignProspect.prospect_id == p.id))
    assert cp is not None and cp.state is EnrollmentState.ENROLLED
    run = db.scalar(select(ResearchRun).where(ResearchRun.prospect_id == p.id))
    assert run is not None
    run.status = "succeeded"  # type: ignore[assignment]
    company = db.get(Company, p.company_id)
    assert company is not None
    company.research_status = ResearchStatus.READY
    db.commit()
    outcome = store.finalize_prospect(db, p.id)
    assert outcome is not None, outcome
    db.commit()
    db.expire_all()
    row = db.get(CampaignProspect, cp.id)
    # Two facts aren't enough evidence: the hook replaces "waiting" with an actionable blocker.
    assert outcome == "insufficient_evidence" and row is not None and row.state is EnrollmentState.ENROLLED
    assert row.blocker is not None and "Not enough verifiable evidence" in row.blocker
    # Once research is ready, the same hook queues drafting.
    from app.services.campaigns.service import on_research_finalized
    prospect = db.get(Prospect, p.id)
    assert prospect is not None
    prospect.research_status = ResearchStatus.READY
    assert on_research_finalized(db, p.id, get_settings()) == 1
    db.commit()
    db.expire_all()
    row = db.get(CampaignProspect, cp.id)
    assert row is not None and row.state is EnrollmentState.AWAITING_DRAFT


def test_drafts_and_campaigns_are_workspace_scoped(authed: AuthedClient, db: Session, client, make_user) -> None:  # type: ignore[no-untyped-def]
    c, d = drafted(authed, db)
    other = Workspace(name="Other")
    db.add(other)
    db.flush()
    outsider = make_user(email="outsider@example.com")
    outsider.workspace_id = other.id
    db.commit()
    client.cookies.clear()
    client.post("/api/v1/auth/login",
                json={"email": "outsider@example.com", "password": "correct horse battery staple"})
    assert client.get(f"/api/v1/drafts/{d['id']}").status_code == 404
    assert client.get(f"/api/v1/campaigns/{c['id']}").status_code == 404
    assert client.get("/api/v1/drafts").json()["meta"]["total"] == 0
