"""Mailbox connection (OAuth state binding, token custody) and the human-approved send path, end to end."""

from __future__ import annotations

import asyncio
import uuid
from email import message_from_bytes
from email.policy import default
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.api.v1.mailboxes import get_mail_services
from app.core.config import get_settings
from app.core.crypto import CredentialCipher
from app.domain.states import (
    EmailAccountStatus,
    EmailProvider,
    EnrollmentState,
    JobState,
    OutboundStatus,
)
from app.integrations.email.base import (
    MailboxAuthError,
    MailboxIdentity,
    MailboxUnavailable,
    SendOutcomeUnknown,
    SendResult,
    TokenSet,
)
from app.jobs.queue import ClaimedJob
from app.jobs.worker import JobContext, RetryableJobError
from app.models import CampaignProspect, EmailAccount, EmailMessage, Job, OAuthState
from app.services.mailboxes import MailServices, handle_send
from tests.conftest import AuthedClient
from tests.integration.test_campaigns_drafts import drafted

pytestmark = pytest.mark.integration
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


class FakeGmail:
    provider = EmailProvider.GMAIL
    scopes = ("openid", "email", SEND_SCOPE)
    required_scopes = (SEND_SCOPE,)
    configured = True

    def __init__(self) -> None:
        self.granted = (SEND_SCOPE, "openid")
        self.verifiers: list[str] = []
        self.sent: list[bytes] = []
        self.revoked: list[str] = []
        self.refresh_error: Exception | None = None
        self.send_error: Exception | None = None

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str,
                          login_hint: str | None = None) -> str:
        return f"https://accounts.example/auth?state={state}&code_challenge={code_challenge}&redirect_uri={redirect_uri}"

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> TokenSet:
        self.verifiers.append(code_verifier)
        return TokenSet("access-1", 3600, "refresh-SECRET-1", self.granted)

    async def refresh(self, refresh_token: str) -> TokenSet:
        if self.refresh_error:
            raise self.refresh_error
        assert refresh_token == "refresh-SECRET-1"
        return TokenSet("access-2", 3600, None, ())

    async def identity(self, access_token: str) -> MailboxIdentity:
        return MailboxIdentity("google-sub-1", "asha@penthus.example", "Asha Rao")

    async def send(self, access_token: str, mime: bytes) -> SendResult:
        if self.send_error:
            raise self.send_error
        self.sent.append(mime)
        return SendResult("gmail-msg-1", "gmail-thread-1")

    async def revoke(self, refresh_token: str) -> bool:
        self.revoked.append(refresh_token)
        return True


@pytest.fixture
def gmail(authed: AuthedClient) -> FakeGmail:
    fake = FakeGmail()
    settings = get_settings()
    cipher = CredentialCipher.from_config(settings.credential_encryption_keys.get_secret_value(),
                                          settings.credential_encryption_active_key)
    svc = MailServices(settings=settings, providers={EmailProvider.GMAIL: fake}, cipher=cipher)  # type: ignore[dict-item]
    authed.client.app.dependency_overrides[get_mail_services] = lambda: svc  # type: ignore[attr-defined]
    fake.svc = svc  # type: ignore[attr-defined]
    return fake


def start(a: AuthedClient) -> str:
    r = a.post("/api/v1/mailboxes/oauth/gmail/start", json={})
    assert r.status_code == 200, r.text
    return parse_qs(urlsplit(r.json()["data"]["authorization_url"]).query)["state"][0]


def callback(a: AuthedClient, state: str, **extra: str) -> dict[str, list[str]]:
    r = a.client.get("/api/v1/mailboxes/oauth/gmail/callback", params={"code": "auth-code", "state": state, **extra},
                     follow_redirects=False)
    assert r.status_code == 303, r.text
    return {"path": [urlsplit(r.headers["location"]).path], **parse_qs(urlsplit(r.headers["location"]).query)}


def connect(a: AuthedClient) -> dict[str, Any]:
    assert callback(a, start(a)).get("connected") == ["asha@penthus.example"]
    return a.get("/api/v1/mailboxes").json()["data"][0]


def run_send(db: Session, gmail: FakeGmail, *, attempts: int = 1, max_attempts: int = 6) -> Any:
    job = db.scalar(select(Job).where(Job.kind == "email.send", Job.state == JobState.QUEUED).order_by(Job.created_at))
    assert job is not None, "no send job queued"
    sessions = sessionmaker(bind=db.connection(), join_transaction_mode="create_savepoint", expire_on_commit=False)
    ctx = JobContext(job=ClaimedJob(job.id, job.kind, job.payload, job.workspace_id, attempts, max_attempts),
                     sessions=sessions, services={"mail": gmail.svc})  # type: ignore[attr-defined]
    try:
        return asyncio.run(handle_send(ctx))
    finally:
        db.expire_all()
        job.state = JobState.SUCCEEDED
        db.commit()


