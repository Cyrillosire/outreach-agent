from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.states import (
    EnrollmentState,
    ResearchStatus,
    ScheduledActionState,
    ScheduledActionType,
)
from app.models import AuditLog, Campaign, CampaignProspect, Prospect, ScheduledAction, Suppression
from tests.conftest import AuthedClient

pytestmark = pytest.mark.integration


def create(a: AuthedClient, **body):  # type: ignore[no-untyped-def]
    r = a.post("/api/v1/prospects", json=body)
    assert r.status_code == 201, r.text
    return r.json()["data"]["prospect"]


def test_create_validates_like_imports(authed: AuthedClient) -> None:
    r = authed.post("/api/v1/prospects", json={"email": "not-an-email", "full_name": "X"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "prospect_invalid"
    assert r.json()["error"]["details"][0]["code"] == "email_invalid"
    r = authed.post("/api/v1/prospects", json={"company_name": "Only Company"})
    assert r.status_code == 422 and r.json()["error"]["details"][0]["code"] == "no_person"

    r = authed.post("/api/v1/prospects", json={"email": "Asha@Delta.example", "full_name": "Asha Rao",
                                                "linkedin_url": "https://linkedin.com/company/delta"})
    assert r.status_code == 201
    body = r.json()["data"]
    assert body["prospect"]["email_normalized"] == "asha@delta.example"
    assert body["prospect"]["linkedin_url"] is None
    assert {w["code"] for w in body["warnings"]} == {"linkedin_not_profile"}


def test_duplicate_email_conflict_points_to_existing(authed: AuthedClient) -> None:
    p = create(authed, email="a@acme.example", full_name="A")
    r = authed.post("/api/v1/prospects", json={"email": "A@ACME.example", "full_name": "A again"})
    assert r.status_code == 409 and r.json()["error"]["details"]["prospect_id"] == p["id"]


def test_create_idempotency(authed: AuthedClient, db: Session) -> None:
    h = {"Idempotency-Key": "create-prospect-1"}
    body = {"full_name": "No Email Person", "company_name": "Acme"}  # no email => no natural dedupe
    a = authed.post("/api/v1/prospects", json=body, headers=h)
    b = authed.post("/api/v1/prospects", json=body, headers=h)
    assert a.status_code == b.status_code == 201 and a.json() == b.json()
    assert db.scalar(select(Prospect.id).where(Prospect.full_name == "No Email Person").limit(2)) is not None
    assert len(db.scalars(select(Prospect).where(Prospect.full_name == "No Email Person")).all()) == 1
    assert authed.post("/api/v1/prospects", json=body, headers={"Idempotency-Key": "bad key!"}).status_code == 400


def test_list_search_filter_sort_paginate(authed: AuthedClient, db: Session) -> None:
    create(authed, email="zoe@acme.example", full_name="Zoe Adams", title="CTO", company_name="Acme")
    create(authed, email="bob@beta.example", full_name="Bob Brown", title="CFO", company_name="Beta")
    nomail = create(authed, full_name="Cara Cole", company_name="Acme", company_domain="acme.example")
    db.get(Prospect, uuid.UUID(nomail["id"])).research_status = ResearchStatus.NEEDS_REVIEW  # type: ignore[union-attr]
    db.commit()

    def names(**params):  # type: ignore[no-untyped-def]
        r = authed.get("/api/v1/prospects", params=params)
        assert r.status_code == 200, r.text
        return [p["full_name"] for p in r.json()["data"]], r.json()["meta"]

    assert names(q="acme")[0] == ["Cara Cole", "Zoe Adams"] or set(names(q="acme")[0]) == {"Cara Cole", "Zoe Adams"}
    assert names(q="cfo")[0] == ["Bob Brown"]
    assert names(q="100%")[0] == []  # LIKE wildcards are escaped
    assert names(has_email="false")[0] == ["Cara Cole"]
    assert names(research_status="needs_review")[0] == ["Cara Cole"]
    assert names(sort="name")[0] == ["Bob Brown", "Cara Cole", "Zoe Adams"]
    page, meta = names(sort="name", page=2, page_size=2)
    assert page == ["Zoe Adams"] and meta == {"page": 2, "page_size": 2, "total": 3}
    # Same company record for both Acme rows (one via email domain, one via explicit domain).
    items = authed.get("/api/v1/prospects", params={"q": "acme"}).json()["data"]
    assert len({i["company_id"] for i in items}) == 1


def test_detail_shows_provenance_blockers_and_empty_histories(authed: AuthedClient) -> None:
    p = create(authed, full_name="Cara Cole", company_name="Acme", company_domain="acme.example")
    d = authed.get(f"/api/v1/prospects/{p['id']}").json()["data"]
    assert d["company"]["normalized_domain"] == "acme.example"
    assert d["provenance"]["source"] == "manual"
    assert {b["code"] for b in d["blockers"]} == {"no_email", "research_incomplete", "not_enrolled"}
    assert d["drafts"] == d["messages"] == d["scheduled_actions"] == []
    assert authed.get(f"/api/v1/prospects/{uuid.uuid4()}").status_code == 404


def test_patch_updates_only_supplied_fields(authed: AuthedClient, db: Session) -> None:
    p = create(authed, email="a@acme.example", full_name="Ann Lee", title="CTO")
    r = authed.patch(f"/api/v1/prospects/{p['id']}", json={"title": "  Chief   Technology Officer ",
                                                            "company_domain": "https://www.newco.example/x"})
    assert r.status_code == 200, r.text
    out = r.json()["data"]["prospect"]
    assert out["title"] == "Chief Technology Officer" and out["full_name"] == "Ann Lee"
    assert out["email"] == "a@acme.example"
    detail = authed.get(f"/api/v1/prospects/{p['id']}").json()["data"]
    assert detail["company"]["normalized_domain"] == "newco.example"
    audit = db.scalar(select(AuditLog).where(AuditLog.action == "prospect.update"))
    assert audit is not None and set(audit.metadata_["fields"]) == {"title", "company"}


def test_patch_email_conflict_and_validation(authed: AuthedClient) -> None:
    a = create(authed, email="a@acme.example", full_name="A")
    create(authed, email="b@acme.example", full_name="B")
    assert authed.patch(f"/api/v1/prospects/{a['id']}", json={"email": "B@acme.example"}).status_code == 409
    assert authed.patch(f"/api/v1/prospects/{a['id']}", json={"email": "nope"}).status_code == 422
    assert authed.patch(f"/api/v1/prospects/{a['id']}", json={"linkedin_url": "https://x.example"}).status_code == 422
    r = authed.patch(f"/api/v1/prospects/{a['id']}", json={"email": None, "full_name": None})
    assert r.status_code == 422 and r.json()["error"]["details"][0]["code"] == "no_person"


def _enroll_with_actions(db: Session, workspace_id: uuid.UUID, prospect_id: uuid.UUID,
                         action_states: list[ScheduledActionState]) -> CampaignProspect:
    campaign = Campaign(workspace_id=workspace_id, name="C")
    db.add(campaign)
    db.flush()
    enrollment = CampaignProspect(campaign_id=campaign.id, prospect_id=prospect_id,
                                  state=EnrollmentState.IN_SEQUENCE, enrolled_at=datetime.now(UTC))
    db.add(enrollment)
    db.flush()
    for i, state in enumerate(action_states):
        db.add(ScheduledAction(workspace_id=workspace_id, action_type=ScheduledActionType.SEND_EMAIL,
                               idempotency_key=f"send:{enrollment.id}:step:{i}", campaign_id=campaign.id,
                               campaign_prospect_id=enrollment.id, prospect_id=prospect_id,
                               due_at=datetime.now(UTC), state=state))
    db.commit()
    return enrollment


def test_suppress_stops_enrollment_and_cancels_only_cancellable_actions(authed: AuthedClient, db: Session) -> None:
    p = create(authed, email="a@acme.example", full_name="A")
    enrollment = _enroll_with_actions(db, authed.user.workspace_id, uuid.UUID(p["id"]),  # type: ignore[attr-defined]
                                      [ScheduledActionState.PENDING, ScheduledActionState.EXECUTING])
    r = authed.post(f"/api/v1/prospects/{p['id']}/suppress", json={"reason": "unsubscribe", "note": "asked by phone"})
    assert r.status_code == 201, r.text
    body = r.json()["data"]
    assert body["created"] is True and body["enrollments_stopped"] == 1 and body["actions_cancelled"] == 1

    db.expire_all()
    assert db.get(CampaignProspect, enrollment.id).state is EnrollmentState.SUPPRESSED  # type: ignore[union-attr]
    states = sorted(a.state.value for a in db.scalars(select(ScheduledAction)).all())
    assert states == ["cancelled", "executing"]  # in-flight send is left to reconciliation, never "cancelled"

    again = authed.post(f"/api/v1/prospects/{p['id']}/suppress", json={"reason": "unsubscribe"})
    assert again.status_code == 200 and again.json()["data"]["created"] is False
    assert len(db.scalars(select(Suppression)).all()) == 1

    detail = authed.get(f"/api/v1/prospects/{p['id']}").json()["data"]
    assert detail["suppressions"][0]["reason"] == "unsubscribe"
    assert "suppressed" in {b["code"] for b in detail["blockers"]}
    listed = authed.get("/api/v1/prospects", params={"suppressed": "true"}).json()["data"]
    assert [x["outreach_status"] for x in listed] == ["suppressed"]


def test_domain_suppression_rules(authed: AuthedClient) -> None:
    gmail = create(authed, email="someone@gmail.com", full_name="G")
    r = authed.post(f"/api/v1/prospects/{gmail['id']}/suppress", json={"target": "domain"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "freemail_domain"

    a = create(authed, email="a@acme.example", full_name="A")
    b = create(authed, full_name="B", company_name="Acme", company_domain="acme.example")
    assert authed.post(f"/api/v1/prospects/{b['id']}/suppress", json={}).json()["error"]["code"] == "no_email"
    r = authed.post(f"/api/v1/prospects/{a['id']}/suppress", json={"target": "domain", "reason": "legal"})
    assert r.status_code == 201 and r.json()["data"]["suppression"]["domain"] == "acme.example"
    # The colleague without an email is covered by the company-domain suppression.
    assert "suppressed" in {x["code"] for x in authed.get(f"/api/v1/prospects/{b['id']}").json()["data"]["blockers"]}


def test_system_only_reasons_rejected(authed: AuthedClient) -> None:
    p = create(authed, email="a@acme.example", full_name="A")
    r = authed.post(f"/api/v1/prospects/{p['id']}/suppress", json={"reason": "hard_bounce"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_reason"


def test_campaign_scope_requires_own_campaign(authed: AuthedClient) -> None:
    p = create(authed, email="a@acme.example", full_name="A")
    r = authed.post(f"/api/v1/prospects/{p['id']}/suppress", json={"scope": "campaign"})
    assert r.status_code == 400
    r = authed.post(f"/api/v1/prospects/{p['id']}/suppress",
                    json={"scope": "campaign", "campaign_id": str(uuid.uuid4())})
    assert r.status_code == 404


def test_company_detail(authed: AuthedClient) -> None:
    a = create(authed, email="a@acme.example", full_name="Ann", company_name="Acme")
    create(authed, email="b@acme.example", full_name="Ben")
    company_id = authed.get(f"/api/v1/prospects/{a['id']}").json()["data"]["company"]["id"]
    d = authed.get(f"/api/v1/companies/{company_id}").json()["data"]
    assert d["name"] == "Acme" and [p["full_name"] for p in d["prospects"]] == ["Ann", "Ben"]
    assert authed.get(f"/api/v1/companies/{uuid.uuid4()}").status_code == 404
