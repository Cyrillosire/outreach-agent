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

    # --- Background jobs -------------------------------------------------------------------------
    worker_concurrency: int = Field(default=4, ge=1, le=32)
    worker_poll_seconds: float = Field(default=2.0, gt=0)
    job_lease_seconds: int = Field(default=300, ge=30)
    job_max_attempts: int = Field(default=4, ge=1, le=20)
    worker_metrics_port: int = Field(default=9101, ge=0)  # 0 disables

    # --- LLM providers ---------------------------------------------------------------------------
    groq_api_key: SecretStr = SecretStr("")
    groq_base_url: str = "https://api.groq.com/openai/v1"
    ollama_base_url: str = "http://127.0.0.1:11434"
    # Ordered fallback chains, "provider:model" separated by commas. Extraction may fall back to the
    # local model; reasoning-heavy tasks never fall back below the listed models.
    llm_extraction_chain: str = "groq:openai/gpt-oss-20b,groq:qwen/qwen3.8-27b,ollama:qwen3:4b"
    llm_reasoning_chain: str = "groq:openai/gpt-oss-120b,groq:openai/gpt-oss-20b"
    llm_concurrency: int = Field(default=2, ge=1, le=16)
    llm_timeout_seconds: float = Field(default=60, gt=0)
    ollama_timeout_seconds: float = Field(default=600, gt=0)  # local CPU inference is slow

    # --- Search providers -------------------------------------------------------------------------
    tavily_api_key: SecretStr = SecretStr("")
    brave_api_key: SecretStr = SecretStr("")
    search_provider_order: str = "tavily,brave"
    search_concurrency: int = Field(default=2, ge=1, le=16)
    search_cache_hours: int = Field(default=24, ge=0)

    # --- Web fetching ----------------------------------------------------------------------------
    fetch_concurrency: int = Field(default=4, ge=1, le=32)
    fetch_timeout_seconds: float = Field(default=20, gt=0)
    fetch_max_bytes: int = Field(default=2_000_000, ge=10_000)
    fetch_user_agent: str = "OutreachAgentResearchBot/0.3 (+business research; respects robots.txt)"
    source_cache_hours: int = Field(default=168, ge=0)

    # --- Research budgets --------------------------------------------------------------------------
    research_auto_start: bool = True
    research_company_freshness_days: int = Field(default=30, ge=0)
    research_company_max_queries: int = Field(default=3, ge=0, le=20)
    research_person_max_queries: int = Field(default=3, ge=0, le=20)
    research_company_max_sources: int = Field(default=8, ge=1, le=50)
    research_person_max_sources: int = Field(default=6, ge=1, le=50)
    research_time_budget_seconds: int = Field(default=900, ge=30)

    @property
    def is_production(self) -> bool:
        return self.app_env == Environment.PRODUCTION

    @staticmethod
    def parse_chain(raw: str) -> list[tuple[str, str]]:
        chain = []
        for item in filter(None, (p.strip() for p in raw.split(","))):
            provider, sep, model = item.partition(":")
            if not sep or not model:
                raise ValueError(f"invalid model chain entry {item!r}; expected provider:model")
            chain.append((provider.strip().lower(), model.strip()))
        return chain

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