def approved_draft(a: AuthedClient, db: Session) -> tuple[dict[str, Any], dict[str, Any]]:
    c, d = drafted(a, db)
    r = a.post(f"/api/v1/drafts/{d['id']}/approve", json={"content_sha256": d["content_sha256"]})
    assert r.status_code == 200, r.text
    for job in db.scalars(select(Job).where(Job.kind == "drafts.generate")).all():  # follow-up drafting not run
        job.state = JobState.SUCCEEDED
    db.commit()
    return c, r.json()["data"]


# --- Connecting --------------------------------------------------------------------------------------

def test_connect_binds_state_and_encrypts_the_token(authed: AuthedClient, db: Session, gmail: FakeGmail) -> None:
    providers = authed.get("/api/v1/mailboxes/providers").json()["data"]
    assert providers["providers"][0]["redirect_uri"] == "http://localhost:3000/api/v1/mailboxes/oauth/gmail/callback"
    state = start(authed)
    row = db.scalar(select(OAuthState))
    assert row is not None and state not in (row.state_hash, row.encrypted_code_verifier)  # only a hash at rest
    assert callback(authed, state) == {"path": ["/settings/mailboxes"], "connected": ["asha@penthus.example"]}
    assert len(gmail.verifiers[0]) >= 43  # PKCE verifier sent on exchange

    account = db.scalar(select(EmailAccount))
    assert account is not None and account.status is EmailAccountStatus.CONNECTED
    assert account.provider_account_id == "google-sub-1" and SEND_SCOPE in account.granted_scopes
    raw = db.execute(text("SELECT encrypted_refresh_token FROM email_accounts")).scalar_one()
    assert "refresh-SECRET-1" not in raw  # never stored in plaintext
    cipher: CredentialCipher = gmail.svc.cipher  # type: ignore[attr-defined]
    assert cipher.decrypt(raw, f"email_account:{account.id}:refresh_token") == "refresh-SECRET-1"
    listed = authed.get("/api/v1/mailboxes").json()["data"]
    assert "refresh" not in str(listed).lower().replace("granted_scopes", "")  # tokens never leave the server


def test_callback_rejects_replay_expiry_and_declined_consent(authed: AuthedClient, db: Session,
                                                             gmail: FakeGmail) -> None:
    state = start(authed)
    callback(authed, state)
    assert callback(authed, state)["error"] == ["state_reused"]  # single use
    assert callback(authed, "forged-state")["error"] == ["invalid_state"]
    expiring = start(authed)
    db.execute(text("UPDATE oauth_states SET expires_at = now() - interval '1 minute' WHERE consumed_at IS NULL"))
    db.commit()
    assert callback(authed, expiring)["error"] == ["state_expired"]
    declined = authed.client.get("/api/v1/mailboxes/oauth/gmail/callback",
                                 params={"error": "access_denied", "state": start(authed)}, follow_redirects=False)
    assert parse_qs(urlsplit(declined.headers["location"]).query)["error"] == ["consent_declined"]


def test_callback_from_another_session_never_connects(authed: AuthedClient, db: Session, gmail: FakeGmail,
                                                      make_user) -> None:  # type: ignore[no-untyped-def]
    state = start(authed)  # started by the owner...
    make_user(email="colleague@example.com")
    authed.client.cookies.clear()
    authed.client.post("/api/v1/auth/login", json={"email": "colleague@example.com",
                                                   "password": "correct horse battery staple"})
    assert callback(authed, state)["error"] == ["session_mismatch"]  # ...completed in someone else's session
    assert db.scalar(select(EmailAccount)) is None


def test_callback_without_login_redirects_to_login(authed: AuthedClient, gmail: FakeGmail) -> None:
    state = start(authed)
    authed.client.cookies.clear()
    loc = callback(authed, state)
    assert loc["path"] == ["/login"] and loc["next"] == ["/settings/mailboxes"]


def test_missing_send_permission_is_refused(authed: AuthedClient, db: Session, gmail: FakeGmail) -> None:
    gmail.granted = ("openid", "email")  # user unticked "send email on your behalf"
    assert callback(authed, start(authed))["error"] == ["missing_scope"]
    assert db.scalar(select(EmailAccount)) is None


# --- Sending -------------------------------------------------------------------------------------------

