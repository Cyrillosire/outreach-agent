"""Append-only audit trail. Callers own the transaction so the audit row commits with the change."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import redact_value
from app.core.request_context import current_request_id
from app.domain.states import ActorType, AuditOutcome
from app.models import AuditLog


def record(
    db: Session,
    *,
    action: str,
    outcome: AuditOutcome,
    actor_type: ActorType,
    actor_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        workspace_id=workspace_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        outcome=outcome,
        metadata_=redact_value(metadata or {}),
        request_id=current_request_id(),
        ip_address=ip_address,
    )
    db.add(entry)
    return entry
