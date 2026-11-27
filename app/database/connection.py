"""Connection management scoped to the Flask application context.

SQLite is the default and the only backend used locally. When DATABASE_URL is
set (postgres:// or postgresql://) connections are Postgres instead, wrapped in
an adapter that speaks the same sqlite3 API — see postgres.py for why.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from flask import current_app, g

_CONNECTION_KEY = "_sqlite_connection"

#: URL schemes that select the Postgres backend.
_PG_SCHEMES = ("postgres://", "postgresql://")


def is_postgres_url(url: str | None) -> bool:
    return bool(url) and str(url).startswith(_PG_SCHEMES)


def database_errors() -> tuple[type[BaseException], ...]:
    """Backend-agnostic error tuple for `except` clauses.

    psycopg is only imported when it is actually installed, so a SQLite-only
    environment never pays for it.
    """
    errors: tuple[type[BaseException], ...] = (sqlite3.Error,)
    try:
        import psycopg
    except ImportError:
        return errors
    return errors + (psycopg.Error,)


def _configure(connection: sqlite3.Connection) -> sqlite3.Connection:
    connection.row_factory = sqlite3.Row
    # Enforce journal -> weather_snapshot integrity.
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def connect(database_path: str | Path) -> Any:
    """Open a standalone connection. Caller owns closing it.

    A Postgres URL is accepted here too, so every existing call site
    (`connect(app.config["DATABASE_PATH"])`) keeps working unchanged.
    """
    if is_postgres_url(str(database_path)):
        from . import postgres

        return postgres.connect(str(database_path))

    path = Path(database_path)
    if path.name != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(path), detect_types=sqlite3.PARSE_DECLTYPES, timeout=15
    )
    return _configure(connection)


def _target() -> str:
    """Postgres URL when configured, else the SQLite file path."""
    url = current_app.config.get("DATABASE_URL")
    if is_postgres_url(url):
        return str(url)
    return str(current_app.config["DATABASE_PATH"])


def get_connection() -> Any:
    """Return the request-scoped connection, creating it on first use."""
    connection = getattr(g, _CONNECTION_KEY, None)
    if connection is None:
        connection = connect(_target())
        setattr(g, _CONNECTION_KEY, connection)
    return connection


def close_connection(exception: BaseException | None = None) -> None:
    """Teardown hook: close the request-scoped connection if one was opened."""
    connection = g.pop(_CONNECTION_KEY, None)
    if connection is not None:
        connection.close()


def register(app) -> None:
    app.teardown_appcontext(close_connection)
