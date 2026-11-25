"""Review MINOR 6 and MINOR 8: chain budget and the remaining test gaps.

- WeatherReport-24d (MINOR 6): one lookup makes up to five sequential
  upstream requests. A total deadline must bound the whole chain, the
  per-request timeout must be the smaller of the remaining budget and the
  configured timeout, and an exhausted budget must raise without starting
  another upstream request.
- WeatherReport-7dj (MINOR 8): the degF and m_s-1 unit branches are covered
  in tests/test_nws_review_minors.py (WeatherReport-x3l). What was still
  untested and is covered here: the malformed-structure guards in get_point
  and _stations.

Time is driven by a fake clock, not by sleeping, so these tests are fast and
deterministic. No test here touches the network.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.errors import ProviderError
from app.services import nws_provider as nws_mod
from app.services.nws_provider import (
    TOTAL_TIMEOUT_MULTIPLIER,
    NwsWeatherProvider,
)

BASE = "https://api.weather.gov"
LAT, LON = 39.7456, -97.0892
POINT_URL = f"{BASE}/points/{LAT},{LON}"
STATIONS_URL = f"{BASE}/gridpoints/TOP/32,81/stations"


def _obs_url(station: str) -> str:
    return f"{BASE}/stations/{station}/observations/latest"


class _FakeResponse:
    def __init__(self, payload: Any, *, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> Any:
        return self._payload


class _Clock:
    """A monotonic clock the test advances explicitly."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _SlowSession:
    """Burns a fixed amount of fake time on every request.

    Records the `timeout` each call was given, which is what proves the
    per-request limit shrinks as the chain's budget is consumed.
    """

    def __init__(self, routes: dict[str, Any], clock: _Clock, cost: float) -> None:
        self._routes = routes
        self._clock = clock
        self._cost = cost
        self.timeouts: list[float | None] = []
        self.urls: list[str] = []

    def get(self, url: str, params=None, timeout=None, headers=None, **kwargs):
        self.urls.append(url)
        self.timeouts.append(timeout)
        self._clock.advance(self._cost)
        if url not in self._routes:
            raise AssertionError(f"unexpected request to {url}")
        response = self._routes[url]
        if isinstance(response, Exception):
            raise response
        return response


def _measure(unit: str, value: Any) -> dict[str, Any]:
    return {"unitCode": unit, "value": value, "qualityControl": "V"}


def _point_payload() -> dict[str, Any]:
    return {
        "properties": {
            "observationStations": STATIONS_URL,
            "forecast": f"{BASE}/gridpoints/TOP/32,81/forecast",
            "gridId": "TOP",
            "gridX": 32,
            "gridY": 81,
            "timeZone": "America/Chicago",
        }
    }


def _stations_payload(*ids: str) -> dict[str, Any]:
    return {"features": [{"properties": {"stationIdentifier": i}} for i in ids]}


def _observation_payload(**overrides: Any) -> dict[str, Any]:
    props: dict[str, Any] = {
        "timestamp": "2026-11-24T02:10:00+00:00",
        "textDescription": "Clear",
        "temperature": _measure("wmoUnit:degC", 10.0),
        "windSpeed": _measure("wmoUnit:km_h-1", 16.09344),
        "relativeHumidity": _measure("wmoUnit:percent", 57.5),
    }
    props.update(overrides)
    return {"properties": props}


def _nulls() -> dict[str, Any]:
    """A reachable station that reports nothing usable."""
    return {
        "properties": {
            "timestamp": "2026-11-24T02:10:00+00:00",
            "textDescription": None,
            "temperature": _measure("wmoUnit:degC", None),
            "windSpeed": _measure("wmoUnit:km_h-1", None),
            "relativeHumidity": _measure("wmoUnit:percent", None),
        }
    }


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(nws_mod, "monotonic", c)
    return c


def _chain(clock: _Clock, cost: float, *, stations=("KAAA", "KBBB", "KCCC"), obs=None):
    """A full happy-path route table where every station reports nulls."""
    routes: dict[str, Any] = {
        POINT_URL: _FakeResponse(_point_payload()),
        STATIONS_URL: _FakeResponse(_stations_payload(*stations)),
    }
    for sid in stations:
        routes[_obs_url(sid)] = _FakeResponse(obs if obs is not None else _nulls())
    return _SlowSession(routes, clock, cost)


# --- WeatherReport-24d: the chain has a total budget ------------------------


def test_default_total_budget_is_derived_from_the_per_request_timeout():
    provider = NwsWeatherProvider(timeout=10)
    assert provider._total_timeout == 10 * TOTAL_TIMEOUT_MULTIPLIER


