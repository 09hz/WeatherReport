"""Request logging / audit trail.

Every request leaves one JSON object per line (JSON Lines) so the trail can
be grepped, tailed and parsed without a log parser. Logging never changes a
response: if the sink cannot be written, the failure is reported once through
the app logger and requests continue.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, g, request

_WRITE_LOCK = threading.Lock()


def client_ip() -> str | None:
    """Best-effort client address, honouring a proxy's X-Forwarded-For.

    Only the left-most entry is used: it is the original client as recorded
    by the first proxy. The header is attacker-controlled, so treat the
    value as a hint for debugging, never as an authorization input.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.headers.get("X-Real-IP") or request.remote_addr


def _record(response: Response) -> dict[str, Any]:
    started = g.get("_request_started_at")
    duration_ms = (
        round((time.perf_counter() - started) * 1000, 3) if started else None
    )
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "method": request.method,
        "path": request.path,
        "endpoint": request.endpoint,
        "query": request.query_string.decode("utf-8", "replace") or None,
        "status_code": response.status_code,
        "duration_ms": duration_ms,
        "client_ip": client_ip(),
        "user_agent": request.headers.get("User-Agent"),
        "content_length": response.calculate_content_length(),
    }


def register_request_logging(app: Flask) -> None:
    """Attach the audit trail when REQUEST_LOG_ENABLED is set."""
    if not app.config.get("REQUEST_LOG_ENABLED"):
        return

    log_path = Path(app.config["REQUEST_LOG_PATH"])
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        app.logger.warning("Request log directory unavailable (%s): %s", log_path, exc)
        return

    @app.before_request
    def _start_timer() -> None:
        g._request_started_at = time.perf_counter()

    @app.after_request
    def _write_entry(response: Response) -> Response:
        line = json.dumps(_record(response), default=str)
        try:
            with _WRITE_LOCK:
                with open(log_path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError as exc:
            # Never let auditing break the response the client asked for.
            app.logger.warning("Request log write failed: %s", exc)
        return response


__all__ = ["client_ip", "register_request_logging"]
