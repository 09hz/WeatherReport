"""Journal persistence and query logic.

Services speak plain Python dictionaries; they never touch Flask request or
response objects.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..errors import NotFoundError, ValidationError

_COLUMNS = (
    "id, title, entry_date, location, description, "
    "weather_snapshot_id, created_at, updated_at"
)


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


def list_entries(
    connection: sqlite3.Connection,
    *,
    search: str | None = None,
    location: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List entries newest-first, optionally filtered by text and location."""
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


def _snapshot_exists(connection: sqlite3.Connection, snapshot_id: int) -> bool:
    row = connection.execute(
        "SELECT 1 FROM weather_snapshots WHERE id = ?", (snapshot_id,)
    ).fetchone()
    return row is not None
