"""In-memory TTL cache for weather provider responses.

The cache exists to stop repeated lookups for the same location from hitting
the provider. It is deliberately process-local: no shared store is assumed,
and nothing is persisted, so a restart simply loses warm entries.

Cached payloads are real provider responses only. A miss never fabricates a
value — it reports the miss and lets the caller decide whether to fetch.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable


def cache_key(location: str) -> str:
    """Normalize a location so 'Austin', ' austin ' share one entry."""
    return " ".join(location.strip().lower().split())


class WeatherCache:
    """Thread-safe TTL cache with hit/miss accounting."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 300,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = max(0, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, dict[str, Any]]] = {}
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0

    def get(self, location: str) -> tuple[dict[str, Any] | None, float | None]:
        """Return (payload, age_seconds) for a live entry, else (None, None)."""
        key = cache_key(location)
        with self._lock:
            record = self._entries.get(key)
            if record is None:
                self.misses += 1
                return None, None
            stored_at, payload = record
            age = self._clock() - stored_at
            if not self.enabled or age >= self.ttl_seconds:
                del self._entries[key]
                self.misses += 1
                return None, None
            self.hits += 1
            return dict(payload), age

    def set(self, location: str, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        key = cache_key(location)
        with self._lock:
            if key not in self._entries and len(self._entries) >= self.max_entries:
                oldest = min(self._entries, key=lambda k: self._entries[k][0])
                del self._entries[oldest]
                self.evictions += 1
            self._entries[key] = (self._clock(), dict(payload))

    def invalidate(self, location: str | None = None) -> None:
        with self._lock:
            if location is None:
                self._entries.clear()
            else:
                self._entries.pop(cache_key(location), None)

    def metadata(self, *, hit: bool, age_seconds: float | None = None) -> dict[str, Any]:
        """Per-response cache metadata."""
        meta: dict[str, Any] = {
            "hit": hit,
            "ttl_seconds": self.ttl_seconds,
            "enabled": self.enabled,
        }
        if age_seconds is not None:
            meta["age_seconds"] = round(age_seconds, 3)
            meta["expires_in_seconds"] = round(
                max(0.0, self.ttl_seconds - age_seconds), 3
            )
        return meta

    def stats(self) -> dict[str, Any]:
        with self._lock:
            entries = len(self._entries)
        total = self.hits + self.misses
        return {
            "enabled": self.enabled,
            "ttl_seconds": self.ttl_seconds,
            "entries": entries,
            "max_entries": self.max_entries,
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "hit_rate": round(self.hits / total, 3) if total else None,
        }


def fetch_with_cache(
    cache: WeatherCache,
    location: str,
    fetch: Callable[[str], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Serve `location` from cache, otherwise fetch and store it.

    Returns (payload, cache_metadata). Provider failures propagate: a failed
    fetch is never cached and never replaced with stale-but-expired data.
    """
    cached, age = cache.get(location)
    if cached is not None:
        return cached, cache.metadata(hit=True, age_seconds=age)

    payload = fetch(location)
    cache.set(location, payload)
    return payload, cache.metadata(hit=False, age_seconds=0.0)


__all__ = ["WeatherCache", "cache_key", "fetch_with_cache"]
