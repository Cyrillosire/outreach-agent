"""Connected mailboxes: OAuth connection, token custody, and the human-approved send path.

Sending is a durable job (`email.send`). One EmailMessage row is one logical send, keyed by an idempotency key, so a
double click or a worker retry can never send twice. Everything that could make a send wrong is re-checked right
before the provider call: approval still bound to this exact content, contact not suppressed, mailbox healthy, daily
cap. A send whose outcome is unknown (timeout after the request left) is marked `unknown` and never retried here.
"""

from __future__ import annotations

import functools
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.crypto import CredentialCipher, CredentialCryptoError
from app.core.errors import AppError, Conflict, NotFound
from app.core.logging import get_logger
from app.db.transitions import StaleTransition, transition_row
from app.domain.states import (
    EMAIL_ACCOUNT,
    ENROLLMENT,
    OUTBOUND,
    ActorType,
    AuditOutcome,
    DraftStatus,
    EmailAccountStatus,
    EmailProvider,
    EnrollmentState,
    MessageDirection,
    OutboundStatus,
)
from app.integrations.email.base import (
    MailboxAuthError,
    MailboxError,
    MailboxRejected,
    MailboxUnavailable,
    MailProvider,
    SendOutcomeUnknown,
    granted,
    pkce_pair,
)
from app.integrations.email.google import GoogleMail
from app.integrations.email.microsoft import MicrosoftMail
from app.integrations.email.mime import build_message, new_message_id
from app.jobs import queue
from app.jobs.worker import JobContext, PermanentJobError, RetryableJobError
from app.models import (
    Campaign,
    CampaignProspect,
    EmailAccount,
    EmailDraft,
    EmailMessage,
    OAuthState,
    Prospect,
    User,
    UserSession,
)
from app.services import audit

log = get_logger(__name__)
SEND_JOB = "email.send"
_LIVE_OUTBOUND = (OutboundStatus.QUEUED, OutboundStatus.SENDING, OutboundStatus.PROVIDER_ACCEPTED,
                  OutboundStatus.UNKNOWN)


@dataclass
class MailServices:
    settings: Settings
    providers: dict[EmailProvider, MailProvider]
    cipher: CredentialCipher | None

    def provider(self, name: EmailProvider) -> MailProvider:
        p = self.providers.get(name)
        if p is None or not p.configured:
            raise AppError(f"{name.value} isn't configured on this server. Add its OAuth client id and secret to "
                           ".env (see docs/provider-setup.md).", code="provider_not_configured")
        return p

    def require_cipher(self) -> CredentialCipher:
        if self.cipher is None:
            raise AppError("CREDENTIAL_ENCRYPTION_KEYS is not configured; mailbox tokens can't be stored safely.",
                           code="encryption_not_configured")
        return self.cipher


def build_mail_services(settings: Settings, client: httpx.AsyncClient | None = None) -> MailServices:
    try:
        cipher: CredentialCipher | None = CredentialCipher.from_config(
            settings.credential_encryption_keys.get_secret_value(), settings.credential_encryption_active_key)
    except CredentialCryptoError:
        cipher = None
    timeout = settings.email_http_timeout_seconds
    return MailServices(settings=settings, cipher=cipher, providers={
        EmailProvider.GMAIL: GoogleMail(client_id=settings.google_client_id,
                                        client_secret=settings.google_client_secret.get_secret_value(),
                                        timeout=timeout, client=client),
        EmailProvider.MICROSOFT: MicrosoftMail(client_id=settings.microsoft_client_id,
                                               client_secret=settings.microsoft_client_secret.get_secret_value(),
                                               tenant=settings.microsoft_tenant, timeout=timeout, client=client),
    })


def _token_context(account_id: uuid.UUID) -> str:
    return f"email_account:{account_id}:refresh_token"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# --- Connection (OAuth 2.0 authorization code + PKCE) -----------------------------------------------

