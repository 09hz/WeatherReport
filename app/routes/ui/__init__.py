"""Server-rendered UI blueprint.

Mounted at /ui so GET / can keep returning the JSON endpoint index that the
API's own tests assert (tests/test_app.py, tests/test_nws.py). The UI calls the
service layer directly rather than looping back through HTTP: a Flask view
issuing a request to its own app would deadlock the single-threaded dev server
and would double the rate-limit accounting.

Package layout, so the weather and journal pages can be developed in parallel
without two authors editing one module:

    __init__.py   blueprint, shared chrome, overview page   (UI-0)
    helpers.py    rendering helpers both page sets need     (UI-0)
    weather.py    /ui/weather*                              (UI-2)
    journal.py    /ui/journal*                              (UI-3)

Every template is written against the FROZEN class vocabulary in
docs/UI_CLASS_CONTRACT.md. Adding a class means editing that file in the same
change; tests/test_ui_foundation.py enforces it.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, render_template

bp = Blueprint("ui", __name__, url_prefix="/ui")

#: Nav entries shared by every page: (endpoint, label).
NAV = (
    ("ui.index", "Overview"),
    ("ui.weather_current", "Weather"),
    ("ui.weather_snapshots", "Snapshots"),
    ("ui.journal_list", "Journal"),
)


def provider_state() -> dict[str, Any]:
    """Provider name/availability for the header banner.

    Read from app.extensions rather than GET /api/weather/provider so a page
    render never depends on the HTTP stack being reentrant.
    """
    provider = current_app.extensions.get("weather_provider")
    return {
        "name": getattr(provider, "name", "unconfigured"),
        "available": bool(getattr(provider, "available", False)),
    }


@bp.app_context_processor
def inject_chrome() -> dict[str, Any]:
    """Values every template's layout needs."""
    # Imported here, not at module scope: app/__init__.py imports the route
    # package while defining __version__, so a top-level import is circular.
    from ... import __version__

    return {"nav": NAV, "version": __version__, "provider": provider_state()}


@bp.get("/")
def index():
    return render_template("ui/index.html", active="ui.index")


# Imported for their side effect of registering routes on `bp`. Must stay at
# the bottom: the modules import `bp` from this one.
from . import journal, weather  # noqa: E402,F401

__all__ = ["bp", "NAV", "provider_state", "journal", "weather"]
