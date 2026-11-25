"""National Weather Service (api.weather.gov) observation provider.

NWS is keyless but requires a descriptive User-Agent. Lookups are
coordinate-based and US-only:

    GET /points/{lat},{lon}                       -> point metadata (cached)
    GET {properties.observationStations}          -> nearest stations first
    GET /stations/{id}/observations/latest        -> latest observation

URLs taken from an NWS response are validated (https + exact host) before
they are fetched. Missing or null measurements stay None; nothing is
defaulted, averaged or interpolated.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

from ..errors import ProviderError, ValidationError
from ..validation import parse_coordinates

ALLOWED_SCHEME = "https"
ALLOWED_HOST = "api.weather.gov"
DEFAULT_BASE_URL = "https://api.weather.gov"
DEFAULT_USER_AGENT = "WeatherReport/0.1 (set NWS_USER_AGENT with a contact)"
ACCEPT = "application/geo+json"

MAX_STATION_ATTEMPTS = 3
POINT_CACHE_SIZE = 256

_STATION_ID_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")

KPH_PER_MPH = 1.609344


def ensure_nws_url(url: Any) -> str:
    """SSRF guard: only https://api.weather.gov/... URLs may be fetched."""
    if not isinstance(url, str) or not url:
        raise ProviderError(
            "NWS returned a missing or invalid URL.",
            details={"reason": "invalid_url"},
        )
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ProviderError(
            "NWS returned a malformed URL.", details={"reason": "invalid_url"}
        ) from exc
    if (
        parts.scheme != ALLOWED_SCHEME
        or parts.hostname != ALLOWED_HOST
        or (parts.netloc or "").lower() != ALLOWED_HOST
        or port is not None
    ):
        raise ProviderError(
            "Refusing to follow a URL outside api.weather.gov.",
            details={"reason": "untrusted_url"},
        )
    return url


def c_to_f(value: float | None) -> float | None:
    """Convert Celsius to Fahrenheit, returning None when the input is None."""
    return None if value is None else round(value * 9 / 5 + 32, 2)


def kph_to_mph(value: float | None) -> float | None:
    """Convert km/h to mph, returning None when the input is None."""
    return None if value is None else round(value / KPH_PER_MPH, 2)


