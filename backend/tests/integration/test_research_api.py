from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.states import (
    FactKind,
    FactVerification,
    JobState,
    ResearchRunStatus,
    ResearchStatus,
    ResearchSubject,
    SourceType,
)
from app.models import (
    Company,
    Job,
    Prospect,
    ResearchFact,
    ResearchFactEvidence,
    ResearchRun,
    ResearchSource,
    Workspace,
)
from tests.conftest import AuthedClient
from tests.integration.test_imports_api import make_csv, upload

pytestmark = pytest.mark.integration


def create(a: AuthedClient, **body):  # type: ignore[no-untyped-def]
    r = a.post("/api/v1/prospects", json=body)
    assert r.status_code == 201, r.text
    return r.json()["data"]["prospect"]


def jobs(db: Session, kind: str | None = None) -> list[Job]:
    stmt = select(Job).where(Job.state == JobState.QUEUED)
    if kind:
        stmt = stmt.where(Job.kind == kind)
    return list(db.scalars(stmt).all())


def test_manual_create_auto_queues_research(authed: AuthedClient, db: Session) -> None:
    p = create(authed, email="lena@northwind.example", full_name="Lena Okafor", company_name="Northwind")
    assert p["research_status"] == "pending"
    assert {j.kind for j in jobs(db)} == {"research.company", "research.prospect"}


def test_start_research_is_async_and_idempotent(authed: AuthedClient, db: Session) -> None:
    p = create(authed, email="a@northwind.example", full_name="Ann")
    r = authed.post(f"/api/v1/prospects/{p['id']}/research", json={})
    assert r.status_code == 202
    body = r.json()["data"]
    assert body["already_running"] is True and body["research_status"] == "pending"
    assert len(jobs(db, "research.prospect")) == 1


def test_colleagues_share_one_company_job(authed: AuthedClient, db: Session) -> None:
    create(authed, email="a@northwind.example", full_name="Ann")
    create(authed, email="b@northwind.example", full_name="Ben")
    assert len(jobs(db, "research.company")) == 1 and len(jobs(db, "research.prospect")) == 2


