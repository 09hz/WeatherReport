"""Journal pages (UI-3, WeatherReport-ck6).

Four pages: a filtered/paginated list, one form shared by create and edit, and
a delete confirmation. An HTML form can only issue GET and POST, so the edit
and delete pages POST and call app.services.journal_service directly — the
PATCH/DELETE verbs of /api/journal/entries are unreachable from markup.

Two rules shape the module:

* Validation goes through app.validation with exactly the arguments
  app/routes/journal.py uses, so a payload the JSON API accepts is a payload
  this UI accepts and vice versa. Divergence here would be a silent contract
  split between the two front ends.
* Every AppError is caught and rendered. The app-wide handler in app/errors.py
  answers in JSON, which is right for the API and wrong for a page, so a view
  that let a ValidationError escape would hand the browser an error envelope
  instead of the form the user was filling in.

All writes answer with a redirect (303 via Flask's default 302 semantics is
not needed: a plain redirect after POST is enough to keep a refresh from
repeating the write).
"""

from __future__ import annotations

from typing import Any, Mapping

from flask import redirect, render_template, request, url_for

from ...database.connection import get_connection
from ...errors import AppError, NotFoundError
from ...services import journal_service
from ...validation import optional_positive_int, required_date, required_string
from . import bp
from .helpers import field_errors, notice_from_error, page_window, pager, present

#: The controls journal_form.html posts, in render order. Validation walks the
#: same order, so the first failure is the topmost offending field.
FORM_FIELDS = ("title", "date", "location", "description", "weather_snapshot_id")

#: Columns of the list table that may be null/empty and so need .placeholder.
NULLABLE_FIELDS = ("description", "weather_snapshot_id")

#: Order the delete page's .datagrid shows a record in.
DETAIL_FIELDS = (
    "id",
    "title",
    "date",
    "location",
    "description",
    "weather_snapshot_id",
    "created_at",
    "updated_at",
)


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #


@bp.get("/journal")
def journal_list():
    """Entries newest-first, narrowed by ?q= and ?location= together."""
    search = (request.args.get("q") or "").strip()
    location = (request.args.get("location") or "").strip()

    try:
        window = page_window(request.args)
    except AppError as error:
        # A hand-edited ?limit=/?offset= must not produce a traceback.
        return _render_list(search, location, error=error)

    connection = get_connection()
    filters = {"search": search or None, "location": location or None}
    try:
        entries = journal_service.list_entries(connection, **filters, **window)
        total_count = journal_service.count_entries(connection, **filters)
    except AppError as error:
        return _render_list(search, location, error=error)

    page = pager(
        limit=window["limit"],
        offset=window["offset"],
        count=len(entries),
        total_count=total_count,
    )
    return _render_list(search, location, entries=entries, page=page)


def _render_list(
    search: str,
    location: str,
    *,
    entries: list[dict[str, Any]] | None = None,
    page: dict[str, Any] | None = None,
    error: AppError | None = None,
):
    return (
        render_template(
            "ui/journal_list.html",
            active="ui.journal_list",
            title="Journal",
            q=search,
            location=location,
            entries=entries or [],
            page=page,
            nullable=NULLABLE_FIELDS,
            notice=notice_from_error(error) if error is not None else None,
        ),
        error.status_code if error is not None else 200,
    )


# --------------------------------------------------------------------------- #
# create / edit
# --------------------------------------------------------------------------- #


@bp.route("/journal/new", methods=["GET", "POST"])
def journal_new():
    if request.method == "GET":
        return _render_form(dict.fromkeys(FORM_FIELDS, ""))

    values = _posted_values(request.form)
    try:
        fields = _validated(values)
        journal_service.create_entry(
            get_connection(),
            title=fields["title"],
            entry_date=fields["date"],
            location=fields["location"],
            description=fields["description"],
            weather_snapshot_id=fields["weather_snapshot_id"],
        )
    except AppError as error:
        # Re-render with what they typed: losing a long description to a typo
        # in the date field would be the worst failure mode of this page.
        return _render_form(values, error=error)

    return redirect(url_for("ui.journal_list"))