class OAuthFailed(Exception):
    """Callback failure with a stable code for the UI; never carries token material."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def start_connection(db: Session, *, session: UserSession, user: User, provider: EmailProvider,
                     svc: MailServices, login_hint: str | None = None) -> str:
    p = svc.provider(provider)
    cipher = svc.require_cipher()
    state = secrets.token_urlsafe(32)
    verifier, challenge = pkce_pair()
    row = OAuthState(id=uuid.uuid4(), workspace_id=user.workspace_id, user_id=user.id, session_id=session.id,
                     provider=provider, state_hash=_hash(state), encrypted_code_verifier="",
                     expires_at=datetime.now(UTC) + timedelta(minutes=svc.settings.oauth_state_ttl_minutes))
    row.encrypted_code_verifier = cipher.encrypt(verifier, f"oauth_state:{row.id}")
    db.add(row)
    audit.record(db, action="mailbox.connect_started", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="oauth_state", entity_id=row.id,
                 metadata={"provider": provider.value})
    return p.authorization_url(state=state, code_challenge=challenge,
                               redirect_uri=svc.settings.oauth_redirect_uri(provider.value), login_hint=login_hint)


async def complete_connection(db: Session, *, session: UserSession, user: User, provider: EmailProvider, code: str,
                              state: str, svc: MailServices) -> EmailAccount:
    row = db.scalar(select(OAuthState).where(OAuthState.state_hash == _hash(state)).with_for_update())
    if row is None or row.provider is not provider:
        raise OAuthFailed("invalid_state", "This connection link isn't valid. Start the connection again.")
    if row.session_id != session.id or row.user_id != user.id:
        # A callback for a flow started by another session: never attach a mailbox across sessions.
        raise OAuthFailed("session_mismatch", "This connection was started in a different session.")
    if row.consumed_at is not None:
        raise OAuthFailed("state_reused", "This connection link was already used. Start the connection again.")
    if row.expires_at < datetime.now(UTC):
        raise OAuthFailed("state_expired", "The connection took too long. Start it again.")
    row.consumed_at = datetime.now(UTC)
    db.flush()  # single use even if the exchange below fails
    cipher = svc.require_cipher()
    verifier = cipher.decrypt(row.encrypted_code_verifier, f"oauth_state:{row.id}")
    p = svc.provider(provider)
    try:
        tokens = await p.exchange_code(code=code, code_verifier=verifier,
                                       redirect_uri=svc.settings.oauth_redirect_uri(provider.value))
        missing = granted(tokens.scopes, p.required_scopes)
        if missing:
            raise OAuthFailed("missing_scope", "Sending permission wasn't granted. Connect again and allow sending "
                                               "email on your behalf.")
        who = await p.identity(tokens.access_token)
    except MailboxError as exc:
        raise OAuthFailed("provider_error", str(exc)) from exc

    account = db.scalar(select(EmailAccount).where(
        EmailAccount.workspace_id == user.workspace_id, EmailAccount.provider == provider,
        EmailAccount.provider_account_id == who.account_id).with_for_update())
    if account is None:
        if not tokens.refresh_token:
            raise OAuthFailed("no_refresh_token", "The provider didn't grant offline access. Connect again.")
        account = EmailAccount(id=uuid.uuid4(), workspace_id=user.workspace_id, provider=provider,
                               provider_account_id=who.account_id, email_address=who.email,
                               status=EmailAccountStatus.CONNECTED)
        db.add(account)
    elif account.status is not EmailAccountStatus.CONNECTED:
        transition_row(db, EmailAccount, account.id, EMAIL_ACCOUNT, EmailAccountStatus.CONNECTED, column="status",
                       by_human=True)
    if tokens.refresh_token:  # Google omits it on some re-consents; keep the one we hold
        account.encrypted_refresh_token = cipher.encrypt(tokens.refresh_token, _token_context(account.id))
    elif not account.encrypted_refresh_token:
        raise OAuthFailed("no_refresh_token", "The provider didn't grant offline access. Connect again.")
    account.email_address = who.email
    account.display_name = who.display_name
    account.granted_scopes = list(tokens.scopes)
    account.connected_by_id = user.id
    account.connected_at = datetime.now(UTC)
    account.disconnected_at = None
    account.last_error = None
    db.flush()
    audit.record(db, action="mailbox.connected", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="email_account", entity_id=account.id,
                 metadata={"provider": provider.value, "scopes": list(tokens.scopes)})
    return account


def list_accounts(db: Session, user: User) -> list[EmailAccount]:
    return list(db.scalars(select(EmailAccount).where(EmailAccount.workspace_id == user.workspace_id)
                           .order_by(EmailAccount.created_at)).all())


def get_account(db: Session, user: User, account_id: uuid.UUID, *, for_update: bool = False) -> EmailAccount:
    stmt = select(EmailAccount).where(EmailAccount.id == account_id, EmailAccount.workspace_id == user.workspace_id)
    account = db.scalar(stmt.with_for_update() if for_update else stmt)
    if account is None:
        raise NotFound("Mailbox not found")
    return account


async def disconnect(db: Session, user: User, account_id: uuid.UUID, svc: MailServices) -> bool:
    """Revoke the grant where the provider supports it, then delete our copy of the token."""
    account = get_account(db, user, account_id, for_update=True)
    revoked = False
    if account.encrypted_refresh_token and svc.cipher is not None:
        p = svc.providers.get(account.provider)
        if p is not None and p.configured:
            try:
                token = svc.cipher.decrypt(account.encrypted_refresh_token, _token_context(account.id))
                revoked = await p.revoke(token)
            except CredentialCryptoError:
                revoked = False
    in_use = db.scalars(select(Campaign.name).where(Campaign.sending_account_id == account.id)).all()
    account.encrypted_refresh_token = None
    account.disconnected_at = datetime.now(UTC)
    if account.status is not EmailAccountStatus.DISCONNECTED:
        transition_row(db, EmailAccount, account.id, EMAIL_ACCOUNT, EmailAccountStatus.DISCONNECTED, column="status")
    audit.record(db, action="mailbox.disconnected", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="email_account", entity_id=account.id,
                 metadata={"provider_revoked": revoked, "campaigns_affected": list(in_use)})
    return revoked


# --- Send eligibility (checked when queuing AND again right before sending) ----------------------------

def daily_count(db: Session, account_id: uuid.UUID) -> int:
    return db.scalar(select(func.count()).select_from(EmailMessage).where(
        EmailMessage.email_account_id == account_id, EmailMessage.direction == MessageDirection.OUTBOUND,
        EmailMessage.outbound_status.in_(_LIVE_OUTBOUND),
        EmailMessage.created_at > func.now() - timedelta(hours=24))) or 0


def mailbox_problem(account: EmailAccount) -> str | None:
    if account.status is not EmailAccountStatus.CONNECTED or not account.encrypted_refresh_token:
        return f"The mailbox {account.email_address} is {account.status.value.replace('_', ' ')}; reconnect it."
    return None


def draft_problem(db: Session, draft: EmailDraft) -> str | None:
    from app.services.campaigns.service import ineligibility_reason
    if not draft.is_current:
        return "A newer version of this draft exists; approve and send that one."
    if draft.status is not DraftStatus.APPROVED or draft.approved_content_sha256 != draft.content_sha256:
        return "This exact version isn't approved."
    if draft.step_order != 0:
        return "Follow-ups are sent by the sequence scheduler (Milestone 6), not one by one."
    prospect = db.get(Prospect, draft.prospect_id)
    campaign = db.get(Campaign, draft.campaign_id)
    if prospect is None or campaign is None:
        return "The contact or campaign no longer exists."
    reason = ineligibility_reason(db, prospect, campaign)
    if reason:
        return reason
    cp = db.scalar(select(CampaignProspect).where(CampaignProspect.campaign_id == campaign.id,
                                                  CampaignProspect.prospect_id == prospect.id))
    if cp is None or cp.state not in (EnrollmentState.AWAITING_APPROVAL, EnrollmentState.SCHEDULED):
        return f"The contact's campaign status is {cp.state.value.replace('_', ' ') if cp else 'removed'}."
    return None


# --- Queuing --------------------------------------------------------------------------------------------

def _enqueue(db: Session, message: EmailMessage) -> None:
    queue.enqueue(db, kind=SEND_JOB, workspace_id=message.workspace_id, payload={"message_id": str(message.id)},
                  dedupe_key=f"email:send:{message.id}", priority=50, max_attempts=6)


def queue_draft_send(db: Session, user: User, draft_id: uuid.UUID, account_id: uuid.UUID | None,
                     settings: Settings) -> EmailMessage:
    draft = db.scalar(select(EmailDraft).where(EmailDraft.id == draft_id, EmailDraft.workspace_id == user.workspace_id)
                      .with_for_update())
    if draft is None:
        raise NotFound("Draft not found")
    key = f"send:draft:{draft.id}:{draft.content_sha256}"
    existing = db.scalar(select(EmailMessage).where(EmailMessage.send_idempotency_key == key))
    if existing is not None:
        return existing  # double click / retry: the same logical send, whatever mailbox is passed
    campaign = db.get(Campaign, draft.campaign_id)
    assert campaign is not None
    chosen = account_id or campaign.sending_account_id
    if chosen is None:
        raise Conflict("Choose a sending mailbox (or set one on the campaign).", code="no_sending_mailbox")
    account = get_account(db, user, chosen)
    problem = mailbox_problem(account) or draft_problem(db, draft)
    if problem:
        raise Conflict(problem, code="send_not_allowed")
    if daily_count(db, account.id) >= settings.mailbox_daily_send_limit:
        raise Conflict(f"{account.email_address} reached its limit of {settings.mailbox_daily_send_limit} emails in "
                       "24 hours.", code="daily_limit_reached")
    prospect = db.get(Prospect, draft.prospect_id)
    assert prospect is not None and prospect.email_normalized
    message = _insert_outbound(db, account=account, key=key, to=prospect.email or prospect.email_normalized,
                               subject=draft.subject, body=draft.body_text, prospect_id=prospect.id,
                               campaign_id=campaign.id, draft_id=draft.id)
    cp = db.scalar(select(CampaignProspect).where(CampaignProspect.campaign_id == campaign.id,
                                                  CampaignProspect.prospect_id == prospect.id))
    if cp is not None and cp.state is EnrollmentState.AWAITING_APPROVAL:
        transition_row(db, CampaignProspect, cp.id, ENROLLMENT, EnrollmentState.SCHEDULED,
                       extra_values={"blocker": None})
    _enqueue(db, message)
    audit.record(db, action="email.send_requested", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="email_message", entity_id=message.id,
                 metadata={"draft_id": str(draft.id), "mailbox": account.email_address,
                           "content_sha256": draft.content_sha256})
    return message


TEST_SUBJECT = "Outreach Agent: test email"
TEST_BODY = ("This is a test email from Outreach Agent.\n\nIf you can read this, the mailbox is connected and "
             "sending works. No prospect received this message.")


def queue_test_send(db: Session, user: User, account_id: uuid.UUID) -> EmailMessage:
    """Send a test email to the mailbox's own address (never to a prospect)."""
    account = get_account(db, user, account_id)
    problem = mailbox_problem(account)
    if problem:
        raise Conflict(problem, code="send_not_allowed")
    message = _insert_outbound(db, account=account, key=f"send:test:{account.id}:{uuid.uuid4()}",
                               to=account.email_address, subject=TEST_SUBJECT, body=TEST_BODY)
    _enqueue(db, message)
    audit.record(db, action="email.test_send_requested", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
                 actor_id=user.id, workspace_id=user.workspace_id, entity_type="email_message", entity_id=message.id)
    return message


