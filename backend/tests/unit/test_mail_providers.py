"""The real Gmail and Microsoft Graph clients against scripted HTTP responses: what each failure means for a send."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.integrations.email.base import (
    MailboxAuthError,
    MailboxRejected,
    MailboxUnavailable,
    SendOutcomeUnknown,
    granted,
    pkce_pair,
)
from app.integrations.email.google import SEND_SCOPE as GMAIL_SEND
from app.integrations.email.google import GoogleMail
from app.integrations.email.microsoft import MicrosoftMail

Handler = Callable[[httpx.Request], httpx.Response]


def google(handler: Handler) -> GoogleMail:
    return GoogleMail(client_id="cid", client_secret="secret",
                      client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def microsoft(handler: Handler) -> MicrosoftMail:
    return MicrosoftMail(client_id="cid", client_secret="secret",
                         client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def raising(exc: type[httpx.HTTPError]) -> Handler:
    def h(req: httpx.Request) -> httpx.Response:
        raise exc("boom", request=req)
    return h


# --- OAuth -----------------------------------------------------------------------------------------

def test_pkce_and_authorization_urls() -> None:
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128 and "=" not in challenge
    q = parse_qs(urlsplit(google(lambda r: httpx.Response(200)).authorization_url(
        state="st", code_challenge=challenge, redirect_uri="http://localhost:3000/cb")).query)
    assert q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert GMAIL_SEND in q["scope"][0] and "gmail.readonly" not in q["scope"][0]  # least privilege
    mq = parse_qs(urlsplit(microsoft(lambda r: httpx.Response(200)).authorization_url(
        state="st", code_challenge=challenge, redirect_uri="http://localhost:3000/cb")).query)
    assert "offline_access" in mq["scope"][0] and "Mail.Send" in mq["scope"][0] and "Mail.Read" not in mq["scope"][0]


def test_code_exchange_sends_verifier_and_parses_tokens() -> None:
    seen: dict[str, list[str]] = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen.update(parse_qs(req.content.decode()))
        return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3599,
                                         "scope": f"openid {GMAIL_SEND}"})
    tokens = run(google(h).exchange_code(code="c", code_verifier="v" * 50, redirect_uri="http://x/cb"))
    assert seen["code_verifier"] == ["v" * 50] and seen["grant_type"] == ["authorization_code"]
    assert tokens.refresh_token == "rt" and granted(tokens.scopes, (GMAIL_SEND,)) == []


@pytest.mark.parametrize("status,body,error", [
    (400, {"error": "invalid_grant"}, MailboxAuthError),
    (401, {"error": "invalid_client"}, MailboxRejected),
    (503, {}, MailboxUnavailable),
    (429, {}, MailboxUnavailable),
])
def test_refresh_failures_are_classified(status: int, body: dict[str, str], error: type[Exception]) -> None:
    with pytest.raises(error):
        run(google(lambda r: httpx.Response(status, json=body)).refresh("rt"))


def test_microsoft_scopes_are_normalized() -> None:
    tokens = run(microsoft(lambda r: httpx.Response(200, json={
        "access_token": "at", "refresh_token": "rt2", "expires_in": 3600, "scope": "Mail.Send User.Read openid"}))
        .refresh("rt"))
    assert granted(tokens.scopes, ("https://graph.microsoft.com/Mail.Send",)) == []


# --- Gmail send ------------------------------------------------------------------------------------

def test_gmail_send_success() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        raw = json.loads(req.content)["raw"]
        assert base64.urlsafe_b64decode(raw) == b"MIME"
        return httpx.Response(200, json={"id": "m1", "threadId": "t1"})
    result = run(google(h).send("at", b"MIME"))
    assert (result.provider_message_id, result.provider_thread_id) == ("m1", "t1")


@pytest.mark.parametrize("handler,error", [
    (lambda r: httpx.Response(500, json={"error": {"code": 500}}), SendOutcomeUnknown),  # may have been sent
    (raising(httpx.ReadTimeout), SendOutcomeUnknown),
    (raising(httpx.ConnectError), MailboxUnavailable),  # never reached Google
    (lambda r: httpx.Response(429, json={}), MailboxUnavailable),
    (lambda r: httpx.Response(401, json={}), MailboxAuthError),
    (lambda r: httpx.Response(400, json={"error": {"code": 400}}), MailboxRejected),
])
def test_gmail_send_failures_are_classified(handler: Handler, error: type[Exception]) -> None:
    with pytest.raises(error):
        run(google(handler).send("at", b"MIME"))


# --- Microsoft Graph send (draft, then send) ----------------------------------------------------------

def graph(send: Handler, *, is_draft: bool | None = None) -> tuple[MicrosoftMail, list[str]]:
    calls: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        calls.append(f"{req.method} {req.url.path}")
        if req.method == "POST" and req.url.path.endswith("/me/messages"):
            assert base64.b64decode(req.content) == b"MIME"
            return httpx.Response(201, json={"id": "g1", "conversationId": "c1", "internetMessageId": "<x@y>"})
        if req.method == "POST" and req.url.path.endswith("/send"):
            return send(req)
        if req.method == "GET":
            if is_draft is None:
                raise httpx.ConnectError("down", request=req)
            return httpx.Response(200, json={"isDraft": is_draft})
        return httpx.Response(204)
    return microsoft(h), calls


def test_graph_send_success() -> None:
    client, calls = graph(lambda r: httpx.Response(202))
    result = run(client.send("at", b"MIME"))
    assert result.provider_message_id == "g1" and calls == ["POST /v1.0/me/messages", "POST /v1.0/me/messages/g1/send"]


def test_graph_unclear_send_that_is_still_a_draft_is_safe_to_retry() -> None:
    client, calls = graph(lambda r: httpx.Response(503), is_draft=True)
    with pytest.raises(MailboxUnavailable):
        run(client.send("at", b"MIME"))
    assert "DELETE /v1.0/me/messages/g1" in calls  # the unsent draft is cleaned up


def test_graph_unclear_send_that_left_counts_as_sent() -> None:
    client, _ = graph(raising(httpx.ReadTimeout), is_draft=False)
    assert run(client.send("at", b"MIME")).provider_message_id == "g1"


def test_graph_unverifiable_send_is_unknown() -> None:
    client, _ = graph(raising(httpx.ReadTimeout), is_draft=None)
    with pytest.raises(SendOutcomeUnknown) as exc:
        run(client.send("at", b"MIME"))
    assert exc.value.provider_message_id == "g1"


def test_graph_refusal_discards_the_draft() -> None:
    client, calls = graph(lambda r: httpx.Response(403, json={"error": {"code": "ErrorAccessDenied"}}))
    with pytest.raises(MailboxRejected):
        run(client.send("at", b"MIME"))
    assert "DELETE /v1.0/me/messages/g1" in calls
