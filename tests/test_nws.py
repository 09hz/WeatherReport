"""NWS (api.weather.gov) provider tests.

Everything except the single `live` test is mocked through an injected fake
session; the default run never touches the network. The live smoke test runs
only with NWS_LIVE_TESTS=1 and `-m live`.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
import requests

from app.errors import ProviderError, ValidationError
from app.services import weather_service
from app.services.nws_provider import NwsWeatherProvider, ensure_nws_url
from app.services.weather_provider import build_provider
from app.validation import parse_coordinates

BASE = "https://api.weather.gov"
LAT, LON = 39.7456, -97.0892
POINT_URL = f"{BASE}/points/{LAT},{LON}"
STATIONS_URL = f"{BASE}/gridpoints/TOP/32,81/stations"


# --- Fakes (same style as tests/test_weather.py) -----------------------------


class _FakeResponse:
    def __init__(self, payload: Any, *, status: int = 200, json_error: bool = False) -> None:
        self._payload = payload
        self.status_code = status
        self._json_error = json_error

    def json(self) -> Any:
        if self._json_error:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class _FakeSession:
    """Routes URL -> canned response (or exception) and records every call."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self._routes = routes
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params=None, timeout=None, headers=None, **kwargs):
        self.calls.append(
            {"url": url, "params": params, "timeout": timeout, "headers": headers, **kwargs}
        )
        if url not in self._routes:
            raise AssertionError(f"unexpected request to {url}")
        response = self._routes[url]
        if isinstance(response, Exception):
            raise response
        return response

    def urls(self) -> list[str]:
        return [c["url"] for c in self.calls]


def _measure(unit: str, value: Any) -> dict[str, Any]:
    return {"unitCode": unit, "value": value, "qualityControl": "V"}


def _point_payload(stations_url: str = STATIONS_URL) -> dict[str, Any]:
    return {
        "properties": {
            "observationStations": stations_url,
            "forecast": f"{BASE}/gridpoints/TOP/32,81/forecast",
            "forecastHourly": f"{BASE}/gridpoints/TOP/32,81/forecast/hourly",
            "gridId": "TOP",
            "gridX": 32,
            "gridY": 81,
            "timeZone": "America/Chicago",
        }
    }


def _stations_payload(*ids: str) -> dict[str, Any]:
    return {
        "observationStations": [f"{BASE}/stations/{i}" for i in ids],
        "features": [
            {"properties": {"stationIdentifier": i, "name": f"Station {i}"}} for i in ids
        ],
        "pagination": {},
    }


def _observation_payload(station: str = "KTOP", **overrides: Any) -> dict[str, Any]:
    props: dict[str, Any] = {
        "station": f"{BASE}/stations/{station}",
        "timestamp": "2026-11-24T02:10:00+00:00",
        "textDescription": "Clear",
        "temperature": _measure("wmoUnit:degC", 10.0),
        "dewpoint": _measure("wmoUnit:degC", 2.0),
        "windDirection": _measure("wmoUnit:degree_(angle)", 180),
        "windSpeed": _measure("wmoUnit:km_h-1", 16.09344),
        "windGust": _measure("wmoUnit:km_h-1", None),
        "barometricPressure": _measure("wmoUnit:Pa", 101800),
        "visibility": _measure("wmoUnit:m", 16090),
        "relativeHumidity": _measure("wmoUnit:percent", 57.5),
    }
    props.update(overrides)
    return {"properties": props}


def _obs_url(station: str) -> str:
    return f"{BASE}/stations/{station}/observations/latest"


def _provider(routes: dict[str, Any], **kwargs: Any) -> tuple[NwsWeatherProvider, _FakeSession]:
    session = _FakeSession(routes)
    provider = NwsWeatherProvider(
        user_agent="WeatherReport-tests (test@example.invalid)",
        timeout=7,
        session=session,
        **kwargs,
    )
    return provider, session