def _insert_outbound(db: Session, *, account: EmailAccount, key: str, to: str, subject: str, body: str,
                     prospect_id: uuid.UUID | None = None, campaign_id: uuid.UUID | None = None,
                     draft_id: uuid.UUID | None = None) -> EmailMessage:
    message_id = db.execute(pg_insert(EmailMessage).values(
        id=uuid.uuid4(), workspace_id=account.workspace_id, email_account_id=account.id, prospect_id=prospect_id,
        campaign_id=campaign_id, draft_id=draft_id, direction=MessageDirection.OUTBOUND, send_idempotency_key=key,
        internet_message_id=new_message_id(account.email_address), from_address=account.email_address,
        to_addresses=[to], subject=subject, snippet=body[:200], body_text=body, outbound_status=OutboundStatus.QUEUED,
    ).on_conflict_do_nothing(index_elements=["send_idempotency_key"],
                             index_where=EmailMessage.send_idempotency_key.isnot(None))
        .returning(EmailMessage.id)).scalar_one_or_none()
    row = db.scalar(select(EmailMessage).where(EmailMessage.send_idempotency_key == key) if message_id is None
                    else select(EmailMessage).where(EmailMessage.id == message_id))
    assert row is not None
    return row


# --- The send job ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Prepared:
    message_id: uuid.UUID
    account_id: uuid.UUID
    provider: EmailProvider
    encrypted_token: str
    mime: bytes
    to_name: str | None


