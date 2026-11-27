"""Database schema definition and initialization.

The SQLite statements are canonical; the Postgres variants are derived from
them (identity columns instead of AUTOINCREMENT, and a timestamp default that
produces the same text SQLite's datetime('now') does). Both backends therefore
store created_at/updated_at as `YYYY-MM-DD HH:MM:SS` text, which the API
serializers return verbatim.
"""

from __future__ import annotations

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS weather_snapshots (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        location        TEXT    NOT NULL,
        observed_at     TEXT,
        temperature_c   REAL,
        humidity        REAL,
        wind_speed_kph  REAL,
        conditions      TEXT,
        provider        TEXT    NOT NULL,
        raw_response    TEXT,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS journal_entries (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        title               TEXT    NOT NULL,
        entry_date          TEXT    NOT NULL,
        location            TEXT    NOT NULL,
        description         TEXT    NOT NULL,
        weather_snapshot_id INTEGER,
        created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
        updated_at          TEXT    NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (weather_snapshot_id)
            REFERENCES weather_snapshots (id)
            ON DELETE SET NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_weather_location ON weather_snapshots (location)",
    "CREATE INDEX IF NOT EXISTS idx_journal_date ON journal_entries (entry_date)",
    "CREATE INDEX IF NOT EXISTS idx_journal_location ON journal_entries (location)",
)

TABLE_NAMES: tuple[str, ...] = ("weather_snapshots", "journal_entries")

_SQLITE_TABLES_QUERY = "SELECT name FROM sqlite_master WHERE type = 'table'"
_PG_TABLES_QUERY = (
    "SELECT table_name AS name FROM information_schema.tables "
    "WHERE table_schema = current_schema()"
)


def _is_postgres(connection: object) -> bool:
    """True for the PgConnection adapter, without importing psycopg."""
    return type(connection).__module__.endswith("database.postgres")


def _postgres_statements() -> tuple[str, ...]:
    from .postgres import PG_NOW_TEXT

    return tuple(
        statement.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
        .replace("REAL", "DOUBLE PRECISION")
        .replace("(datetime('now'))", f"({PG_NOW_TEXT})")
        for statement in SCHEMA_STATEMENTS
    )


def statements_for(connection: object) -> tuple[str, ...]:
    """DDL for whichever backend `connection` talks to."""
    return _postgres_statements() if _is_postgres(connection) else SCHEMA_STATEMENTS


def initialize(connection) -> None:
    """Create tables and indexes if they do not already exist (idempotent)."""
    with connection:
        for statement in statements_for(connection):
            connection.execute(statement)


def existing_tables(connection) -> set[str]:
    query = _PG_TABLES_QUERY if _is_postgres(connection) else _SQLITE_TABLES_QUERY
    rows = connection.execute(query).fetchall()
    return {row["name"] if hasattr(row, "keys") else row[0] for row in rows}
