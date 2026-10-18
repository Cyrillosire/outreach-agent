"""End-to-end import workflow against PostgreSQL. All data is synthetic."""

from __future__ import annotations

import csv
import io
import time
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.states import ImportRowStatus
from app.models import AuditLog, Company, Prospect, ProspectImportRow, Workspace
from tests.conftest import AuthedClient

pytestmark = pytest.mark.integration

HEADERS = ["Full Name", "Job Title", "Company Name", "Individual Email", "Website", "Location", "Assigned To",
           "Other Emails (Company / General)"]


def make_csv(rows: list[list[str]], headers: list[str] = HEADERS) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    return buf.getvalue().encode()


def upload(a: AuthedClient, data: bytes, name: str = "contacts.csv", **kw):  # type: ignore[no-untyped-def]
    return a.post("/api/v1/imports", files={"file": (name, data, "text/csv")}, **kw)


SMALL = [
    ["Jane Doe", "Head of IT", "Acme Hospital", "jane@acmehospital.example", "acmehospital.example", "Delhi", "cyril",
     "info@acmehospital.example"],
    ["John Roe", "CFO", "Acme Hospital", "john@acmehospital.example", "", "Delhi", "adrian", ""],
    ["Jane Doe", "Head of IT", "Acme Hospital", "JANE@acmehospital.example", "", "", "", ""],   # dup in file
    ["No Email Person", "COO", "Beta Clinic", "N/A", "betaclinic.example", "", "", ""],         # no email: ok
    ["Bad Email", "CTO", "Gamma", "bad(at)gamma", "", "", "", ""],                               # invalid
    ["", "", "Company Only Ltd", "", "companyonly.example", "", "", ""],                         # invalid
]


def test_full_import_flow(authed: AuthedClient, db: Session) -> None:
    r = upload(authed, make_csv(SMALL))
    assert r.status_code == 201, r.text
    imp = r.json()["data"]
    assert imp["status"] == "uploaded" and imp["row_count"] == 6
    # Suggested mapping recognizes real-world headers and refuses to guess ambiguous ones.
    assert imp["column_mapping"]["Individual Email"] == "email"
    assert imp["column_mapping"]["Other Emails (Company / General)"] == "ignore"
    assert imp["column_mapping"]["Assigned To"] == "ignore"
    assert len(imp["sample_rows"]) == 5
    assert db.scalar(select(func.count()).select_from(Prospect)) == 0  # nothing imported yet

    mapping = {**imp["column_mapping"], "Assigned To": "attribute"}
    r = authed.put(f"/api/v1/imports/{imp['id']}/mapping", json={"mapping": mapping})
    assert r.status_code == 200, r.text
    summary = r.json()["data"]["validation_summary"]
    assert r.json()["data"]["status"] == "validated"
    assert summary["valid"] == 3 and summary["invalid"] == 2 and summary["duplicate_in_file"] == 1
    assert summary["without_email"] == 1
    assert summary["companies_new"] == 2  # acmehospital.example + betaclinic.example
    assert summary["issue_counts"]["no_person"] == 1

    rows = authed.get(f"/api/v1/imports/{imp['id']}/rows", params={"status": "invalid"}).json()
    assert rows["meta"]["total"] == 2
    messages = " ".join(i["message"] for row in rows["data"] for i in row["issues"])
    assert "Company-only rows" in messages and "Invalid email" in messages

    r = authed.post(f"/api/v1/imports/{imp['id']}/commit")
    assert r.status_code == 200, r.text
    done = r.json()["data"]
    assert done["status"] == "completed" and done["validation_summary"]["imported"] == 3
    assert done["validation_summary"]["companies_created"] == 2

    prospects = db.scalars(select(Prospect).order_by(Prospect.source_row_number)).all()
    assert [p.source_row_number for p in prospects] == [2, 3, 5]
    jane = prospects[0]
    assert jane.email_normalized == "jane@acmehospital.example"
    assert jane.attributes == {"assigned_to": "cyril", "company_domain_source": "company_website"}
    assert jane.location == "Delhi"
    assert jane.source_row["Other Emails (Company / General)"] == "info@acmehospital.example"  # provenance kept
    # John shares Jane's company via the email domain; company research will be reused.
    assert prospects[1].company_id == jane.company_id
    assert prospects[2].email is None and prospects[2].full_name == "No Email Person"

    imported = db.scalars(select(ProspectImportRow).where(ProspectImportRow.status == ImportRowStatus.IMPORTED)).all()
    assert {r.prospect_id for r in imported} == {p.id for p in prospects}
    actions = set(db.scalars(select(AuditLog.action).where(AuditLog.entity_id == uuid.UUID(imp["id"]))).all())
    assert {"import.upload", "import.map", "import.commit"} <= actions


