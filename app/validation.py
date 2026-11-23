"""Small input-validation helpers shared by the route layer."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

from .errors import ValidationError

MAX_TEXT_LENGTH = 10_000


def require_mapping(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValidationError(
            "Request body must be a JSON object.",
            details={"received_type": type(payload).__name__},
        )
    return payload


def required_string(
    payload: Mapping[str, Any],
    field: str,
    *,
    max_length: int = 255,
    min_length: int = 1,
) -> str:
    value = payload.get(field)
    if value is None:
        raise ValidationError(
            f"Field '{field}' is required.", details={"field": field}
        )
    if not isinstance(value, str):
        raise ValidationError(
            f"Field '{field}' must be a string.",
            details={"field": field, "received_type": type(value).__name__},
        )
    cleaned = value.strip()
    if len(cleaned) < min_length:
        raise ValidationError(
            f"Field '{field}' must not be empty.", details={"field": field}
        )
    if len(cleaned) > max_length:
        raise ValidationError(
            f"Field '{field}' must be at most {max_length} characters.",
            details={"field": field, "max_length": max_length},
        )
    return cleaned


def optional_string(
    payload: Mapping[str, Any], field: str, *, max_length: int = 255
) -> str | None:
    if payload.get(field) in (None, ""):
        return None
    return required_string(payload, field, max_length=max_length)


def required_date(payload: Mapping[str, Any], field: str) -> str:
    """Validate an ISO-8601 date (YYYY-MM-DD) and return it normalized."""
    raw = required_string(payload, field, max_length=32)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(raw).date()
        except ValueError as exc:
            raise ValidationError(
                f"Field '{field}' must be an ISO-8601 date (YYYY-MM-DD).",
                details={"field": field, "received": raw},
            ) from exc
    return parsed.isoformat()


def optional_positive_int(
    payload: Mapping[str, Any], field: str
) -> int | None:
    value = payload.get(field)
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValidationError(
            f"Field '{field}' must be an integer.", details={"field": field}
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"Field '{field}' must be an integer.",
            details={"field": field, "received": str(value)},
        ) from exc
    if parsed <= 0:
        raise ValidationError(
            f"Field '{field}' must be a positive integer.",
            details={"field": field, "received": parsed},
        )
    return parsed


def bounded_limit(raw: Any, *, default: int = 50, maximum: int = 200) -> int:
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "Query parameter 'limit' must be an integer.",
            details={"received": str(raw)},
        ) from exc
    if value < 1:
        raise ValidationError(
            "Query parameter 'limit' must be at least 1.", details={"received": value}
        )
    return min(value, maximum)


def bounded_offset(raw: Any) -> int:
    if raw in (None, ""):
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "Query parameter 'offset' must be an integer.",
            details={"received": str(raw)},
        ) from exc
    if value < 0:
        raise ValidationError(
            "Query parameter 'offset' must not be negative.",
            details={"received": value},
        )
    return value
