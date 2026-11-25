"""Rate-limit quota endpoint."""

from __future__ import annotations

from flask import Blueprint

from ..rate_limit import quota_response

bp = Blueprint("limits", __name__, url_prefix="/api/limits")


@bp.get("/status")
def limit_status():
    """Report the calling client's remaining request quota."""
    return quota_response()
