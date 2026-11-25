"""Request audit trail and rate limiting (TASK-005, TASK-006).

Both features are process-wide side effects, so every test here builds its
own app with the feature explicitly enabled and a temp-dir log sink. Nothing
is written inside the repository.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import create_app
from app.rate_limit import TokenBucketLimiter


# --- TASK-005: request logging ------------------------------------------------


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "app.log"


@pytest.fixture
def logging_client(clean_env, db_path: Path, log_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "ENV": "testing",
            "DATABASE_PATH": str(db_path),
            "REQUEST_LOG_ENABLED": True,
            "REQUEST_LOG_PATH": str(log_path),
        }
    )
    return app.test_client()


def _entries(log_path: Path) -> list[dict]:
    text = log_path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_a_request_writes_one_json_line(logging_client, log_path):
    assert logging_client.get("/health").status_code in (200, 503)
    records = _entries(log_path)
    assert len(records) == 1
    record = records[0]
    assert record["method"] == "GET"
    assert record["path"] == "/health"
    assert record["endpoint"] == "health.health"
    assert record["status_code"] in (200, 503)
    assert record["timestamp"]
    assert isinstance(record["duration_ms"], float)


def test_the_log_directory_is_created_on_demand(logging_client, log_path):
    logging_client.get("/health")
    assert log_path.parent.is_dir()
    assert log_path.is_file()


def test_each_request_appends_a_line(logging_client, log_path):
    for _ in range(3):
        logging_client.get("/health")
    assert len(_entries(log_path)) == 3


def test_error_responses_are_logged_with_their_status(logging_client, log_path):
    logging_client.get("/api/journal/entries/4242")
    record = _entries(log_path)[-1]
    assert record["status_code"] == 404


def test_x_forwarded_for_is_preferred_over_the_socket_address(
    logging_client, log_path
):
    logging_client.get(
        "/health", headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"}
    )
    assert _entries(log_path)[-1]["client_ip"] == "203.0.113.7"


def test_client_ip_falls_back_to_the_remote_address(logging_client, log_path):
    logging_client.get("/health")
    assert _entries(log_path)[-1]["client_ip"] == "127.0.0.1"


def test_the_query_string_is_recorded(logging_client, log_path):
    logging_client.get("/api/journal/entries?limit=5")
    assert _entries(log_path)[-1]["query"] == "limit=5"


def test_logging_is_off_unless_enabled(client, tmp_path):
    client.get("/health")
    assert not list(tmp_path.glob("**/app.log"))


def test_an_unwritable_sink_does_not_break_requests(clean_env, db_path, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("occupied", encoding="utf-8")
    app = create_app(
        {
            "TESTING": True,
            "ENV": "testing",
            "DATABASE_PATH": str(db_path),
            "REQUEST_LOG_ENABLED": True,
            "REQUEST_LOG_PATH": str(blocker / "logs" / "app.log"),
        }
    )
    # The sink is unusable, but the API still answers.
    assert app.test_client().get("/health").status_code in (200, 503)


# --- TASK-006: rate limiting --------------------------------------------------


class _ManualClock:
    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def limited_client(clean_env, db_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "ENV": "testing",
            "DATABASE_PATH": str(db_path),
            "RATE_LIMIT_ENABLED": True,
            "RATE_LIMIT_REQUESTS": 3,
            "RATE_LIMIT_WINDOW_SECONDS": 60,
        }
    )
    return app.test_client()


def test_bucket_allows_up_to_capacity_then_refuses():
    limiter = TokenBucketLimiter(capacity=2, window_seconds=60, clock=_ManualClock())
    assert limiter.consume("c")[0] is True
    assert limiter.consume("c")[0] is True
    allowed, quota = limiter.consume("c")
    assert allowed is False
    assert quota["retry_after_seconds"] > 0


def test_bucket_refills_over_time():
    clock = _ManualClock()
    limiter = TokenBucketLimiter(capacity=2, window_seconds=60, clock=clock)
    limiter.consume("c")
    limiter.consume("c")
    assert limiter.consume("c")[0] is False
    clock.advance(31)  # one token per 30s at 2/60s
    assert limiter.consume("c")[0] is True


def test_buckets_are_per_client():
    limiter = TokenBucketLimiter(capacity=1, window_seconds=60, clock=_ManualClock())
    assert limiter.consume("a")[0] is True
    assert limiter.consume("a")[0] is False
    assert limiter.consume("b")[0] is True


def test_peek_does_not_consume_a_token():
    limiter = TokenBucketLimiter(capacity=2, window_seconds=60, clock=_ManualClock())
    assert limiter.peek("c")["remaining"] == 2
    assert limiter.peek("c")["remaining"] == 2
    limiter.consume("c")
    assert limiter.peek("c")["remaining"] == 1


def test_requests_over_the_limit_return_429(limited_client):
    for _ in range(3):
        assert limited_client.get("/health").status_code in (200, 503)
    response = limited_client.get("/health")
    assert response.status_code == 429
    body = response.get_json()
    assert body["rate_limit_exceeded"] is True
    assert body["error"]["code"] == "rate_limit_exceeded"
    assert body["error"]["details"]["limit"] == 3


def test_quota_headers_are_advertised(limited_client):
    response = limited_client.get("/health")
    assert response.headers["X-RateLimit-Limit"] == "3"
    assert int(response.headers["X-RateLimit-Remaining"]) <= 3


def test_status_endpoint_reports_remaining_quota(limited_client):
    limited_client.get("/health")
    body = limited_client.get("/api/limits/status").get_json()["data"]
    assert body["limit"] == 3
    assert body["enabled"] is True
    assert body["remaining"] <= 3
    assert "reset_in_seconds" in body


def test_status_endpoint_stays_reachable_while_throttled(limited_client):
    for _ in range(5):
        limited_client.get("/health")
    response = limited_client.get("/api/limits/status")
    assert response.status_code == 200
    assert response.get_json()["data"]["remaining"] == 0


def test_the_limiter_keys_on_the_forwarded_client(limited_client):
    for _ in range(3):
        limited_client.get("/health", headers={"X-Forwarded-For": "198.51.100.1"})
    throttled = limited_client.get(
        "/health", headers={"X-Forwarded-For": "198.51.100.1"}
    )
    assert throttled.status_code == 429
    # A different client still has its full allowance.
    other = limited_client.get("/health", headers={"X-Forwarded-For": "198.51.100.2"})
    assert other.status_code in (200, 503)


def test_rate_limiting_is_off_unless_enabled(client):
    for _ in range(150):
        assert client.get("/health").status_code in (200, 503)


def test_status_endpoint_works_with_limiting_disabled(client):
    body = client.get("/api/limits/status").get_json()["data"]
    assert body["enabled"] is False
    assert body["limit"] >= 1