def test_explicit_total_timeout_wins():
    provider = NwsWeatherProvider(timeout=10, total_timeout=7)
    assert provider._total_timeout == 7.0


@pytest.mark.parametrize("bogus", [0, -5, None])
def test_unusable_total_timeout_falls_back_to_the_derived_default(bogus):
    provider = NwsWeatherProvider(timeout=4, total_timeout=bogus)
    assert provider._total_timeout == 4 * TOTAL_TIMEOUT_MULTIPLIER


def test_chain_stops_when_the_budget_is_exhausted(clock):
    """Five requests at 4s each would take 20s; the budget is 12s."""
    session = _chain(clock, cost=4.0)
    provider = NwsWeatherProvider(timeout=10, total_timeout=12, session=session)

    with pytest.raises(ProviderError) as excinfo:
        provider.fetch_observation(LAT, LON)

    assert excinfo.value.details["reason"] == "timeout"
    assert excinfo.value.details["scope"] == "total_budget"
    assert excinfo.value.status_code == 504
    assert excinfo.value.code == "weather_provider_timeout"
    # point + stations + one observation = 12s, which exhausts the budget,
    # so the remaining two stations are never contacted.
    assert len(session.urls) == 3
    assert _obs_url("KBBB") not in session.urls
    assert _obs_url("KCCC") not in session.urls


def test_no_further_request_is_started_once_over_budget(clock):
    session = _chain(clock, cost=20.0)
    provider = NwsWeatherProvider(timeout=10, total_timeout=15, session=session)

    with pytest.raises(ProviderError):
        provider.fetch_observation(LAT, LON)

    # The very first request already blows the budget, so only it was issued.
    assert session.urls == [POINT_URL]


def test_per_request_timeout_shrinks_to_the_remaining_budget(clock):
    session = _chain(clock, cost=3.0)
    provider = NwsWeatherProvider(timeout=10, total_timeout=11, session=session)

    with pytest.raises(ProviderError):
        provider.fetch_observation(LAT, LON)

    # Budget 11s, per-request 10s, 3s consumed per call:
    #   call 1 -> min(11, 10) = 10
    #   call 2 -> min(8, 10)  = 8
    #   call 3 -> min(5, 10)  = 5
    #   call 4 -> min(2, 10)  = 2
    assert session.timeouts == [10.0, 8.0, 5.0, 2.0]


def test_per_request_timeout_is_never_larger_than_the_configured_timeout(clock):
    """A generous budget must not widen an individual request's limit."""
    session = _chain(clock, cost=0.1, obs=_observation_payload())
    provider = NwsWeatherProvider(timeout=2, total_timeout=600, session=session)

    provider.fetch_observation(LAT, LON)

    assert session.timeouts, "expected at least one request"
    assert all(t == 2.0 for t in session.timeouts)


def test_a_fast_chain_still_succeeds(clock):
    session = _chain(clock, cost=0.5, obs=_observation_payload())
    provider = NwsWeatherProvider(timeout=10, total_timeout=12, session=session)

    result = provider.fetch_observation(LAT, LON)

    assert result["station_id"] == "KAAA"
    assert result["temperature_c"] == 10.0


def test_budget_exhaustion_aborts_instead_of_trying_the_next_station(clock):
    """reason='timeout' is in STATION_ABORT_REASONS, so the loop must stop.

    Without this the loop would treat an exhausted budget as a per-station
    failure and keep trying candidates it cannot pay for.
    """
    session = _chain(clock, cost=5.0)
    provider = NwsWeatherProvider(timeout=10, total_timeout=10, session=session)

    with pytest.raises(ProviderError) as excinfo:
        provider.fetch_observation(LAT, LON)

    assert excinfo.value.details["reason"] == "timeout"
    assert session.urls == [POINT_URL, STATIONS_URL]


def test_budget_error_does_not_log_a_traceback(clock):
    """This endpoint is unauthenticated; a slow upstream must not be loud."""
    session = _chain(clock, cost=20.0)
    provider = NwsWeatherProvider(timeout=10, total_timeout=5, session=session)

    with pytest.raises(ProviderError) as excinfo:
        provider.fetch_observation(LAT, LON)

    assert excinfo.value.log_traceback is False


def test_cached_point_does_not_spend_budget(clock):
    """A cache hit issues no request, so it must not consume the deadline."""
    session = _chain(clock, cost=1.0, obs=_observation_payload())
    provider = NwsWeatherProvider(timeout=10, total_timeout=30, session=session)

    provider.fetch_observation(LAT, LON)
    first_count = len(session.urls)
    provider.fetch_observation(LAT, LON)

    assert POINT_URL not in session.urls[first_count:]


