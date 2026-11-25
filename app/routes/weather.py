"""Weather REST endpoints."""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from ..database.connection import get_connection
from ..errors import ValidationError
from ..services import weather_service
from ..validation import bounded_limit, require_mapping, required_string

bp = Blueprint("weather", __name__, url_prefix="/api/weather")


def _provider():
    """Provider instance attached to the app by the factory."""
    return current_app.extensions["weather_provider"]


@bp.get("/provider")
def provider_status():
    """Report whether external weather lookups are enabled."""
    provider = _provider()
    return (
        jsonify(
            {
                "data": {
                    "provider": provider.name,
                    "available": provider.available,
                }
            }
        ),
        200,
    )


@bp.get("/current")
def current_weather():
    """Fetch live conditions for ?location=. 503 when no provider is set."""
    location = (request.args.get("location") or request.args.get("city") or "").strip()
    if not location:
        raise ValidationError(
            "Query parameter 'location' is required.", details={"field": "location"}
        )
    snapshot = weather_service.fetch_current(location, _provider())
    snapshot.pop("raw_response", None)
    return jsonify({"data": snapshot}), 200


@bp.post("/snapshots")
def save_snapshot():
    """Fetch and persist a snapshot for the given location."""
    payload = require_mapping(request.get_json(silent=True) or {})
    location = required_string(payload, "location")
    snapshot = weather_service.fetch_current(location, _provider())
    stored = weather_service.save_snapshot(get_connection(), snapshot)
    return jsonify({"data": stored}), 201


@bp.get("/snapshots")
def list_snapshots():
    snapshots = weather_service.list_snapshots(
        get_connection(),
        location=(request.args.get("location") or "").strip() or None,
        limit=bounded_limit(request.args.get("limit")),
    )
    return jsonify({"data": snapshots, "count": len(snapshots)}), 200


@bp.get("/snapshots/<int:snapshot_id>")
def get_snapshot(snapshot_id: int):
    snapshot = weather_service.get_snapshot(get_connection(), snapshot_id)
    return jsonify({"data": snapshot}), 200
