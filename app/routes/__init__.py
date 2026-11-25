"""HTTP layer: blueprints and their registration."""

from __future__ import annotations

from flask import Flask

from . import health, journal, limits, ui, weather

BLUEPRINTS = (health.bp, weather.bp, journal.bp, limits.bp, ui.bp)


def register_blueprints(app: Flask) -> None:
    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)


__all__ = ["register_blueprints", "health", "journal", "limits", "ui", "weather"]
