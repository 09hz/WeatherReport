"""SQLite connection management scoped to the Flask application context."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from flask import current_app, g

_CONNECTION_KEY = "_sqlite_connection"


def _configure(connection: sqlite3.Connection) -> sqlite3.Connection:
    connection.row_factory = sqlite3.Row
    # Enforce journal -> weather_snapshot integrity.
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def connect(database_path: str | Path) -> sqlite3.Connection:
    """Open a standalone connection. Caller owns closing it."""
    path = Path(database_path)
    if path.name != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(path), detect_types=sqlite3.PARSE_DECLTYPES, timeout=15
    )
    return _configure(connection)


def get_connection() -> sqlite3.Connection:
    """Return the request-scoped connection, creating it on first use."""
    connection = getattr(g, _CONNECTION_KEY, None)
    if connection is None:
        connection = connect(current_app.config["DATABASE_PATH"])
        setattr(g, _CONNECTION_KEY, connection)
    return connection


def close_connection(exception: BaseException | None = None) -> None:
    """Teardown hook: close the request-scoped connection if one was opened."""
    connection = g.pop(_CONNECTION_KEY, None)
    if connection is not None:
        connection.close()


def register(app) -> None:
    app.teardown_appcontext(close_connection)
