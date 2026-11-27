"""Flask application factory for the Weather & Journal backend."""

from __future__ import annotations

from typing import Any, Mapping

from flask import Flask, jsonify, redirect, url_for

from . import database
from .config import load_config
from .errors import register_error_handlers
from .rate_limit import register_rate_limiting
from .request_log import register_request_logging
from .routes import register_blueprints
from .services.weather_cache import WeatherCache
from .services.weather_provider import build_nws_provider, build_provider

__version__ = "0.1.0"

# Side effects a test must opt into explicitly: a shared log file and a
# process-wide throttle would otherwise leak across the suite.
_TEST_DISABLED_FEATURES = ("REQUEST_LOG_ENABLED", "RATE_LIMIT_ENABLED")


def _load_dotenv() -> None:
    """Load a local .env when python-dotenv is installed. Never required."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(override=False)


def create_app(overrides: Mapping[str, Any] | None = None) -> Flask:
    """Build and configure the application.

    `overrides` lets tests inject config (e.g. a temporary DATABASE_PATH)
    without mutating the process environment.
    """
    _load_dotenv()

    app = Flask(__name__, instance_relative_config=False)
    app.config.from_mapping(load_config().to_flask_mapping())
    if overrides:
        app.config.from_mapping(overrides)

    if app.config.get("TESTING"):
        for key in _TEST_DISABLED_FEATURES:
            if not overrides or key not in overrides:
                app.config[key] = False

    # Weather provider is resolved once, at startup. With no credentials it is
    # the unconfigured provider, so no external request can ever be made.
    app.extensions["weather_provider"] = build_provider(app.config)
    # The coordinate-based observations route always uses NWS, which is keyless.
    # Restored after merge 2667f24 dropped this wiring (WeatherReport-83j).
    provider = app.extensions["weather_provider"]
    app.extensions["nws_provider"] = (
        provider
        if getattr(provider, "name", None) == "nws"
        else build_nws_provider(app.config)
    )
    # TTL cache in front of the provider: cuts repeat lookups for one
    # location down to a single outbound request per window.
    app.extensions["weather_cache"] = WeatherCache(
        ttl_seconds=int(app.config.get("WEATHER_CACHE_TTL_SECONDS", 300)),
        max_entries=int(app.config.get("WEATHER_CACHE_MAX_ENTRIES", 256)),
    )

    database.init_app(app)
    register_error_handlers(app)
    register_request_logging(app)
    register_rate_limiting(app)
    register_blueprints(app)

    @app.get("/")
    def index():
        # A browser hitting the bare domain must get the UI, not a JSON dump.
        # The machine-readable listing moved to /api; this redirect keeps /ui/
        # as the single canonical UI URL instead of mounting it twice.
        return redirect(url_for("ui.index"))

    @app.get("/api")
    def api_index():
        return (
            jsonify(
                {
                    "name": "weather-journal",
                    "version": __version__,
                    "ui": "GET  /ui/",
                    "endpoints": [
                        "GET  /health",
                        "GET  /api/limits/status",
                        "GET  /api/weather/provider",
                        "GET  /api/weather/current?location=",
                        "GET  /api/weather/observations?lat=&lon=",
                        "GET  /api/weather/cached/<location>",
                        "POST /api/weather/snapshots",
                        "GET  /api/weather/snapshots?location=&limit=&offset=",
                        "GET  /api/weather/snapshots/<id>",
                        "POST /api/journal/entries",
                        "GET  /api/journal/entries?q=&location=&limit=&offset=",
                        "GET  /api/journal/entries/<id>",
                        "PATCH /api/journal/entries/<id>",
                        "DELETE /api/journal/entries/<id>",
                        "DELETE /api/journal/entries?ids=1,2,3",
                    ],
                }
            ),
            200,
        )

    return app


__all__ = ["create_app", "__version__"]
