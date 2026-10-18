import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.security import (
    hash_password,
    hash_token,
    new_token,
    tokens_match,
    verify_password,
)

PASSWORD = "correct horse battery staple"


def test_password_hash_and_verify() -> None:
    hashed = hash_password(PASSWORD)
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, PASSWORD)
    assert not verify_password(hashed, PASSWORD + "x")


def test_short_password_rejected() -> None:
    with pytest.raises(ValueError, match="at least 12"):
        hash_password("short")


def test_verify_without_user_is_false_not_error() -> None:
    assert verify_password(None, PASSWORD) is False
    assert verify_password("not-a-hash", PASSWORD) is False


def test_tokens_are_random_and_compared_by_hash() -> None:
    a, b = new_token(), new_token()
    assert a != b and len(a) >= 43
    assert len(hash_token(a)) == 64
    assert tokens_match(a, hash_token(a))
    assert not tokens_match(b, hash_token(a))


def _prod(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "production",
        "cookie_secure": True,
        "credential_encryption_keys": "v1:" + "A" * 43,
        "database_url": "postgresql+psycopg://app:s3cret@db:5432/outreach",
        "allowed_origins": ["https://outreach.example.com"],
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type,call-arg]


def test_valid_production_settings() -> None:
    assert _prod().is_production


@pytest.mark.parametrize("override,message", [
    ({"cookie_secure": False}, "COOKIE_SECURE"),
    ({"credential_encryption_keys": ""}, "CREDENTIAL_ENCRYPTION_KEYS"),
    ({"database_url": "postgresql+psycopg://outreach:outreach@db/outreach"}, "DATABASE_URL"),
    ({"allowed_origins": ["http://outreach.example.com"]}, "https"),
])
def test_production_rejects_unsafe_settings(override: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _prod(**override)


def test_secrets_do_not_appear_in_repr() -> None:
    s = _prod()
    assert "s3cret" not in repr(s)
    assert "AAAA" not in repr(s)
