"""Gmail via Google OAuth 2.0 (authorization code + PKCE) and the Gmail API.

Scope is `gmail.send` only (send, no read access); reply reading arrives with Milestone 6.
"""

from __future__ import annotations

import base64
from urllib.parse import urlencode

import httpx

from app.domain.states import EmailProvider
from app.integrations.email.base import MailboxIdentity, MailboxRejected, SendResult, TokenSet
from app.integrations.email.http import raise_for_api, send_transport_error, token_request

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 (an endpoint, not a credential)
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


class GoogleMail:
    provider: EmailProvider = EmailProvider.GMAIL
    scopes: tuple[str, ...] = ("openid", "email", SEND_SCOPE)
    required_scopes: tuple[str, ...] = (SEND_SCOPE,)

    def __init__(self, *, client_id: str, client_secret: str, timeout: float = 30,
                 client: httpx.AsyncClient | None = None) -> None:
        self.client_id = client_id
        self._secret = client_secret
        self._http = client or httpx.AsyncClient(timeout=timeout)

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self._secret)

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str,
                          login_hint: str | None = None) -> str:
        params = {"client_id": self.client_id, "redirect_uri": redirect_uri, "response_type": "code",
                  "scope": " ".join(self.scopes), "state": state, "code_challenge": code_challenge,
                  "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent"}
        if login_hint:
            params["login_hint"] = login_hint
        return f"{AUTH_URL}?{urlencode(params)}"

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> TokenSet:
        return await token_request(self._http, TOKEN_URL, {
            "grant_type": "authorization_code", "code": code, "code_verifier": code_verifier,
            "redirect_uri": redirect_uri, "client_id": self.client_id, "client_secret": self._secret}, "Google")

    async def refresh(self, refresh_token: str) -> TokenSet:
        return await token_request(self._http, TOKEN_URL, {
            "grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": self.client_id,
            "client_secret": self._secret}, "Google")

    async def identity(self, access_token: str) -> MailboxIdentity:
        response = await self._http.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        raise_for_api(response, "Google", sending=False)
        info = response.json()
        if not info.get("email") or info.get("email_verified") is False:
            raise MailboxRejected("Google did not return a verified email address for this account")
        return MailboxIdentity(account_id=str(info["sub"]), email=str(info["email"]).lower(),
                               display_name=info.get("name"))

    async def send(self, access_token: str, mime: bytes) -> SendResult:
        raw = base64.urlsafe_b64encode(mime).decode()
        try:
            response = await self._http.post(SEND_URL, json={"raw": raw},
                                             headers={"Authorization": f"Bearer {access_token}"})
        except httpx.HTTPError as exc:
            raise send_transport_error(exc, "Gmail") from exc
        raise_for_api(response, "Gmail", sending=True)
        body = response.json()
        return SendResult(provider_message_id=body.get("id"), provider_thread_id=body.get("threadId"))

    async def revoke(self, refresh_token: str) -> bool:
        try:
            response = await self._http.post(REVOKE_URL, data={"token": refresh_token})
        except httpx.HTTPError:
            return False
        return response.status_code == 200
