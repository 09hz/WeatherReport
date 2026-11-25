"""Journal REST endpoints."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request

from ..database.connection import get_connection
from ..errors import ValidationError
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
    search = (request.args.get("q") or "").strip() or None
    location = (request.args.get("location") or "").strip() or None

    connection = get_connection()
    entries = journal_service.list_entries(
        connection,
        search=search,
        location=location,
        limit=limit,
        offset=offset,
    )
    total_count = journal_service.count_entries(
        connection, search=search, location=location
    )
    return (
        jsonify(
            {
                "data": entries,
                "count": len(entries),
                "total_count": total_count,
                "has_more": offset + len(entries) < total_count,
                "limit": limit,
                "offset": offset,
            }
        ),
        200,
    )


@bp.get("/entries/<int:entry_id>")
def get_entry(entry_id: int):
    return jsonify({"data": journal_service.get_entry(get_connection(), entry_id)}), 200


@bp.patch("/entries/<int:entry_id>")
def update_entry(entry_id: int):
    """Partially update an entry. Only the supplied fields change."""
    payload = require_mapping(request.get_json(silent=True) or {})

    unknown = sorted(set(payload) - set(journal_service.UPDATABLE_FIELDS))
    if unknown:
        raise ValidationError(
            "Unknown field(s) in update payload.",
            details={
                "fields": unknown,
                "updatable": sorted(journal_service.UPDATABLE_FIELDS),
            },
        )

    changes: dict[str, Any] = {}
    if "title" in payload:
        changes["title"] = required_string(payload, "title", max_length=200)
    if "date" in payload:
        changes["date"] = required_date(payload, "date")
    if "location" in payload:
        changes["location"] = required_string(payload, "location")
    if "description" in payload:
        changes["description"] = required_string(
            payload, "description", max_length=10_000
        )
    if "weather_snapshot_id" in payload:
        # An explicit null detaches the snapshot.
        changes["weather_snapshot_id"] = optional_positive_int(
            payload, "weather_snapshot_id"
        )

    entry = journal_service.update_entry(get_connection(), entry_id, changes)
    return jsonify({"data": entry}), 200


@bp.delete("/entries/<int:entry_id>")
def delete_entry(entry_id: int):
    journal_service.delete_entry(get_connection(), entry_id)
    return "", 204


@bp.delete("/entries")
def bulk_delete_entries():
    """Delete several entries: ?ids=1,2,3 or a JSON body of ids."""
    entry_ids = _collect_ids()
    result = journal_service.delete_entries(get_connection(), entry_ids)
    return (
        jsonify(
            {
                "data": {
                    "deleted": result["deleted"],
                    "not_found": result["not_found"],
                },
                "deleted_count": len(result["deleted"]),
            }
        ),
        200,
    )


def _collect_ids() -> list[int]:
    """Read ids from the query string or a JSON body, in either shape."""
    raw: Any = request.args.get("ids")
    if raw is None:
        body = request.get_json(silent=True)
        if isinstance(body, list):
            raw = body
        elif isinstance(body, dict):
            raw = body.get("ids")

    if raw is None or raw == "" or raw == []:
        raise ValidationError(
            'Provide ids as ?ids=1,2,3 or a JSON body {"ids": [1, 2, 3]}.',
            details={"field": "ids"},
        )

    values: Any = raw.split(",") if isinstance(raw, str) else raw
    if not isinstance(values, list):
        raise ValidationError(
            "Field 'ids' must be a list or a comma-separated string.",
            details={"field": "ids", "received_type": type(raw).__name__},
        )

    parsed: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValidationError(
                "Every id must be a positive integer.",
                details={"field": "ids", "received": str(value)},
            )
        try:
            number = int(str(value).strip())
        except ValueError as exc:
            raise ValidationError(
                "Every id must be a positive integer.",
                details={"field": "ids", "received": str(value)},
            ) from exc
        if number <= 0:
            raise ValidationError(
                "Every id must be a positive integer.",
                details={"field": "ids", "received": number},
            )
        parsed.append(number)

    if not parsed:
        raise ValidationError("At least one id is required.", details={"field": "ids"})
    return parsed