def test_commit_is_idempotent_and_mapping_locked_after_commit(authed: AuthedClient, db: Session) -> None:
    imp = upload(authed, make_csv(SMALL[:2])).json()["data"]
    authed.put(f"/api/v1/imports/{imp['id']}/mapping", json={"mapping": imp["column_mapping"]})
    first = authed.post(f"/api/v1/imports/{imp['id']}/commit")
    second = authed.post(f"/api/v1/imports/{imp['id']}/commit")
    assert first.status_code == second.status_code == 200
    assert db.scalar(select(func.count()).select_from(Prospect)) == 2
    r = authed.put(f"/api/v1/imports/{imp['id']}/mapping", json={"mapping": imp["column_mapping"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "import_not_mappable"
    assert authed.post(f"/api/v1/imports/{imp['id']}/cancel").status_code == 409


def test_commit_requires_validation(authed: AuthedClient) -> None:
    imp = upload(authed, make_csv(SMALL[:1])).json()["data"]
    r = authed.post(f"/api/v1/imports/{imp['id']}/commit")
    assert r.status_code == 409 and r.json()["error"]["code"] == "import_not_committable"


def test_existing_prospects_are_never_overwritten(authed: AuthedClient, db: Session) -> None:
    first = upload(authed, make_csv(SMALL[:1])).json()["data"]
    authed.put(f"/api/v1/imports/{first['id']}/mapping", json={"mapping": first["column_mapping"]})
    authed.post(f"/api/v1/imports/{first['id']}/commit")

    changed = [["Jane Changed", "CEO", "Other Co", "jane@acmehospital.example", "", "", "", ""]]
    second = upload(authed, make_csv(changed)).json()["data"]
    summary = authed.put(f"/api/v1/imports/{second['id']}/mapping",
                         json={"mapping": second["column_mapping"]}).json()["data"]["validation_summary"]
    assert summary["already_exists"] == 1 and summary["valid"] == 0
    authed.post(f"/api/v1/imports/{second['id']}/commit")
    jane = db.scalar(select(Prospect).where(Prospect.email_normalized == "jane@acmehospital.example"))
    assert jane is not None and jane.full_name == "Jane Doe" and jane.title == "Head of IT"


def test_prospect_created_after_validation_is_not_duplicated(authed: AuthedClient, db: Session) -> None:
    """Race: someone adds the same email between validation and commit."""
    imp = upload(authed, make_csv(SMALL[:2])).json()["data"]
    authed.put(f"/api/v1/imports/{imp['id']}/mapping", json={"mapping": imp["column_mapping"]})
    r = authed.post("/api/v1/prospects", json={"email": "john@acmehospital.example", "full_name": "John Manual"})
    assert r.status_code == 201
    done = authed.post(f"/api/v1/imports/{imp['id']}/commit").json()["data"]
    assert done["validation_summary"]["imported"] == 1
    assert done["validation_summary"]["already_exists"] == 1
    assert db.scalar(select(func.count()).select_from(Prospect)) == 2


def test_possible_duplicate_against_existing_is_flagged_not_merged(authed: AuthedClient, db: Session) -> None:
    authed.post("/api/v1/prospects", json={"full_name": "Asha Rao", "company_name": "Delta Care"})
    rows = [["Asha Rao", "", "Delta Care", "", "", "", "", ""]]
    imp = upload(authed, make_csv(rows)).json()["data"]
    authed.put(f"/api/v1/imports/{imp['id']}/mapping", json={"mapping": imp["column_mapping"]})
    issues = authed.get(f"/api/v1/imports/{imp['id']}/rows").json()["data"][0]["issues"]
    assert "possible_existing_duplicate" in {i["code"] for i in issues}
    authed.post(f"/api/v1/imports/{imp['id']}/commit")
    assert db.scalar(select(func.count()).select_from(Prospect).where(Prospect.full_name == "Asha Rao")) == 2
    # Domainless company matched by exact name rather than duplicated.
    assert db.scalar(select(func.count()).select_from(Company).where(Company.name == "Delta Care")) == 1


@pytest.mark.parametrize("name,data,message", [
    ("people.xlsx", b"PK\x03\x04", "Save the sheet as CSV"),
    ("people.pdf", b"%PDF", "Upload a .csv"),
    ("people.csv", b"", "empty"),
    ("people.csv", b"Name,Email\n", "no data rows"),
])
def test_upload_rejections(authed: AuthedClient, name: str, data: bytes, message: str) -> None:
    r = upload(authed, data, name=name)
    assert r.status_code == 422 and message in r.json()["error"]["message"]


def test_upload_size_limit(authed: AuthedClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "import_max_file_mb", 1)
    r = upload(authed, b"Name\n" + b"x" * (1024 * 1024 + 10))
    assert r.status_code == 422 and "larger than 1 MB" in r.json()["error"]["message"]


def test_invalid_mapping_rejected(authed: AuthedClient) -> None:
    imp = upload(authed, make_csv(SMALL[:1])).json()["data"]
    r = authed.put(f"/api/v1/imports/{imp['id']}/mapping",
                   json={"mapping": {"Full Name": "email", "Individual Email": "email"}})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_mapping"


def test_upload_idempotency_key_replays(authed: AuthedClient) -> None:
    data = make_csv(SMALL[:1])
    h = {"Idempotency-Key": "upload-key-0001"}
    a, b = upload(authed, data, headers=h), upload(authed, data, headers=h)
    assert a.status_code == b.status_code == 201
    assert a.json()["data"]["id"] == b.json()["data"]["id"]
    assert b.headers.get("idempotent-replayed") == "true"
    c = upload(authed, make_csv(SMALL[:2]), headers=h)
    assert c.status_code == 422 and c.json()["error"]["code"] == "idempotency_key_reused"


def test_imports_are_workspace_isolated(authed: AuthedClient, db: Session, client, make_user) -> None:  # type: ignore[no-untyped-def]
    imp = upload(authed, make_csv(SMALL[:1])).json()["data"]
    other_ws = Workspace(name="Other")
    db.add(other_ws)
    db.flush()
    outsider = make_user(email="outsider@example.com")
    outsider.workspace_id = other_ws.id
    db.commit()
    client.cookies.clear()
    r = client.post("/api/v1/auth/login", json={"email": "outsider@example.com",
                                                "password": "correct horse battery staple"})
    csrf = r.json()["data"]["csrf_token"]
    assert client.get(f"/api/v1/imports/{imp['id']}").status_code == 404
    assert client.post(f"/api/v1/imports/{imp['id']}/commit", headers={"X-CSRF-Token": csrf}).status_code == 404
    assert client.get("/api/v1/imports").json()["data"] == []


def test_mutations_require_csrf(authed: AuthedClient) -> None:
    r = authed.client.post("/api/v1/imports", files={"file": ("a.csv", make_csv(SMALL[:1]), "text/csv")})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"


def test_150_prospect_batch_with_company_reuse(authed: AuthedClient, db: Session) -> None:
    """Spec E2E step 1: a 150-prospect CSV; colleagues at one company share one company record."""
    rows = []
    for i in range(150):
        company = i % 40
        rows.append([f"Person {i}", "Director", f"Hospital {company}", f"person{i}@hospital{company}.example",
                     f"https://www.hospital{company}.example", "India", "", ""])
    start = time.perf_counter()
    imp = upload(authed, make_csv(rows)).json()["data"]
    summary = authed.put(f"/api/v1/imports/{imp['id']}/mapping",
                         json={"mapping": imp["column_mapping"]}).json()["data"]["validation_summary"]
    assert summary["valid"] == 150 and summary["companies_new"] == 40
    done = authed.post(f"/api/v1/imports/{imp['id']}/commit").json()["data"]
    elapsed = time.perf_counter() - start
    assert done["validation_summary"]["imported"] == 150
    assert db.scalar(select(func.count()).select_from(Company)) == 40
    assert db.scalar(select(func.count(func.distinct(Prospect.company_id)))) == 40
    assert elapsed < 10, f"150-row import took {elapsed:.1f}s"
