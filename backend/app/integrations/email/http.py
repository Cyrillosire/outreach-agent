"""HTTP helpers shared by the mailbox providers: OAuth token calls and send-safety classification."""

from __future__ import annotations

from typing import Any

import httpx

from app.integrations.email.base import (
    MailboxAuthError,
    MailboxRejected,
    MailboxUnavailable,
    SendOutcomeUnknown,
    TokenSet,
)

# The request never reached the provider, so nothing can have been sent.
PRE_SEND_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers.get("retry-after", ""))
    except ValueError:
        return None


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("code") or err.get("status") or "")
    return str(err or "")


async def token_request(client: httpx.AsyncClient, url: str, data: dict[str, str], provider: str) -> TokenSet:
    """Authorization-code exchange or refresh. Never logs or echoes token values."""
    try:
        response = await client.post(url, data=data, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise MailboxUnavailable(f"{provider} token endpoint unreachable: {type(exc).__name__}") from exc
    if response.status_code >= 500 or response.status_code == 429:
        raise MailboxUnavailable(f"{provider} token endpoint returned {response.status_code}",
                                 status=response.status_code, retry_after=_retry_after(response))
    if response.status_code >= 400:
        code = _error_code(response)
        if code in ("invalid_grant", "interaction_required", "consent_required"):
            raise MailboxAuthError(f"{provider} authorization is no longer valid ({code}); reconnect the mailbox",
                                   status=response.status_code)
        raise MailboxRejected(f"{provider} rejected the token request ({code or response.status_code}); check the "
                              "OAuth client id, secret and redirect URI", status=response.status_code)
    body: dict[str, Any] = response.json()
    return TokenSet(access_token=body["access_token"], expires_in=int(body.get("expires_in", 3600)),
                    refresh_token=body.get("refresh_token"), scopes=tuple(str(body.get("scope", "")).split()))


def raise_for_api(response: httpx.Response, provider: str, *, sending: bool) -> None:
    """Map an API error response. For the actual send call, 5xx means the outcome is unknown."""
    status = response.status_code
    if status < 400:
        return
    detail = _error_code(response) or str(status)
    if status == 401:
        raise MailboxAuthError(f"{provider} rejected the access token ({detail})", status=status)
    if status == 429:
        raise MailboxUnavailable(f"{provider} rate limit ({detail})", status=status, retry_after=_retry_after(response))
    if status >= 500:
        if sending:
            raise SendOutcomeUnknown(f"{provider} returned {status} to the send request ({detail})")
        raise MailboxUnavailable(f"{provider} returned {status} ({detail})", status=status)
    raise MailboxRejected(f"{provider} refused the request ({detail})", status=status)


def send_transport_error(exc: httpx.HTTPError, provider: str) -> Exception:
    if isinstance(exc, PRE_SEND_ERRORS):
        return MailboxUnavailable(f"{provider} unreachable: {type(exc).__name__}")
    return SendOutcomeUnknown(f"{provider} send interrupted: {type(exc).__name__}")
