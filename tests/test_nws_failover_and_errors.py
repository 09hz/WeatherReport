"""Regression tests for the two review findings that blocked merge of
feature/nws-weather-api into master.

MAJOR 1 — station failover fired only on an HTTP error status, so one station
          serving a non-JSON body or resetting the connection suppressed the
          remaining candidates.
MAJOR 2 — any upstream 4xx that was not exactly problem type "/InvalidPoint"
          became a 502 with a logged stack trace, on an unauthenticated
          endpoint.

Source: docs/NWS_IMPLEMENTATION_REVIEW.md (on master), beads WeatherReport-gxo.
No test here touches the network: every request goes through an injected fake
session that records its calls.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from app.errors import ProviderError
from app.services.nws_provider import (
    KIND_OBSERVATION,
    KIND_POINT,
    STATION_ABORT_REASONS,
    NwsWeatherProvider,
)

BASE = "https://api.weather.gov"
LAT, LON = 39.7456, -97.0892
POINT_URL = f"{BASE}/points/{LAT},{LON}"
STATIONS_URL = f"{BASE}/gridpoints/TOP/32,81/stations"


class _FakeResponse:
    def __init__(
        self, payload: Any, *, status: int = 200, json_error: bool = False
    ) -> None:
        self._payload = payload
        self.status_code = status
        self._json_error = json_error

    def json(self) -> Any:
        if self._json_error:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class _FakeSession:
    """Routes URL -> canned response (or exception); records every call."""

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

    def observation_calls(self) -> list[str]:
        return [u for u in self.calls if u.endswith("/observations/latest")]


def _measure(unit: str, value: Any) -> dict[str, Any]:
    return {"unitCode": unit, "value": value, "qualityControl": "V"}


def _point_payload() -> dict[str, Any]:
    return {
        "properties": {
            "observationStations": STATIONS_URL,
            "gridId": "TOP",
            "gridX": 32,
            "gridY": 81,
            "timeZone": "America/Chicago",
        }
    }


def _stations_payload(*ids: str) -> dict[str, Any]:
    return {
        "features": [
            {"properties": {"stationIdentifier": i, "name": f"Station {i}"}}
            for i in ids
        ]
    }


def _observation_payload(station: str = "KTOP") -> dict[str, Any]:
    return {
        "properties": {
            "station": f"{BASE}/stations/{station}",
            "timestamp": "2026-11-24T02:10:00+00:00",
            "textDescription": "Clear",
            "temperature": _measure("wmoUnit:degC", 10.0),
        }
    }


def _obs_url(station: str) -> str:
    return f"{BASE}/stations/{station}/observations/latest"


def _provider(routes: dict[str, Any], **kwargs: Any):
    session = _FakeSession(routes)
    provider = NwsWeatherProvider(
        user_agent="WeatherReport-tests (test@example.invalid)",
        timeout=7,
        session=session,
        **kwargs,
    )
    return provider, session


def _routes_with_first_station(first: Any) -> dict[str, Any]:
    """Two candidate stations; the first one behaves as `first`."""
    return {
        POINT_URL: _FakeResponse(_point_payload()),
        STATIONS_URL: _FakeResponse(_stations_payload("KFOE", "KTOP")),
        _obs_url("KFOE"): first,
        _obs_url("KTOP"): _FakeResponse(_observation_payload("KTOP")),
    }


# --- MAJOR 1: failover must survive non-HTTP-status failures ------------------


def test_failover_on_a_non_json_body_served_with_http_200():
    """The review's exact repro: station #1 returns an HTML interstitial."""
    provider, session = _provider(
        _routes_with_first_station(_FakeResponse(None, json_error=True))
    )
    obs = provider.fetch_observation(LAT, LON)
    assert obs["station_id"] == "KTOP"
    assert len(session.observation_calls()) == 2


def test_failover_on_a_json_body_that_is_not_an_object():
    provider, session = _provider(
        _routes_with_first_station(_FakeResponse(["not", "an", "object"]))
    )
    assert provider.fetch_observation(LAT, LON)["station_id"] == "KTOP"
    assert len(session.observation_calls()) == 2


def test_failover_on_a_connection_level_failure():
    provider, session = _provider(
        _routes_with_first_station(requests.ConnectionError("connection reset"))
    )
    assert provider.fetch_observation(LAT, LON)["station_id"] == "KTOP"
    assert len(session.observation_calls()) == 2


def test_failover_still_works_on_an_http_error_status():
    """Control case: the behaviour that already worked must keep working."""
    provider, session = _provider(
        _routes_with_first_station(_FakeResponse({"status": 404}, status=404))
    )
    assert provider.fetch_observation(LAT, LON)["station_id"] == "KTOP"
    assert len(session.observation_calls()) == 2


