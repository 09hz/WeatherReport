"""Weather REST endpoints."""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from ..database.connection import get_connection
from ..errors import NotFoundError, ValidationError
from ..services import weather_service
from ..services.weather_cache import WeatherCache, fetch_with_cache
from ..validation import bounded_limit, bounded_offset, require_mapping, required_string

bp = Blueprint("weather", __name__, url_prefix="/api/weather")


def _provider():
    """Provider instance attached to the app by the factory."""
    return current_app.extensions["weather_provider"]


def _cache() -> WeatherCache:
    """Response cache attached to the app by the factory."""
    return current_app.extensions["weather_cache"]


def _fetch_cached(location: str):
    """Fetch conditions through the TTL cache; returns (snapshot, metadata)."""
    provider = _provider()
    return fetch_with_cache(
        _cache(),
        location,
        lambda loc: weather_service.fetch_current(loc, provider),
    )


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
                    "cache": _cache().stats(),
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
    snapshot, cache_meta = _fetch_cached(location)
    snapshot.pop("raw_response", None)
    return jsonify({"data": snapshot, "cache": cache_meta}), 200


@bp.get("/cached/<path:location>")
def cached_weather(location: str):
    """Serve only what is already cached for `location`; never refetches."""
    cache = _cache()
    cached, age = cache.get(location)
    if cached is None:
        raise NotFoundError(
            "No cached weather for this location.",
            details={"location": location, "ttl_seconds": cache.ttl_seconds},
        )
    cached.pop("raw_response", None)
    return (
        jsonify({"data": cached, "cache": cache.metadata(hit=True, age_seconds=age)}),
        200,
    )


@bp.post("/snapshots")
def save_snapshot():
    """Fetch and persist a snapshot for the given location."""
    payload = require_mapping(request.get_json(silent=True) or {})
    location = required_string(payload, "location")
    snapshot, cache_meta = _fetch_cached(location)
    stored = weather_service.save_snapshot(get_connection(), snapshot)
    return jsonify({"data": stored, "cache": cache_meta}), 201


@bp.get("/snapshots")
def list_snapshots():
    limit = bounded_limit(request.args.get("limit"))
    offset = bounded_offset(request.args.get("offset"))
    location = (request.args.get("location") or "").strip() or None

    connection = get_connection()
    snapshots = weather_service.list_snapshots(
        connection, location=location, limit=limit, offset=offset
    )
    total_count = weather_service.count_snapshots(connection, location=location)
    return (
        jsonify(
            {
                "data": snapshots,
                "count": len(snapshots),
                "total_count": total_count,
                "has_more": offset + len(snapshots) < total_count,
                "limit": limit,
                "offset": offset,
            }
        ),
        200,
    )


@bp.get("/snapshots/<int:snapshot_id>")
def get_snapshot(snapshot_id: int):
    snapshot = weather_service.get_snapshot(get_connection(), snapshot_id)
    return jsonify({"data": snapshot}), 200
