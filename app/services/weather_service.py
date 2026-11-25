"""Weather retrieval and snapshot persistence."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..errors import NotFoundError, ValidationError
from ..validation import parse_coordinates
from .weather_provider import (
    WeatherProvider,
    build_provider,
    classify_conditions,
    normalize,
    serialize_raw,
)

_COLUMNS = (
    "id, location, observed_at, temperature_c, humidity, "
    "wind_speed_kph, conditions, provider, raw_response, created_at"
)


def canonical_location(location: str) -> str | None:
    """Canonical "lat,lon" form of a location, or None if it is not one.

    The NWS provider stores `location` as f"{latitude},{longitude}" built from
    coordinates rounded to 4 decimals, so a client that submitted
    "45.50000, -122.60" has its snapshot stored as "45.5,-122.6". Returning
    the canonical form lets a filter match the stored row regardless of how
    the caller spelled the same coordinates.

    None means "not a coordinate pair", which is the signal to fall back to
    exact string matching rather than guessing at a free-text place name.
    """
    if not isinstance(location, str) or location.count(",") != 1:
        return None
    lat, lon = location.split(",")
    try:
        latitude, longitude = parse_coordinates(lat, lon)
    except ValidationError:
        return None
    return f"{latitude},{longitude}"


def _to_dict(row: sqlite3.Row, *, include_raw: bool = False) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "id": row["id"],
        "location": row["location"],
        "observed_at": row["observed_at"],
        "temperature_c": row["temperature_c"],
        "humidity": row["humidity"],
        "wind_speed_kph": row["wind_speed_kph"],
        "conditions": row["conditions"],
        # Derived on read, so stored rows need no migration and the
        # canonical vocabulary can evolve without rewriting history.
        "condition_code": classify_conditions(row["conditions"]),
        "provider": row["provider"],
        "created_at": row["created_at"],
    }
    if include_raw and row["raw_response"]:
        try:
            snapshot["raw_response"] = json.loads(row["raw_response"])
        except (TypeError, ValueError):
            snapshot["raw_response"] = None
    return snapshot


def fetch_current(location: str, provider: WeatherProvider) -> dict[str, Any]:
    """Fetch live conditions. Raises if no provider is configured."""
    return provider.fetch_current(location)


def save_snapshot(
    connection: sqlite3.Connection, snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Persist a normalized weather dictionary and return the stored record.

    A coordinate location is stored in canonical "lat,lon" form so that two
    spellings of the same point cannot become two un-joinable groups of rows.
    Free-text locations are stored exactly as given.
    """
    observed_at = snapshot.get("observed_at")
    location = snapshot["location"]
    location = canonical_location(location) or location
    with connection:
        cursor = connection.execute(
            """
            INSERT INTO weather_snapshots
                (location, observed_at, temperature_c, humidity,
                 wind_speed_kph, conditions, provider, raw_response)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                location,
                str(observed_at) if observed_at is not None else None,
                snapshot.get("temperature_c"),
                snapshot.get("humidity"),
                snapshot.get("wind_speed_kph"),
                snapshot.get("conditions"),
                snapshot.get("provider", "unknown"),
                serialize_raw(snapshot.get("raw_response")),
            ),
        )
        snapshot_id = int(cursor.lastrowid or 0)
    return get_snapshot(connection, snapshot_id)


def get_snapshot(
    connection: sqlite3.Connection, snapshot_id: int, *, include_raw: bool = False
) -> dict[str, Any]:
    row = connection.execute(
        f"SELECT {_COLUMNS} FROM weather_snapshots WHERE id = ?", (snapshot_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError("Weather snapshot not found.", details={"id": snapshot_id})
    return _to_dict(row, include_raw=include_raw)


def _location_filter(location: str | None) -> tuple[str, list[Any]]:
    """WHERE clause + params matching a location the way list/count both must.

    Factored out so `list_snapshots` and `count_snapshots` cannot drift: when
    only the list side matched both the submitted and canonical spellings,
    `total_count` and `has_more` disagreed with the rows actually returned and
    pagination silently lied.
    """
    if not location:
        return "", []
    canonical = canonical_location(location)
    if canonical is not None and canonical != location:
        # Match the submitted string AND its canonical coordinate form: a row
        # written before canonicalization kept the caller's spelling.
        return " WHERE location IN (?, ?)", [location, canonical]
    return " WHERE location = ?", [location]


def list_snapshots(
    connection: sqlite3.Connection,
    *,
    location: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where, params = _location_filter(location)
    params = [*params, limit, offset]
    rows = connection.execute(
        f"SELECT {_COLUMNS} FROM weather_snapshots{where} "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        params,
    ).fetchall()
    return [_to_dict(row) for row in rows]


def count_snapshots(
    connection: sqlite3.Connection, *, location: str | None = None
) -> int:
    """Total snapshots matching the same filter `list_snapshots` accepts."""
    where, params = _location_filter(location)
    row = connection.execute(
        f"SELECT COUNT(*) AS total FROM weather_snapshots{where}", params
    ).fetchone()
    return int(row["total"] if hasattr(row, "keys") else row[0])


__all__ = [
    "build_provider",
    "canonical_location",
    "classify_conditions",
    "count_snapshots",
    "fetch_current",
    "get_snapshot",
    "list_snapshots",
    "normalize",
    "save_snapshot",
]
