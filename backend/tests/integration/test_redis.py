import os
import uuid

import pytest
import redis

from app.core.ratelimit import RedisRateLimiter

pytestmark = pytest.mark.integration

# Database 15 is reserved for tests so development rate-limit state is untouched.
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15")


@pytest.fixture
def redis_client() -> redis.Redis:
    client = redis.Redis.from_url(TEST_REDIS_URL, socket_connect_timeout=2)
    try:
        client.ping()
    except redis.exceptions.ConnectionError:
        pytest.skip("Redis not reachable at TEST_REDIS_URL; run `docker compose up -d redis`")
    return client


def test_redis_rate_limiter_counts_and_expires(redis_client: redis.Redis) -> None:
    limiter = RedisRateLimiter(redis_client)
    key = f"test:{uuid.uuid4()}"
    results = [limiter.hit(key, limit=2, window_seconds=30) for _ in range(3)]
    assert [r.allowed for r in results] == [True, True, False]
    assert 0 < results[-1].retry_after_seconds <= 30
    # The window must not be extended by further hits (expire NX).
    assert 0 < redis_client.ttl(f"ratelimit:{key}") <= 30
    limiter.reset(key)
    assert limiter.hit(key, limit=2, window_seconds=30).allowed
    limiter.reset(key)