def test_failover_on_a_station_serving_only_null_measurements():
    empty = {
        "properties": {
            "station": f"{BASE}/stations/KFOE",
            "timestamp": "2026-11-24T02:10:00+00:00",
            "textDescription": None,
            "temperature": _measure("wmoUnit:degC", None),
        }
    }
    provider, session = _provider(_routes_with_first_station(_FakeResponse(empty)))
    assert provider.fetch_observation(LAT, LON)["station_id"] == "KTOP"
    assert len(session.observation_calls()) == 2


def test_a_timeout_still_aborts_the_whole_request():
    provider, session = _provider(
        _routes_with_first_station(requests.Timeout("read timed out"))
    )
    with pytest.raises(ProviderError) as caught:
        provider.fetch_observation(LAT, LON)
    assert caught.value.details["reason"] == "timeout"
    assert caught.value.status_code == 504
    # The second candidate must NOT be tried: the chain is out of budget.
    assert len(session.observation_calls()) == 1


def test_abort_reasons_are_exactly_the_non_recoverable_ones():
    assert STATION_ABORT_REASONS == {"timeout", "untrusted_url", "invalid_url"}


def test_all_stations_failing_reports_why_the_last_one_failed():
    routes = {
        POINT_URL: _FakeResponse(_point_payload()),
        STATIONS_URL: _FakeResponse(_stations_payload("KFOE", "KTOP")),
        _obs_url("KFOE"): _FakeResponse(None, json_error=True),
        _obs_url("KTOP"): _FakeResponse({"error": "boom"}, status=503),
    }
    provider, session = _provider(routes)
    with pytest.raises(ProviderError) as caught:
        provider.fetch_observation(LAT, LON)
    details = caught.value.details
    assert caught.value.code == "nws_no_observation"
    assert details["stations_tried"] == ["KFOE", "KTOP"]
    assert details["last_failure"] == "http_error"
    assert details["last_upstream_status"] == 503
    assert len(session.observation_calls()) == 2


def test_exhausted_failover_does_not_log_a_stack_trace():
    routes = {
        POINT_URL: _FakeResponse(_point_payload()),
        STATIONS_URL: _FakeResponse(_stations_payload("KFOE")),
        _obs_url("KFOE"): _FakeResponse(None, json_error=True),
    }
    provider, _ = _provider(routes)
    with pytest.raises(ProviderError) as caught:
        provider.fetch_observation(LAT, LON)
    assert caught.value.log_traceback is False


def test_failover_is_still_bounded_by_max_station_attempts():
    routes = {
        POINT_URL: _FakeResponse(_point_payload()),
        STATIONS_URL: _FakeResponse(_stations_payload("A", "B", "C", "D", "E")),
        **{
            _obs_url(sid): _FakeResponse(None, json_error=True)
            for sid in ("A", "B", "C", "D", "E")
        },
    }
    provider, session = _provider(routes, max_station_attempts=2)
    with pytest.raises(ProviderError):
        provider.fetch_observation(LAT, LON)
    assert len(session.observation_calls()) == 2


# --- MAJOR 2: upstream 4xx maps to a client error, without traceback ---------


def test_point_404_without_a_json_body_is_a_client_404():
    """The review's repro: a 404 whose body is not JSON at all."""
    provider, _ = _provider({POINT_URL: _FakeResponse(None, status=404, json_error=True)})
    with pytest.raises(ProviderError) as caught:
        provider.fetch_observation(LAT, LON)
    error = caught.value
    assert error.status_code == 404
    assert error.code == "nws_point_unavailable"
    assert error.log_traceback is False
    assert "US coverage only" in error.message


def test_point_404_with_an_unrelated_problem_type_is_a_client_404():
    """The review's repro: type=.../problems/SomethingElse used to be a 502."""
    body = {
        "type": "https://api.weather.gov/problems/SomethingElse",
        "title": "Unexpected Problem",
        "detail": "something else entirely",
    }
    provider, _ = _provider({POINT_URL: _FakeResponse(body, status=404)})
    with pytest.raises(ProviderError) as caught:
        provider.fetch_observation(LAT, LON)
    assert caught.value.status_code == 404
    assert caught.value.code == "nws_point_unavailable"
    assert caught.value.log_traceback is False
    # The upstream problem detail is preserved for debugging.
    assert caught.value.details["title"] == "Unexpected Problem"