def _prepare(s: Session, message_id: uuid.UUID, settings: Settings) -> _Prepared | str:
    """Lock the message and re-validate everything. Returns what to send, or why it must not be sent."""
    m = s.scalar(select(EmailMessage).where(EmailMessage.id == message_id).with_for_update())
    if m is None:
        return "gone"
    if m.outbound_status is not OutboundStatus.QUEUED:
        return f"already {m.outbound_status.value if m.outbound_status else 'handled'}"
    account = s.scalar(select(EmailAccount).where(EmailAccount.id == m.email_account_id).with_for_update())
    assert account is not None
    problem = mailbox_problem(account)
    to_name: str | None = None
    from_name = account.display_name
    if problem is None and m.draft_id is not None:
        draft = s.get(EmailDraft, m.draft_id)
        if draft is None or draft.content_sha256 != _sha_of_message(m, draft):
            problem = "The draft changed after this send was requested."
        else:
            problem = draft_problem(s, draft)
            prospect = s.get(Prospect, draft.prospect_id)
            to_name = prospect.full_name if prospect else None
            campaign = s.get(Campaign, draft.campaign_id)
            sender = ((campaign.brief or {}).get("sender") or {}) if campaign else {}
            from_name = sender.get("name") or from_name
    if problem is None and daily_count(s, account.id) > settings.mailbox_daily_send_limit:
        problem = f"Daily limit of {settings.mailbox_daily_send_limit} emails reached for {account.email_address}."
    if problem:
        _fail(s, m, problem)
        return problem
    assert account.encrypted_refresh_token is not None and m.internet_message_id
    mime = build_message(from_address=account.email_address, from_name=from_name, to_address=m.to_addresses[0],
                         to_name=to_name, subject=m.subject or "", body=m.body_text or "",
                         message_id=m.internet_message_id, unsubscribe_mailto=account.email_address)
    return _Prepared(m.id, account.id, account.provider, account.encrypted_refresh_token, mime, to_name)


