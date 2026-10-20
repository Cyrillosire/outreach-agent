"""Provider-neutral mailbox interface: OAuth, identity, sending, revocation.

Errors are classified by what they mean for the send, because that decides whether retrying is safe:
- `MailboxAuthError`: the grant is gone (revoked, expired, password change). Nothing was sent; reconnect.
- `MailboxRejected`: the provider refused the request (4xx). Nothing was sent; retrying won't help.
- `MailboxUnavailable`: a failure *before* the send request was made (token refresh outage). Safe to retry.
- `SendOutcomeUnknown`: the send request may or may not have been accepted (timeout, 5xx, dropped
  connection). Never retried automatically; reconciliation decides (Milestone 6).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Protocol

from app.domain.states import EmailProvider


class MailboxError(Exception):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class MailboxAuthError(MailboxError):
    pass


class MailboxRejected(MailboxError):
    pass


class MailboxUnavailable(MailboxError):
    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message, status=status)
        self.retry_after = retry_after


class SendOutcomeUnknown(MailboxError):
    def __init__(self, message: str, *, provider_message_id: str | None = None) -> None:
        super().__init__(message)
        self.provider_message_id = provider_message_id


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    expires_in: int
    refresh_token: str | None  # providers may rotate it; persist when present
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class MailboxIdentity:
    account_id: str  # Google `sub` / Microsoft `id` (object id): stable even if the address changes
    email: str
    display_name: str | None = None


@dataclass(frozen=True)
class SendResult:
    provider_message_id: str | None
    provider_thread_id: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


class MailProvider(Protocol):
    provider: EmailProvider
    scopes: tuple[str, ...]
    required_scopes: tuple[str, ...]

    @property
    def configured(self) -> bool: ...

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str,
                          login_hint: str | None = None) -> str: ...

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> TokenSet: ...

    async def refresh(self, refresh_token: str) -> TokenSet: ...

    async def identity(self, access_token: str) -> MailboxIdentity: ...

    async def send(self, access_token: str, mime: bytes) -> SendResult: ...

    async def revoke(self, refresh_token: str) -> bool: ...


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge) per RFC 7636."""
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def granted(scopes: tuple[str, ...], required: tuple[str, ...]) -> list[str]:
    """Required scopes that were NOT granted (users can untick permissions on the consent screen)."""
    have = {s.lower() for s in scopes}
    return [r for r in required if r.lower() not in have]
