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


def test_index_lists_endpoints(client):
    response = client.get("/")
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
