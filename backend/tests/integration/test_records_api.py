"""Deletion, deduplication, merging and bulk actions against PostgreSQL."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.states import (
    DraftStatus,
    DraftType,
    EnrollmentState,
    FactKind,
    FactVerification,
    MessageDirection,
    ResearchStatus,
    ResearchSubject,
)
from app.models import (
    Campaign,
    CampaignProspect,
    Company,
    EmailAccount,
    EmailDraft,
    EmailDraftEvidence,
    EmailMessage,
    Prospect,
    ResearchFact,
    ResearchRun,
    Suppression,
    Workspace,
)
from tests.conftest import AuthedClient
from tests.integration.test_imports_api import make_csv, upload

pytestmark = pytest.mark.integration


def create(a: AuthedClient, **body):  # type: ignore[no-untyped-def]
    r = a.post("/api/v1/prospects", json=body)
    assert r.status_code == 201, r.text
    return r.json()["data"]["prospect"]


def count(db: Session, model, *where) -> int:  # type: ignore[no-untyped-def]
    return int(db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def add_draft_with_evidence(db: Session, prospect: Prospect, *, company_fact: bool) -> EmailDraft:
    campaign = db.scalar(select(Campaign).where(Campaign.workspace_id == prospect.workspace_id)) or Campaign(
        workspace_id=prospect.workspace_id, name="C")
    db.add(campaign)
    db.flush()
    run = ResearchRun(workspace_id=prospect.workspace_id,
                      subject_type=ResearchSubject.COMPANY if company_fact else ResearchSubject.PROSPECT,
                      company_id=prospect.company_id if company_fact else None,
                      prospect_id=None if company_fact else prospect.id)
    db.add(run)
    db.flush()
    fact = ResearchFact(workspace_id=prospect.workspace_id, research_run_id=run.id,
                        company_id=prospect.company_id if company_fact else None,
                        prospect_id=None if company_fact else prospect.id, category="technology", claim="c",
                        kind=FactKind.VERIFIED_FACT, verification_status=FactVerification.SINGLE_SOURCE,
                        confidence=0.9, relevance=0.9)
    db.add(fact)
    db.flush()
    sha = hashlib.sha256(b"x").hexdigest()
    draft =EmailDraft(workspace_id=prospect.workspace_id, campaign_id=campaign.id, prospect_id=prospect.id,
                       draft_type=DraftType.INITIAL, subject="s", body_text="b", content_sha256=sha)
    db.add(draft)
    db.flush()
    db.add(EmailDraftEvidence(draft_id=draft.id, fact_id=fact.id, claim_text="claim"))
    db.commit()
    return draft


# --- Deletion ------------------------------------------------------------------------------------

def test_delete_contact_removes_dependents_but_keeps_suppression(authed: AuthedClient, db: Session) -> None:
    p = create(authed, email="ann@acme.example", full_name="Ann Lee", company_name="Acme")
    pid = uuid.UUID(p["id"])
    authed.post(f"/api/v1/prospects/{pid}/suppress", json={"reason": "unsubscribe"})
    prospect = db.get(Prospect, pid)
    assert prospect is not None
    draft = add_draft_with_evidence(db, prospect, company_fact=False)
    account = EmailAccount(workspace_id=prospect.workspace_id, provider="gmail", provider_account_id="sub",
                           email_address="me@x.example")
    db.add(account)
    db.flush()
    db.add(EmailMessage(workspace_id=prospect.workspace_id, email_account_id=account.id, prospect_id=pid,
                        draft_id=draft.id, direction=MessageDirection.INBOUND, from_address="ann@acme.example"))
    db.commit()

    r = authed.client.delete(f"/api/v1/prospects/{pid}", headers={"X-CSRF-Token": authed.csrf})
    assert r.status_code == 200 and r.json()["data"]["affected"] == 1
    db.expire_all()
    assert db.get(Prospect, pid) is None
    assert count(db, EmailDraft, EmailDraft.prospect_id == pid) == 0
    assert count(db, ResearchFact, ResearchFact.prospect_id == pid) == 0
    assert count(db, EmailMessage) == 0  # message history (personal data) removed
    assert count(db, Suppression, Suppression.email_normalized == "ann@acme.example") == 1  # opt-out survives
    # Re-adding the same person is still blocked from outreach.
    again = create(authed, email="ann@acme.example", full_name="Ann Lee")
    detail = authed.get(f"/api/v1/prospects/{again['id']}").json()["data"]
    assert "suppressed" in {b["code"] for b in detail["blockers"]}


def test_delete_company_keep_contacts_invalidates_drafts(authed: AuthedClient, db: Session) -> None:
    p = create(authed, email="ann@acme.example", full_name="Ann Lee", company_name="Acme")
    prospect = db.get(Prospect, uuid.UUID(p["id"]))
    assert prospect is not None and prospect.company_id
    company_id = prospect.company_id
    prospect.research_status = ResearchStatus.READY
    db.commit()
    draft = add_draft_with_evidence(db, prospect, company_fact=True)

    r = authed.client.delete(f"/api/v1/companies/{company_id}", headers={"X-CSRF-Token": authed.csrf})
    assert r.status_code == 200 and r.json()["meta"]["contacts_deleted"] == 0
    db.expire_all()
    kept = db.get(Prospect, prospect.id)
    assert kept is not None and kept.company_id is None
    assert kept.research_status is ResearchStatus.NEEDS_REVIEW  # company evidence is gone
    stale = db.get(EmailDraft, draft.id)
    assert stale is not None and stale.status is DraftStatus.SUPERSEDED and not stale.is_current
    assert stale.validation_issues[-1]["code"] == "evidence_deleted"


def test_delete_company_with_contacts(authed: AuthedClient, db: Session) -> None:
    a = create(authed, email="ann@acme.example", full_name="Ann", company_name="Acme")
    create(authed, email="ben@acme.example", full_name="Ben")
    company_id = authed.get(f"/api/v1/prospects/{a['id']}").json()["data"]["company"]["id"]
    r = authed.client.delete(f"/api/v1/companies/{company_id}?delete_contacts=true",
                             headers={"X-CSRF-Token": authed.csrf})
    assert r.json()["meta"]["contacts_deleted"] == 2
    assert count(db, Prospect) == 0 and count(db, Company) == 0


def test_bulk_delete_is_workspace_scoped(authed: AuthedClient, db: Session) -> None:
    mine = [create(authed, email=f"u{i}@acme.example", full_name=f"U {i}")["id"] for i in range(3)]
    other = Workspace(name="Other")
    db.add(other)
    db.flush()
    foreign = Prospect(workspace_id=other.id, full_name="Not yours", company_id=None)
    db.add(foreign)
    db.commit()
    r = authed.post("/api/v1/prospects/bulk-delete", json={"ids": [*mine[:2], str(foreign.id)]})
    assert r.status_code == 200 and r.json()["data"]["affected"] == 2
    assert db.get(Prospect, foreign.id) is not None
    assert authed.post("/api/v1/prospects/bulk-delete", json={"ids": []}).status_code == 422


# --- Dedupe on entry -----------------------------------------------------------------------------

def test_manual_create_rejects_same_name_at_same_company(authed: AuthedClient) -> None:
    first = create(authed, full_name="Jane Doe", company_name="Acme Health")
    r = authed.post("/api/v1/prospects", json={"full_name": "  jane DOE ", "company_name": "ACME   health",
                                               "email": "jane@acmehealth.example"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "prospect_duplicate"
    assert r.json()["error"]["details"]["prospect_id"] == first["id"]
    # Same name at a different company is a different person.
    assert authed.post("/api/v1/prospects", json={"full_name": "Jane Doe", "company_name": "Beta"}).status_code == 201


def test_editing_into_a_duplicate_is_rejected(authed: AuthedClient) -> None:
    create(authed, full_name="Jane Doe", company_name="Acme")
    other = create(authed, full_name="John Roe", company_name="Acme")
    r = authed.patch(f"/api/v1/prospects/{other['id']}", json={"full_name": "Jane Doe"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "prospect_duplicate"


def test_companies_deduplicate_by_exact_name(authed: AuthedClient, db: Session) -> None:
    create(authed, full_name="A", company_name="Northwind Health")                              # name only
    create(authed, email="b@northwind.example", full_name="B", company_name="NORTHWIND  health")  # adopts domain
    create(authed, full_name="C", company_name="Northwind Health", company_domain="northwind.example")
    companies = db.scalars(select(Company)).all()
    assert len(companies) == 1 and companies[0].normalized_domain == "northwind.example"


def test_same_name_different_domains_are_kept_apart_and_flagged(authed: AuthedClient, db: Session) -> None:
    create(authed, email="a@apollo-in.example", full_name="A", company_name="Apollo Hospitals")
    create(authed, email="b@apollo-uk.example", full_name="B", company_name="Apollo Hospitals")
    assert count(db, Company) == 2
    groups = authed.get("/api/v1/duplicates").json()["data"]["companies"]
    assert len(groups) == 1 and groups[0]["domain_conflict"] is True and len(groups[0]["records"]) == 2


def test_import_unifies_name_only_rows_with_domain_rows(authed: AuthedClient, db: Session) -> None:
    rows = [["Ann Lee", "CTO", "Acme Care", "ann@acmecare.example", "", "", "", ""],
            ["Ben Roy", "CFO", "acme care", "", "", "", "", ""]]
    imp = upload(authed, make_csv(rows)).json()["data"]
    summary = authed.put(f"/api/v1/imports/{imp['id']}/mapping",
                         json={"mapping": imp["column_mapping"]}).json()["data"]["validation_summary"]
    assert summary["companies_new"] == 1
    authed.post(f"/api/v1/imports/{imp['id']}/commit")
    assert count(db, Company) == 1
    assert len({p.company_id for p in db.scalars(select(Prospect)).all()}) == 1


# --- Existing duplicates & merging ---------------------------------------------------------------

def seed_existing_duplicates(db: Session, workspace_id: uuid.UUID) -> tuple[Company, Company, Prospect, Prospect]:
    """Records created before dedupe rules existed (bypassing the API)."""
    c1 = Company(workspace_id=workspace_id, name="Delta Care")
    c2 = Company(workspace_id=workspace_id, name="delta  care", normalized_domain="deltacare.example",
                 industry="Hospitals")
    db.add_all([c1, c2])
    db.flush()
    p1 = Prospect(workspace_id=workspace_id, company_id=c1.id, full_name="Asha Rao", title="CIO")
    p2 = Prospect(workspace_id=workspace_id, company_id=c2.id, full_name="asha rao", email="asha@deltacare.example",
                  email_normalized="asha@deltacare.example", location="Pune")
    db.add_all([p1, p2])
    db.commit()
    return c1, c2, p1, p2


def test_duplicates_page_flags_existing_records(authed: AuthedClient, db: Session) -> None:
    seed_existing_duplicates(db, authed.user.workspace_id)  # type: ignore[attr-defined]
    data = authed.get("/api/v1/duplicates").json()["data"]
    assert len(data["companies"]) == 1 and data["companies"][0]["domain_conflict"] is False
    assert len(data["contacts"]) == 1 and len(data["contacts"][0]["records"]) == 2


def test_merge_companies_then_contacts(authed: AuthedClient, db: Session) -> None:
    c1, c2, p1, p2 = seed_existing_duplicates(db, authed.user.workspace_id)  # type: ignore[attr-defined]
    campaign = Campaign(workspace_id=c1.workspace_id, name="C")
    db.add(campaign)
    db.flush()
    db.add(CampaignProspect(campaign_id=campaign.id, prospect_id=p2.id, state=EnrollmentState.ENROLLED,
                            enrolled_at=datetime.now(UTC)))
    db.commit()

    r = authed.post("/api/v1/companies/merge", json={"survivor_id": str(c1.id), "duplicate_ids": [str(c2.id)]})
    assert r.status_code == 200
    db.expire_all()
    survivor = db.get(Company, c1.id)
    assert survivor is not None and survivor.normalized_domain == "deltacare.example"
    assert survivor.industry == "Hospitals" and db.get(Company, c2.id) is None

    r = authed.post("/api/v1/prospects/merge", json={"survivor_id": str(p1.id), "duplicate_ids": [str(p2.id)]})
    assert r.status_code == 200
    db.expire_all()
    kept = db.get(Prospect, p1.id)
    assert kept is not None and kept.title == "CIO"                        # survivor's values win
    assert kept.email_normalized == "asha@deltacare.example"               # blanks filled from duplicate
    assert kept.location == "Pune" and db.get(Prospect, p2.id) is None
    assert count(db, CampaignProspect, CampaignProspect.prospect_id == p1.id) == 1  # enrollment moved over
    assert authed.get("/api/v1/duplicates").json()["data"] == {"companies": [], "contacts": []}


def test_merge_validation(authed: AuthedClient, db: Session) -> None:
    c1, _, _, _ = seed_existing_duplicates(db, authed.user.workspace_id)  # type: ignore[attr-defined]
    r = authed.post("/api/v1/companies/merge", json={"survivor_id": str(c1.id), "duplicate_ids": [str(c1.id)]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "nothing_to_merge"
    r = authed.post("/api/v1/companies/merge", json={"survivor_id": str(c1.id), "duplicate_ids": [str(uuid.uuid4())]})
    assert r.status_code == 404


# --- Bulk edit & companies list --------------------------------------------------------------------

def test_bulk_update_contacts(authed: AuthedClient, db: Session) -> None:
    ids = [create(authed, email=f"u{i}@acme.example", full_name=f"User {i}",
                  attributes={"tier": "B", "old": "x"})["id"] for i in range(3)]
    r = authed.post("/api/v1/prospects/bulk-update", json={"ids": ids, "changes": {
        "industry": "Hospitals", "attributes_set": {"tier": "A"}, "attributes_remove": ["old"]}})
    assert r.status_code == 200 and r.json()["data"]["affected"] == 3
    for p in db.scalars(select(Prospect)).all():
        assert p.industry == "Hospitals" and p.attributes.get("tier") == "A" and "old" not in p.attributes
    assert authed.post("/api/v1/prospects/bulk-update", json={"ids": ids, "changes": {}}).status_code == 400


def test_bulk_update_reports_per_record_failures(authed: AuthedClient) -> None:
    create(authed, full_name="Jane Doe", company_name="Beta")
    a = create(authed, full_name="Jane Doe", company_name="Acme")
    b = create(authed, full_name="John Roe", company_name="Acme")
    r = authed.post("/api/v1/prospects/bulk-update", json={"ids": [a["id"], b["id"]],
                                                            "changes": {"company_name": "Beta"}})
    data = r.json()["data"]
    assert data["affected"] == 1 and data["failed"][0]["id"] == a["id"]  # would duplicate Jane Doe @ Beta


def test_companies_list_edit_and_bulk(authed: AuthedClient) -> None:
    create(authed, email="a@acme.example", full_name="A", company_name="Acme")
    create(authed, email="b@acme.example", full_name="B")
    create(authed, full_name="C", company_name="Beta Clinic")
    listing = authed.get("/api/v1/companies", params={"sort": "-contacts"}).json()
    assert listing["meta"]["total"] == 2 and listing["data"][0]["contacts"] == 2
    acme, beta = listing["data"][0]["id"], listing["data"][1]["id"]
    r = authed.patch(f"/api/v1/companies/{beta}", json={"name": "ACME"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "company_name_exists"
    r = authed.patch(f"/api/v1/companies/{beta}", json={"normalized_domain": "https://www.betaclinic.example/x"})
    assert r.json()["data"]["normalized_domain"] == "betaclinic.example"
    r = authed.post("/api/v1/companies/bulk-update", json={"ids": [acme, beta], "changes": {"industry": "Hospitals"}})
    assert r.json()["data"]["affected"] == 2
    r = authed.post("/api/v1/companies/bulk-delete", json={"ids": [acme, beta], "delete_contacts": False})
    assert r.json()["data"]["affected"] == 2
    assert authed.get("/api/v1/prospects").json()["meta"]["total"] == 3  # contacts kept, unlinked


def test_bulk_research(authed: AuthedClient) -> None:
    ids = [create(authed, email=f"u{i}@acme.example", full_name=f"U{i}")["id"] for i in range(2)]
    r = authed.post("/api/v1/prospects/bulk-research", json={"ids": ids})
    assert r.status_code == 200 and r.json()["data"]["affected"] == 2
