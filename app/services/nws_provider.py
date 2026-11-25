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
from typing import Any, Mapping, NamedTuple
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

# A station request that fails for one of these reasons ends the whole
# request: a timeout means the chain is already over its latency budget, and
# a URL that failed the trust check must never be retried against another
# candidate. Every other failure (HTTP error, non-JSON body, connection
# reset) is specific to that one station, so the next candidate is tried.
STATION_ABORT_REASONS = frozenset({"timeout", "untrusted_url", "invalid_url"})

# Request kinds, used to map an upstream HTTP status onto the right
# client-facing error. Keyed on what we were fetching rather than on the
# problem-type string, which NWS does not guarantee.
KIND_POINT = "point"
KIND_STATIONS = "stations"
KIND_OBSERVATION = "observation"

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


def _optional_nws_url(url: Any) -> str | None:
    """Trust-check a non-essential URL, dropping it to None when it fails.

    Same guard as `ensure_nws_url`, different failure mode: for fields the
    request does not depend on, a bad value should be discarded rather than
    abort the call. Returning None keeps untrusted data out of the cache
    without making an unused extra field load-bearing.
    """
    if url is None:
        return None
    try:
        return ensure_nws_url(url)
    except ProviderError:
        return None


def c_to_f(value: float | None) -> float | None:
    """Convert Celsius to Fahrenheit, returning None when the input is None.

    Rounds at output. Pass the *unrounded* Celsius value: rounding the input
    first and converting second introduces a second rounding error, which is
    how a 33 degF reading used to come back out as 33.01 degF.
    """
    return None if value is None else round(value * 9 / 5 + 32, 2)


def kph_to_mph(value: float | None) -> float | None:
    """Convert km/h to mph, returning None when the input is None.

    Rounds at output; pass the unrounded km/h value (see `c_to_f`).
    """
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


class _Reading(NamedTuple):
    """A measurement in canonical units, plus how it got there.

    `value` is always exact and unrounded. `converted` says whether a unit
    conversion was applied, which decides whether the value may be rounded
    for output: an upstream reading already in canonical units is reported
    verbatim, because rounding it would make our output disagree with the
    source for no reason.
    """

    value: float | None
    converted: bool


_NO_READING = _Reading(None, False)


def _temperature_c(props: Mapping[str, Any], key: str) -> _Reading:
    """Read a temperature as EXACT Celsius; the caller rounds at output.

    Deliberately unrounded: `normalize_observation` reports both Celsius and
    Fahrenheit, and rounding here would make the second conversion round a
    rounded number -- which is how 33 degF used to come back as 33.01 degF.
    """
    item = props.get(key)
    if not isinstance(item, Mapping):
        return _NO_READING
    value = _number(item.get("value"))
    if value is None:
        return _NO_READING
    unit = item.get("unitCode")
    if unit in ("wmoUnit:degC",):
        return _Reading(value, False)
    if unit in ("wmoUnit:degF",):
        return _Reading((value - 32) * 5 / 9, True)
    return _NO_READING


def _speed_kph(props: Mapping[str, Any], key: str) -> _Reading:
    """Read a wind speed as EXACT km/h; the caller rounds at output."""
    item = props.get(key)
    if not isinstance(item, Mapping):
        return _NO_READING
    value = _number(item.get("value"))
    if value is None:
        return _NO_READING
    unit = item.get("unitCode")
    if unit == "wmoUnit:km_h-1":
        return _Reading(value, False)
    if unit == "wmoUnit:m_s-1":
        return _Reading(value * 3.6, True)
    return _NO_READING


def _present(reading: _Reading) -> float | None:
    """The canonical-unit value as reported: rounded only if we converted."""
    if reading.value is None:
        return None
    return round(reading.value, 2) if reading.converted else reading.value


