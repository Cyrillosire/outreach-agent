from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import (
    AppSettings,
    CurrentSession,
    CurrentUser,
    DbSession,
    client_ip,
    get_rate_limiter,
)
from app.core.config import Settings
from app.core.errors import NotAuthenticated, RateLimited, ServiceUnavailable
from app.core.logging import get_logger
from app.core.ratelimit import RateLimiter
from app.schemas.auth import LoginRequest, SessionOut, UserOut
from app.schemas.common import ERROR_RESPONSES, Envelope
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"], responses=ERROR_RESPONSES)
log = get_logger(__name__)


def _set_session_cookies(response: Response, settings: Settings, token: str, csrf: str) -> None:
    max_age = settings.session_ttl_hours * 3600
    common = {"max_age": max_age, "secure": settings.cookie_secure, "samesite": "lax", "path": "/"}
    response.set_cookie(settings.session_cookie_name, token, httponly=True, **common)  # type: ignore[arg-type]
    # Readable by the dashboard's JS so it can echo it in the X-CSRF-Token header.
    response.set_cookie(settings.csrf_cookie_name, csrf, httponly=False, **common)  # type: ignore[arg-type]


def _clear_session_cookies(response: Response, settings: Settings) -> None:
    for name in (settings.session_cookie_name, settings.csrf_cookie_name):
        response.delete_cookie(name, path="/", secure=settings.cookie_secure, samesite="lax")


def _enforce_login_rate_limit(limiter: RateLimiter, settings: Settings, ip: str | None, email: str) -> None:
    email_key = hashlib.sha256(email.encode()).hexdigest()[:32]
    checks = [
        (f"login:ip-email:{ip}:{email_key}", settings.login_rate_limit_attempts),
        (f"login:ip:{ip}", settings.login_rate_limit_attempts * 5),
    ]
    try:
        results = [limiter.hit(key, limit, settings.login_rate_limit_window_seconds) for key, limit in checks]
    except Exception as exc:
        # Fail closed: without the limiter, password guessing would be unthrottled.
        log.error("login_rate_limiter_unavailable", error_type=type(exc).__name__)
        raise ServiceUnavailable("Login is temporarily unavailable") from exc
    blocked = [r for r in results if not r.allowed]
    if blocked:
        raise RateLimited("Too many login attempts. Try again later.",
                          retry_after_seconds=max(r.retry_after_seconds for r in blocked))


@router.post("/login", response_model=Envelope[SessionOut], summary="Start a session")
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: DbSession,
    settings: AppSettings,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> Envelope[SessionOut]:
    ip = client_ip(request)
    email = auth_service.normalize_login_email(body.email)
    _enforce_login_rate_limit(limiter, settings, ip, email)
    issued = auth_service.authenticate(
        db, email=email, password=body.password, ttl=timedelta(hours=settings.session_ttl_hours),
        ip_address=ip, user_agent=request.headers.get("user-agent"),
    )
    if issued is None:
        raise NotAuthenticated("Invalid email or password", code="invalid_credentials")
    _set_session_cookies(response, settings, issued.token, issued.csrf_token)
    return Envelope(data=SessionOut(
        user=UserOut.model_validate(issued.session.user),
        csrf_token=issued.csrf_token,
        expires_at=issued.session.expires_at,
    ))


@router.post("/logout", status_code=204, summary="End the current session")
def logout(
    request: Request, session: CurrentSession, db: DbSession, settings: AppSettings
) -> Response:
    auth_service.revoke_session(db, session, ip_address=client_ip(request))
    response = Response(status_code=204)
    _clear_session_cookies(response, settings)
    return response


@router.get("/me", response_model=Envelope[UserOut], summary="Current user")
def me(user: CurrentUser) -> Envelope[UserOut]:
    return Envelope(data=UserOut.model_validate(user))