def _sha_of_message(m: EmailMessage, draft: EmailDraft) -> str:
    """The content hash the send was requested for (embedded in the idempotency key)."""
    key = m.send_idempotency_key or ""
    return key.rsplit(":", 1)[-1] if key.startswith("send:draft:") else draft.content_sha256


def _fail(s: Session, m: EmailMessage, reason: str) -> None:
    transition_row(s, EmailMessage, m.id, OUTBOUND, OutboundStatus.FAILED, column="outbound_status",
                   extra_values={"last_error": reason[:1000]})
    _release_enrollment(s, m, f"Not sent: {reason}")


def _release_enrollment(s: Session, m: EmailMessage, blocker: str) -> None:
    if m.campaign_id is None or m.prospect_id is None:
        return
    cp = s.scalar(select(CampaignProspect).where(CampaignProspect.campaign_id == m.campaign_id,
                                                 CampaignProspect.prospect_id == m.prospect_id))
    if cp is not None and cp.state is EnrollmentState.SCHEDULED:
        transition_row(s, CampaignProspect, cp.id, ENROLLMENT, EnrollmentState.AWAITING_APPROVAL,
                       extra_values={"blocker": blocker[:500]})


def _mark_sending(s: Session, message_id: uuid.UUID) -> bool:
    try:
        transition_row(s, EmailMessage, message_id, OUTBOUND, OutboundStatus.SENDING, column="outbound_status",
                       expected_from=OutboundStatus.QUEUED)
        return True
    except StaleTransition:
        return False


