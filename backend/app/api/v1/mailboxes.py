"""Mailbox connection (OAuth), test sends, sending approved drafts, and outbound message status."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentSession, CurrentUser, DbSession
from app.core.config import Settings, get_settings
from app.core.errors import NotFound
from app.domain.states import EmailAccountStatus, EmailProvider, OutboundStatus
from app.models import EmailAccount, EmailMessage
from app.schemas.common import ERROR_RESPONSES, Envelope
from app.services import auth as auth_service
from app.services import mailboxes as service

router = APIRouter(tags=["mailboxes"], responses=ERROR_RESPONSES)


async def get_mail_services(settings: Annotated[Settings, Depends(get_settings)]) -> AsyncIterator[
        service.MailServices]:
    async with httpx.AsyncClient(timeout=settings.email_http_timeout_seconds) as client:
        yield service.build_mail_services(settings, client)


Mail = Annotated[service.MailServices, Depends(get_mail_services)]


class ProviderOut(BaseModel):
    provider: EmailProvider
    configured: bool
    redirect_uri: str
    scopes: list[str]


class ProvidersOut(BaseModel):
    app_public_url: str
    encryption_configured: bool
    daily_send_limit: int
    providers: list[ProviderOut]


class MailboxOut(BaseModel):
    id: uuid.UUID
    provider: EmailProvider
    email_address: str
    display_name: str | None
    status: EmailAccountStatus
    granted_scopes: list[str]
    last_error: str | None
    connected_at: datetime | None
    sent_last_24h: int


class StartIn(BaseModel):
    login_hint: str | None = Field(default=None, max_length=320)


class MessageOut(BaseModel):
    id: uuid.UUID
    email_account_id: uuid.UUID
    draft_id: uuid.UUID | None
    from_address: str
    to_addresses: list[str]
    subject: str | None
    status: OutboundStatus | None
    last_error: str | None
    provider_message_id: str | None
    internet_message_id: str | None
    sent_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, m: EmailMessage) -> MessageOut:
        return cls(id=m.id, email_account_id=m.email_account_id, draft_id=m.draft_id, from_address=m.from_address,
                   to_addresses=list(m.to_addresses), subject=m.subject, status=m.outbound_status,
                   last_error=m.last_error, provider_message_id=m.provider_message_id,
                   internet_message_id=m.internet_message_id, sent_at=m.sent_at, created_at=m.created_at)


class SendIn(BaseModel):
    email_account_id: uuid.UUID | None = Field(default=None, description="Defaults to the campaign's mailbox")


def _mailbox_out(db: DbSession, a: EmailAccount) -> MailboxOut:
    return MailboxOut(id=a.id, provider=a.provider, email_address=a.email_address, display_name=a.display_name,
                      status=a.status, granted_scopes=list(a.granted_scopes), last_error=a.last_error,
                      connected_at=a.connected_at, sent_last_24h=service.daily_count(db, a.id))


@router.get("/mailboxes/providers", response_model=Envelope[ProvidersOut],
            summary="Which mailbox providers are configured, and the redirect URIs to register")
def providers(_: CurrentUser, settings: AppSettings, mail: Mail) -> Envelope[ProvidersOut]:
    return Envelope(data=ProvidersOut(
        app_public_url=settings.app_public_url, encryption_configured=mail.cipher is not None,
        daily_send_limit=settings.mailbox_daily_send_limit,
        providers=[ProviderOut(provider=name, configured=p.configured, scopes=list(p.scopes),
                               redirect_uri=settings.oauth_redirect_uri(name.value))
                   for name, p in mail.providers.items()]))


@router.get("/mailboxes", response_model=Envelope[list[MailboxOut]], summary="Connected mailboxes")
def list_mailboxes(user: CurrentUser, db: DbSession) -> Envelope[list[MailboxOut]]:
    return Envelope(data=[_mailbox_out(db, a) for a in service.list_accounts(db, user)])


@router.post("/mailboxes/oauth/{provider}/start", response_model=Envelope[dict[str, str]],
             summary="Begin connecting a mailbox; returns the provider's consent URL")
def start(provider: EmailProvider, body: StartIn, session: CurrentSession, db: DbSession,
          mail: Mail) -> Envelope[dict[str, str]]:
    url = service.start_connection(db, session=session, user=session.user, provider=provider, svc=mail,
                                   login_hint=body.login_hint)
    db.commit()
    return Envelope(data={"authorization_url": url})


@router.get("/mailboxes/oauth/{provider}/callback", include_in_schema=False)
async def callback(provider: EmailProvider, request: Request, db: DbSession, settings: AppSettings,
                   mail: Mail) -> RedirectResponse:
    """The provider redirects the browser here. Always answers with a redirect back to the dashboard."""
    base = settings.app_public_url.rstrip("/")

    def back(**params: str) -> RedirectResponse:
        return RedirectResponse(f"{base}/settings/mailboxes?{urlencode(params)}", status_code=303)

    token = request.cookies.get(settings.session_cookie_name)
    session = auth_service.resolve_session(db, token) if token else None
    if session is None:
        return RedirectResponse(f"{base}/login?{urlencode({'next': '/settings/mailboxes'})}", status_code=303)
    q = request.query_params
    if q.get("error"):
        return back(error="consent_declined" if q["error"] == "access_denied" else "provider_error")
    if not q.get("code") or not q.get("state"):
        return back(error="invalid_callback")
    try:
        account = await service.complete_connection(db, session=session, user=session.user, provider=provider,
                                                    code=q["code"], state=q["state"], svc=mail)
    except service.OAuthFailed as exc:
        db.commit()  # keep the state consumed: a link is never usable twice
        return back(error=exc.code)
    db.commit()
    return back(connected=account.email_address)


@router.post("/mailboxes/{account_id}/test-send", status_code=202, response_model=Envelope[MessageOut],
             summary="Send a test email to the mailbox itself")
def test_send(account_id: uuid.UUID, user: CurrentUser, db: DbSession) -> JSONResponse:
    message = service.queue_test_send(db, user, account_id)
    db.commit()
    return JSONResponse(Envelope(data=MessageOut.of(message)).model_dump(mode="json"), status_code=202)


@router.delete("/mailboxes/{account_id}", response_model=Envelope[dict[str, bool]],
               summary="Disconnect a mailbox (revokes the grant where the provider allows it)")
async def disconnect(account_id: uuid.UUID, user: CurrentUser, db: DbSession,
                     mail: Mail) -> Envelope[dict[str, bool]]:
    revoked = await service.disconnect(db, user, account_id, mail)
    db.commit()
    return Envelope(data={"provider_revoked": revoked})


@router.post("/drafts/{draft_id}/send", status_code=202, response_model=Envelope[MessageOut],
             summary="Send an approved first email now (asynchronous; idempotent per approved version)")
def send_draft(draft_id: uuid.UUID, body: SendIn, user: CurrentUser, db: DbSession,
               settings: AppSettings) -> JSONResponse:
    message = service.queue_draft_send(db, user, draft_id, body.email_account_id, settings)
    db.commit()
    return JSONResponse(Envelope(data=MessageOut.of(message)).model_dump(mode="json"), status_code=202)


@router.get("/email-messages/{message_id}", response_model=Envelope[MessageOut], summary="Outbound message status")
def get_message(message_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Envelope[MessageOut]:
    m = db.scalar(select(EmailMessage).where(EmailMessage.id == message_id,
                                             EmailMessage.workspace_id == user.workspace_id))
    if m is None:
        raise NotFound("Message not found")
    return Envelope(data=MessageOut.of(m))
