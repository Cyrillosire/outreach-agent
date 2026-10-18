"""Authenticated operational status for the Settings page."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from app.api.deps import AppSettings, CurrentUser, DbSession
from app.api.health import check_database, check_redis
from app.schemas.common import ERROR_RESPONSES, Envelope

router = APIRouter(prefix="/system", tags=["system"], responses=ERROR_RESPONSES)

_ALEMBIC_DIR = Path(__file__).resolve().parents[3] / "alembic"


class SystemStatus(BaseModel):
    environment: str
    database: bool
    redis: bool
    schema_revision: str | None
    schema_head: str | None
    schema_up_to_date: bool
    credential_encryption_configured: bool


def _head_revision() -> str | None:
    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    return ScriptDirectory.from_config(cfg).get_current_head()


@router.get("/status", response_model=Envelope[SystemStatus], summary="Operational status")
def status(_: CurrentUser, db: DbSession, settings: AppSettings) -> Envelope[SystemStatus]:
    db_ok = check_database(db)
    current = db.scalar(text("SELECT version_num FROM alembic_version")) if db_ok else None
    head = _head_revision()
    return Envelope(data=SystemStatus(
        environment=settings.app_env.value,
        database=db_ok,
        redis=check_redis(),
        schema_revision=current,
        schema_head=head,
        schema_up_to_date=current is not None and current == head,
        credential_encryption_configured=bool(settings.credential_encryption_keys.get_secret_value()),
    ))
