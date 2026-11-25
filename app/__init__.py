"""Flask application factory for the Weather & Journal backend."""

from __future__ import annotations

from typing import Any, Mapping

from flask import Flask, jsonify

from . import database
from .config import load_config
from .errors import register_error_handlers
from .routes import register_blueprints
from .services.weather_provider import build_nws_provider, build_provider

__version__ = "0.1.0"


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

    # Weather provider is resolved once, at startup. With no credentials it is
    # the unconfigured provider, so no external request can ever be made.
    app.extensions["weather_provider"] = build_provider(app.config)
    # The coordinate-based observations route always uses NWS, which is keyless.
    provider = app.extensions["weather_provider"]
    app.extensions["nws_provider"] = (
        provider if getattr(provider, "name", None) == "nws" else build_nws_provider(app.config)
    )

    database.init_app(app)
    register_error_handlers(app)
    register_blueprints(app)

    @app.get("/")
    def index():
        return (
            jsonify(
                {
                    "name": "weather-journal",
                    "version": __version__,
                    "endpoints": [
                        "GET  /health",
                        "GET  /api/weather/provider",
                        "GET  /api/weather/current?location=",
                        "GET  /api/weather/observations?lat=&lon=",
                        "POST /api/weather/snapshots",
                        "GET  /api/weather/snapshots",
                        "GET  /api/weather/snapshots/<id>",
                        "POST /api/journal/entries",
                        "GET  /api/journal/entries?q=&location=&limit=&offset=",
                        "GET  /api/journal/entries/<id>",
                        "DELETE /api/journal/entries/<id>",
                    ],
                }
            ),
            200,
        )

    return app


__all__ = ["create_app", "__version__"]