def test_point_404_with_invalidpoint_still_maps_the_same_way():
    body = {"type": "https://api.weather.gov/problems/InvalidPoint"}
    provider, _ = _provider({POINT_URL: _FakeResponse(body, status=404)})
    with pytest.raises(ProviderError) as caught:
        provider.fetch_observation(LAT, LON)
    assert caught.value.code == "nws_point_unavailable"


def test_point_400_is_a_client_400_not_a_server_fault():
    provider, _ = _provider(
        {POINT_URL: _FakeResponse({"title": "Bad Request"}, status=400)}
    )
    with pytest.raises(ProviderError) as caught:
        provider.fetch_observation(LAT, LON)
    assert caught.value.status_code == 400
    assert caught.value.code == "nws_invalid_point"
    assert caught.value.log_traceback is False


def test_upstream_5xx_remains_a_502_but_logs_no_traceback():
    provider, _ = _provider(
        {POINT_URL: _FakeResponse({"title": "Gateway Timeout"}, status=504)}
    )
    with pytest.raises(ProviderError) as caught:
        provider.fetch_observation(LAT, LON)
    assert caught.value.status_code == 502
    assert caught.value.details["upstream_status"] == 504
    assert caught.value.log_traceback is False


def test_error_details_record_which_request_kind_failed():
    provider, _ = _provider({POINT_URL: _FakeResponse({}, status=500)})
    with pytest.raises(ProviderError) as caught:
        provider.fetch_observation(LAT, LON)
    assert caught.value.details["request_kind"] == KIND_POINT

    routes = {
        POINT_URL: _FakeResponse(_point_payload()),
        STATIONS_URL: _FakeResponse(_stations_payload("KFOE")),
        _obs_url("KFOE"): _FakeResponse({}, status=500),
    }
    provider, _ = _provider(routes)
    with pytest.raises(ProviderError) as caught:
        provider.fetch_observation(LAT, LON)
    # The station loop swallows the per-station error, so assert on the
    # summary it produces instead.
    assert caught.value.code == "nws_no_observation"
    assert caught.value.details["last_failure"] == "http_error"


def test_station_404_is_not_turned_into_a_client_404():
    """A missing station is an internal failover signal, not a caller error."""
    provider, session = _provider(
        _routes_with_first_station(_FakeResponse({"status": 404}, status=404))
    )
    obs = provider.fetch_observation(LAT, LON)
    assert obs["station_id"] == "KTOP"
    assert len(session.observation_calls()) == 2


def test_stations_list_404_does_not_masquerade_as_missing_coverage():
    routes = {
        POINT_URL: _FakeResponse(_point_payload()),
        STATIONS_URL: _FakeResponse({"title": "Not Found"}, status=404),
    }
    provider, _ = _provider(routes)
    with pytest.raises(ProviderError) as caught:
        provider.fetch_observation(LAT, LON)
    assert caught.value.code != "nws_point_unavailable"
    assert caught.value.status_code == 502
    assert caught.value.details["request_kind"] == "stations"


# --- The whole point of MAJOR 2: no caller-drivable log volume ---------------


def test_out_of_coverage_request_writes_no_traceback_through_the_api(
    app, client, caplog, monkeypatch
):
    provider, _ = _provider(
        {POINT_URL: _FakeResponse(None, status=404, json_error=True)}
    )
    app.extensions["weather_provider"] = provider
    app.extensions["nws_provider"] = provider

    with caplog.at_level("ERROR", logger=app.logger.name):
        response = client.get(f"/api/weather/observations?lat={LAT}&lon={LON}")

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "nws_point_unavailable"
    assert not [r for r in caplog.records if r.exc_info]


def test_repeated_upstream_4xx_cannot_fill_the_log_with_tracebacks(
    app, client, caplog
):
    provider, _ = _provider(
        {POINT_URL: _FakeResponse({"title": "Nope"}, status=404)}
    )
    app.extensions["weather_provider"] = provider
    app.extensions["nws_provider"] = provider

    with caplog.at_level("ERROR", logger=app.logger.name):
        for _ in range(5):
            client.get(f"/api/weather/observations?lat={LAT}&lon={LON}")

    assert not [r for r in caplog.records if r.exc_info]


def test_kind_default_keeps_observation_semantics():
    """_get_json defaults to the observation kind, the recoverable one."""
    assert KIND_OBSERVATION == "observation"
    provider, _ = _provider(
        {_obs_url("KFOE"): _FakeResponse({"title": "Not Found"}, status=404)}
    )
    with pytest.raises(ProviderError) as caught:
        provider._get_json(_obs_url("KFOE"))
    assert caught.value.details["request_kind"] == KIND_OBSERVATION
    assert caught.value.status_code == 502
