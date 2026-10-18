from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.domain.states import AuditOutcome, ResearchStatus, UserRole
from app.models import AuditLog, Prospect, UserSession, Workspace

pytestmark = pytest.mark.integration
PASSWORD = "correct horse battery staple"


def login(client: TestClient, email: str, password: str = PASSWORD):  # type: ignore[no-untyped-def]
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def test_health_endpoints(client: TestClient, fake_redis) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200
    fake_redis.healthy = False
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"] == {"database": "ok", "redis": "fail"}


def test_login_sets_hardened_cookies_and_me_works(client: TestClient, make_user) -> None:  # type: ignore[no-untyped-def]
    user = make_user(email="Admin@Example.com")
    r = login(client, "  admin@EXAMPLE.com ")
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["user"]["email"] == "admin@example.com"
    cookies = r.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith("oa_session="))
    csrf_cookie = next(c for c in cookies if c.startswith("oa_csrf="))
    assert "HttpOnly" in session_cookie and "SameSite=lax" in session_cookie
    assert "HttpOnly" not in csrf_cookie
    assert body["csrf_token"] in csrf_cookie

    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200 and me.json()["data"]["id"] == str(user.id)


def test_session_token_is_stored_only_as_hash(client: TestClient, make_user, db: Session) -> None:  # type: ignore[no-untyped-def]
    make_user(email="a@example.com")
    login(client, "a@example.com")
    raw = client.cookies.get("oa_session")
    stored = db.scalars(select(UserSession.token_hash)).all()
    assert raw and raw not in stored and len(stored[-1]) == 64


@pytest.mark.parametrize("email,password", [("a@example.com", "wrong password!!"), ("nobody@example.com", PASSWORD)])
def test_bad_credentials_get_identical_generic_error(client: TestClient, make_user, email, password) -> None:  # type: ignore[no-untyped-def]
    make_user(email="a@example.com")
    r = login(client, email, password)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_credentials"
    assert r.json()["error"]["message"] == "Invalid email or password"


def test_inactive_user_cannot_log_in(client: TestClient, make_user, db: Session) -> None:  # type: ignore[no-untyped-def]
    user = make_user(email="a@example.com")
    user.is_active = False
    db.commit()
    assert login(client, "a@example.com").status_code == 401


def test_login_rate_limited(client: TestClient, make_user) -> None:  # type: ignore[no-untyped-def]
    make_user(email="a@example.com")
    limit = get_settings().login_rate_limit_attempts
    for _ in range(limit):
        assert login(client, "a@example.com", "wrong password!!").status_code == 401
    r = login(client, "a@example.com", PASSWORD)  # even the right password is blocked once limited
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0


def test_validation_errors_never_echo_password(client: TestClient) -> None:
    r = client.post("/api/v1/auth/login", json={"email": "not-an-email", "password": "SuperSecret123!"})
    assert r.status_code == 422
    assert "SuperSecret123!" not in r.text


def test_logout_requires_csrf_and_revokes_session(client: TestClient, make_user) -> None:  # type: ignore[no-untyped-def]
    make_user(email="a@example.com")
    csrf = login(client, "a@example.com").json()["data"]["csrf_token"]

    r = client.post("/api/v1/auth/logout")
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    assert client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "forged"}).status_code == 403

    session_cookie = client.cookies.get("oa_session")
    assert client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
    # Replaying the old cookie after logout must fail server-side, not just because the browser forgot it.
    client.cookies.set("oa_session", session_cookie)
    assert client.get("/api/v1/auth/me").status_code == 401


def test_expired_session_rejected(client: TestClient, make_user, db: Session) -> None:  # type: ignore[no-untyped-def]
    make_user(email="a@example.com")
    login(client, "a@example.com")
    for s in db.scalars(select(UserSession)).all():
        s.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert client.get("/api/v1/auth/me").status_code == 401


def test_cross_origin_login_rejected(client: TestClient, make_user) -> None:  # type: ignore[no-untyped-def]
    make_user(email="a@example.com")
    r = client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_protected_endpoints_require_auth(client: TestClient) -> None:
    for path in ("/api/v1/auth/me", "/api/v1/dashboard/summary", "/api/v1/system/status"):
        r = client.get(path)
        assert r.status_code == 401 and r.json()["error"]["code"] == "not_authenticated"


def test_auth_events_are_audited(client: TestClient, make_user, db: Session) -> None:  # type: ignore[no-untyped-def]
    make_user(email="a@example.com")
    login(client, "a@example.com", "wrong password!!")
    login(client, "a@example.com")
    rows = db.scalars(select(AuditLog).where(AuditLog.action == "auth.login").order_by(AuditLog.created_at)).all()
    assert [r.outcome for r in rows][-2:] == [AuditOutcome.FAILURE, AuditOutcome.SUCCESS]
    assert all("password" not in str(r.metadata_) for r in rows)


def test_dashboard_summary_is_live_and_workspace_scoped(client: TestClient, make_user, db: Session) -> None:  # type: ignore[no-untyped-def]
    user = make_user(email="a@example.com")
    other = Workspace(name="Someone else")
    db.add(other)
    db.flush()
    db.add_all([
        Prospect(workspace_id=user.workspace_id, email_normalized="p1@x.com"),
        Prospect(workspace_id=user.workspace_id, email_normalized="p2@x.com",
                 research_status=ResearchStatus.NEEDS_REVIEW),
        Prospect(workspace_id=other.id, email_normalized="p3@x.com"),  # must not be counted
    ])
    db.commit()
    login(client, "a@example.com")
    data = client.get("/api/v1/dashboard/summary").json()["data"]
    assert data["prospects_total"] == 2
    assert data["research_by_status"]["pending"] == 1
    assert data["research_by_status"]["needs_review"] == 1
    assert data["drafts_awaiting_approval"] == 0
    assert data["email_accounts"] == []


def test_system_status(client: TestClient, make_user) -> None:  # type: ignore[no-untyped-def]
    make_user(email="a@example.com", role=UserRole.MEMBER)
    login(client, "a@example.com")
    data = client.get("/api/v1/system/status").json()["data"]
    assert data["database"] is True and data["redis"] is True
    assert data["schema_up_to_date"] is True
    assert data["credential_encryption_configured"] is True


def test_openapi_schema_is_served(client: TestClient) -> None:
    spec = client.get("/api/openapi.json").json()
    assert "/api/v1/auth/login" in spec["paths"]
    assert "/api/v1/dashboard/summary" in spec["paths"]
