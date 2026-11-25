"""Token-bucket rate limiting, keyed by client address.

Process-local and in-memory: it protects a single instance from a chatty
client, it is not a distributed quota. Behind several workers each worker
enforces its own bucket, so the effective limit is per worker — documented
here rather than silently assumed.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from flask import Flask, Response, jsonify, request

from .errors import RateLimitExceededError
from .request_log import client_ip


class TokenBucketLimiter:
    """`capacity` requests per `window_seconds`, refilled continuously."""

    def __init__(
        self,
        *,
        capacity: int = 100,
        window_seconds: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = max(1, int(capacity))
        self.window_seconds = max(1, int(window_seconds))
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}

    @property
    def refill_per_second(self) -> float:
        return self.capacity / self.window_seconds

    def _refill(self, key: str, now: float) -> float:
        tokens, last_seen = self._buckets.get(key, (float(self.capacity), now))
        tokens = min(
            float(self.capacity), tokens + (now - last_seen) * self.refill_per_second
        )
        return tokens

    def consume(self, key: str, amount: float = 1.0) -> tuple[bool, dict[str, Any]]:
        """Take one token. Returns (allowed, quota snapshot)."""
        with self._lock:
            now = self._clock()
            tokens = self._refill(key, now)
            allowed = tokens >= amount
            if allowed:
                tokens -= amount
            self._buckets[key] = (tokens, now)
            deficit = max(0.0, amount - tokens) if not allowed else 0.0
            return allowed, {
                "limit": self.capacity,
                "window_seconds": self.window_seconds,
                "remaining": int(tokens),
                "retry_after_seconds": (
                    round(deficit / self.refill_per_second, 3) if deficit else 0
                ),
            }

    def peek(self, key: str) -> dict[str, Any]:
        """Current quota without consuming a token."""
        with self._lock:
            now = self._clock()
            tokens = self._refill(key, now)
            return {
                "limit": self.capacity,
                "window_seconds": self.window_seconds,
                "remaining": int(tokens),
                "reset_in_seconds": round(
                    (self.capacity - tokens) / self.refill_per_second, 3
                ),
            }

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)


def limiter_key() -> str:
    return client_ip() or "unknown"


def register_rate_limiting(app: Flask) -> None:
    """Attach the limiter when RATE_LIMIT_ENABLED is set."""
    limiter = TokenBucketLimiter(
        capacity=int(app.config.get("RATE_LIMIT_REQUESTS", 100)),
        window_seconds=int(app.config.get("RATE_LIMIT_WINDOW_SECONDS", 60)),
    )
    app.extensions["rate_limiter"] = limiter

    if not app.config.get("RATE_LIMIT_ENABLED"):
        return

    @app.before_request
    def _enforce_limit():
        # The quota endpoint must stay reachable so a throttled client can
        # find out when it may retry.
        if request.endpoint == "limits.limit_status":
            return None
        allowed, quota = limiter.consume(limiter_key())
        if allowed:
            return None
        raise RateLimitExceededError(
            "Rate limit exceeded. Slow down and retry after the window resets.",
            details=quota,
        )

    @app.after_request
    def _advertise_quota(response: Response) -> Response:
        quota = limiter.peek(limiter_key())
        response.headers["X-RateLimit-Limit"] = str(quota["limit"])
        response.headers["X-RateLimit-Remaining"] = str(quota["remaining"])
        response.headers["X-RateLimit-Window"] = str(quota["window_seconds"])
        return response


def quota_response():
    """Payload for GET /api/limits/status."""
    from flask import current_app

    limiter: TokenBucketLimiter = current_app.extensions["rate_limiter"]
    quota = limiter.peek(limiter_key())
    quota["enabled"] = bool(current_app.config.get("RATE_LIMIT_ENABLED"))
    quota["client"] = limiter_key()
    return jsonify({"data": quota}), 200


__all__ = [
    "TokenBucketLimiter",
    "limiter_key",
    "quota_response",
    "register_rate_limiting",
]
