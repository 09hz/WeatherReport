"""Tests for the journal CRUD additions: PATCH, bulk delete, pagination
metadata and duplicate rejection (TASK-001, 002, 004, 007)."""

from __future__ import annotations

import pytest

BASE = {
    "title": "Rainy walk along the canal",
    "date": "2026-11-23",
    "location": "Amsterdam",
    "description": "Grey skies, wind off the water, good coffee afterwards.",
}


def _create(client, **overrides):
    return client.post("/api/journal/entries", json={**BASE, **overrides})


def _new_id(client, **overrides) -> int:
    response = _create(client, **overrides)
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["id"]


# --- TASK-001: PATCH ----------------------------------------------------------


def test_patch_updates_a_single_field(client):
    entry_id = _new_id(client)
    response = client.patch(
        f"/api/journal/entries/{entry_id}", json={"title": "Sunny walk"}
    )
    assert response.status_code == 200
    entry = response.get_json()["data"]
    assert entry["title"] == "Sunny walk"
    # Untouched fields survive the partial update.
    assert entry["description"] == BASE["description"]
    assert entry["location"] == BASE["location"]


def test_patch_persists_to_the_database(client, connection):
    entry_id = _new_id(client)
    client.patch(f"/api/journal/entries/{entry_id}", json={"location": "Rotterdam"})
    row = connection.execute(
        "SELECT location FROM journal_entries WHERE id = ?", (entry_id,)
    ).fetchone()
    assert row["location"] == "Rotterdam"


def test_patch_can_update_several_fields_at_once(client):
    entry_id = _new_id(client)
    entry = client.patch(
        f"/api/journal/entries/{entry_id}",
        json={"title": "Edited", "description": "Rewritten", "date": "2026-12-01"},
    ).get_json()["data"]
    assert (entry["title"], entry["description"], entry["date"]) == (
        "Edited",
        "Rewritten",
        "2026-12-01",
    )


def test_patch_normalizes_a_datetime_date(client):
    entry_id = _new_id(client)
    entry = client.patch(
        f"/api/journal/entries/{entry_id}", json={"date": "2026-12-05T09:15:00"}
    ).get_json()["data"]
    assert entry["date"] == "2026-12-05"


def test_patch_touches_updated_at(client):
    entry_id = _new_id(client)
    entry = client.patch(
        f"/api/journal/entries/{entry_id}", json={"title": "Edited"}
    ).get_json()["data"]
    assert entry["updated_at"] >= entry["created_at"]


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "   "},
        {"title": 42},
        {"title": "x" * 201},
        {"date": "23-11-2026"},
        {"location": ""},
        {"weather_snapshot_id": 999},
    ],
)
def test_patch_applies_the_same_validation_as_post(client, payload):
    entry_id = _new_id(client)
    response = client.patch(f"/api/journal/entries/{entry_id}", json=payload)
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "validation_error"


def test_patch_rejects_an_empty_payload(client):
    entry_id = _new_id(client)
    response = client.patch(f"/api/journal/entries/{entry_id}", json={})
    assert response.status_code == 400


def test_patch_rejects_unknown_fields(client):
    entry_id = _new_id(client)
    response = client.patch(
        f"/api/journal/entries/{entry_id}", json={"sneaky_column": "x"}
    )
    assert response.status_code == 400
    assert "sneaky_column" in response.get_json()["error"]["details"]["fields"]


def test_patch_unknown_entry_returns_404(client):
    response = client.patch("/api/journal/entries/4242", json={"title": "Ghost"})
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"


def test_patch_can_attach_and_detach_a_snapshot(client, connection):
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
    entry_id = _new_id(client)
    attached = client.patch(
        f"/api/journal/entries/{entry_id}",
        json={"weather_snapshot_id": snapshot["id"]},
    ).get_json()["data"]
    assert attached["weather_snapshot_id"] == snapshot["id"]

    detached = client.patch(
        f"/api/journal/entries/{entry_id}", json={"weather_snapshot_id": None}
    ).get_json()["data"]
    assert detached["weather_snapshot_id"] is None


