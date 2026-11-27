"""Postgres backend presented through the sqlite3 connection API.

Why an adapter instead of a rewrite: every call site in app/services speaks a
small, stable subset of the sqlite3 API — `connection.execute(sql, params)`
returning a cursor with `.fetchone()`, `.fetchall()`, `.lastrowid` and
`.rowcount`, plus `with connection:` for a transaction. Reproducing that subset
here keeps all 17 query sites and their SQL unchanged, so SQLite stays the
local/dev default and Postgres is a deployment detail (WeatherReport-eil).

Three dialect gaps are closed by translation, not by editing the services:

* ``?`` placeholders become ``%s``.
* ``LIKE`` becomes ``ILIKE``. SQLite's LIKE is case-insensitive for ASCII and
  the journal search relies on that; Postgres LIKE is case-sensitive.
* ``datetime('now')`` becomes the ``to_char`` expression that produces the
  identical ``YYYY-MM-DD HH:MM:SS`` text SQLite writes, so stored timestamps
  stay byte-compatible between backends.

INSERTs get ``RETURNING id`` appended so ``cursor.lastrowid`` keeps working.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

import psycopg
from psycopg.rows import dict_row

#: Mirrors SQLite's datetime('now'): UTC, space-separated, second precision.
PG_NOW_TEXT = "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"

_LIKE_RE = re.compile(r"\bLIKE\b", re.IGNORECASE)
_DATETIME_NOW_RE = re.compile(r"datetime\(\s*'now'\s*\)", re.IGNORECASE)
_RETURNING_RE = re.compile(r"\bRETURNING\b", re.IGNORECASE)


def translate(sql: str) -> str:
    """Rewrite the SQLite dialect this project writes into Postgres."""
    sql = _DATETIME_NOW_RE.sub(PG_NOW_TEXT, sql)
    sql = _LIKE_RE.sub("ILIKE", sql)
    return sql.replace("?", "%s")


def _is_insert(sql: str) -> bool:
    return sql.lstrip().lstrip("(").upper().startswith("INSERT")


class PgCursor:
    """The slice of sqlite3.Cursor the services actually use."""

    def __init__(self, cursor: Any, lastrowid: int | None = None) -> None:
        self._cursor = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def description(self):  # pragma: no cover - parity shim
        return self._cursor.description

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor)


class PgConnection:
    """sqlite3-shaped facade over a psycopg connection.

    ``with connection:`` commits on success and rolls back on error — matching
    sqlite3. It deliberately does NOT close, which is what psycopg's own
    context manager would do and would break the request-scoped connection
    reused across several transactions.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        # row_factory is set after connect: psycopg's connect() overload is
        # typed for tuple rows, and this adapter is dynamic by design.
        conn = psycopg.connect(dsn, autocommit=True)
        conn.row_factory = dict_row  # type: ignore[assignment]
        self._conn = conn

    def execute(self, sql: str, params: Sequence[Any] | Iterable[Any] = ()) -> PgCursor:
        statement = translate(sql)
        wants_id = _is_insert(statement) and not _RETURNING_RE.search(statement)
        if wants_id:
            statement = statement.rstrip().rstrip(";") + " RETURNING id"

        cursor = self._conn.cursor()
        cursor.execute(statement, tuple(params) if params else None)  # type: ignore[arg-type]

        lastrowid = None
        if wants_id:
            row = cursor.fetchone()
            if row is not None:
                lastrowid = int(row["id"])
        return PgCursor(cursor, lastrowid)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PgConnection":
        self._conn.autocommit = False
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.autocommit = True
        return False


def connect(dsn: str) -> PgConnection:
    return PgConnection(dsn)


__all__ = ["PgConnection", "PgCursor", "connect", "translate", "PG_NOW_TEXT"]
