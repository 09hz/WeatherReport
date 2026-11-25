"""Weather provider interface and a minimal HTTP adapter.

No provider is bundled or assumed. Until WEATHER_API_KEY and
WEATHER_API_BASE_URL are both set to real values, `build_provider` returns a
provider that refuses to run, so the application never emits fabricated
observations and never makes outbound requests.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Protocol, runtime_checkable

from ..errors import ProviderError, ProviderNotConfiguredError


@runtime_checkable
class WeatherProvider(Protocol):
    """Contract every weather backend must satisfy."""

    name: str

    @property
    def available(self) -> bool:
        """True when the provider can actually serve requests."""
        ...

    def fetch_current(self, location: str) -> dict[str, Any]:
        """Return a normalized weather dictionary for `location`."""
        ...


def normalize(
    raw: Mapping[str, Any], *, location: str, provider: str
) -> dict[str, Any]:
    """Map a provider payload onto the internal weather dictionary shape.

    Values absent from the payload stay None rather than being guessed.
    Supports the common OpenWeatherMap-style layout and a flat fallback.
    """
    raw_main = raw.get("main")
    main: Mapping[str, Any] = raw_main if isinstance(raw_main, Mapping) else {}
    raw_wind = raw.get("wind")
    wind: Mapping[str, Any] = raw_wind if isinstance(raw_wind, Mapping) else {}
    weather_list = raw.get("weather")
    conditions: Any = None
    if isinstance(weather_list, list) and weather_list:
        first = weather_list[0]
        if isinstance(first, Mapping):
            conditions = first.get("description") or first.get("main")
    if conditions is None:
        conditions = raw.get("conditions")

    wind_speed = wind.get("speed", raw.get("wind_speed_kph"))

    return {
        "location": raw.get("name") or location,
        "observed_at": raw.get("observed_at") or raw.get("dt"),
        "temperature_c": _as_float(main.get("temp", raw.get("temperature_c"))),
        "humidity": _as_float(main.get("humidity", raw.get("humidity"))),
        "wind_speed_kph": _as_float(wind_speed),
        "conditions": conditions,
        "provider": provider,
        "raw_response": raw,
    }


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class UnconfiguredWeatherProvider:
    """Default provider: always refuses, never performs network I/O."""

    name = "unconfigured"

    @property
    def available(self) -> bool:
        return False

    def fetch_current(self, location: str) -> dict[str, Any]:
        raise ProviderNotConfiguredError()


class HttpWeatherProvider:
    """Generic REST adapter for a configured weather service."""

    name = "http"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: int = 10,
        query_param: str = "q",
        key_param: str = "appid",
        session: Any | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._query_param = query_param
        self._key_param = key_param
        self._session = session

    @property
    def available(self) -> bool:
        return bool(self._base_url and self._api_key)

    def fetch_current(self, location: str) -> dict[str, Any]:
        if not self.available:
            raise ProviderNotConfiguredError()
        try:
            import requests  # imported lazily so it stays an optional runtime dep
        except ImportError as exc:  # pragma: no cover - requests is in requirements
            raise ProviderError(
                "The 'requests' package is required for external weather lookups."
            ) from exc

        client = self._session or requests
        params = {self._query_param: location, self._key_param: self._api_key, "units": "metric"}
        try:
            response = client.get(self._base_url, params=params, timeout=self._timeout)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # network, HTTP and decode failures alike
            raise ProviderError(
                "Weather provider request failed.",
                details={"reason": type(exc).__name__},
            ) from exc

        if not isinstance(payload, Mapping):
            raise ProviderError("Weather provider returned an unexpected payload.")
        return normalize(payload, location=location, provider=self.name)


def build_provider(config: Mapping[str, Any]) -> WeatherProvider:
    """Select a provider from Flask config; unconfigured by default."""
    if not config.get("WEATHER_PROVIDER_CONFIGURED"):
        return UnconfiguredWeatherProvider()
    return HttpWeatherProvider(
        base_url=config["WEATHER_API_BASE_URL"],
        api_key=config["WEATHER_API_KEY"],
        timeout=int(config.get("WEATHER_TIMEOUT_SECONDS", 10)),
    )


def serialize_raw(raw: Any) -> str | None:
    """JSON-encode a provider payload for storage."""
    if raw is None:
        return None
    try:
        return json.dumps(raw, default=str)
    except (TypeError, ValueError):
        return None
