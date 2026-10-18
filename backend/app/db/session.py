"""Engine and session management.

Synchronous SQLAlchemy + psycopg 3 is used deliberately: FastAPI runs sync endpoints in a threadpool,
the code is simpler to reason about for transactional state transitions, and it avoids event-loop
driver incompatibilities on developer machines. I/O-heavy research fan-out (Milestone 3) will use
async HTTP inside workers with short, explicit DB transactions.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.database_pool_size,
        pool_pre_ping=True,
        pool_recycle=1800,
        # Never echo SQL: bound parameters could contain message content.
        echo=False,
    )


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request; commit is explicit in services."""
    session = get_sessionmaker()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
