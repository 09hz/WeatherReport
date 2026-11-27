"""Postgres backend integration tests.

Skipped unless PG_TEST_URL points at a reachable Postgres, so the default
`pytest` run stays SQLite-only and offline. Verified locally against a real
Postgres 16 (WeatherReport-eil):

    PG_TEST_URL="postgresql://user:***@host/db" pytest tests/test_postgres_backend.py

These tests exercise the dialect seam in app/database/postgres.py, not Flask:
lastrowid via RETURNING, repeated `with connection:` transactions on one
connection, case-insensitive search (LIKE -> ILIKE), and the text timestamp
format shared with SQLite.
"""

from __future__ import annotations

import os
import re

import pytest

from app import create_app
from app.database.connection import connect, is_postgres_url
from app.services import journal_service

PG_TEST_URL = os.environ.get("PG_TEST_URL", "")

pytestmark = pytest.mark.skipif(
    not is_postgres_url(PG_TEST_URL),
    reason="PG_TEST_URL is not set to a postgres:// URL",
)

_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


@pytest.fixture
def pg_app(clean_env):
    """App bound to Postgres, with the two tables recreated from scratch."""
    bootstrap = connect(PG_TEST_URL)
    with bootstrap:
        bootstrap.execute("DROP TABLE IF EXISTS journal_entries CASCADE")
        bootstrap.execute("DROP TABLE IF EXISTS weather_snapshots CASCADE")
    bootstrap.close()

    application = create_app(
        {"TESTING": True, "ENV": "testing", "DATABASE_URL": PG_TEST_URL}
    )
    yield application


@pytest.fixture
def pg_connection(pg_app):
    conn = connect(PG_TEST_URL)
    yield conn
    conn.close()


def test_schema_is_created_on_postgres(pg_app, pg_connection):
    from app.database.schema import TABLE_NAMES, existing_tables

    assert set(TABLE_NAMES) <= existing_tables(pg_connection)


def test_health_endpoint_reports_connected(pg_app):
    response = pg_app.test_client().get("/health")
    assert response.status_code == 200
    assert response.get_json()["database"] == {
        "connected": True,
        "missing_tables": [],
    }


def test_insert_returns_generated_id(pg_connection):
    """lastrowid is synthesised from RETURNING id."""
    entry = journal_service.create_entry(
        pg_connection,
        title="First",
        entry_date="2026-11-25",
        location="Boston, MA",
        description="Cold and clear.",
    )
    assert entry["id"] > 0
    assert _TIMESTAMP_RE.match(entry["created_at"]), entry["created_at"]

    second = journal_service.create_entry(
        pg_connection,
        title="Second",
        entry_date="2026-11-26",
        location="Boston, MA",
        description="Snow.",
    )
    assert second["id"] > entry["id"]


def test_search_is_case_insensitive_like_sqlite(pg_connection):
    journal_service.create_entry(
        pg_connection,
        title="Thunderstorm Watch",
        entry_date="2026-07-04",
        location="Austin, TX",
        description="Loud.",
    )
    hits = journal_service.list_entries(pg_connection, search="thunderstorm")
    assert [hit["title"] for hit in hits] == ["Thunderstorm Watch"]


def test_update_and_delete_round_trip(pg_connection):
    entry = journal_service.create_entry(
        pg_connection,
        title="Draft",
        entry_date="2026-01-01",
        location="Reno, NV",
        description="Windy.",
    )
    updated = journal_service.update_entry(
        pg_connection, entry["id"], {"title": "Final"}
    )
    assert updated["title"] == "Final"
    assert _TIMESTAMP_RE.match(updated["updated_at"]), updated["updated_at"]

    journal_service.delete_entry(pg_connection, entry["id"])
    assert journal_service.list_entries(pg_connection) == []


def test_journal_api_round_trip_over_postgres(pg_app):
    client = pg_app.test_client()
    created = client.post(
        "/api/journal/entries",
        json={
            "title": "Via API",
            "date": "2026-03-03",
            "location": "Denver, CO",
            "description": "Bluebird day.",
        },
    )
    assert created.status_code == 201, created.get_data(as_text=True)
    entry_id = created.get_json()["data"]["id"]

    listed = client.get("/api/journal/entries")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.get_json()["data"]] == [entry_id]

    assert client.delete(f"/api/journal/entries/{entry_id}").status_code in (200, 204)