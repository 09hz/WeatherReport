"""Helpers shared by the UI page modules (UI-0).

Kept deliberately small: the UI's job is to render what the service layer
already returns, not to re-implement it.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from ...errors import AppError
from ...validation import bounded_limit, bounded_offset

#: What a null API value renders as. The class contract forbids blanks and the
#: literal string "None"; the template pairs this with `.placeholder`.
NULL_TEXT = "\u2014"


def page_window(args: Mapping[str, Any], *, default_limit: int = 20) -> dict[str, int]:
    """Read limit/offset from a query string, reusing the API's own bounds.

    Returns the kwargs the service-layer list functions take. A bad value
    raises ValidationError exactly as it would on the JSON API, so the UI and
    the API cannot disagree about what a valid page is.
    """
    return {
        "limit": bounded_limit(args.get("limit"), default=default_limit),
        "offset": bounded_offset(args.get("offset")),
    }


def pager(
    *, limit: int, offset: int, count: int, total_count: int
) -> dict[str, Any]:
    """Context for the `.pager` block: what to show and what to disable."""
    first = offset + 1 if count else 0
    return {
        "limit": limit,
        "offset": offset,
        "count": count,
        "total_count": total_count,
        "first": first,
        "last": offset + count,
        "has_more": offset + count < total_count,
        "has_previous": offset > 0,
        "previous_offset": max(offset - limit, 0),
        "next_offset": offset + limit,
    }


def notice_from_error(error: AppError) -> dict[str, Any]:
    """Turn a service-layer error into `.notice--error` context.

    `details` is flattened to a list of strings so the template never has to
    branch on the shape of an error payload.
    """
    details = getattr(error, "details", None) or {}
    return {
        "level": "error",
        "title": str(error),
        "details": [f"{key}: {value}" for key, value in sorted(details.items())],
        "field": details.get("field"),
    }


def field_errors(error: AppError | None) -> dict[str, str]:
    """Map a validation error onto `{field: message}` for `.field--invalid`.

    The API reports one field at a time (it raises on the first failure), so
    this is a one-entry dict or empty.
    """
    if error is None:
        return {}
    details = getattr(error, "details", None) or {}
    field = details.get("field")
    return {field: str(error)} if field else {}


def present(record: Mapping[str, Any], keys: Iterable[str]) -> list[dict[str, Any]]:
    """Order a record into (label, value, is_null) rows for a `.datagrid`.

    `is_null` is what the template keys `.datagrid__value--null` off, so a
    missing measurement is visibly absent rather than silently blank.
    """
    rows = []
    for key in keys:
        value = record.get(key)
        rows.append(
            {
                "key": key.replace("_", " "),
                "value": NULL_TEXT if value is None or value == "" else value,
                "is_null": value is None or value == "",
            }
        )
    return rows


__all__ = [
    "NULL_TEXT",
    "field_errors",
    "notice_from_error",
    "page_window",
    "pager",
    "present",
]
