"""Test configuration.

Unit tests need nothing external. Integration tests (marked `integration`) need PostgreSQL at
TEST_DATABASE_URL; the schema is rebuilt from Alembic migrations once per session, and each test runs
inside a transaction that is rolled back. If the database is unreachable, integration tests are
skipped with an explicit reason (never silently passed).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://outreach:outreach@127.0.0.1:5432/outreach_test"
)
# Must be set before any app module reads settings.
os.environ.update({
    "APP_ENV": "test",
    "DATABASE_URL": TEST_DATABASE_URL,
    "COOKIE_SECURE": "false",
    "LOG_JSON": "false",
    "LOG_LEVEL": "WARNING",
    "CREDENTIAL_ENCRYPTION_KEYS": "v1:" + "A" * 43,
})

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import Engine, create_engine, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parents[1]


def alembic_config(connection: object) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.attributes["connection"] = connection
    return cfg


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(TEST_DATABASE_URL, connect_args={"connect_timeout": 3})
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL not reachable at TEST_DATABASE_URL ({type(exc).__name__}); "
                    "run `docker compose up -d postgres`")
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    # Exercise the full migration path: up, all the way down, and up again.
    for step in ("upgrade", "downgrade", "upgrade"):
        with eng.begin() as conn:
            cfg = alembic_config(conn)
            if step == "upgrade":
                command.upgrade(cfg, "head")
            else:
                command.downgrade(cfg, "base")
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    """Session bound to an outer transaction that is always rolled back.

    `join_transaction_mode="create_savepoint"` lets application code call commit() freely.
    """
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


class FakeRedis:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy

    def ping(self) -> bool:
        if not self.healthy:
            raise ConnectionError("redis down")
        return True


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr("app.api.health.get_redis", lambda: fake)
    return fake


@pytest.fixture
def client(db: Session, fake_redis: FakeRedis) -> Iterator[TestClient]:
    from app.api.deps import get_rate_limiter
    from app.core.config import get_settings
    from app.core.ratelimit import InMemoryRateLimiter
    from app.db.session import get_db
    from app.main import create_app

    app = create_app(get_settings())
    limiter = InMemoryRateLimiter()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    with TestClient(app) as c:
        yield c


@pytest.fixture
def make_user(db: Session):  # type: ignore[no-untyped-def]
    from app.domain.states import UserRole
    from app.services.auth import create_user

    counter = {"n": 0}

    def _make(role: UserRole = UserRole.ADMIN, password: str = "correct horse battery staple",
              email: str | None = None):  # type: ignore[no-untyped-def]
        counter["n"] += 1
        return create_user(db, email=email or f"user{counter['n']}@example.com", password=password,
                           display_name=f"User {counter['n']}", role=role)

    return _make


class AuthedClient:
    """A logged-in TestClient that attaches the CSRF header to every unsafe request."""

    def __init__(self, client: TestClient, csrf: str, user: object) -> None:
        self.client, self.csrf, self.user = client, csrf, user

    def get(self, url: str, **kw):  # type: ignore[no-untyped-def]
        return self.client.get(url, **kw)

    def _unsafe(self, method: str, url: str, headers: dict[str, str] | None = None, **kw):  # type: ignore[no-untyped-def]
        return self.client.request(method, url, headers={"X-CSRF-Token": self.csrf, **(headers or {})}, **kw)

    def post(self, url: str, **kw):  # type: ignore[no-untyped-def]
        return self._unsafe("POST", url, **kw)

    def put(self, url: str, **kw):  # type: ignore[no-untyped-def]
        return self._unsafe("PUT", url, **kw)

    def patch(self, url: str, **kw):  # type: ignore[no-untyped-def]
        return self._unsafe("PATCH", url, **kw)


@pytest.fixture
def authed(client: TestClient, make_user) -> AuthedClient:  # type: ignore[no-untyped-def]
    user = make_user(email="owner@example.com")
    r = client.post("/api/v1/auth/login", json={"email": "owner@example.com",
                                                "password": "correct horse battery staple"})
    assert r.status_code == 200, r.text
    return AuthedClient(client, r.json()["data"]["csrf_token"], user)
