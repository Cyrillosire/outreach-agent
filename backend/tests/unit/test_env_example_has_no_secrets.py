"""Guard: .env.example is committed, so it must never contain real secret values (placeholders only)."""

from pathlib import Path

ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"
SECRET_KEYS = {"GROQ_API_KEY", "TAVILY_API_KEY", "BRAVE_API_KEY", "GOOGLE_OAUTH_CLIENT_SECRET",
               "MICROSOFT_OAUTH_CLIENT_SECRET"}
ALLOWED_PLACEHOLDERS = {"", "v1:REPLACE_WITH_GENERATED_KEY"}


def test_env_example_contains_only_placeholders() -> None:
    offenders = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or key.startswith("#"):
            continue
        if (key in SECRET_KEYS or key.endswith(("_SECRET", "_API_KEY", "_TOKEN", "ENCRYPTION_KEYS"))) \
                and value.strip() not in ALLOWED_PLACEHOLDERS:
            offenders.append(key)
    assert not offenders, f".env.example must not contain real values for: {offenders} (put them in .env)"