def _happy_routes(**obs_overrides: Any) -> dict[str, Any]:
    return {
        POINT_URL: _FakeResponse(_point_payload()),
        STATIONS_URL: _FakeResponse(_stations_payload("KTOP", "KMYZ")),
        _obs_url("KTOP"): _FakeResponse(_observation_payload(**obs_overrides)),
    }


# --- A. Coordinate validation -------------------------------------------------


def test_valid_coordinates_are_rounded_to_four_decimals():
    assert parse_coordinates("39.745612345", "-97.08924999") == (39.7456, -97.0892)
    assert parse_coordinates(90, -180) == (90.0, -180.0)


@pytest.mark.parametrize(
    "lat, lon",
    [
        ("abc", "-97"),
        ("39", "west"),
        (None, "-97"),
        ("39", ""),
        ("nan", "-97"),
        ("90.0001", "-97"),
        ("-91", "-97"),
        ("39", "180.5"),
        ("39", "-181"),
    ],
)
def test_invalid_coordinates_raise_before_any_request(lat, lon):
    provider, session = _provider({})
    with pytest.raises(ValidationError):
        provider.fetch_observation(lat, lon)
    assert session.calls == []


def test_observations_route_rejects_bad_coordinates_with_400(app, client):
    provider, session = _provider({})
    app.extensions["nws_provider"] = provider
    assert client.get("/api/weather/observations?lat=abc&lon=1").status_code == 400
    assert client.get("/api/weather/observations?lat=95&lon=1").status_code == 400
    assert client.get("/api/weather/observations?lon=1").status_code == 400
    assert session.calls == []


# --- B. Provider wiring ------------------------------------------------------


def test_nws_provider_is_available_without_any_api_key():
    provider = NwsWeatherProvider()
    assert provider.name == "nws"
    assert provider.available is True


def test_build_provider_selects_nws_without_api_key():
    provider = build_provider({"WEATHER_PROVIDER": "nws", "WEATHER_PROVIDER_CONFIGURED": False})
    assert isinstance(provider, NwsWeatherProvider)
    assert provider.available is True


def test_point_lookup_uses_rounded_coordinates_and_headers():
    provider, session = _provider(_happy_routes())
    provider.fetch_observation("39.745612", "-97.089249")
    first = session.calls[0]
    assert first["url"] == POINT_URL
    assert first["timeout"] == 7
    assert first["headers"]["User-Agent"] == "WeatherReport-tests (test@example.invalid)"
    assert first["headers"]["Accept"] == "application/geo+json"
    assert all(c["headers"]["User-Agent"] and c["timeout"] == 7 for c in session.calls)


def test_station_discovery_follows_point_metadata_to_nearest_station():
    provider, session = _provider(_happy_routes())
    result = provider.fetch_observation(LAT, LON)
    assert session.urls() == [POINT_URL, STATIONS_URL, _obs_url("KTOP")]
    assert result["station_id"] == "KTOP"


def test_falls_through_to_next_station_on_404_or_null_observation():
    empty = _observation_payload(
        "KAAA",
        textDescription="",
        temperature=_measure("wmoUnit:degC", None),
        windSpeed=_measure("wmoUnit:km_h-1", None),
        relativeHumidity=_measure("wmoUnit:percent", None),
    )
    routes = {
        POINT_URL: _FakeResponse(_point_payload()),
        STATIONS_URL: _FakeResponse(_stations_payload("KNOP", "KAAA", "KMYZ")),
        _obs_url("KNOP"): _FakeResponse({"title": "Not Found"}, status=404),
        _obs_url("KAAA"): _FakeResponse(empty),
        _obs_url("KMYZ"): _FakeResponse(_observation_payload("KMYZ")),
    }
    provider, _ = _provider(routes)
    assert provider.fetch_observation(LAT, LON)["station_id"] == "KMYZ"