def normalize_observation(
    raw: Mapping[str, Any], *, latitude: float, longitude: float, station_id: str
) -> dict[str, Any]:
    """Map an NWS observation feature onto the internal weather shape."""
    props_raw = raw.get("properties")
    props: Mapping[str, Any] = props_raw if isinstance(props_raw, Mapping) else {}

    temperature = _temperature_c(props, "temperature")
    dewpoint = _temperature_c(props, "dewpoint")
    wind = _speed_kph(props, "windSpeed")
    gust = _speed_kph(props, "windGust")
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
        # Each conversion happens exactly once, from the exact upstream
        # value. The _f/_mph figures are derived from the full-precision
        # reading rather than from the rounded _c/_kph field beside them, so
        # a degF reading round-trips to the number NWS actually sent.
        "temperature_c": _present(temperature),
        "temperature_f": c_to_f(temperature.value),
        "dewpoint_c": _present(dewpoint),
        "humidity": humidity,
        "wind_speed_kph": _present(wind),
        "wind_speed_mph": kph_to_mph(wind.value),
        "wind_gust_kph": _present(gust),
        "wind_gust_mph": kph_to_mph(gust.value),
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
    """True if the observation carries at least one actual measurement.

    A missing `timestamp` is NOT disqualifying. Requiring it threw away
    readings that had a temperature and a text description but no timestamp,
    turning a usable answer into nws_no_observation/502. The timestamp is
    reported as null instead, and the caller can decide what to do about an
    undated reading. A payload with no measurements at all is still rejected,
    which is what actually makes an observation worthless.
    """
    return any(
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
        last_failure: ProviderError | None = None
        for station_id in stations[: self._max_attempts]:
            tried.append(station_id)
            url = f"{self._base_url}/stations/{quote(station_id, safe='')}/observations/latest"
            try:
                raw = self._get_json(url, kind=KIND_OBSERVATION)
            except ProviderError as exc:
                # A station that fails in a recoverable way falls through to
                # the next candidate; timeouts and URL-trust failures abort
                # the whole request immediately.
                if exc.details.get("reason") in STATION_ABORT_REASONS:
                    raise
                last_failure = exc
                continue
            obs = normalize_observation(
                raw, latitude=latitude, longitude=longitude, station_id=station_id
            )
            if _is_usable(obs):
                return obs
        details: dict[str, Any] = {
            "reason": "no_usable_observation",
            "stations_tried": tried,
        }
        if last_failure is not None:
            # Surface why the candidates were skipped; without this the caller
            # cannot tell "all stations reported nulls" from "all were down".
            details["last_failure"] = last_failure.details.get("reason")
            upstream = last_failure.details.get("upstream_status")
            if upstream is not None:
                details["last_upstream_status"] = upstream
        raise ProviderError(
            "No usable NWS observation was found at nearby stations.",
            details=details,
            status_code=502,
            code="nws_no_observation",
            log_traceback=False,
        )

    def get_point(self, latitude: float, longitude: float) -> dict[str, Any]:
        key = (latitude, longitude)
        with self._lock:
            cached = self._points.get(key)
            if cached is not None:
                self._points.move_to_end(key)
                # Copy on read: handing out the cached dict let any caller
                # mutate state shared by every other caller.
                return dict(cached)
        raw = self._get_json(
            f"{self._base_url}/points/{latitude},{longitude}", kind=KIND_POINT
        )
        props = raw.get("properties")
        if not isinstance(props, Mapping):
            raise ProviderError(
                "NWS point metadata was malformed.", details={"reason": "malformed"}
            )
        point = {
            "observation_stations": ensure_nws_url(props.get("observationStations")),
            # Validated at the extraction site, not at first use. Nothing
            # fetches `forecast` today, so an untrusted value would sit in the
            # cache until some future consumer inherited the SSRF. A rejected
            # URL is dropped to None rather than raised, because this field is
            # not needed for the observation lookup and a malformed extra
            # should not fail the whole point request.
            "forecast": _optional_nws_url(props.get("forecast")),
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
        return dict(point)

    def _stations(self, url: str) -> list[str]:
        raw = self._get_json(ensure_nws_url(url), kind=KIND_STATIONS)
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

    def _get_json(
        self, url: str, *, kind: str = KIND_OBSERVATION
    ) -> Mapping[str, Any]:
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

        # A response whose status is missing, non-numeric or outside the
        # valid HTTP range is treated as a failure, not as success. The
        # previous `or 200` fallback meant a missing or falsy status (None, 0)
        # read as 200 and the body was then parsed as a valid payload.
        raw_status = getattr(response, "status_code", None)
        try:
            status = int(raw_status)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            status = -1
        if not 100 <= status <= 599:
            raise ProviderError(
                "NWS response had no usable HTTP status.",
                details={"reason": "malformed"},
            )
        payload: Any
        try:
            payload = response.json()
        except Exception:
            payload = None

        if status >= 300:
            problem = payload if isinstance(payload, Mapping) else {}
            details: dict[str, Any] = {
                "reason": "http_error",
                "upstream_status": status,
                "request_kind": kind,
            }
            for k in ("type", "title", "detail"):
                if isinstance(problem.get(k), str):
                    details[k] = problem[k]

            # Keyed on what we were fetching, not on the problem-type string:
            # NWS does not guarantee a JSON problem body, so a 404 with an
            # HTML page must still read as "no coverage here".
            if kind == KIND_POINT and status == 404:
                raise ProviderError(
                    "NWS has no data for the requested point (US coverage only).",
                    details=details,
                    status_code=404,
                    code="nws_point_unavailable",
                    log_traceback=False,
                )
            if kind == KIND_POINT and status == 400:
                raise ProviderError(
                    "NWS rejected the requested coordinates.",
                    details=details,
                    status_code=400,
                    code="nws_invalid_point",
                    log_traceback=False,
                )
            # Any other upstream HTTP status is expected operational noise on
            # a third-party API: still a 502, but it must not write a stack
            # trace, because this endpoint is unauthenticated and the volume
            # would be caller-drivable.
            raise ProviderError(
                "NWS returned an HTTP error.", details=details, log_traceback=False
            )

        if not isinstance(payload, Mapping):
            raise ProviderError(
                "NWS returned a non-JSON or unexpected body.",
                details={"reason": "malformed"},
            )
        return payload