def _accepted(s: Session, message_id: uuid.UUID, provider_message_id: str | None, thread_id: str | None) -> None:
    transition_row(s, EmailMessage, message_id, OUTBOUND, OutboundStatus.PROVIDER_ACCEPTED, column="outbound_status",
                   extra_values={"provider_message_id": provider_message_id, "provider_thread_id": thread_id,
                                 "sent_at": datetime.now(UTC), "last_error": None})
    m = s.get(EmailMessage, message_id)
    if m is not None and m.campaign_id and m.prospect_id:
        cp = s.scalar(select(CampaignProspect).where(CampaignProspect.campaign_id == m.campaign_id,
                                                     CampaignProspect.prospect_id == m.prospect_id))
        if cp is not None and cp.state is EnrollmentState.SCHEDULED:
            transition_row(s, CampaignProspect, cp.id, ENROLLMENT, EnrollmentState.IN_SEQUENCE,
                           extra_values={"blocker": None})


def _not_sent_retry(s: Session, message_id: uuid.UUID, error: str, *, final: bool) -> None:
    """Nothing was sent (the request never reached the provider). Requeue, or give up on the last attempt."""
    m = s.get(EmailMessage, message_id)
    if m is None:
        return
    if m.outbound_status is OutboundStatus.SENDING:
        transition_row(s, EmailMessage, message_id, OUTBOUND, OutboundStatus.QUEUED, column="outbound_status",
                       expected_from=OutboundStatus.SENDING, extra_values={"last_error": error[:1000]})
        s.refresh(m)
    if final and m.outbound_status is OutboundStatus.QUEUED:
        _fail(s, m, f"Gave up after repeated provider errors: {error}")


def _unknown(s: Session, message_id: uuid.UUID, error: str, provider_message_id: str | None) -> None:
    transition_row(s, EmailMessage, message_id, OUTBOUND, OutboundStatus.UNKNOWN, column="outbound_status",
                   extra_values={"last_error": error[:1000], "provider_message_id": provider_message_id})


def _account_auth_lost(s: Session, account_id: uuid.UUID, error: str) -> None:
    account = s.get(EmailAccount, account_id)
    if account is not None and account.status is EmailAccountStatus.CONNECTED:
        transition_row(s, EmailAccount, account_id, EMAIL_ACCOUNT, EmailAccountStatus.NEEDS_REAUTH, column="status",
                       extra_values={"last_error": error[:1000]})


def _store_rotated_token(s: Session, account_id: uuid.UUID, ciphertext: str) -> None:
    account = s.get(EmailAccount, account_id)
    if account is not None:
        account.encrypted_refresh_token = ciphertext


