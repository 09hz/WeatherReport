"""Weather provider interface and HTTP adapters.

Two kinds of backend live behind the `WeatherProvider` protocol:

- The generic `HttpWeatherProvider`, which needs credentials. Until
  WEATHER_API_KEY and WEATHER_API_BASE_URL are both set to real values,
  `build_provider` returns a provider that refuses to run, so the
  application never emits fabricated observations and never makes an
  outbound request on this route.
- The keyless NWS provider, selected with WEATHER_PROVIDER=nws. It needs no
  credentials and therefore DOES make outbound requests to api.weather.gov
  as soon as it is selected. Choosing it is the opt-in; there is no key to
  withhold. Outbound traffic is confined to api.weather.gov by the
  `ensure_nws_url` guard.

In neither case is an observation ever fabricated.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping, Protocol, runtime_checkable

from ..errors import ProviderError, ProviderNotConfiguredError

logger = logging.getLogger(__name__)


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


# Canonical condition vocabulary. Order matters: the first pattern that
# matches wins, so the more specific and more hazardous conditions are
# checked before the broad ones ("thunderstorm" before "rain",
# "freezing rain" before "rain", "partly cloudy" before "clear").
CONDITION_UNKNOWN = "unknown"

_CONDITION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("thunderstorm", re.compile(r"thunder|t-?storm|lightning|squall")),
    ("sleet", re.compile(r"sleet|freezing\s+rain|ice\s+pellet|freezing\s+drizzle")),
    ("hail", re.compile(r"\bhail\b|graupel")),
    ("snow", re.compile(r"snow|blizzard|flurr(y|ies)|wintry\s+mix")),
    ("rain", re.compile(r"rain|drizzle|shower|precip|\bwet\b")),
    ("fog", re.compile(r"\bfog(gy)?\b|\bmist\b|\bhaze\b|\bhazy\b|\bsmoke\b|\bdust\b")),
    ("windy", re.compile(r"\bwind(y|s)?\b|\bbreez(y|e)\b|\bgust")),
    ("cloudy", re.compile(r"cloud|overcast|\bobscured\b")),
    ("clear", re.compile(r"\bclear\b|\bsunny\b|\bsun\b|\bfair\b|\bfine\b")),
)

CANONICAL_CONDITIONS: tuple[str, ...] = tuple(
    code for code, _ in _CONDITION_PATTERNS
) + (CONDITION_UNKNOWN,)

# "partly cloudy" and friends are cloud cover, not clear sky; this guard
# keeps a "partly sunny" style string out of the "clear" bucket.
_PARTIAL_CLOUD = re.compile(r"part(ly|ial(ly)?)|scattered|few|broken|mostly\s+cloud")


def classify_conditions(text: Any) -> str:
    """Map free-text provider conditions onto one canonical code.

    Tokenizes loosely (whitespace, punctuation and case are ignored) rather
    than matching provider fields exactly, so wording differences between
    providers collapse onto the same code. Anything unrecognized becomes
    "unknown" — never a guessed condition.
    """
    if text is None or isinstance(text, bool):
        return CONDITION_UNKNOWN
    if isinstance(text, (list, tuple, set)):
        text = " ".join(str(item) for item in text)
    haystack = re.sub(r"[^a-z0-9\s-]+", " ", str(text).lower())
    haystack = " ".join(haystack.split())
    if not haystack:
        return CONDITION_UNKNOWN

    for code, pattern in _CONDITION_PATTERNS:
        if not pattern.search(haystack):
            continue
        if code == "clear" and _PARTIAL_CLOUD.search(haystack):
            return "cloudy"
        return code
    return CONDITION_UNKNOWN


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
        "condition_code": classify_conditions(conditions),
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


_NWS_UA_PLACEHOLDER = "your_app_name (your_contact_here)"


def build_nws_provider(config: Mapping[str, Any]) -> WeatherProvider:
    """Build the keyless NWS provider from Flask config.

    A hostile or unusable NWS_BASE_URL must not stop the app from booting.
    app/config.py's contract is that bad configuration degrades to a safe
    default, so the configured value is trust-checked here and discarded
    with a warning when it fails. The hard ensure_nws_url check inside the
    provider stays: URLs that arrive from outside (the ones NWS hands back
    in its own payloads) are still rejected outright.
    """
    from .nws_provider import DEFAULT_BASE_URL, NwsWeatherProvider, ensure_nws_url

    user_agent = str(config.get("NWS_USER_AGENT") or "")
    if user_agent.strip().lower() == _NWS_UA_PLACEHOLDER:
        user_agent = ""  # unedited .env.example value: use the generic default

    configured = str(config.get("NWS_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    try:
        base_url = ensure_nws_url(configured)
    except ProviderError as exc:
        logger.warning(
            "Ignoring NWS_BASE_URL=%r (%s); falling back to %s.",
            configured,
            (exc.details or {}).get("reason", "rejected"),
            DEFAULT_BASE_URL,
        )
        base_url = DEFAULT_BASE_URL

    try:
        timeout = int(config.get("NWS_TIMEOUT_SECONDS", 10))
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring non-numeric NWS_TIMEOUT_SECONDS=%r; falling back to 10.",
            config.get("NWS_TIMEOUT_SECONDS"),
        )
        timeout = 10

    # 0 / unset / unusable means "let the provider derive the chain budget
    # from the per-request timeout", so a bad value degrades to the default
    # rather than raising.
    try:
        total_timeout: int | None = int(config.get("NWS_TOTAL_TIMEOUT_SECONDS", 0)) or None
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring non-numeric NWS_TOTAL_TIMEOUT_SECONDS=%r; deriving it "
            "from NWS_TIMEOUT_SECONDS instead.",
            config.get("NWS_TOTAL_TIMEOUT_SECONDS"),
        )
        total_timeout = None

    return NwsWeatherProvider(
        user_agent=user_agent,
        base_url=base_url,
        timeout=timeout,
        total_timeout=total_timeout,
    )


def build_provider(config: Mapping[str, Any]) -> WeatherProvider:
    """Select a provider from Flask config; unconfigured by default.

    WEATHER_PROVIDER=nws selects the keyless NWS provider (no API key needed).
    This branch was dropped by merge 2667f24, which left build_nws_provider
    defined but unreachable and silently disabled the whole NWS feature
    (WeatherReport-83j).
    """
    if str(config.get("WEATHER_PROVIDER") or "").strip().lower() == "nws":
        return build_nws_provider(config)
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
