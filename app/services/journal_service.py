"""Journal persistence and query logic.

Services speak plain Python dictionaries; they never touch Flask request or
response objects.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..errors import ConflictError, NotFoundError, ValidationError

_COLUMNS = (
    "id, title, entry_date, location, description, "
    "weather_snapshot_id, created_at, updated_at"
)

# Fields a client may change through a partial update, mapped to DB columns.
UPDATABLE_FIELDS: dict[str, str] = {
    "title": "title",
    "date": "entry_date",
    "location": "location",
    "description": "description",
    "weather_snapshot_id": "weather_snapshot_id",
}


def _to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "date": row["entry_date"],
        "location": row["location"],
        "description": row["description"],
        "weather_snapshot_id": row["weather_snapshot_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_entry(
    connection: sqlite3.Connection,
    *,
    title: str,
    entry_date: str,
    location: str,
    description: str,
    weather_snapshot_id: int | None = None,
) -> dict[str, Any]:
    """Insert a journal entry and return it as a dictionary."""
    if weather_snapshot_id is not None and not _snapshot_exists(
        connection, weather_snapshot_id
    ):
        raise ValidationError(
            "Referenced weather snapshot does not exist.",
            details={"field": "weather_snapshot_id", "value": weather_snapshot_id},
        )

    existing_id = find_duplicate(connection, entry_date=entry_date, location=location)
    if existing_id is not None:
        raise ConflictError(
            "A journal entry for this date and location already exists. "
            "Update that entry instead of inserting a new one.",
            details={
                "existing_entry_id": existing_id,
                "date": entry_date,
                "location": location,
                "suggested_action": f"PATCH /api/journal/entries/{existing_id}",
            },
        )

    with connection:
        cursor = connection.execute(
            """
            INSERT INTO journal_entries
                (title, entry_date, location, description, weather_snapshot_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (title, entry_date, location, description, weather_snapshot_id),
        )
        entry_id = int(cursor.lastrowid or 0)

    return get_entry(connection, entry_id)


def get_entry(connection: sqlite3.Connection, entry_id: int) -> dict[str, Any]:
    row = connection.execute(
        f"SELECT {_COLUMNS} FROM journal_entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(
            "Journal entry not found.", details={"id": entry_id}
        )
    return _to_dict(row)