def test_send_approved_draft_end_to_end(authed: AuthedClient, db: Session, gmail: FakeGmail) -> None:
    mailbox = connect(authed)
    _, d = approved_draft(authed, db)
    r = authed.post(f"/api/v1/drafts/{d['id']}/send", json={"email_account_id": mailbox["id"]})
    assert r.status_code == 202, r.text
    msg = r.json()["data"]
    assert msg["status"] == "queued" and msg["to_addresses"] == ["lena@northwind.example"]
    again = authed.post(f"/api/v1/drafts/{d['id']}/send", json={"email_account_id": mailbox["id"]}).json()["data"]
    assert again["id"] == msg["id"]  # double click: same logical send
    cp = db.scalar(select(CampaignProspect))
    assert cp is not None and cp.state is EnrollmentState.SCHEDULED

    assert run_send(db, gmail) == {"outcome": "provider_accepted", "provider_message_id": "gmail-msg-1"}
    sent = message_from_bytes(gmail.sent[0], policy=default)
    assert sent["To"].addresses[0].addr_spec == "lena@northwind.example"
    assert sent["From"].addresses[0].display_name == "Asha Rao" and sent["Subject"] == d["subject"]
    assert sent["Message-ID"] == msg["internet_message_id"]
    assert sent["List-Unsubscribe"] == "<mailto:asha@penthus.example?subject=unsubscribe>"
    assert sent.get_content().replace("\r\n", "\n").strip() == d["body_text"].strip()  # exactly what was approved

    status = authed.get(f"/api/v1/email-messages/{msg['id']}").json()["data"]
    assert status["status"] == "provider_accepted" and status["provider_message_id"] == "gmail-msg-1"
    db.refresh(cp)
    assert cp.state is EnrollmentState.IN_SEQUENCE
    detail = authed.get(f"/api/v1/drafts/{d['id']}").json()["data"]
    assert detail["sends"][0]["status"] == "provider_accepted"
    assert authed.post(f"/api/v1/drafts/{d['id']}/send", json={}).json()["data"]["id"] == msg["id"]
    assert len(gmail.sent) == 1  # never twice


def test_unapproved_or_edited_drafts_cannot_be_sent(authed: AuthedClient, db: Session, gmail: FakeGmail) -> None:
    mailbox = connect(authed)
    _, d = drafted(authed, db)
    r = authed.post(f"/api/v1/drafts/{d['id']}/send", json={"email_account_id": mailbox["id"]})
    assert r.status_code == 409 and "isn't approved" in r.json()["error"]["message"]
    authed.post(f"/api/v1/drafts/{d['id']}/approve", json={"content_sha256": d["content_sha256"]})
    edited = authed.patch(f"/api/v1/drafts/{d['id']}", json={"base_version": 1, "subject": "New subject"}).json()
    r = authed.post(f"/api/v1/drafts/{d['id']}/send", json={"email_account_id": mailbox["id"]})
    assert r.status_code == 409 and "newer version" in r.json()["error"]["message"]
    r = authed.post(f"/api/v1/drafts/{edited['data']['id']}/send", json={"email_account_id": mailbox["id"]})
    assert r.status_code == 409  # the edit removed the approval


def test_suppression_after_queuing_stops_the_send(authed: AuthedClient, db: Session, gmail: FakeGmail) -> None:
    mailbox = connect(authed)
    _, d = approved_draft(authed, db)
    msg = authed.post(f"/api/v1/drafts/{d['id']}/send", json={"email_account_id": mailbox["id"]}).json()["data"]
    authed.post(f"/api/v1/prospects/{d['prospect_id']}/suppress", json={"reason": "unsubscribe"})
    result = run_send(db, gmail)
    assert result["outcome"] == "not_sent" and gmail.sent == []
    m = db.get(EmailMessage, uuid.UUID(msg["id"]))
    assert m is not None and m.outbound_status is OutboundStatus.FAILED and "Suppressed" in (m.last_error or "")


def test_unknown_outcome_is_recorded_and_never_retried(authed: AuthedClient, db: Session, gmail: FakeGmail) -> None:
    mailbox = connect(authed)
    _, d = approved_draft(authed, db)
    msg = authed.post(f"/api/v1/drafts/{d['id']}/send", json={"email_account_id": mailbox["id"]}).json()["data"]
    gmail.send_error = SendOutcomeUnknown("Gmail send interrupted: ReadTimeout")
    assert run_send(db, gmail) == {"outcome": "unknown"}
    m = db.get(EmailMessage, uuid.UUID(msg["id"]))
    assert m is not None and m.outbound_status is OutboundStatus.UNKNOWN
    assert db.scalar(select(Job).where(Job.kind == "email.send", Job.state == JobState.QUEUED)) is None


