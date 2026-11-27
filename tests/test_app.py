"""Application initialization, health check and schema tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import create_app
from app.database.schema import TABLE_NAMES, existing_tables


def test_app_factory_builds_independent_instances(clean_env, tmp_path: Path):
    first = create_app({"DATABASE_PATH": str(tmp_path / "a.db")})
    second = create_app({"DATABASE_PATH": str(tmp_path / "b.db")})
    assert first is not second
    assert first.config["DATABASE_PATH"] != second.config["DATABASE_PATH"]


def test_app_starts_without_any_credentials(app):
    assert app.config["WEATHER_PROVIDER_CONFIGURED"] is False
    assert app.config["SECRET_KEY"]  # ephemeral key generated for dev/test


def test_root_redirects_to_the_ui(client):
    """A browser hitting the bare domain must land on the UI, not JSON."""
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/ui/")


def test_api_index_lists_endpoints(client):
    response = client.get("/api")
    assert response.status_code == 200
    assert response.get_json()["name"] == "weather-journal"


def test_health_endpoint_returns_json_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.mimetype == "application/json"
    body = response.get_json()
    assert body["status"] == "ok"
    assert body["database"]["connected"] is True
    assert body["database"]["missing_tables"] == []
    assert body["weather_provider"]["available"] is False


def test_database_file_and_schema_are_created(app, db_path: Path, connection):
    assert db_path.exists()
    assert set(TABLE_NAMES).issubset(existing_tables(connection))


def test_schema_initialization_is_idempotent(app, connection):
    from app.database import schema

    schema.initialize(connection)
    schema.initialize(connection)
    assert set(TABLE_NAMES).issubset(existing_tables(connection))


def test_foreign_keys_enforced(connection):
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_unknown_route_returns_json_error(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.mimetype == "application/json"
    assert "error" in response.get_json()


def test_method_not_allowed_returns_json_error(client):
    response = client.put("/health")
    assert response.status_code == 405
    assert "error" in response.get_json()


def test_health_reports_degraded_when_a_required_table_is_missing(client, connection):
    dropped = ["journal_entries"]
    with connection:
        for table in dropped:
            connection.execute(f"DROP TABLE {table}")

    response = client.get("/health")
    assert response.status_code == 503
    assert response.mimetype == "application/json"
    body = response.get_json()
    assert body["status"] == "degraded"
    assert body["database"]["connected"] is False
    assert body["database"]["missing_tables"] == sorted(dropped)


def test_health_handles_sqlite_error_without_raising(client, monkeypatch: pytest.MonkeyPatch):
    def boom(_connection):
        raise sqlite3.Error("database is locked")

    monkeypatch.setattr("app.routes.health.existing_tables", boom)

    response = client.get("/health")
    assert response.status_code == 503
    assert response.mimetype == "application/json"
    body = response.get_json()
    assert body["status"] == "degraded"
    assert body["database"] == {"connected": False, "missing_tables": []}
    assert set(body) == {"status", "environment", "database", "weather_provider"}


def test_health_reports_injected_weather_provider_and_environment(app, client):
    class FakeProvider:
        name = "fake-provider"
        available = True

    app.extensions["weather_provider"] = FakeProvider()

    response = client.get("/health")
    assert response.status_code == 200
    body = response.get_json()
    assert body["weather_provider"]["name"] == "fake-provider"
    assert body["weather_provider"]["available"] is True
    assert body["environment"] == app.config["ENV"]


# WeatherReport-9vj: Config declared these fields but to_flask_mapping never
# emitted them, so every app.config.get(...) in create_app saw None. Rate
# limiting was therefore off in any real deployment despite defaulting to True,
# and the RATE_LIMIT_*/WEATHER_CACHE_* env vars had no effect at all. These
# tests pin the pass-through so the keys cannot be dropped again silently.
_PASSED_THROUGH_KEYS = (
    "WEATHER_CACHE_TTL_SECONDS",
    "WEATHER_CACHE_MAX_ENTRIES",
    "REQUEST_LOG_ENABLED",
    "REQUEST_LOG_PATH",
    "RATE_LIMIT_ENABLED",
    "RATE_LIMIT_REQUESTS",
    "RATE_LIMIT_WINDOW_SECONDS",
)


def test_to_flask_mapping_emits_every_runtime_key(clean_env):
    from app.config import load_config

    mapping = load_config().to_flask_mapping()
    missing = [key for key in _PASSED_THROUGH_KEYS if key not in mapping]
    assert missing == [], f"to_flask_mapping dropped: {missing}"


def test_rate_limit_defaults_reach_app_config(clean_env, tmp_path: Path):
    """Outside TESTING the declared default (enabled, 100/60s) must survive."""
    from app.config import load_config

    mapping = load_config().to_flask_mapping()
    assert mapping["RATE_LIMIT_ENABLED"] is True
    assert mapping["RATE_LIMIT_REQUESTS"] == 100
    assert mapping["RATE_LIMIT_WINDOW_SECONDS"] == 60


def test_rate_limit_and_cache_env_overrides_are_honoured(
    clean_env, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("RATE_LIMIT_REQUESTS", "7")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "11")
    monkeypatch.setenv("WEATHER_CACHE_TTL_SECONDS", "13")
    monkeypatch.setenv("WEATHER_CACHE_MAX_ENTRIES", "17")

    from app.config import load_config

    mapping = load_config().to_flask_mapping()
    assert mapping["RATE_LIMIT_REQUESTS"] == 7
    assert mapping["RATE_LIMIT_WINDOW_SECONDS"] == 11
    assert mapping["WEATHER_CACHE_TTL_SECONDS"] == 13
    assert mapping["WEATHER_CACHE_MAX_ENTRIES"] == 17


def test_testing_still_disables_rate_limit_and_request_log(app):
    """The suite must not start throttling itself once the keys flow through."""
    assert app.config["TESTING"] is True
    assert app.config["RATE_LIMIT_ENABLED"] is False
    assert app.config["REQUEST_LOG_ENABLED"] is False


def test_request_log_path_present_whenever_logging_enabled(clean_env):
    """request_log.py indexes REQUEST_LOG_PATH directly, so it must exist."""
    from app.config import load_config

    mapping = load_config().to_flask_mapping()
    if mapping["REQUEST_LOG_ENABLED"]:
        assert mapping["REQUEST_LOG_PATH"]