def test_station_attempts_are_bounded():
    routes = {
        POINT_URL: _FakeResponse(_point_payload()),
        STATIONS_URL: _FakeResponse(_stations_payload("KS1", "KS2", "KS3", "KS4", "KS5")),
    }
    for sid in ("KS1", "KS2", "KS3", "KS4", "KS5"):
        routes[_obs_url(sid)] = _FakeResponse({}, status=500)
    provider, session = _provider(routes)
    with pytest.raises(ProviderError) as exc:
        provider.fetch_observation(LAT, LON)
    assert exc.value.code == "nws_no_observation"
    assert len([u for u in session.urls() if "/observations/latest" in u]) == 3


# --- C. Normalization ---------------------------------------------------------


def test_observation_is_normalized():
    provider, _ = _provider(_happy_routes())
    result = provider.fetch_observation(LAT, LON)
    assert result["latitude"] == LAT and result["longitude"] == LON
    assert result["station_id"] == "KTOP"
    assert result["observed_at"] == "2026-11-24T02:10:00+00:00"
    assert result["description"] == "Clear"
    assert result["humidity"] == 57.5
    assert result["source"] == "api.weather.gov"
    assert result["provider"] == "nws"
    assert result["data_type"] == "observation"
    assert "periods" not in result


def test_celsius_is_converted_to_fahrenheit():
    provider, _ = _provider(_happy_routes(temperature=_measure("wmoUnit:degC", 10.0)))
    result = provider.fetch_observation(LAT, LON)
    assert result["temperature_c"] == 10.0
    assert result["temperature_f"] == 50.0


def test_wind_kph_is_converted_to_mph():
    provider, _ = _provider(_happy_routes())
    result = provider.fetch_observation(LAT, LON)
    assert result["wind_speed_kph"] == pytest.approx(16.09344)
    assert result["wind_speed_mph"] == pytest.approx(10.0)


def test_null_and_missing_measurements_stay_none():
    overrides = {
        "temperature": _measure("wmoUnit:degC", None),
        "windGust": _measure("wmoUnit:km_h-1", None),
    }
    routes = _happy_routes(**overrides)
    payload = routes[_obs_url("KTOP")]._payload
    del payload["properties"]["visibility"]
    provider, _ = _provider(routes)
    result = provider.fetch_observation(LAT, LON)
    assert result["temperature_c"] is None
    assert result["temperature_f"] is None
    assert result["wind_gust_kph"] is None
    assert result["wind_gust_mph"] is None
    assert result["visibility_m"] is None
    # Other values are still reported unchanged.
    assert result["humidity"] == 57.5


def test_unexpected_unit_code_is_not_silently_converted():
    provider, _ = _provider(_happy_routes(windSpeed=_measure("wmoUnit:furlong", 5)))
    result = provider.fetch_observation(LAT, LON)
    assert result["wind_speed_kph"] is None
    assert result["wind_speed_mph"] is None


def test_normalized_observation_can_be_saved_as_snapshot(connection, app):
    provider, _ = _provider(_happy_routes())
    with app.app_context():
        snapshot = provider.fetch_current(f"{LAT},{LON}")
        stored = weather_service.save_snapshot(connection, snapshot)
    assert stored["provider"] == "nws"
    assert stored["temperature_c"] == 10.0
    assert stored["conditions"] == "Clear"


def test_observations_route_returns_normalized_data(app, client):
    provider, _ = _provider(_happy_routes())
    app.extensions["nws_provider"] = provider
    response = client.get(f"/api/weather/observations?lat={LAT}&lon={LON}")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["station_id"] == "KTOP"
    assert data["data_type"] == "observation"
    assert "raw_response" not in data


def test_index_lists_observations_endpoint(client):
    endpoints = client.get("/").get_json()["endpoints"]
    assert any("/api/weather/observations" in e for e in endpoints)


# --- F. Error handling --------------------------------------------------------


def test_timeout_is_a_distinct_provider_error():
    provider, _ = _provider({POINT_URL: requests.exceptions.Timeout("read timed out")})
    with pytest.raises(ProviderError) as exc:
        provider.fetch_observation(LAT, LON)
    assert exc.value.code == "weather_provider_timeout"
    assert exc.value.status_code == 504