def test_failure_before_the_request_is_retried_then_given_up(authed: AuthedClient, db: Session,
                                                              gmail: FakeGmail) -> None:
    mailbox = connect(authed)
    _, d = approved_draft(authed, db)
    msg = authed.post(f"/api/v1/drafts/{d['id']}/send", json={"email_account_id": mailbox["id"]}).json()["data"]
    gmail.send_error = MailboxUnavailable("Gmail unreachable: ConnectError")
    with pytest.raises(RetryableJobError):
        run_send(db, gmail)
    m = db.get(EmailMessage, uuid.UUID(msg["id"]))
    assert m is not None and m.outbound_status is OutboundStatus.QUEUED  # nothing left: safe to retry
    db.execute(text("UPDATE jobs SET state = 'queued' WHERE kind = 'email.send'"))
    db.commit()
    assert run_send(db, gmail, attempts=6, max_attempts=6)["outcome"] == "not_sent"
    db.refresh(m)
    cp = db.scalar(select(CampaignProspect))
    assert m.outbound_status is OutboundStatus.FAILED
    assert cp is not None and cp.state is EnrollmentState.AWAITING_APPROVAL  # back for a human decision


def test_revoked_grant_marks_mailbox_for_reconnection(authed: AuthedClient, db: Session, gmail: FakeGmail) -> None:
    mailbox = connect(authed)
    _, d = approved_draft(authed, db)
    authed.post(f"/api/v1/drafts/{d['id']}/send", json={"email_account_id": mailbox["id"]})
    gmail.refresh_error = MailboxAuthError("Google authorization is no longer valid (invalid_grant)")
    assert run_send(db, gmail)["outcome"] == "not_sent" and gmail.sent == []
    listed = authed.get("/api/v1/mailboxes").json()["data"][0]
    assert listed["status"] == "needs_reauth" and "invalid_grant" in listed["last_error"]
    r = authed.post(f"/api/v1/mailboxes/{mailbox['id']}/test-send")
    assert r.status_code == 409  # can't send until reconnected
    connect(authed)  # reconnecting the same Google account restores it
    assert authed.get("/api/v1/mailboxes").json()["data"][0]["status"] == "connected"


def test_daily_limit(authed: AuthedClient, db: Session, gmail: FakeGmail, monkeypatch: pytest.MonkeyPatch) -> None:
    mailbox = connect(authed)
    _, d = approved_draft(authed, db)
    monkeypatch.setattr(get_settings(), "mailbox_daily_send_limit", 1)
    authed.post(f"/api/v1/mailboxes/{mailbox['id']}/test-send")
    r = authed.post(f"/api/v1/drafts/{d['id']}/send", json={"email_account_id": mailbox["id"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "daily_limit_reached"


def test_test_send_goes_only_to_the_mailbox_itself(authed: AuthedClient, db: Session, gmail: FakeGmail) -> None:
    mailbox = connect(authed)
    r = authed.post(f"/api/v1/mailboxes/{mailbox['id']}/test-send")
    assert r.status_code == 202 and r.json()["data"]["to_addresses"] == ["asha@penthus.example"]
    assert run_send(db, gmail)["outcome"] == "provider_accepted"
    sent = message_from_bytes(gmail.sent[0], policy=default)
    assert sent["To"].addresses[0].addr_spec == "asha@penthus.example" and "test email" in sent["Subject"]


def test_campaign_mailbox_activation_and_disconnect(authed: AuthedClient, db: Session, gmail: FakeGmail) -> None:
    mailbox = connect(authed)
    c, _ = approved_draft(authed, db)
    r = authed.patch(f"/api/v1/campaigns/{c['id']}", json={"sending_account_id": mailbox["id"]})
    assert r.json()["data"]["sending_account_email"] == "asha@penthus.example"
    assert authed.post(f"/api/v1/campaigns/{c['id']}/activate").json()["data"]["status"] == "active"
    r = authed.client.delete(f"/api/v1/mailboxes/{mailbox['id']}", headers={"X-CSRF-Token": authed.csrf})
    assert r.json()["data"] == {"provider_revoked": True} and gmail.revoked == ["refresh-SECRET-1"]
    account = db.get(EmailAccount, uuid.UUID(mailbox["id"]))
    db.refresh(account)
    assert account is not None and account.encrypted_refresh_token is None
    assert account.status is EmailAccountStatus.DISCONNECTED


def test_mailboxes_are_workspace_scoped(authed: AuthedClient, db: Session, gmail: FakeGmail, client,  # type: ignore[no-untyped-def]
                                        make_user) -> None:
    from app.models import Workspace
    mailbox = connect(authed)
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
    assert client.get("/api/v1/mailboxes").json()["data"] == []
    assert client.post(f"/api/v1/mailboxes/{mailbox['id']}/test-send",
                       headers={"X-CSRF-Token": csrf}).status_code == 404
