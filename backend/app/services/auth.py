"""Login, session resolution, and logout. HTTP concerns (cookies, rate limits) live in the API layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.security import hash_password, hash_token, new_token, password_needs_rehash, verify_password
from app.domain.states import ActorType, AuditOutcome, UserRole
from app.models import User, UserSession, Workspace
from app.services import audit

# Avoid a DB write on every request; refresh last_seen_at at most this often.
_LAST_SEEN_RESOLUTION = timedelta(minutes=5)


@dataclass(frozen=True)
class IssuedSession:
    session: UserSession
    token: str
    csrf_token: str


def normalize_login_email(email: str) -> str:
    return email.strip().lower()


def authenticate(
    db: Session,
    *,
    email: str,
    password: str,
    ttl: timedelta,
    ip_address: str | None,
    user_agent: str | None,
) -> IssuedSession | None:
    user = db.scalar(select(User).where(User.email == normalize_login_email(email)))
    valid = verify_password(user.password_hash if user else None, password)
    if not user or not valid or not user.is_active:
        metrics.auth_events_total.labels("login", "failure").inc()
        audit.record(
            db, action="auth.login", outcome=AuditOutcome.FAILURE, actor_type=ActorType.USER,
            actor_id=user.id if user else None, workspace_id=user.workspace_id if user else None,
            metadata={"reason": "inactive" if user and valid else "invalid_credentials"},
            ip_address=ip_address,
        )
        db.commit()
        return None

    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    now = datetime.now(UTC)
    token, csrf = new_token(), new_token()
    session = UserSession(
        user_id=user.id,
        token_hash=hash_token(token),
        csrf_token_hash=hash_token(csrf),
        created_at=now,
        last_seen_at=now,
        expires_at=now + ttl,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:300] or None,
    )
    session.user = user
    user.last_login_at = now
    db.add(session)
    audit.record(
        db, action="auth.login", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
        actor_id=user.id, workspace_id=user.workspace_id, entity_type="user", entity_id=user.id,
        ip_address=ip_address,
    )
    db.commit()
    metrics.auth_events_total.labels("login", "success").inc()
    return IssuedSession(session=session, token=token, csrf_token=csrf)


def resolve_session(db: Session, token: str) -> UserSession | None:
    session = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(token)))
    now = datetime.now(UTC)
    if session is None or session.revoked_at is not None or session.expires_at <= now:
        return None
    if not session.user.is_active:
        return None
    if now - session.last_seen_at > _LAST_SEEN_RESOLUTION:
        session.last_seen_at = now
        db.commit()
    return session


def revoke_session(db: Session, session: UserSession, *, ip_address: str | None) -> None:
    session.revoked_at = datetime.now(UTC)
    audit.record(
        db, action="auth.logout", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.USER,
        actor_id=session.user_id, workspace_id=session.user.workspace_id, ip_address=ip_address,
    )
    db.commit()


def create_user(
    db: Session,
    *,
    email: str,
    password: str,
    display_name: str,
    role: UserRole,
    workspace_name: str | None = None,
) -> User:
    """Create a user in the existing workspace (or a new one if none exists). No public signup."""
    normalized = normalize_login_email(email)
    if db.scalar(select(User.id).where(User.email == normalized)):
        raise ValueError("a user with this email already exists")
    workspace = db.scalar(select(Workspace).order_by(Workspace.created_at).limit(1))
    if workspace is None:
        workspace = Workspace(name=workspace_name or "Default workspace")
        db.add(workspace)
        db.flush()
    user = User(
        workspace_id=workspace.id,
        email=normalized,
        display_name=display_name,
        password_hash=hash_password(password),
        role=role,
    )
    db.add(user)
    db.flush()
    audit.record(
        db, action="user.create", outcome=AuditOutcome.SUCCESS, actor_type=ActorType.SYSTEM,
        workspace_id=workspace.id, entity_type="user", entity_id=user.id, metadata={"role": role.value},
    )
    db.commit()
    return user