def test_http_error_is_a_provider_error():
    provider, _ = _provider({POINT_URL: _FakeResponse({"title": "Server"}, status=500)})
    with pytest.raises(ProviderError) as exc:
        provider.fetch_observation(LAT, LON)
    assert exc.value.details["upstream_status"] == 500


def test_point_outside_us_coverage_is_a_clear_404(app, client):
    problem = {
        "type": "https://api.weather.gov/problems/InvalidPoint",
        "title": "Data Unavailable For Requested Point",
        "status": 404,
        "detail": "Unable to provide data for requested point 51.5,-0.12",
    }
    routes = {f"{BASE}/points/51.5,-0.12": _FakeResponse(problem, status=404)}
    provider, _ = _provider(routes)
    app.extensions["nws_provider"] = provider
    response = client.get("/api/weather/observations?lat=51.5&lon=-0.12")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "nws_point_unavailable"


def test_non_json_body_is_a_provider_error_not_a_crash():
    """The Akamai 'Access Denied' HTML page returned for a missing User-Agent."""
    provider, _ = _provider({POINT_URL: _FakeResponse(None, json_error=True)})
    with pytest.raises(ProviderError) as exc:
        provider.fetch_observation(LAT, LON)
    assert exc.value.details["reason"] == "malformed"


def test_zero_stations_is_a_provider_error():
    routes = {
        POINT_URL: _FakeResponse(_point_payload()),
        STATIONS_URL: _FakeResponse(_stations_payload()),
    }
    provider, _ = _provider(routes)
    with pytest.raises(ProviderError) as exc:
        provider.fetch_observation(LAT, LON)
    assert exc.value.code == "nws_no_stations"


# --- SSRF guard ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/gridpoints/TOP/32,81/stations",
        "http://api.weather.gov/gridpoints/TOP/32,81/stations",
        "https://api.weather.gov.evil.example/stations",
        "https://user@api.weather.gov/stations",
        "https://api.weather.gov:8443/stations",
        "file:///etc/passwd",
        None,
    ],
)
def test_ssrf_guard_rejects_untrusted_observation_stations_url(url):
    routes = {POINT_URL: _FakeResponse(_point_payload(url))}
    provider, session = _provider(routes)
    with pytest.raises(ProviderError):
        provider.fetch_observation(LAT, LON)
    assert session.urls() == [POINT_URL]


def test_ssrf_guard_accepts_api_weather_gov():
    assert ensure_nws_url(STATIONS_URL) == STATIONS_URL


# --- Caching ------------------------------------------------------------------


def test_point_metadata_is_cached_per_rounded_coordinate():
    provider, session = _provider(_happy_routes())
    provider.fetch_observation(LAT, LON)
    provider.fetch_observation("39.74561", "-97.08919")  # rounds to the same point
    assert session.urls().count(POINT_URL) == 1
    assert session.urls().count(STATIONS_URL) == 2


def test_point_cache_is_bounded():
    routes = _happy_routes()
    other = f"{BASE}/points/40.0,-97.0"
    routes[other] = _FakeResponse(_point_payload())
    provider, session = _provider(routes, cache_size=1)
    provider.fetch_observation(LAT, LON)
    provider.fetch_observation(40, -97)
    provider.fetch_observation(LAT, LON)
    assert session.urls().count(POINT_URL) == 2


# --- H. Live smoke test (opt-in) ----------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("NWS_LIVE_TESTS") != "1", reason="set NWS_LIVE_TESTS=1 to hit api.weather.gov"
)
def test_live_nws_observation():
    provider = NwsWeatherProvider(
        user_agent=os.environ.get("NWS_USER_AGENT", ""),
        timeout=int(os.environ.get("NWS_TIMEOUT_SECONDS", "15")),
    )
    result = provider.fetch_observation(LAT, LON)
    print(
        "LIVE NWS:",
        result["station_id"],
        result["observed_at"],
        result["temperature_c"],
        result["temperature_f"],
        result["description"],
    )
    assert result["data_type"] == "observation"
    assert result["station_id"]
    assert result["observed_at"]
