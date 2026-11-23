"""Database schema definition and initialization."""

from __future__ import annotations

import sqlite3

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


def initialize(connection: sqlite3.Connection) -> None:
    """Create tables and indexes if they do not already exist (idempotent)."""
    with connection:
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)


def existing_tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row["name"] if hasattr(row, "keys") else row[0] for row in rows}