def _filter_clause(
    *, search: str | None = None, location: str | None = None
) -> tuple[str, list[Any]]:
    """Build the shared WHERE clause used by listing and counting."""
    clauses: list[str] = []
    params: list[Any] = []

    if search:
        clauses.append("(title LIKE ? OR description LIKE ? OR location LIKE ?)")
        needle = f"%{search}%"
        params.extend([needle, needle, needle])
    if location:
        clauses.append("location = ?")
        params.append(location)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def list_entries(
    connection: sqlite3.Connection,
    *,
    search: str | None = None,
    location: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List entries newest-first, optionally filtered by text and location."""
    where, params = _filter_clause(search=search, location=location)
    params.extend([limit, offset])

    rows = connection.execute(
        f"SELECT {_COLUMNS} FROM journal_entries{where} "
        "ORDER BY entry_date DESC, id DESC LIMIT ? OFFSET ?",
        params,
    ).fetchall()
    return [_to_dict(row) for row in rows]


def delete_entry(connection: sqlite3.Connection, entry_id: int) -> None:
    with connection:
        cursor = connection.execute(
            "DELETE FROM journal_entries WHERE id = ?", (entry_id,)
        )
    if cursor.rowcount == 0:
        raise NotFoundError("Journal entry not found.", details={"id": entry_id})


def update_entry(
    connection: sqlite3.Connection,
    entry_id: int,
    changes: dict[str, Any],
) -> dict[str, Any]:
    """Apply a partial update to an entry and return the stored result.

    `changes` is keyed by API field name (see UPDATABLE_FIELDS) and must
    already be validated. Absent keys are left untouched.
    """
    unknown = sorted(set(changes) - set(UPDATABLE_FIELDS))
    if unknown:
        raise ValidationError(
            "Unknown field(s) in update payload.",
            details={"fields": unknown, "updatable": sorted(UPDATABLE_FIELDS)},
        )
    if not changes:
        raise ValidationError(
            "Update payload must change at least one field.",
            details={"updatable": sorted(UPDATABLE_FIELDS)},
        )

    # 404 before any write, so a bad id never looks like a successful no-op.
    current = get_entry(connection, entry_id)

    snapshot_id = changes.get("weather_snapshot_id")
    if snapshot_id is not None and not _snapshot_exists(connection, snapshot_id):
        raise ValidationError(
            "Referenced weather snapshot does not exist.",
            details={"field": "weather_snapshot_id", "value": snapshot_id},
        )

    new_date = changes.get("date", current["date"])
    new_location = changes.get("location", current["location"])
    if ("date" in changes or "location" in changes) and (
        new_date != current["date"] or new_location != current["location"]
    ):
        clash = find_duplicate(
            connection,
            entry_date=new_date,
            location=new_location,
            exclude_id=entry_id,
        )
        if clash is not None:
            raise ConflictError(
                "Another journal entry already covers this date and location.",
                details={"existing_entry_id": clash},
            )

    assignments = ", ".join(f"{UPDATABLE_FIELDS[key]} = ?" for key in changes)
    params: list[Any] = [changes[key] for key in changes]
    params.append(entry_id)

    with connection:
        connection.execute(
            f"UPDATE journal_entries SET {assignments}, "
            "updated_at = datetime('now') WHERE id = ?",
            params,
        )

    return get_entry(connection, entry_id)


def count_entries(
    connection: sqlite3.Connection,
    *,
    search: str | None = None,
    location: str | None = None,
) -> int:
    """Total entries matching the same filters `list_entries` accepts."""
    where, params = _filter_clause(search=search, location=location)
    row = connection.execute(
        f"SELECT COUNT(*) AS total FROM journal_entries{where}", params
    ).fetchone()
    return int(row["total"] if hasattr(row, "keys") else row[0])


def delete_entries(
    connection: sqlite3.Connection, entry_ids: list[int]
) -> dict[str, list[int]]:
    """Delete several entries at once.

    Returns the ids actually removed and the ids that did not exist. A
    missing id is reported, not raised, so one bad id cannot void the batch.
    """
    unique_ids = sorted({int(value) for value in entry_ids})
    if not unique_ids:
        raise ValidationError("At least one entry id is required.")

    placeholders = ", ".join("?" for _ in unique_ids)
    rows = connection.execute(
        f"SELECT id FROM journal_entries WHERE id IN ({placeholders})", unique_ids
    ).fetchall()
    found = sorted(int(row["id"]) for row in rows)

    if found:
        found_placeholders = ", ".join("?" for _ in found)
        with connection:
            connection.execute(
                f"DELETE FROM journal_entries WHERE id IN ({found_placeholders})",
                found,
            )

    return {
        "deleted": found,
        "not_found": [value for value in unique_ids if value not in set(found)],
    }


def find_duplicate(
    connection: sqlite3.Connection,
    *,
    entry_date: str,
    location: str,
    exclude_id: int | None = None,
) -> int | None:
    """Id of an existing entry with the same date and location, if any.

    Location is compared case-insensitively and whitespace-trimmed so
    "Amsterdam" and " amsterdam " count as the same place.
    """
    sql = (
        "SELECT id FROM journal_entries "
        "WHERE entry_date = ? AND LOWER(TRIM(location)) = LOWER(TRIM(?))"
    )
    params: list[Any] = [entry_date, location]
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    row = connection.execute(sql + " ORDER BY id LIMIT 1", params).fetchone()
    return int(row["id"]) if row is not None else None


def _snapshot_exists(connection: sqlite3.Connection, snapshot_id: int) -> bool:
    row = connection.execute(
        "SELECT 1 FROM weather_snapshots WHERE id = ?", (snapshot_id,)
    ).fetchone()
    return row is not None
