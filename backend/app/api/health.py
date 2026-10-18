"""Unauthenticated infrastructure probes. They reveal no configuration or version details."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import get_redis
from app.core.logging import get_logger
from app.db.session import get_db

router = APIRouter(tags=["health"])
log = get_logger(__name__)


def check_database(db: Session) -> bool:
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        log.warning("readiness_db_failed", error_type=type(exc).__name__)
        return False


def check_redis() -> bool:
    try:
        return bool(get_redis().ping())
    except Exception as exc:
        log.warning("readiness_redis_failed", error_type=type(exc).__name__)
        return False


@router.get("/healthz", summary="Liveness probe")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe (database and Redis reachable)")
def readyz(db: Annotated[Session, Depends(get_db)]) -> JSONResponse:
    checks = {"database": check_database(db), "redis": check_redis()}
    ok = all(checks.values())
    return JSONResponse(
        {"status": "ok" if ok else "unavailable", "checks": {k: "ok" if v else "fail" for k, v in checks.items()}},
        status_code=200 if ok else 503,
    )