def test_direct_get_point_without_a_deadline_uses_the_configured_timeout(clock):
    """get_point is public and callable on its own; no deadline is fine."""
    session = _chain(clock, cost=1.0)
    provider = NwsWeatherProvider(timeout=9, total_timeout=30, session=session)

    provider.get_point(LAT, LON)

    assert session.timeouts == [9]


# --- WeatherReport-7dj: malformed-structure guards --------------------------


@pytest.mark.parametrize(
    "props", [None, [], "nope", 42, True], ids=["null", "list", "str", "int", "bool"]
)
def test_malformed_point_properties_is_rejected(props, clock):
    session = _SlowSession({POINT_URL: _FakeResponse({"properties": props})}, clock, 0.1)
    provider = NwsWeatherProvider(timeout=10, session=session)

    with pytest.raises(ProviderError) as excinfo:
        provider.fetch_observation(LAT, LON)

    assert excinfo.value.details["reason"] == "malformed"
    assert session.urls == [POINT_URL], "must not continue to the station step"


def test_point_payload_without_a_properties_key_is_rejected(clock):
    session = _SlowSession({POINT_URL: _FakeResponse({"id": "x"})}, clock, 0.1)
    provider = NwsWeatherProvider(timeout=10, session=session)

    with pytest.raises(ProviderError) as excinfo:
        provider.fetch_observation(LAT, LON)
    assert excinfo.value.details["reason"] == "malformed"


@pytest.mark.parametrize(
    "features",
    [None, {}, "nope", 42],
    ids=["null", "dict", "str", "int"],
)
def test_malformed_station_features_is_rejected(features, clock):
    routes = {
        POINT_URL: _FakeResponse(_point_payload()),
        STATIONS_URL: _FakeResponse({"features": features}),
    }
    session = _SlowSession(routes, clock, 0.1)
    provider = NwsWeatherProvider(timeout=10, session=session)

    with pytest.raises(ProviderError) as excinfo:
        provider.fetch_observation(LAT, LON)

    assert excinfo.value.details["reason"] == "malformed"
    assert session.urls == [POINT_URL, STATIONS_URL]


def test_station_list_without_a_features_key_is_rejected(clock):
    routes = {
        POINT_URL: _FakeResponse(_point_payload()),
        STATIONS_URL: _FakeResponse({"observationStations": []}),
    }
    session = _SlowSession(routes, clock, 0.1)
    provider = NwsWeatherProvider(timeout=10, session=session)

    with pytest.raises(ProviderError) as excinfo:
        provider.fetch_observation(LAT, LON)
    assert excinfo.value.details["reason"] == "malformed"


@pytest.mark.parametrize(
    "feature",
    [
        None,
        "nope",
        42,
        {},
        {"properties": None},
        {"properties": "nope"},
        {"properties": {}},
        {"properties": {"stationIdentifier": None}},
        {"properties": {"stationIdentifier": ""}},
        {"properties": {"stationIdentifier": "has space"}},
        {"properties": {"stationIdentifier": "WAY_TOO_LONG_IDENTIFIER"}},
        {"properties": {"stationIdentifier": 1234}},
    ],
)
def test_individual_malformed_features_are_skipped_not_fatal(feature, clock):
    """A well-formed list containing junk entries yields no usable stations."""
    routes = {
        POINT_URL: _FakeResponse(_point_payload()),
        STATIONS_URL: _FakeResponse({"features": [feature]}),
    }
    session = _SlowSession(routes, clock, 0.1)
    provider = NwsWeatherProvider(timeout=10, session=session)

    with pytest.raises(ProviderError) as excinfo:
        provider.fetch_observation(LAT, LON)

    assert excinfo.value.code == "nws_no_stations"
    assert excinfo.value.details["reason"] == "no_stations"


def test_a_good_feature_among_junk_is_still_used(clock):
    routes = {
        POINT_URL: _FakeResponse(_point_payload()),
        STATIONS_URL: _FakeResponse(
            {
                "features": [
                    None,
                    {"properties": {"stationIdentifier": "bad id"}},
                    {"properties": {"stationIdentifier": "KAAA"}},
                ]
            }
        ),
        _obs_url("KAAA"): _FakeResponse(_observation_payload()),
    }
    session = _SlowSession(routes, clock, 0.1)
    provider = NwsWeatherProvider(timeout=10, session=session)

    assert provider.fetch_observation(LAT, LON)["station_id"] == "KAAA"
