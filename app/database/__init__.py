"""Database package: connection handling and schema management."""

from __future__ import annotations

from flask import Flask

from . import connection, schema


def init_app(app: Flask) -> None:
    """Register teardown hooks and ensure the schema exists."""
    connection.register(app)
    with app.app_context():
        schema.initialize(connection.get_connection())


__all__ = ["connection", "schema", "init_app"]
