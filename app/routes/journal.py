"""Journal REST endpoints."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..database.connection import get_connection
from ..services import journal_service
from ..validation import (
    bounded_limit,
    bounded_offset,
    optional_positive_int,
    require_mapping,
    required_date,
    required_string,
)

bp = Blueprint("journal", __name__, url_prefix="/api/journal")


@bp.post("/entries")
def create_entry():
    """Create a journal entry from a JSON body."""
    payload = require_mapping(request.get_json(silent=True) or {})
    entry = journal_service.create_entry(
        get_connection(),
        title=required_string(payload, "title", max_length=200),
        entry_date=required_date(payload, "date"),
        location=required_string(payload, "location"),
        description=required_string(payload, "description", max_length=10_000),
        weather_snapshot_id=optional_positive_int(payload, "weather_snapshot_id"),
    )
    return jsonify({"data": entry}), 201


@bp.get("/entries")
def list_entries():
    """List entries, optionally filtered by free-text search or location."""
    limit = bounded_limit(request.args.get("limit"))
    offset = bounded_offset(request.args.get("offset"))
    entries = journal_service.list_entries(
        get_connection(),
        search=(request.args.get("q") or "").strip() or None,
        location=(request.args.get("location") or "").strip() or None,
        limit=limit,
        offset=offset,
    )
    return jsonify({"data": entries, "count": len(entries)}), 200


@bp.get("/entries/<int:entry_id>")
def get_entry(entry_id: int):
    return jsonify({"data": journal_service.get_entry(get_connection(), entry_id)}), 200


@bp.delete("/entries/<int:entry_id>")
def delete_entry(entry_id: int):
    journal_service.delete_entry(get_connection(), entry_id)
    return "", 204
