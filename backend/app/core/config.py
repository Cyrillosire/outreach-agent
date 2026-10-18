"""Application configuration, loaded exclusively from environment variables (or a local .env in dev)."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Environment = Environment.DEVELOPMENT
    app_name: str = "Outreach Agent"
    log_level: str = "INFO"
    log_json: bool = True

    database_url: SecretStr = SecretStr("postgresql+psycopg://outreach:outreach@127.0.0.1:5432/outreach")
    database_pool_size: int = Field(default=10, ge=1, le=100)
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")

    # Comma-separated "version:base64-32-byte-key" pairs, e.g. "v1:AbC...=,v2:XyZ...=".
    # The active key encrypts new data; older keys remain for decryption during rotation.
    credential_encryption_keys: SecretStr = SecretStr("")
    credential_encryption_active_key: str = "v1"

    session_cookie_name: str = "oa_session"
    csrf_cookie_name: str = "oa_csrf"
    session_ttl_hours: int = Field(default=12, ge=1, le=24 * 30)
    cookie_secure: bool = True
    # Origins allowed to issue state-changing requests (the dashboard's public origin).
    allowed_origins: list[str] = ["http://localhost:3000"]

    login_rate_limit_attempts: int = Field(default=10, ge=1)
    login_rate_limit_window_seconds: int = Field(default=300, ge=1)

    metrics_enabled: bool = True

    import_max_file_mb: int = Field(default=5, ge=1, le=50)
    import_max_rows: int = Field(default=5000, ge=1, le=50_000)
    import_max_columns: int = Field(default=100, ge=2, le=500)

    @property
    def is_production(self) -> bool:
        return self.app_env == Environment.PRODUCTION

    @model_validator(mode="after")
    def _enforce_production_safety(self) -> Settings:
        if not self.is_production:
            return self
        problems: list[str] = []
        if not self.cookie_secure:
            problems.append("COOKIE_SECURE must be true in production")
        if not self.credential_encryption_keys.get_secret_value():
            problems.append("CREDENTIAL_ENCRYPTION_KEYS must be set in production")
        if "outreach:outreach@" in self.database_url.get_secret_value():
            problems.append("DATABASE_URL must not use the development default credentials")
        if any(o.startswith("http://") and "localhost" not in o for o in self.allowed_origins):
            problems.append("ALLOWED_ORIGINS must use https in production")
        if problems:
            raise ValueError("; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
