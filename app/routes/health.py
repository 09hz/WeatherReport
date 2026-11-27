"""Health-check and service metadata endpoints."""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify

from ..database.connection import database_errors, get_connection
from ..database.schema import TABLE_NAMES, existing_tables

bp = Blueprint("health", __name__)


@bp.get("/health")
def health():
    """Report process, database and weather-provider status as JSON."""
    database_ok = False
    missing_tables: list[str] = []
    try:
        tables = existing_tables(get_connection())
        missing_tables = sorted(set(TABLE_NAMES) - tables)
        database_ok = not missing_tables
    except database_errors() as exc:
        current_app.logger.warning("Health check database error: %s", exc)

    provider = current_app.extensions.get("weather_provider")
    payload = {
        "status": "ok" if database_ok else "degraded",
        "environment": current_app.config.get("ENV"),
        "database": {"connected": database_ok, "missing_tables": missing_tables},
        "weather_provider": {
            "name": getattr(provider, "name", "unconfigured"),
            "available": bool(getattr(provider, "available", False)),
        },
    }
    return jsonify(payload), 200 if database_ok else 503
