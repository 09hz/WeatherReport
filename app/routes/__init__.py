"""HTTP layer: blueprints and their registration."""

from __future__ import annotations

from flask import Flask

from . import health, journal, limits, weather

BLUEPRINTS = (health.bp, weather.bp, journal.bp, limits.bp)


def register_blueprints(app: Flask) -> None:
    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)


__all__ = ["register_blueprints", "health", "journal", "limits", "weather"]
