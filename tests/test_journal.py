"""Journal endpoint and service tests."""

from __future__ import annotations

import pytest

VALID_ENTRY = {
    "title": "Rainy walk along the canal",
    "date": "2026-11-23",
    "location": "Amsterdam",
    "description": "Grey skies, wind off the water, good coffee afterwards.",
}


def _create(client, **overrides):
    payload = {**VALID_ENTRY, **overrides}
    return client.post("/api/journal/entries", json=payload)


def test_create_entry_returns_created_resource(client):
    response = _create(client)
    assert response.status_code == 201
    entry = response.get_json()["data"]
    assert entry["id"] > 0
    assert entry["title"] == VALID_ENTRY["title"]
    assert entry["date"] == "2026-11-23"
    assert entry["location"] == "Amsterdam"
    assert entry["weather_snapshot_id"] is None
    assert entry["created_at"]


def test_created_entry_is_retrievable_by_id(client):
    entry_id = _create(client).get_json()["data"]["id"]
    response = client.get(f"/api/journal/entries/{entry_id}")
    assert response.status_code == 200
    assert response.get_json()["data"]["id"] == entry_id


def test_entry_persists_across_app_contexts(client, connection):
    _create(client)
    row = connection.execute(
        "SELECT title, location FROM journal_entries"
    ).fetchone()
    assert row["title"] == VALID_ENTRY["title"]
    assert row["location"] == "Amsterdam"


def test_list_entries_returns_all_created(client):
    _create(client, title="First")
    _create(client, title="Second")
    body = client.get("/api/journal/entries").get_json()
    assert body["count"] == 2
    assert {e["title"] for e in body["data"]} == {"First", "Second"}


def test_search_filters_by_text(client):
    _create(client, title="Snow in Oslo", location="Oslo")
    _create(client, title="Sun in Lisbon", location="Lisbon")
    body = client.get("/api/journal/entries?q=Oslo").get_json()
    assert body["count"] == 1
    assert body["data"][0]["title"] == "Snow in Oslo"


def test_search_filters_by_location(client):
    _create(client, location="Oslo")
    _create(client, location="Lisbon")
    body = client.get("/api/journal/entries?location=Lisbon").get_json()
    assert body["count"] == 1
    assert body["data"][0]["location"] == "Lisbon"


def test_limit_and_offset_paginate(client):
    for index in range(3):
        _create(client, title=f"Entry {index}")
    page = client.get("/api/journal/entries?limit=2").get_json()
    assert page["count"] == 2
    rest = client.get("/api/journal/entries?limit=2&offset=2").get_json()
    assert rest["count"] == 1


def test_delete_entry(client):
    entry_id = _create(client).get_json()["data"]["id"]
    assert client.delete(f"/api/journal/entries/{entry_id}").status_code == 204
    assert client.get(f"/api/journal/entries/{entry_id}").status_code == 404


def test_missing_entry_returns_structured_404(client):
    body = client.get("/api/journal/entries/4242").get_json()
    assert body["error"]["code"] == "not_found"


@pytest.mark.parametrize("field", ["title", "date", "location", "description"])
def test_missing_required_field_is_rejected(client, field):
    payload = {k: v for k, v in VALID_ENTRY.items() if k != field}
    response = client.post("/api/journal/entries", json=payload)
    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["field"] == field


def test_blank_title_is_rejected(client):
    response = _create(client, title="   ")
    assert response.status_code == 400
    assert response.get_json()["error"]["details"]["field"] == "title"


def test_non_string_title_is_rejected(client):
    response = _create(client, title=42)
    assert response.status_code == 400
    assert response.get_json()["error"]["details"]["received_type"] == "int"


def test_overlong_title_is_rejected(client):
    response = _create(client, title="x" * 201)
    assert response.status_code == 400
    assert response.get_json()["error"]["details"]["max_length"] == 200


@pytest.mark.parametrize("bad_date", ["23-11-2026", "not-a-date", "2026-13-45"])
def test_invalid_date_is_rejected(client, bad_date):
    response = _create(client, date=bad_date)
    assert response.status_code == 400
    assert response.get_json()["error"]["details"]["field"] == "date"


def test_datetime_date_is_normalized_to_iso_date(client):
    entry = _create(client, date="2026-11-23T14:30:00").get_json()["data"]
    assert entry["date"] == "2026-11-23"


def test_empty_body_is_rejected(client):
    response = client.post("/api/journal/entries", json={})
    assert response.status_code == 400


def test_non_object_body_is_rejected(client):
    response = client.post("/api/journal/entries", json=["not", "an", "object"])
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "validation_error"


def test_invalid_limit_is_rejected(client):
    response = client.get("/api/journal/entries?limit=abc")
    assert response.status_code == 400


def test_negative_offset_is_rejected(client):
    response = client.get("/api/journal/entries?offset=-5")
    assert response.status_code == 400


def test_unknown_weather_snapshot_reference_is_rejected(client):
    response = _create(client, weather_snapshot_id=999)
    assert response.status_code == 400
    assert response.get_json()["error"]["details"]["field"] == "weather_snapshot_id"


def test_entry_can_reference_a_saved_snapshot(app, client, connection):
    from app.services import weather_service

    snapshot = weather_service.save_snapshot(
        connection,
        {
            "location": "Oslo",
            "observed_at": None,
            "temperature_c": -2.0,
            "humidity": 80.0,
            "wind_speed_kph": 11.0,
            "conditions": "light snow",
            "provider": "test-double",
            "raw_response": {"source": "unit-test fixture"},
        },
    )
    entry = _create(client, weather_snapshot_id=snapshot["id"]).get_json()["data"]
    assert entry["weather_snapshot_id"] == snapshot["id"]


def test_sql_injection_attempt_is_stored_as_data(client, connection):
    _create(client, title="Robert'); DROP TABLE journal_entries;--")
    rows = connection.execute("SELECT COUNT(*) AS n FROM journal_entries").fetchone()
    assert rows["n"] == 1
