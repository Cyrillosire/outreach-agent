"""Per-request context (request id) propagated through contextvars into logs and error bodies."""

from __future__ import annotations

import json
import re
import time
import uuid
from contextvars import ContextVar

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import metrics
from app.core.logging import get_logger

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_VALID_INCOMING_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

log = get_logger("http")


def current_request_id() -> str | None:
    return _request_id.get()


class RequestContextMiddleware:
    """Assigns a request id, binds it to logs, records metrics, and logs one line per request.

    Logged fields deliberately exclude query strings, headers, and bodies.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope.get("headers", [])).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _VALID_INCOMING_ID.match(incoming) else uuid.uuid4().hex
        token = _request_id.set(request_id)
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                message.setdefault("headers", [])
                message["headers"].append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            # Unhandled errors are converted here (not in Starlette's outermost ServerErrorMiddleware)
            # so the response still carries the request id. Details are logged, never returned.
            log.exception("unhandled_exception", error_type=type(exc).__name__)
            if response_started:
                raise
            body = json.dumps(
                {"error": {"code": "internal_error", "message": "An unexpected error occurred",
                           "details": None, "request_id": request_id}}
            ).encode()
            await send_wrapper({"type": "http.response.start", "status": 500,
                                "headers": [(b"content-type", b"application/json")]})
            await send_wrapper({"type": "http.response.body", "body": body})
        finally:
            elapsed = time.perf_counter() - start
            route = scope.get("route")
            route_path = getattr(route, "path", None) or "unmatched"
            metrics.observe_http_request(scope["method"], route_path, status_code, elapsed)
            log.info(
                "http_request",
                method=scope["method"],
                route=route_path,
                status=status_code,
                duration_ms=round(elapsed * 1000, 2),
            )
            structlog.contextvars.unbind_contextvars("request_id")
            _request_id.reset(token)