def test_patch_into_an_existing_date_location_pair_conflicts(client):
    first = _new_id(client, location="Oslo")
    second = _new_id(client, location="Lisbon")
    response = client.patch(
        f"/api/journal/entries/{second}", json={"location": "Oslo"}
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["details"]["existing_entry_id"] == first


# --- TASK-002: bulk delete ----------------------------------------------------


def test_bulk_delete_accepts_a_comma_separated_query_string(client):
    ids = [_new_id(client, location=f"City {i}") for i in range(3)]
    response = client.delete(
        "/api/journal/entries?ids=" + ",".join(str(i) for i in ids)
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["data"]["deleted"] == sorted(ids)
    assert body["deleted_count"] == 3
    for entry_id in ids:
        assert client.get(f"/api/journal/entries/{entry_id}").status_code == 404


def test_bulk_delete_accepts_a_json_array_body(client):
    ids = [_new_id(client, location=f"Town {i}") for i in range(2)]
    body = client.delete("/api/journal/entries", json=ids).get_json()
    assert body["data"]["deleted"] == sorted(ids)


def test_bulk_delete_accepts_an_ids_object_body(client):
    ids = [_new_id(client, location=f"Village {i}") for i in range(2)]
    body = client.delete("/api/journal/entries", json={"ids": ids}).get_json()
    assert body["data"]["deleted"] == sorted(ids)


def test_bulk_delete_reports_ids_that_did_not_exist(client):
    existing = _new_id(client)
    body = client.delete(f"/api/journal/entries?ids={existing},9999").get_json()
    assert body["data"]["deleted"] == [existing]
    assert body["data"]["not_found"] == [9999]


def test_bulk_delete_deduplicates_repeated_ids(client):
    entry_id = _new_id(client)
    body = client.delete(
        f"/api/journal/entries?ids={entry_id},{entry_id}"
    ).get_json()
    assert body["data"]["deleted"] == [entry_id]


@pytest.mark.parametrize("query", ["", "?ids=", "?ids=abc", "?ids=0", "?ids=-3"])
def test_bulk_delete_rejects_bad_input(client, query):
    response = client.delete(f"/api/journal/entries{query}")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "validation_error"


def test_bulk_delete_leaves_untargeted_entries_alone(client):
    doomed = _new_id(client, location="Oslo")
    keeper = _new_id(client, location="Lisbon")
    client.delete(f"/api/journal/entries?ids={doomed}")
    assert client.get(f"/api/journal/entries/{keeper}").status_code == 200


# --- TASK-004: pagination metadata -------------------------------------------


def test_list_reports_total_count_and_has_more(client):
    for index in range(5):
        _new_id(client, date=f"2026-11-1{index}")
    page = client.get("/api/journal/entries?limit=2&offset=0").get_json()
    assert page["count"] == 2
    assert page["total_count"] == 5
    assert page["has_more"] is True
    assert page["limit"] == 2
    assert page["offset"] == 0


def test_last_page_reports_has_more_false(client):
    for index in range(3):
        _new_id(client, date=f"2026-11-1{index}")
    page = client.get("/api/journal/entries?limit=2&offset=2").get_json()
    assert page["count"] == 1
    assert page["total_count"] == 3
    assert page["has_more"] is False


def test_total_count_respects_the_active_filter(client):
    _new_id(client, location="Oslo")
    _new_id(client, location="Lisbon")
    page = client.get("/api/journal/entries?location=Oslo").get_json()
    assert page["total_count"] == 1
    assert page["has_more"] is False


def test_empty_list_has_zero_total_and_no_more(client):
    page = client.get("/api/journal/entries").get_json()
    assert page["total_count"] == 0
    assert page["has_more"] is False


# --- TASK-007: duplicate rejection -------------------------------------------


def test_duplicate_date_and_location_returns_409(client):
    first = _new_id(client)
    response = _create(client, title="Different title, same day and place")
    assert response.status_code == 409
    error = response.get_json()["error"]
    assert error["code"] == "conflict"
    assert error["details"]["existing_entry_id"] == first
    assert str(first) in error["details"]["suggested_action"]


def test_duplicate_detection_ignores_case_and_padding(client):
    _new_id(client, location="Amsterdam")
    assert _create(client, location="  amsterdam ").status_code == 409


def test_a_different_date_is_not_a_duplicate(client):
    _new_id(client)
    assert _create(client, date="2026-11-24").status_code == 201


def test_a_different_location_is_not_a_duplicate(client):
    _new_id(client)
    assert _create(client, location="Utrecht").status_code == 201


def test_conflict_does_not_write_a_second_row(client, connection):
    _new_id(client)
    _create(client)
    count = connection.execute(
        "SELECT COUNT(*) AS n FROM journal_entries"
    ).fetchone()["n"]
    assert count == 1
