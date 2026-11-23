"""Application initialization, health check and schema tests."""

from __future__ import annotations

from pathlib import Path

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
