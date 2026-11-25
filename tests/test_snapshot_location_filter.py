"""Review MINOR 10: snapshot location filter misses the submitted string.

WeatherReport-boi. The NWS provider stores `location` as
f"{latitude},{longitude}" built from coordinates rounded to 4 decimals, so a
snapshot POSTed as "45.50000, -122.60" is stored as "45.5,-122.6". Filtering
on exact string equality meant a client listing by the string it had just
submitted got zero rows back.

The fix matches both the submitted string and its canonical coordinate form,
so formatting is irrelevant in either direction, while a non-coordinate
location keeps exact-match behaviour.
"""

from __future__ import annotations

import pytest

from app.services import weather_service
from app.services.weather_service import canonical_location


# --- canonical_location -----------------------------------------------------


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [
        ("45.50000, -122.60", "45.5,-122.6"),
        ("45.5,-122.6", "45.5,-122.6"),
        ("  45.5 , -122.6  ", "45.5,-122.6"),
        ("45.50,-122.600", "45.5,-122.6"),
        ("39.74560,-97.08920", "39.7456,-97.0892"),
        ("39.745600001,-97.089200001", "39.7456,-97.0892"),
        ("0,0", "0.0,0.0"),
        ("-0.0,0.0", "0.0,0.0"),
        ("90,-180", "90.0,-180.0"),
    ],
)
def test_coordinate_strings_canonicalize(submitted, expected):
    assert canonical_location(submitted) == expected


@pytest.mark.parametrize(
    "not_a_coordinate",
    [
        "Topeka",
        "Portland, OR",
        "",
        ",",
        "45.5",
        "45.5,-122.6,100",
        "abc,def",
        "91,0",  # latitude out of range
        "0,181",  # longitude out of range
        "nan,0",
        None,
        12345,
    ],
)
def test_non_coordinates_return_none(not_a_coordinate):
    """None is the signal to fall back to exact matching."""
    assert canonical_location(not_a_coordinate) is None


def test_differently_spelled_coordinates_agree():
    assert canonical_location("45.50000, -122.60") == canonical_location("45.5,-122.6")


# --- list_snapshots filtering ----------------------------------------------


def _save(connection, location: str, *, temp: float = 1.0) -> int:
    snapshot = weather_service.save_snapshot(
        connection,
        {
            "location": location,
            "observed_at": "2026-11-24T02:10:00+00:00",
            "temperature_c": temp,
            "humidity": 50.0,
            "wind_speed_kph": 10.0,
            "conditions": "Clear",
            "provider": "nws",
        },
    )
    return snapshot["id"]


@pytest.mark.parametrize(
    "query",
    [
        "45.5,-122.6",
        "45.50000, -122.60",
        "45.50,-122.600",
        "  45.5 , -122.6 ",
        "45.5, -122.6",
    ],
)
def test_listing_by_any_spelling_finds_the_stored_row(app, connection, query):
    """The regression: the client's own submitted string returned nothing."""
    _save(connection, "45.5,-122.6")

    rows = weather_service.list_snapshots(connection, location=query)

    assert len(rows) == 1
    assert rows[0]["location"] == "45.5,-122.6"


@pytest.mark.parametrize(
    "stored",
    ["45.5,-122.6", "45.50000, -122.60"],
)
def test_any_submitted_spelling_is_stored_canonically(app, connection, stored):
    """save_snapshot canonicalizes, so storage cannot fragment by spelling."""
    _save(connection, stored)

    for query in ("45.5,-122.6", "45.50000, -122.60", "  45.5 , -122.60 "):
        rows = weather_service.list_snapshots(connection, location=query)
        assert len(rows) == 1, f"stored={stored!r} query={query!r}"
        assert rows[0]["location"] == "45.5,-122.6"


def test_rows_saved_under_two_spellings_group_together(app, connection):
    """Both end up canonical, so one query returns both."""
    _save(connection, "45.5,-122.6", temp=1.0)
    _save(connection, "45.50000, -122.60", temp=2.0)

    rows = weather_service.list_snapshots(connection, location="45.5,-122.6")

    assert len(rows) == 2
    assert {r["location"] for r in rows} == {"45.5,-122.6"}


def test_a_legacy_non_canonical_row_is_still_found_by_its_own_string(
    app, connection
):
    """Rows written before this fix keep their original string.

    Read-side matching covers them when the caller uses that same string, or
    a spelling whose canonical form equals it. Canonicalizing pre-existing
    rows would need a migration and is deliberately out of scope here.
    """
    connection.execute(
        "INSERT INTO weather_snapshots (location, provider) VALUES (?, ?)",
        ("45.50000, -122.60", "nws"),
    )
    connection.commit()

    rows = weather_service.list_snapshots(connection, location="45.50000, -122.60")

    assert len(rows) == 1


def test_a_different_coordinate_is_not_matched(app, connection):
    _save(connection, "45.5,-122.6")

    rows = weather_service.list_snapshots(connection, location="45.6,-122.6")

    assert rows == []


def test_non_coordinate_location_keeps_exact_match(app, connection):
    _save(connection, "Topeka")
    _save(connection, "Portland")

    assert len(weather_service.list_snapshots(connection, location="Topeka")) == 1
    assert weather_service.list_snapshots(connection, location="topeka") == []
    assert weather_service.list_snapshots(connection, location="Tope") == []


def test_no_location_filter_returns_everything(app, connection):
    _save(connection, "45.5,-122.6")
    _save(connection, "Topeka")

    assert len(weather_service.list_snapshots(connection)) == 2


def test_limit_and_offset_still_apply_with_a_canonical_match(app, connection):
    """The IN (?, ?) branch must not disturb the trailing LIMIT/OFFSET params."""
    for i in range(5):
        _save(connection, "45.5,-122.6", temp=float(i))

    page = weather_service.list_snapshots(
        connection, location="45.50000, -122.60", limit=2, offset=1
    )

    assert len(page) == 2


def test_limit_and_offset_still_apply_with_an_exact_match(app, connection):
    for i in range(5):
        _save(connection, "Topeka", temp=float(i))

    page = weather_service.list_snapshots(connection, location="Topeka", limit=2, offset=3)

    assert len(page) == 2
