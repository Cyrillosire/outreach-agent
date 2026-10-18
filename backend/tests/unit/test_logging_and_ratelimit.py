from app.core.logging import REDACTED, redact_event, redact_value
from app.core.ratelimit import InMemoryRateLimiter


def test_sensitive_keys_are_redacted() -> None:
    event = redact_event(None, "info", {
        "event": "oauth_callback",
        "refresh_token": "1//abc",
        "Authorization": "Bearer xyz",
        "password": "hunter2hunter2",
        "account_id": "acct-1",
    })
    assert event["refresh_token"] == REDACTED
    assert event["Authorization"] == REDACTED
    assert event["password"] == REDACTED
    assert event["account_id"] == "acct-1"


def test_nested_structures_are_redacted() -> None:
    out = redact_value({"provider": {"response": {"access_token": "ya29.zzz", "ok": True}}, "items": [
        {"client_secret": "s"}]})
    assert out["provider"]["response"]["access_token"] == REDACTED
    assert out["provider"]["response"]["ok"] is True
    assert out["items"][0]["client_secret"] == REDACTED


def test_token_shaped_values_are_scrubbed_under_innocent_keys() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    out = redact_value({
        "error": f"upstream said: Bearer abc.def and {jwt}",
        "detail": "google token ya29.a0AfH6SMBx and refresh 1//0gAbCdEfGhIjKlMnOpQrStUv",
        "dsn": "postgresql+psycopg://user:pw@db:5432/x",
    })
    assert "abc.def" not in out["error"] and jwt not in out["error"]
    assert "ya29." not in out["detail"] and "1//0g" not in out["detail"]
    assert "pw@" not in out["dsn"]


def test_message_bodies_are_redacted_by_key() -> None:
    assert redact_value({"body_text": "Hi Jane, ..."})["body_text"] == REDACTED


def test_in_memory_rate_limiter_window() -> None:
    now = [1000.0]
    limiter = InMemoryRateLimiter(clock=lambda: now[0])
    results = [limiter.hit("k", limit=3, window_seconds=60) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert results[-1].retry_after_seconds == 60
    now[0] += 61
    assert limiter.hit("k", limit=3, window_seconds=60).allowed
    limiter.reset("k")
    assert limiter.hit("k", limit=3, window_seconds=60).remaining == 2