async def handle_send(ctx: JobContext) -> dict[str, Any]:
    svc: MailServices | None = ctx.services.get("mail")
    if svc is None:
        raise PermanentJobError("mail services are not configured in this worker")
    message_id = uuid.UUID(ctx.job.payload["message_id"])
    prepared = await ctx.db(lambda s: _prepare(s, message_id, svc.settings))
    if isinstance(prepared, str):
        return {"outcome": "not_sent", "reason": prepared}
    cipher = svc.require_cipher()
    provider = svc.provider(prepared.provider)
    final = ctx.job.attempts >= ctx.job.max_attempts

    # 1. Access token. Failing here means nothing was sent, so it's always safe to retry or stop.
    try:
        refresh_token = cipher.decrypt(prepared.encrypted_token, _token_context(prepared.account_id))
        tokens = await provider.refresh(refresh_token)
    except (MailboxAuthError, MailboxRejected, CredentialCryptoError) as exc:
        err = str(exc)
        await ctx.db(functools.partial(_auth_failed, message_id=message_id, account_id=prepared.account_id,
                                       error=err))
        return {"outcome": "not_sent", "reason": err}
    except MailboxUnavailable as exc:
        err = str(exc)
        if final:
            await ctx.db(lambda s: _not_sent_retry(s, message_id, err, final=True))
            return {"outcome": "not_sent", "reason": err}
        raise RetryableJobError(err, retry_after_seconds=exc.retry_after) from exc
    if tokens.refresh_token and tokens.refresh_token != refresh_token:  # rotation (Microsoft, sometimes Google)
        ciphertext = cipher.encrypt(tokens.refresh_token, _token_context(prepared.account_id))
        await ctx.db(lambda s: _store_rotated_token(s, prepared.account_id, ciphertext))

    # 2. Claim, then send exactly once.
    if not await ctx.db(lambda s: _mark_sending(s, message_id)):
        return {"outcome": "not_sent", "reason": "already claimed"}
    try:
        result = await provider.send(tokens.access_token, prepared.mime)
    except SendOutcomeUnknown as exc:
        err, pid = str(exc), exc.provider_message_id
        await ctx.db(lambda s: _unknown(s, message_id, err, pid))
        log.warning("send_outcome_unknown", message_id=str(message_id), error=err)
        return {"outcome": "unknown"}
    except MailboxUnavailable as exc:
        err = str(exc)
        await ctx.db(lambda s: _not_sent_retry(s, message_id, err, final=final))
        if final:
            return {"outcome": "not_sent", "reason": err}
        raise RetryableJobError(err, retry_after_seconds=exc.retry_after) from exc
    except (MailboxAuthError, MailboxRejected) as exc:
        err = str(exc)
        await ctx.db(functools.partial(_auth_failed if isinstance(exc, MailboxAuthError) else _rejected,
                                       message_id=message_id, account_id=prepared.account_id, error=err))
        return {"outcome": "not_sent", "reason": err}
    await ctx.db(lambda s: _accepted(s, message_id, result.provider_message_id, result.provider_thread_id))
    return {"outcome": "provider_accepted", "provider_message_id": result.provider_message_id}


def _auth_failed(s: Session, *, message_id: uuid.UUID, account_id: uuid.UUID, error: str) -> None:
    _account_auth_lost(s, account_id, error)
    m = s.get(EmailMessage, message_id)
    if m is not None and m.outbound_status in (OutboundStatus.QUEUED, OutboundStatus.SENDING):
        _fail(s, m, f"Mailbox authorization failed: {error}")


def _rejected(s: Session, *, message_id: uuid.UUID, account_id: uuid.UUID, error: str) -> None:
    m = s.get(EmailMessage, message_id)
    if m is not None and m.outbound_status in (OutboundStatus.QUEUED, OutboundStatus.SENDING):
        _fail(s, m, error)


HANDLERS = {SEND_JOB: handle_send}