@bp.route("/journal/<int:entry_id>/edit", methods=["GET", "POST"])
def journal_edit(entry_id: int):
    connection = get_connection()

    if request.method == "GET":
        try:
            entry = journal_service.get_entry(connection, entry_id)
        except AppError as error:
            return _render_list("", "", error=error)
        return _render_form(_values_from_entry(entry), entry_id=entry_id)

    values = _posted_values(request.form)
    try:
        fields = _validated(values)
        # Every field is sent by the form, so the partial-update API is driven
        # as a full replacement. UPDATABLE_FIELDS keys, not column names.
        journal_service.update_entry(
            connection,
            entry_id,
            {
                "title": fields["title"],
                "date": fields["date"],
                "location": fields["location"],
                "description": fields["description"],
                "weather_snapshot_id": fields["weather_snapshot_id"],
            },
        )
    except NotFoundError as error:
        # The entry vanished between GET and POST: there is no form to show.
        return _render_list("", "", error=error)
    except AppError as error:
        return _render_form(values, error=error, entry_id=entry_id)

    return redirect(url_for("ui.journal_list"))


def _render_form(
    values: Mapping[str, str],
    *,
    error: AppError | None = None,
    entry_id: int | None = None,
):
    editing = entry_id is not None
    return (
        render_template(
            "ui/journal_form.html",
            active="ui.journal_list",
            title="Edit entry" if editing else "New entry",
            editing=editing,
            entry_id=entry_id,
            action=(
                url_for("ui.journal_edit", entry_id=entry_id)
                if editing
                else url_for("ui.journal_new")
            ),
            values=values,
            errors=field_errors(error),
            notice=notice_from_error(error) if error is not None else None,
        ),
        error.status_code if error is not None else 200,
    )


def _posted_values(form: Mapping[str, Any]) -> dict[str, str]:
    """The submitted form as plain trimmed strings, one key per control."""
    return {name: (form.get(name) or "").strip() for name in FORM_FIELDS}


def _values_from_entry(entry: Mapping[str, Any]) -> dict[str, str]:
    """A stored entry as form values. None becomes "", never the text "None"."""
    return {
        name: "" if entry.get(name) is None else str(entry.get(name))
        for name in FORM_FIELDS
    }


def _validated(values: Mapping[str, str]) -> dict[str, Any]:
    """Validate a submission with the JSON API's own rules and limits.

    Raises ValidationError on the first bad field, which is what gives
    field_errors() a single field to mark .field--invalid.
    """
    return {
        "title": required_string(values, "title", max_length=200),
        "date": required_date(values, "date"),
        "location": required_string(values, "location"),
        "description": required_string(values, "description", max_length=10_000),
        "weather_snapshot_id": optional_positive_int(values, "weather_snapshot_id"),
    }


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #


@bp.route("/journal/<int:entry_id>/delete", methods=["GET", "POST"])
def journal_delete(entry_id: int):
    """Confirmation on GET, the deletion itself on POST."""
    connection = get_connection()

    if request.method == "POST":
        try:
            journal_service.delete_entry(connection, entry_id)
        except AppError as error:
            return _render_delete(entry_id, error=error)
        return redirect(url_for("ui.journal_list"))

    try:
        entry = journal_service.get_entry(connection, entry_id)
    except AppError as error:
        return _render_delete(entry_id, error=error)
    return _render_delete(entry_id, entry=entry)


def _render_delete(
    entry_id: int,
    *,
    entry: Mapping[str, Any] | None = None,
    error: AppError | None = None,
):
    return (
        render_template(
            "ui/journal_delete.html",
            active="ui.journal_list",
            title="Delete entry",
            entry_id=entry_id,
            entry=entry,
            # present() already substitutes the placeholder text and flags
            # which rows were null, so the template never prints "None".
            rows=present(entry, DETAIL_FIELDS) if entry is not None else [],
            notice=notice_from_error(error) if error is not None else None,
        ),
        error.status_code if error is not None else 200,
    )
