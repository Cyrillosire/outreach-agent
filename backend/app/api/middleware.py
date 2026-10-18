"""Security middleware: origin checks for state-changing requests and baseline response headers."""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import error_body

_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
]
# The OpenAPI docs page needs scripts/styles from FastAPI's CDN, so it gets no CSP override.
_DOCS_PATHS = ("/api/docs", "/api/redoc")


class OriginCheckMiddleware:
    """Rejects cross-origin state-changing requests (login CSRF and defense in depth beyond SameSite).

    Browsers always send `Origin` on cross-origin POSTs. Requests without `Origin` (server-to-server,
    CLI) are allowed only if `Sec-Fetch-Site` does not mark them as cross-site.
    """

    def __init__(self, app: ASGIApp, allowed_origins: list[str]) -> None:
        self.app = app
        self.allowed = {o.rstrip("/") for o in allowed_origins}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in _UNSAFE:
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
            origin = headers.get("origin")
            fetch_site = headers.get("sec-fetch-site")
            rejected = (origin is not None and origin.rstrip("/") not in self.allowed) or (
                origin is None and fetch_site == "cross-site"
            )
            if rejected:
                body = json.dumps(error_body("origin_rejected", "Cross-origin request rejected")).encode()
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_docs = scope["path"].startswith(_DOCS_PATHS)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = {k.lower() for k, _ in message.get("headers", [])}
                extra = [
                    (k, v) for k, v in _SECURITY_HEADERS
                    if k not in existing and not (is_docs and k == b"content-security-policy")
                ]
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, send_wrapper)