def _seed_facts(db: Session, prospect_id: uuid.UUID, company_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    now = datetime.now(UTC)
    src = ResearchSource(workspace_id=workspace_id, url="https://northwind.example/news/x",
                         url_normalized="https://northwind.example/news/x", title="News", publisher="northwind.example",
                         source_type=SourceType.PRESS_RELEASE, accessed_at=now, extracted_text="text",
                         content_sha256="0" * 64)
    db.add(src)
    db.flush()
    for subject in ("company", "prospect"):
        run = ResearchRun(workspace_id=workspace_id, subject_type=ResearchSubject(subject),
                          company_id=company_id if subject == "company" else None,
                          prospect_id=prospect_id if subject == "prospect" else None,
                          status=ResearchRunStatus.SUCCEEDED, sufficiency={"reason": "Both sufficient."})
        db.add(run)
        db.flush()
        fact = ResearchFact(workspace_id=workspace_id, research_run_id=run.id,
                            company_id=company_id if subject == "company" else None,
                            prospect_id=prospect_id if subject == "prospect" else None,
                            category="technology" if subject == "company" else "current_role",
                            claim=f"{subject} claim", kind=FactKind.VERIFIED_FACT,
                            verification_status=FactVerification.CORROBORATED, confidence=0.9, relevance=0.8)
        db.add(fact)
        db.flush()
        db.add(ResearchFactEvidence(fact_id=fact.id, source_id=src.id, excerpt=f"{subject} excerpt", accessed_at=now))
    db.commit()


def test_get_research_returns_facts_with_evidence(authed: AuthedClient, db: Session) -> None:
    p = create(authed, email="a@northwind.example", full_name="Ann", company_name="Northwind")
    prospect = db.get(Prospect, uuid.UUID(p["id"]))
    assert prospect is not None and prospect.company_id
    _seed_facts(db, prospect.id, prospect.company_id, prospect.workspace_id)
    data = authed.get(f"/api/v1/prospects/{p['id']}/research").json()["data"]
    assert data["in_progress"] is True and data["outcome_reason"] == "Both sufficient."
    fact = data["individual_facts"][0]
    assert fact["claim"] == "prospect claim" and fact["verification_status"] == "corroborated"
    assert fact["evidence"][0]["source"]["url"] == "https://northwind.example/news/x"
    assert fact["evidence"][0]["excerpt"] == "prospect excerpt"
    assert data["company"]["facts"][0]["category"] == "technology"
    company = authed.get(f"/api/v1/companies/{prospect.company_id}").json()["data"]
    assert company["facts"][0]["claim"] == "company claim"


def test_expired_facts_are_hidden(authed: AuthedClient, db: Session) -> None:
    p = create(authed, email="a@northwind.example", full_name="Ann", company_name="Northwind")
    prospect = db.get(Prospect, uuid.UUID(p["id"]))
    _seed_facts(db, prospect.id, prospect.company_id, prospect.workspace_id)  # type: ignore[union-attr,arg-type]
    for f in db.scalars(select(ResearchFact)).all():
        f.expires_at = datetime(2020, 1, 1, tzinfo=UTC)
    db.commit()
    assert authed.get(f"/api/v1/prospects/{p['id']}/research").json()["data"]["individual_facts"] == []


def test_run_detail_and_retry_rules(authed: AuthedClient, db: Session) -> None:
    p = create(authed, email="a@northwind.example", full_name="Ann")
    pid = uuid.UUID(p["id"])
    for job in db.scalars(select(Job)).all():  # simulate the original attempt having finished
        job.state = JobState.DEAD
    prospect = db.get(Prospect, pid)
    prospect.research_status = ResearchStatus.FAILED_RETRYABLE  # type: ignore[union-attr]
    failed = ResearchRun(workspace_id=prospect.workspace_id, subject_type=ResearchSubject.PROSPECT,  # type: ignore[union-attr]
                         prospect_id=pid, status=ResearchRunStatus.FAILED, error={"message": "LLM unavailable"})
    ok = ResearchRun(workspace_id=prospect.workspace_id, subject_type=ResearchSubject.PROSPECT,  # type: ignore[union-attr]
                     prospect_id=pid, status=ResearchRunStatus.SUCCEEDED)
    db.add_all([failed, ok])
    db.commit()

    detail = authed.get(f"/api/v1/research-runs/{failed.id}").json()["data"]
    assert detail["status"] == "failed" and detail["error"]["message"] == "LLM unavailable"
    r = authed.post(f"/api/v1/research-runs/{failed.id}/retry")
    assert r.status_code == 202 and r.json()["data"]["research_status"] == "pending"
    assert len(jobs(db, "research.prospect")) == 1
    r = authed.post(f"/api/v1/research-runs/{ok.id}/retry")
    assert r.status_code == 409 and r.json()["error"]["code"] == "run_not_retryable"


def test_retry_failed_requeues_every_failed_prospect(authed: AuthedClient, db: Session) -> None:
    ids = [uuid.UUID(create(authed, email=f"{n}@northwind.example", full_name=n)["id"]) for n in ("ann", "ben")]
    for job in db.scalars(select(Job)).all():
        job.state = JobState.DEAD
    for pid in ids:
        db.get(Prospect, pid).research_status = ResearchStatus.FAILED_RETRYABLE  # type: ignore[union-attr]
    db.commit()
    r = authed.post("/api/v1/research/retry-failed")
    assert r.status_code == 202 and r.json()["data"]["requeued"] == 2
    assert len(jobs(db, "research.prospect")) == 2


def test_overview_reports_providers_and_queue(authed: AuthedClient) -> None:
    create(authed, email="a@northwind.example", full_name="Ann")
    data = authed.get("/api/v1/research/overview").json()["data"]
    assert data["providers"]["groq"] is False and data["providers"]["tavily"] is False  # no keys in tests
    assert data["jobs_by_state"]["queued"] >= 1
    assert data["prospects_by_status"]["pending"] >= 1
    assert data["llm_extraction_chain"][0].startswith("groq:")


def test_import_commit_queues_research(authed: AuthedClient, db: Session) -> None:
    rows = [["Ann Lee", "CTO", "Northwind", "ann@northwind.example", "", "", "", ""],
            ["Ben Roy", "CFO", "Northwind", "ben@northwind.example", "", "", "", ""]]
    imp = upload(authed, make_csv(rows)).json()["data"]
    authed.put(f"/api/v1/imports/{imp['id']}/mapping", json={"mapping": imp["column_mapping"]})
    done = authed.post(f"/api/v1/imports/{imp['id']}/commit").json()["data"]
    assert done["validation_summary"]["research_queued"] == 2
    assert len(jobs(db, "research.prospect")) == 2 and len(jobs(db, "research.company")) == 1


def test_research_endpoints_are_workspace_scoped(authed: AuthedClient, db: Session, client, make_user) -> None:  # type: ignore[no-untyped-def]
    p = create(authed, email="a@northwind.example", full_name="Ann")
    other = Workspace(name="Other")
    db.add(other)
    db.flush()
    outsider = make_user(email="outsider@example.com")
    outsider.workspace_id = other.id
    db.commit()
    client.cookies.clear()
    login = client.post("/api/v1/auth/login", json={"email": "outsider@example.com",
                                                    "password": "correct horse battery staple"})
    csrf = login.json()["data"]["csrf_token"]
    assert client.get(f"/api/v1/prospects/{p['id']}/research").status_code == 404
    assert client.post(f"/api/v1/prospects/{p['id']}/research", json={},
                       headers={"X-CSRF-Token": csrf}).status_code == 404
    assert client.get("/api/v1/research/overview").json()["data"]["recent_runs"] == []
    assert db.scalar(select(func.count()).select_from(Company)) >= 0
