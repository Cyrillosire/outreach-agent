"""Idempotency-Key support for mutating requests that clients may retry.

Contract (mirrors common payment-API semantics):
- Same key + same request  -> the original response is replayed (header `Idempotent-Replayed: true`).
- Same key + different request -> 422 `idempotency_key_reused`.
- A concurrent duplicate blocks on the unique index until the first request commits, then replays.
- Failed requests (any exception) roll back the key with the work, so the client may retry.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.domain.states import IdempotencyState
from app.models import IdempotencyKey, User

HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replayed"
TTL = timedelta(hours=24)
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,200}$")


class IdempotencyKeyReused(AppError):
    status_code = 422
    code = "idempotency_key_reused"


class IdempotencyInProgress(AppError):
    status_code = 409
    code = "idempotency_in_progress"


def request_hash(*parts: bytes | str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        data = part.encode() if isinstance(part, str) else part
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def run_idempotent(
    db: Session,
    *,
    user: User,
    key: str | None,
    scope: str,
    fingerprint: str,
    action: Callable[[], tuple[int, Any]],
) -> JSONResponse:
    """Run `action` (which must flush, not commit) and commit once, recording the response under `key`."""
    if key is None:
        status, body = action()
        db.commit()
        return JSONResponse(jsonable_encoder(body), status_code=status)

    if not _KEY_PATTERN.match(key):
        raise AppError(f"{HEADER} must be 8-200 characters of letters, digits, '.', '_', ':' or '-'",
                       code="invalid_idempotency_key")

    record = _claim(db, user=user, key=key, scope=scope, fingerprint=fingerprint)
    if isinstance(record, JSONResponse):
        return record

    status, body = action()
    encoded = jsonable_encoder(body)
    record.state = IdempotencyState.COMPLETED
    record.response_status = status
    record.response_body = encoded
    db.commit()
    return JSONResponse(encoded, status_code=status)


def _claim(db: Session, *, user: User, key: str, scope: str, fingerprint: str) -> IdempotencyKey | JSONResponse:
    now = datetime.now(UTC)
    for _ in range(2):
        record = IdempotencyKey(workspace_id=user.workspace_id, user_id=user.id, scope=scope, key=key,
                                request_sha256=fingerprint, state=IdempotencyState.IN_PROGRESS,
                                expires_at=now + TTL)
        try:
            with db.begin_nested():
                db.add(record)
                db.flush()
            return record
        except IntegrityError:
            pass
        existing = db.scalar(select(IdempotencyKey).where(
            IdempotencyKey.user_id == user.id, IdempotencyKey.scope == scope, IdempotencyKey.key == key))
        if existing is None:
            continue  # deleted concurrently; try once more
        if existing.expires_at <= now:
            db.delete(existing)
            db.flush()
            continue
        if existing.request_sha256 != fingerprint:
            raise IdempotencyKeyReused(f"This {HEADER} was already used for a different request.")
        if existing.state is IdempotencyState.COMPLETED and existing.response_status is not None:
            return JSONResponse(existing.response_body, status_code=existing.response_status,
                                headers={REPLAY_HEADER: "true"})
        raise IdempotencyInProgress("A request with this key is still being processed.")
    raise IdempotencyInProgress("Could not acquire the idempotency key; retry shortly.")
