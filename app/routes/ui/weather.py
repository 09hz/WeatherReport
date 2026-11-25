"""Weather pages (UI-2, WeatherReport-bei).

Two pages:

    GET,POST /ui/weather            lookup form + current conditions + save
    GET      /ui/weather/snapshots  paginated list of stored snapshots

Both consume app.services.weather_service directly — see the package docstring
for why a view never issues an HTTP request to this app's own JSON API.

Error policy: every expected failure in the service layer is an AppError, and
the app-wide handler turns an uncaught one into a JSON body. A UI view must
never hand a browser that envelope, so each view catches AppError itself and
renders it as a `.notice--error` page (keeping the error's own status code) with
`helpers.field_errors()` marking the offending control. The common case in a
deployment without credentials is ProviderNotConfiguredError (503).
"""

from __future__ import annotations

from typing import Any

from flask import current_app, redirect, render_template, request, url_for

from ...database.connection import get_connection
from ...errors import AppError
from ...services import weather_service
from ...services.weather_cache import WeatherCache, fetch_with_cache
from ...validation import required_string
from . import bp
from .helpers import field_errors, notice_from_error, page_window, pager, present

#: Observation fields of a live lookup, in the order the `.datagrid` shows
#: them. Every one of them can be null, which is why the template renders
#: through `helpers.present()`.
CURRENT_FIELDS = (
    "location",
    "observed_at",
    "temperature_c",
    "humidity",
    "wind_speed_kph",
    "conditions",
    "condition_code",
    "provider",
)

#: Columns of the snapshot table, as (field, label, numeric).
SNAPSHOT_COLUMNS = (
    ("id", "ID", True),
    ("location", "Location", False),
    ("observed_at", "Observed at", False),
    ("temperature_c", "Temp °C", True),
    ("humidity", "Humidity %", True),
    ("wind_speed_kph", "Wind kph", True),
    ("conditions", "Conditions", False),
    ("provider", "Provider", False),
    ("created_at", "Stored at", False),
)

#: Rows per page of the snapshot list.
DEFAULT_LIMIT = 20


def _table_rows(snapshots: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """One `.table__row` per snapshot, as null-aware cells in column order.

    Built with `helpers.present()` so a null measurement carries `is_null` into
    the template and renders as `.placeholder`, never as a blank cell or the
    string "None".
    """
    fields = [field for field, _label, _numeric in SNAPSHOT_COLUMNS]
    numeric = [flag for _field, _label, flag in SNAPSHOT_COLUMNS]
    return [
        [
            {**cell, "numeric": is_numeric}
            for cell, is_numeric in zip(present(snapshot, fields), numeric)
        ]
        for snapshot in snapshots
    ]


def _cache() -> WeatherCache:
    return current_app.extensions["weather_cache"]


def _fetch_cached(location: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch conditions through the same TTL cache the JSON API uses."""
    provider = current_app.extensions["weather_provider"]
    snapshot, cache_meta = fetch_with_cache(
        _cache(),
        location,
        lambda loc: weather_service.fetch_current(loc, provider),
    )
    # The raw provider payload is API plumbing, not something to render.
    snapshot = dict(snapshot)
    snapshot.pop("raw_response", None)
    return snapshot, cache_meta


@bp.route("/weather", methods=("GET", "POST"))
def weather_current():
    """Lookup form; on POST, current conditions or a save-then-redirect.

    GET never calls the provider: navigating to the page must not spend a
    rate-limit slot, so the form is merely prefilled from ?location=.
    """
    location = (
        request.form.get("location") if request.method == "POST"
        else request.args.get("location")
    ) or ""
    location = location.strip()

    context: dict[str, Any] = {
        "active": "ui.weather_current",
        "title": "Current conditions",
        "location": location,
        "snapshot": None,
        "rows": [],
        "cache": None,
        "notice": None,
        "invalid": {},
    }

    if request.method != "POST":
        return render_template("ui/weather_current.html", **context)

    save = (request.form.get("action") or "lookup") == "save"
    try:
        # Reuse the API's own validator so the two interfaces cannot disagree
        # about what a usable location is.
        cleaned = required_string({"location": location or None}, "location")
        snapshot, cache_meta = _fetch_cached(cleaned)
        if save:
            stored = weather_service.save_snapshot(get_connection(), snapshot)
            # POST-then-redirect: a refresh re-renders the list, not the write.
            return redirect(
                url_for(
                    "ui.weather_snapshots",
                    location=stored["location"],
                    saved=stored["id"],
                )
            )
        context.update(
            location=cleaned,
            snapshot=snapshot,
            rows=present(snapshot, CURRENT_FIELDS),
            cache=cache_meta,
        )
    except AppError as error:
        context.update(notice=notice_from_error(error), invalid=field_errors(error))
        return render_template("ui/weather_current.html", **context), error.status_code

    return render_template("ui/weather_current.html", **context)


@bp.get("/weather/snapshots")
def weather_snapshots():
    """Paginated stored snapshots, optionally filtered by ?location=."""
    location = (request.args.get("location") or "").strip()
    saved = (request.args.get("saved") or "").strip()

    context: dict[str, Any] = {
        "active": "ui.weather_snapshots",
        "title": "Stored snapshots",
        "location": location,
        "saved": saved,
        "columns": SNAPSHOT_COLUMNS,
        "rows": [],
        "notice": None,
        "invalid": {},
    }

    try:
        window = page_window(request.args, default_limit=DEFAULT_LIMIT)
        connection = get_connection()
        snapshots = weather_service.list_snapshots(
            connection, location=location or None, **window
        )
        total_count = weather_service.count_snapshots(
            connection, location=location or None
        )
        context["rows"] = _table_rows(snapshots)
        context["page"] = pager(
            limit=window["limit"],
            offset=window["offset"],
            count=len(snapshots),
            total_count=total_count,
        )
    except AppError as error:
        # A bad ?limit=/?offset= must still render the page's chrome and the
        # filter form, so the reader can correct it without editing the URL.
        context["page"] = pager(
            limit=DEFAULT_LIMIT, offset=0, count=0, total_count=0
        )
        context.update(notice=notice_from_error(error), invalid=field_errors(error))
        return (
            render_template("ui/weather_snapshots.html", **context),
            error.status_code,
        )

    return render_template("ui/weather_snapshots.html", **context)


__all__ = [
    "CURRENT_FIELDS",
    "DEFAULT_LIMIT",
    "SNAPSHOT_COLUMNS",
    "weather_current",
    "weather_snapshots",
]