def _number(value: Any) -> float | None:
    """Normalize a numeric value; returns None for booleans or None input."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _measure(props: Mapping[str, Any], key: str, expected_units: tuple[str, ...]) -> float | None:
    """Read a {unitCode, value} measurement; None if null or unit unexpected."""
    item = props.get(key)
    if not isinstance(item, Mapping):
        return None
    value = _number(item.get("value"))
    if value is None:
        return None
    if item.get("unitCode") not in expected_units:
        # Unknown unit: report nothing rather than mis-convert.
        return None
    return value


def _temperature_c(props: Mapping[str, Any], key: str) -> float | None:
    item = props.get(key)
    if not isinstance(item, Mapping):
        return None
    value = _number(item.get("value"))
    if value is None:
        return None
    unit = item.get("unitCode")
    if unit in ("wmoUnit:degC",):
        return value
    if unit in ("wmoUnit:degF",):
        return round((value - 32) * 5 / 9, 2)
    return None


def _speed_kph(props: Mapping[str, Any], key: str) -> float | None:
    item = props.get(key)
    if not isinstance(item, Mapping):
        return None
    value = _number(item.get("value"))
    if value is None:
        return None
    unit = item.get("unitCode")
    if unit == "wmoUnit:km_h-1":
        return value
    if unit == "wmoUnit:m_s-1":
        return round(value * 3.6, 2)
    return None


def normalize_observation(
    raw: Mapping[str, Any], *, latitude: float, longitude: float, station_id: str
) -> dict[str, Any]:
    """Map an NWS observation feature onto the internal weather shape."""
    props_raw = raw.get("properties")
    props: Mapping[str, Any] = props_raw if isinstance(props_raw, Mapping) else {}

    temperature_c = _temperature_c(props, "temperature")
    dewpoint_c = _temperature_c(props, "dewpoint")
    wind_kph = _speed_kph(props, "windSpeed")
    gust_kph = _speed_kph(props, "windGust")
    humidity = _measure(props, "relativeHumidity", ("wmoUnit:percent",))
    description = props.get("textDescription")
    if not isinstance(description, str) or not description.strip():
        description = None
    observed_at = props.get("timestamp") if isinstance(props.get("timestamp"), str) else None

    return {
        "location": f"{latitude},{longitude}",
        "latitude": latitude,
        "longitude": longitude,
        "station_id": station_id,
        "observed_at": observed_at,
        "temperature_c": temperature_c,
        "temperature_f": c_to_f(temperature_c),
        "dewpoint_c": dewpoint_c,
        "humidity": humidity,
        "wind_speed_kph": wind_kph,
        "wind_speed_mph": kph_to_mph(wind_kph),
        "wind_gust_kph": gust_kph,
        "wind_gust_mph": kph_to_mph(gust_kph),
        "wind_direction_deg": _measure(props, "windDirection", ("wmoUnit:degree_(angle)",)),
        "barometric_pressure_pa": _measure(props, "barometricPressure", ("wmoUnit:Pa",)),
        "visibility_m": _measure(props, "visibility", ("wmoUnit:m",)),
        "description": description,
        "conditions": description,
        "source": "api.weather.gov",
        "provider": "nws",
        "data_type": "observation",
        "raw_response": raw,
    }


def _is_usable(obs: Mapping[str, Any]) -> bool:
    """True if the observation has a timestamp and at least one measurement."""
    return obs.get("observed_at") is not None and any(
        obs.get(k) is not None
        for k in ("temperature_c", "humidity", "wind_speed_kph", "description")
    )


class NwsWeatherProvider:
    """WeatherProvider backed by api.weather.gov (keyless)."""

    name = "nws"

    def __init__(
        self,
        *,
        user_agent: str = "",
        base_url: str = DEFAULT_BASE_URL,
        timeout: int | float = 10,
        session: Any | None = None,
        max_station_attempts: int = MAX_STATION_ATTEMPTS,
        cache_size: int = POINT_CACHE_SIZE,
    ) -> None:
        self._base_url = ensure_nws_url((base_url or DEFAULT_BASE_URL).rstrip("/"))
        self._user_agent = (user_agent or "").strip() or DEFAULT_USER_AGENT
        self._timeout = timeout
        self._session = session
        self._max_attempts = max(1, int(max_station_attempts))
        self._cache_size = max(1, int(cache_size))
        self._points: OrderedDict[tuple[float, float], dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        # NWS needs no API key.
        return True

    # -- WeatherProvider protocol ------------------------------------------

    def fetch_current(self, location: str) -> dict[str, Any]:
        """Accept "lat,lon" so the generic /current and snapshot routes work."""
        if not isinstance(location, str) or location.count(",") != 1:
            raise ValidationError(
                "The NWS provider expects a location of the form 'lat,lon'.",
                details={"field": "location"},
            )
        lat, lon = location.split(",")
        return self.fetch_observation(lat, lon)

    # -- NWS specific -------------------------------------------------------

    def fetch_observation(self, lat: Any, lon: Any) -> dict[str, Any]:
        latitude, longitude = parse_coordinates(lat, lon)
        point = self.get_point(latitude, longitude)
        stations = self._stations(point["observation_stations"])
        if not stations:
            raise ProviderError(
                "No NWS observation stations are available for this point.",
                details={"reason": "no_stations"},
                status_code=404,
                code="nws_no_stations",
            )
        tried: list[str] = []
        for station_id in stations[: self._max_attempts]:
            tried.append(station_id)
            url = f"{self._base_url}/stations/{quote(station_id, safe='')}/observations/latest"
            try:
                raw = self._get_json(url)
            except ProviderError as exc:
                # A station that is missing/down falls through to the next one;
                # timeouts and trust failures abort immediately.
                if exc.details.get("reason") in ("http_error",):
                    continue
                raise
            obs = normalize_observation(
                raw, latitude=latitude, longitude=longitude, station_id=station_id
            )
            if _is_usable(obs):
                return obs
        raise ProviderError(
            "No usable NWS observation was found at nearby stations.",
            details={"reason": "no_usable_observation", "stations_tried": tried},
            status_code=502,
            code="nws_no_observation",
        )

    def get_point(self, latitude: float, longitude: float) -> dict[str, Any]:
        key = (latitude, longitude)
        with self._lock:
            cached = self._points.get(key)
            if cached is not None:
                self._points.move_to_end(key)
                return cached
        raw = self._get_json(f"{self._base_url}/points/{latitude},{longitude}")
        props = raw.get("properties")
        if not isinstance(props, Mapping):
            raise ProviderError(
                "NWS point metadata was malformed.", details={"reason": "malformed"}
            )
        point = {
            "observation_stations": ensure_nws_url(props.get("observationStations")),
            "forecast": props.get("forecast"),
            "grid_id": props.get("gridId"),
            "grid_x": props.get("gridX"),
            "grid_y": props.get("gridY"),
            "time_zone": props.get("timeZone"),
        }
        with self._lock:
            self._points[key] = point
            self._points.move_to_end(key)
            while len(self._points) > self._cache_size:
                self._points.popitem(last=False)
        return point

    def _stations(self, url: str) -> list[str]:
        raw = self._get_json(ensure_nws_url(url))
        features = raw.get("features")
        if not isinstance(features, list):
            raise ProviderError(
                "NWS station list was malformed.", details={"reason": "malformed"}
            )
        ids: list[str] = []
        for feature in features:
            props = feature.get("properties") if isinstance(feature, Mapping) else None
            sid = props.get("stationIdentifier") if isinstance(props, Mapping) else None
            if isinstance(sid, str) and _STATION_ID_RE.match(sid):
                ids.append(sid)
        return ids

    def _get_json(self, url: str) -> Mapping[str, Any]:
        ensure_nws_url(url)
        try:
            import requests
        except ImportError as exc:  # pragma: no cover - requests is in requirements
            raise ProviderError("The 'requests' package is required.") from exc

        client = self._session or requests
        headers = {"User-Agent": self._user_agent, "Accept": ACCEPT}
        try:
            response = client.get(
                url, headers=headers, timeout=self._timeout, allow_redirects=False
            )
        except (requests.Timeout, TimeoutError) as exc:
            raise ProviderError(
                "NWS request timed out.",
                details={"reason": "timeout"},
                status_code=504,
                code="weather_provider_timeout",
            ) from exc
        except Exception as exc:
            raise ProviderError(
                "NWS request failed.", details={"reason": type(exc).__name__}
            ) from exc

        status = int(getattr(response, "status_code", 200) or 200)
        payload: Any
        try:
            payload = response.json()
        except Exception:
            payload = None

        if status >= 300:
            problem = payload if isinstance(payload, Mapping) else {}
            details: dict[str, Any] = {"reason": "http_error", "upstream_status": status}
            for k in ("type", "title", "detail"):
                if isinstance(problem.get(k), str):
                    details[k] = problem[k]
            if status == 404 and str(problem.get("type", "")).endswith("/InvalidPoint"):
                raise ProviderError(
                    "NWS has no data for the requested point (US coverage only).",
                    details=details,
                    status_code=404,
                    code="nws_point_unavailable",
                )
            raise ProviderError("NWS returned an HTTP error.", details=details)

        if not isinstance(payload, Mapping):
            raise ProviderError(
                "NWS returned a non-JSON or unexpected body.",
                details={"reason": "malformed"},
            )
        return payload
