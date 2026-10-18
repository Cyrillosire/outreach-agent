"""Shared FastAPI dependencies: database, Redis, rate limiting, authentication, CSRF, roles."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from functools import lru_cache
from typing import Annotated

import redis
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import NotAuthenticated, PermissionDenied
from app.core.ratelimit import RateLimiter, RedisRateLimiter
from app.core.security import tokens_match
from app.db.session import get_db
from app.domain.states import UserRole
from app.models import User, UserSession
from app.services import auth as auth_service

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_HEADER = "X-CSRF-Token"

DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


@lru_cache
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(
        get_settings().redis_url.get_secret_value(), socket_timeout=2, socket_connect_timeout=2
    )


def get_rate_limiter() -> RateLimiter:
    return RedisRateLimiter(get_redis())


def client_ip(request: Request) -> str | None:
    # Behind a reverse proxy, configure uvicorn --proxy-headers/--forwarded-allow-ips so this is the
    # real client address. We never trust X-Forwarded-For directly here.
    host = request.client.host if request.client else None
    if host is None:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None  # unix sockets, test clients, malformed proxy values


def get_current_session(request: Request, db: DbSession, settings: AppSettings) -> UserSession:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise NotAuthenticated("Authentication required")
    session = auth_service.resolve_session(db, token)
    if session is None:
        raise NotAuthenticated("Session is invalid or has expired")
    if request.method in UNSAFE_METHODS:
        presented = request.headers.get(CSRF_HEADER, "")
        if not presented or not tokens_match(presented, session.csrf_token_hash):
            raise PermissionDenied("Missing or invalid CSRF token", code="csrf_failed")
    return session


CurrentSession = Annotated[UserSession, Depends(get_current_session)]


def get_current_user(session: CurrentSession) -> User:
    return session.user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(*roles: UserRole) -> Callable[[User], User]:
    def _check(user: CurrentUser) -> User:
        if user.role not in roles:
            raise PermissionDenied("You do not have permission to perform this action")
        return user

    return _check


AdminUser = Annotated[User, Depends(require_role(UserRole.ADMIN))]
