"""Microsoft 365 / Outlook.com via the Microsoft identity platform (auth code + PKCE) and Microsoft Graph.

Sending is two steps so the message has an id before it leaves: create a draft from our MIME (headers kept), then
send it. If the send call's outcome is unclear, the draft's `isDraft` flag tells us whether it actually went.
Microsoft has no API to revoke a single app grant; disconnecting deletes our token and the user can remove the app
at https://myaccount.microsoft.com (or an admin in Entra ID).
"""

from __future__ import annotations

import base64
import contextlib
from urllib.parse import urlencode

import httpx

from app.domain.states import EmailProvider
from app.integrations.email.base import (
    MailboxIdentity,
    MailboxRejected,
    MailboxUnavailable,
    SendOutcomeUnknown,
    SendResult,
    TokenSet,
)
from app.integrations.email.http import raise_for_api, send_transport_error, token_request

GRAPH = "https://graph.microsoft.com/v1.0"
SEND_SCOPE = "https://graph.microsoft.com/Mail.Send"


class MicrosoftMail:
    provider: EmailProvider = EmailProvider.MICROSOFT
    scopes: tuple[str, ...] = ("openid", "email", "offline_access", "https://graph.microsoft.com/User.Read",
                               SEND_SCOPE)
    required_scopes: tuple[str, ...] = (SEND_SCOPE,)

    def __init__(self, *, client_id: str, client_secret: str, tenant: str = "common", timeout: float = 30,
                 client: httpx.AsyncClient | None = None) -> None:
        self.client_id = client_id
        self._secret = client_secret
        self._base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
        self._http = client or httpx.AsyncClient(timeout=timeout)

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self._secret)

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str,
                          login_hint: str | None = None) -> str:
        params = {"client_id": self.client_id, "redirect_uri": redirect_uri, "response_type": "code",
                  "response_mode": "query", "scope": " ".join(self.scopes), "state": state,
                  "code_challenge": code_challenge, "code_challenge_method": "S256", "prompt": "select_account"}
        if login_hint:
            params["login_hint"] = login_hint
        return f"{self._base}/authorize?{urlencode(params)}"

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> TokenSet:
        tokens = await token_request(self._http, f"{self._base}/token", {
            "grant_type": "authorization_code", "code": code, "code_verifier": code_verifier,
            "redirect_uri": redirect_uri, "client_id": self.client_id, "client_secret": self._secret,
            "scope": " ".join(self.scopes)}, "Microsoft")
        return _normalized(tokens)

    async def refresh(self, refresh_token: str) -> TokenSet:
        tokens = await token_request(self._http, f"{self._base}/token", {
            "grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": self.client_id,
            "client_secret": self._secret, "scope": " ".join(self.scopes)}, "Microsoft")
        return _normalized(tokens)

    async def identity(self, access_token: str) -> MailboxIdentity:
        response = await self._http.get(f"{GRAPH}/me", params={"$select": "id,mail,userPrincipalName,displayName"},
                                        headers=_auth(access_token))
        raise_for_api(response, "Microsoft Graph", sending=False)
        me = response.json()
        email = me.get("mail") or me.get("userPrincipalName")
        if not email or "@" not in email:
            raise MailboxRejected("This Microsoft account has no mailbox address (is Exchange Online licensed?)")
        return MailboxIdentity(account_id=str(me["id"]), email=str(email).lower(), display_name=me.get("displayName"))

    async def send(self, access_token: str, mime: bytes) -> SendResult:
        headers = {**_auth(access_token), "Content-Type": "text/plain"}
        try:  # 1. create a draft from our MIME. Failing here can't have sent anything.
            created = await self._http.post(f"{GRAPH}/me/messages", content=base64.b64encode(mime), headers=headers)
        except httpx.HTTPError as exc:
            raise MailboxUnavailable(f"Microsoft Graph draft creation failed: {type(exc).__name__}") from exc
        if created.status_code >= 500:
            raise MailboxUnavailable(f"Microsoft Graph returned {created.status_code} creating the draft")
        raise_for_api(created, "Microsoft Graph", sending=False)
        draft = created.json()
        message_id = str(draft["id"])
        try:  # 2. send it
            sent = await self._http.post(f"{GRAPH}/me/messages/{message_id}/send", headers=_auth(access_token))
        except httpx.HTTPError as exc:
            if isinstance(send_transport_error(exc, "Microsoft Graph"), MailboxUnavailable):
                await self._discard(access_token, message_id)  # never reached Graph: nothing sent
                raise MailboxUnavailable(f"Microsoft Graph unreachable: {type(exc).__name__}") from exc
            await self._resolve_unclear(access_token, message_id, f"send interrupted: {type(exc).__name__}")
        else:
            if sent.status_code >= 500:
                await self._resolve_unclear(access_token, message_id, f"send returned {sent.status_code}")
            elif sent.status_code >= 400:  # a definite refusal: nothing was sent
                await self._discard(access_token, message_id)
                raise_for_api(sent, "Microsoft Graph", sending=True)
        return SendResult(provider_message_id=message_id, provider_thread_id=draft.get("conversationId"),
                          extra={"internet_message_id": draft.get("internetMessageId") or ""})

    async def _resolve_unclear(self, access_token: str, message_id: str, why: str) -> None:
        """After an unclear send: return if it went out, raise MailboxUnavailable if it provably didn't, else
        SendOutcomeUnknown (never guess)."""
        if await self._still_draft(access_token, message_id):
            await self._discard(access_token, message_id)
            raise MailboxUnavailable(f"Microsoft Graph {why}; the message is still an unsent draft, safe to retry")
        if await self._sent(access_token, message_id):
            return
        raise SendOutcomeUnknown(f"Microsoft Graph {why}; delivery state unknown", provider_message_id=message_id)

    async def _sent(self, access_token: str, message_id: str) -> bool:
        try:
            r = await self._http.get(f"{GRAPH}/me/messages/{message_id}", params={"$select": "isDraft"},
                                     headers=_auth(access_token))
        except httpx.HTTPError:
            return False
        return r.status_code == 200 and r.json().get("isDraft") is False

    async def _still_draft(self, access_token: str, message_id: str) -> bool:
        """True only when Graph positively confirms the message is still an unsent draft."""
        try:
            r = await self._http.get(f"{GRAPH}/me/messages/{message_id}", params={"$select": "isDraft"},
                                     headers=_auth(access_token))
        except httpx.HTTPError:
            return False
        return r.status_code == 200 and r.json().get("isDraft") is True

    async def _discard(self, access_token: str, message_id: str) -> None:
        with contextlib.suppress(httpx.HTTPError):  # a leftover draft is harmless; it was never sent
            await self._http.delete(f"{GRAPH}/me/messages/{message_id}", headers=_auth(access_token))

    async def revoke(self, refresh_token: str) -> bool:
        return False  # no per-grant revocation endpoint; see module docstring


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _normalized(tokens: TokenSet) -> TokenSet:
    """Graph reports delegated scopes either as full URLs or short names; compare on full URLs."""
    full = tuple(s if s.startswith("https://") or s in ("openid", "email", "profile", "offline_access")
                 else f"https://graph.microsoft.com/{s}" for s in tokens.scopes)
    return TokenSet(tokens.access_token, tokens.expires_in, tokens.refresh_token, full)
