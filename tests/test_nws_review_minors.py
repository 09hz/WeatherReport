"""Review MINOR 5, 7, 9 and the INFORMATIONAL cleanup items.

Covers four issues that all live in app/services/nws_provider.py:

- WeatherReport-x3l (MINOR 9): conversions must round once, at output, so a
  non-canonical input unit does not drift by 0.01 on the round trip.
- WeatherReport-em8 (MINOR 5): an observation with measurements but no
  timestamp is usable, with observed_at null.
- WeatherReport-d53 (MINOR 7): the non-essential `forecast` URL is trust
  checked at extraction and never reaches the cache untrusted.
- WeatherReport-95x: cached point is copied on read; a missing HTTP status
  no longer reads as success.

Everything is mocked through an injected fake session; no test here touches
the network.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.errors import ProviderError
from app.services.nws_provider import NwsWeatherProvider, normalize_observation

BASE = "https://api.weather.gov"
LAT, LON = 39.7456, -97.0892
POINT_URL = f"{BASE}/points/{LAT},{LON}"
STATIONS_URL = f"{BASE}/gridpoints/TOP/32,81/stations"
OBS_URL = f"{BASE}/stations/KTOP/observations/latest"


class _FakeResponse:
    def __init__(self, payload: Any, *, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> Any:
        return self._payload


class _NoStatusResponse:
    """A response object that never got a status_code (e.g. a bad stub)."""

    def json(self) -> Any:
        return {"properties": {"observationStations": STATIONS_URL}}


class _NullStatusResponse:
    status_code = None

    def json(self) -> Any:
        return {"properties": {"observationStations": STATIONS_URL}}


class _FakeSession:
    def __init__(self, routes: dict[str, Any]) -> None:
        self._routes = routes
        self.calls: list[str] = []

    def get(self, url: str, params=None, timeout=None, headers=None, **kwargs):
        self.calls.append(url)
        if url not in self._routes:
            raise AssertionError(f"unexpected request to {url}")
        response = self._routes[url]
        if isinstance(response, Exception):
            raise response
        return response


def _measure(unit: str, value: Any) -> dict[str, Any]:
    return {"unitCode": unit, "value": value, "qualityControl": "V"}


def _point_payload(*, forecast: Any = f"{BASE}/gridpoints/TOP/32,81/forecast") -> dict:
    return {
        "properties": {
            "observationStations": STATIONS_URL,
            "forecast": forecast,
            "gridId": "TOP",
            "gridX": 32,
            "gridY": 81,
            "timeZone": "America/Chicago",
        }
    }


def _stations_payload(*ids: str) -> dict[str, Any]:
    return {
        "features": [{"properties": {"stationIdentifier": i}} for i in ids],
    }


def _obs(**overrides: Any) -> dict[str, Any]:
    props: dict[str, Any] = {
        "timestamp": "2026-11-24T02:10:00+00:00",
        "textDescription": "Clear",
        "temperature": _measure("wmoUnit:degC", 10.0),
        "windSpeed": _measure("wmoUnit:km_h-1", 16.09344),
        "relativeHumidity": _measure("wmoUnit:percent", 57.5),
    }
    props.update(overrides)
    return {"properties": props}


def _provider(obs_payload: dict[str, Any] | None = None, *, point: dict | None = None):
    routes = {
        POINT_URL: _FakeResponse(point if point is not None else _point_payload()),
        STATIONS_URL: _FakeResponse(_stations_payload("KTOP")),
        OBS_URL: _FakeResponse(obs_payload if obs_payload is not None else _obs()),
    }
    session = _FakeSession(routes)
    return NwsWeatherProvider(user_agent="test (t@example.com)", session=session), session


def _norm(**overrides: Any) -> dict[str, Any]:
    return normalize_observation(
        _obs(**overrides), latitude=LAT, longitude=LON, station_id="KTOP"
    )


# --- WeatherReport-x3l: round once, at output --------------------------------


@pytest.mark.parametrize("degf", [33.0, 32.0, 0.0, -40.0, 72.0, 98.6, 212.0, -17.0])
def test_degf_temperature_round_trips_to_the_upstream_value(degf):
    """The reported Fahrenheit must equal what NWS sent, not drift by 0.01."""
    result = _norm(temperature=_measure("wmoUnit:degF", degf))
    assert result["temperature_f"] == pytest.approx(degf, abs=1e-9)


def test_degf_celsius_is_rounded_for_presentation():
    result = _norm(temperature=_measure("wmoUnit:degF", 33.0))
    assert result["temperature_c"] == 0.56  # rounded, because we converted


def test_canonical_celsius_is_reported_verbatim():
    """A value already in canonical units is not rounded at all."""
    result = _norm(temperature=_measure("wmoUnit:degC", 10.123456))
    assert result["temperature_c"] == 10.123456
    assert result["temperature_f"] == pytest.approx(50.22, abs=0.005)


def test_canonical_kph_is_reported_verbatim():
    result = _norm(windSpeed=_measure("wmoUnit:km_h-1", 16.09344))
    assert result["wind_speed_kph"] == pytest.approx(16.09344)
    assert result["wind_speed_mph"] == 10.0


@pytest.mark.parametrize("ms", [0.0, 1.0, 5.0, 3.6, 12.5, 27.7])
def test_m_s_wind_round_trips_consistently(ms):
    """mph must be derived from the exact km/h, not from the rounded field."""
    result = _norm(windSpeed=_measure("wmoUnit:m_s-1", ms))
    assert result["wind_speed_kph"] == pytest.approx(ms * 3.6, abs=0.005)
    assert result["wind_speed_mph"] == pytest.approx(ms * 3.6 / 1.609344, abs=0.005)


def test_m_s_wind_kph_is_rounded_for_presentation():
    result = _norm(windSpeed=_measure("wmoUnit:m_s-1", 4.47))
    assert result["wind_speed_kph"] == 16.09


def test_degf_dewpoint_also_round_trips():
    result = _norm(dewpoint=_measure("wmoUnit:degF", 33.0))
    from app.services.nws_provider import c_to_f

    assert c_to_f(result["dewpoint_c"]) == pytest.approx(33.0, abs=0.01)


def test_gust_in_m_s_is_converted_and_paired_consistently():
    result = _norm(windGust=_measure("wmoUnit:m_s-1", 10.0))
    assert result["wind_gust_kph"] == 36.0
    assert result["wind_gust_mph"] == pytest.approx(22.37, abs=0.005)


def test_unknown_unit_still_reports_nothing():
    result = _norm(temperature=_measure("wmoUnit:degK", 300))
    assert result["temperature_c"] is None
    assert result["temperature_f"] is None


# --- WeatherReport-em8: no timestamp is not a reason to discard --------------


def test_observation_without_timestamp_is_returned_with_null_observed_at():
    provider, _ = _provider(_obs(timestamp=None))
    result = provider.fetch_observation(LAT, LON)

    assert result["observed_at"] is None
    assert result["temperature_c"] == 10.0
    assert result["description"] == "Clear"


def test_observation_with_non_string_timestamp_is_still_returned():
    provider, _ = _provider(_obs(timestamp=1764000000))
    result = provider.fetch_observation(LAT, LON)
    assert result["observed_at"] is None
    assert result["temperature_c"] == 10.0


def test_observation_with_only_a_description_and_no_timestamp_is_usable():
    provider, _ = _provider(
        _obs(timestamp=None, temperature=_measure("wmoUnit:degC", None),
             windSpeed=_measure("wmoUnit:km_h-1", None),
             relativeHumidity=_measure("wmoUnit:percent", None))
    )
    result = provider.fetch_observation(LAT, LON)
    assert result["observed_at"] is None
    assert result["description"] == "Clear"


def test_payload_with_neither_timestamp_nor_measurements_is_still_rejected():
    """Dropping the timestamp requirement must not accept an empty reading."""
    provider, _ = _provider(
        _obs(timestamp=None, textDescription=None,
             temperature=_measure("wmoUnit:degC", None),
             windSpeed=_measure("wmoUnit:km_h-1", None),
             relativeHumidity=_measure("wmoUnit:percent", None))
    )
    with pytest.raises(ProviderError) as excinfo:
        provider.fetch_observation(LAT, LON)

    assert excinfo.value.code == "nws_no_observation"
    assert excinfo.value.details["reason"] == "no_usable_observation"


def test_empty_properties_is_still_rejected():
    provider, _ = _provider({"properties": {}})
    with pytest.raises(ProviderError) as excinfo:
        provider.fetch_observation(LAT, LON)
    assert excinfo.value.code == "nws_no_observation"


# --- WeatherReport-d53: forecast URL validated at extraction ----------------


HOSTILE_FORECAST = (
    "http://localhost:8080/forecast",
    "https://evil.test/forecast",
    "https://api.weather.gov.evil.test/forecast",
    "https://api.weather.gov:8443/forecast",
    "not a url",
    123,
    {"href": f"{BASE}/forecast"},
)


@pytest.mark.parametrize("bad", HOSTILE_FORECAST)
def test_untrusted_forecast_url_does_not_reach_the_cache(bad):
    provider, _ = _provider(point=_point_payload(forecast=bad))
    point = provider.get_point(LAT, LON)

    assert point["forecast"] is None
    assert provider._points[(LAT, LON)]["forecast"] is None


@pytest.mark.parametrize("bad", HOSTILE_FORECAST)
def test_point_lookup_still_succeeds_despite_a_bad_forecast_url(bad):
    """`forecast` is unused, so a bad value must not fail the whole request."""
    provider, _ = _provider(point=_point_payload(forecast=bad))
    result = provider.fetch_observation(LAT, LON)
    assert result["station_id"] == "KTOP"


def test_valid_forecast_url_is_preserved():
    provider, _ = _provider()
    point = provider.get_point(LAT, LON)
    assert point["forecast"] == f"{BASE}/gridpoints/TOP/32,81/forecast"


def test_missing_forecast_key_is_none_not_an_error():
    payload = {"properties": {"observationStations": STATIONS_URL}}
    provider, _ = _provider(point=payload)
    assert provider.get_point(LAT, LON)["forecast"] is None


def test_observation_stations_is_still_hard_rejected():
    """The essential URL keeps raising; only the optional one degrades."""
    payload = {"properties": {"observationStations": "https://evil.test/stations"}}
    provider, _ = _provider(point=payload)
    with pytest.raises(ProviderError) as excinfo:
        provider.get_point(LAT, LON)
    assert excinfo.value.details["reason"] == "untrusted_url"


# --- WeatherReport-95x: cache copy-on-read, status fail-closed --------------


def test_cached_point_is_copied_on_read():
    provider, session = _provider()

    first = provider.get_point(LAT, LON)
    first["observation_stations"] = "https://evil.test/stations"
    first["grid_id"] = "MUTATED"
    second = provider.get_point(LAT, LON)

    assert second["observation_stations"] == STATIONS_URL
    assert second["grid_id"] == "TOP"
    assert session.calls.count(POINT_URL) == 1, "second call should still be cached"


def test_first_point_result_is_also_a_copy():
    """The dict returned on a cache MISS must not alias the cached one either."""
    provider, _ = _provider()
    first = provider.get_point(LAT, LON)
    first["grid_id"] = "MUTATED"
    assert provider._points[(LAT, LON)]["grid_id"] == "TOP"


def test_missing_status_code_does_not_read_as_success():
    session = _FakeSession({POINT_URL: _NoStatusResponse()})
    provider = NwsWeatherProvider(user_agent="test (t@example.com)", session=session)

    with pytest.raises(ProviderError) as excinfo:
        provider.get_point(LAT, LON)
    assert excinfo.value.details["reason"] == "malformed"


def test_null_status_code_does_not_read_as_success():
    session = _FakeSession({POINT_URL: _NullStatusResponse()})
    provider = NwsWeatherProvider(user_agent="test (t@example.com)", session=session)

    with pytest.raises(ProviderError) as excinfo:
        provider.get_point(LAT, LON)
    assert excinfo.value.details["reason"] == "malformed"


def test_status_zero_does_not_read_as_success():
    """A falsy-but-present 0 used to be rewritten to 200 by `or 200`.

    The payload is deliberately VALID, so the old code parsed it happily and
    returned a point built from a response that never carried a real status.
    """
    session = _FakeSession({POINT_URL: _FakeResponse(_point_payload(), status=0)})
    provider = NwsWeatherProvider(user_agent="test (t@example.com)", session=session)

    with pytest.raises(ProviderError) as excinfo:
        provider.get_point(LAT, LON)
    assert excinfo.value.details["reason"] == "malformed"


def test_a_real_200_still_works():
    provider, _ = _provider()
    assert provider.get_point(LAT, LON)["grid_id"] == "TOP"
