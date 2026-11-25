"""Domain errors and consistent JSON error responses.

Every failure leaving the API has the same envelope:

    {"error": {"code": "...", "message": "...", "details": {...}}}
"""

from __future__ import annotations

from typing import Any

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException


class AppError(Exception):
    """Base class for expected, client-reportable failures."""

    status_code = 500
    code = "internal_error"
    # Expected conditions set this False so they do not emit stack traces.
    log_traceback = True

    def __init__(
        self,
        message: str = "An unexpected error occurred.",
        *,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        code: str | None = None,
        log_traceback: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        if log_traceback is not None:
            # Lets a caller mark one instance as expected (e.g. upstream 4xx
            # noise) without inventing a subclass per condition.
            self.log_traceback = log_traceback

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return {"error": payload}


class ValidationError(AppError):
    status_code = 400
    code = "validation_error"

    def __init__(
        self,
        message: str = "Request payload failed validation.",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"

    def __init__(self, message: str = "Resource not found.", **kwargs: Any) -> None:
        super().__init__(message, **kwargs)


class ConflictError(AppError):
    """Raised when a request would duplicate an existing resource."""

    status_code = 409
    code = "conflict"
    # A rejected duplicate is an expected outcome, not a fault.
    log_traceback = False

    def __init__(self, message: str = "Resource already exists.", **kwargs: Any) -> None:
        super().__init__(message, **kwargs)


class RateLimitExceededError(AppError):
    """Raised when a client exceeds its request quota."""

    status_code = 429
    code = "rate_limit_exceeded"
    log_traceback = False

    def __init__(
        self, message: str = "Rate limit exceeded.", **kwargs: Any
    ) -> None:
        super().__init__(message, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        # Acceptance criterion: the flag is readable at the top level.
        payload["rate_limit_exceeded"] = True
        return payload


class ProviderNotConfiguredError(AppError):
    """Raised when a weather provider is requested but no credentials exist."""

    status_code = 503
    code = "weather_provider_not_configured"
    # A missing provider is a documented deployment state, not a fault.
    log_traceback = False

    def __init__(
        self,
        message: str = (
            "No weather provider is configured. Set WEATHER_API_KEY and "
            "WEATHER_API_BASE_URL to enable external weather requests."
        ),
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)


class ProviderError(AppError):
    """Raised when a configured provider fails or returns unusable data."""

    status_code = 502
    code = "weather_provider_error"


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(AppError)
    def _handle_app_error(error: AppError):
        if error.status_code >= 500 and error.log_traceback:
            app.logger.exception("Application error: %s", error.message)
        elif error.status_code >= 500:
            app.logger.info("%s: %s", error.code, error.message)
        return jsonify(error.to_dict()), error.status_code

    @app.errorhandler(HTTPException)
    def _handle_http_exception(error: HTTPException):
        payload = {
            "error": {
                "code": (error.name or "http_error").lower().replace(" ", "_"),
                "message": error.description or error.name or "HTTP error.",
            }
        }
        return jsonify(payload), error.code or 500

    @app.errorhandler(Exception)
    def _handle_unexpected(error: Exception):
        app.logger.exception("Unhandled exception", exc_info=error)
        return jsonify(AppError().to_dict()), 500
