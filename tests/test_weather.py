"""Weather provider, normalization and snapshot tests.

No test performs a real network request. The configured-provider cases use a
fake HTTP session so no observation here is ever presented as real data.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Config
from app.errors import ProviderError, ProviderNotConfiguredError
from app.services import weather_service
from app.services.weather_provider import (
    HttpWeatherProvider,
    UnconfiguredWeatherProvider,
    build_provider,
    normalize,
)


# --- Behaviour without credentials -------------------------------------------


def test_provider_is_unconfigured_without_credentials(app):
    provider = app.extensions["weather_provider"]
    assert isinstance(provider, UnconfiguredWeatherProvider)
    assert provider.available is False


def test_unconfigured_provider_refuses_instead_of_inventing_data():
    with pytest.raises(ProviderNotConfiguredError):
        UnconfiguredWeatherProvider().fetch_current("Oslo")


def test_current_weather_returns_503_when_unconfigured(client):
    response = client.get("/api/weather/current?location=Oslo")
    assert response.status_code == 503
    error = response.get_json()["error"]
    assert error["code"] == "weather_provider_not_configured"
    assert "WEATHER_API_KEY" in error["message"]


def test_unconfigured_503_does_not_log_a_stack_trace(app, client, caplog):
    """An absent provider is a deployment state, not an incident."""
    with caplog.at_level("ERROR", logger=app.logger.name):
        assert client.get("/api/weather/current?location=Oslo").status_code == 503
    assert not [r for r in caplog.records if r.exc_info]


def test_provider_failure_does_log_a_stack_trace(app, client, caplog):
    provider, _ = _http_provider(RuntimeError("timeout"))
    app.extensions["weather_provider"] = provider
    with caplog.at_level("ERROR", logger=app.logger.name):
        assert client.get("/api/weather/current?location=Oslo").status_code == 502
    assert [r for r in caplog.records if r.exc_info]


def test_provider_status_endpoint_reports_unavailable(client):
    body = client.get("/api/weather/provider").get_json()["data"]
    assert body["available"] is False
    assert body["provider"] == "unconfigured"


def test_saving_snapshot_requires_a_provider(client):
    response = client.post("/api/weather/snapshots", json={"location": "Oslo"})
    assert response.status_code == 503


def test_placeholder_env_values_do_not_enable_provider(monkeypatch):
    monkeypatch.setenv("WEATHER_API_KEY", "YOUR_WEATHER_API_KEY_HERE")
    monkeypatch.setenv("WEATHER_API_BASE_URL", "YOUR_WEATHER_PROVIDER_URL_HERE")
    assert Config().weather_provider_configured is False
    assert isinstance(
        build_provider(Config().to_flask_mapping()), UnconfiguredWeatherProvider
    )


def test_real_env_values_select_the_http_provider(monkeypatch):
    monkeypatch.setenv("WEATHER_API_KEY", "k-123")
    monkeypatch.setenv("WEATHER_API_BASE_URL", "https://api.example.test/weather")
    provider = build_provider(Config().to_flask_mapping())
    assert isinstance(provider, HttpWeatherProvider)
    assert provider.available is True


# --- Input validation ---------------------------------------------------------


@pytest.mark.parametrize("query", ["", "?location=", "?location=%20%20"])
def test_missing_location_is_rejected_before_any_provider_call(client, query):
    response = client.get(f"/api/weather/current{query}")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "validation_error"


def test_snapshot_post_requires_location(client):
    response = client.post("/api/weather/snapshots", json={})
    assert response.status_code == 400


# --- Normalization ------------------------------------------------------------


def test_normalize_maps_openweather_style_payload():
    raw = {
        "name": "Oslo",
        "dt": 1764000000,
        "main": {"temp": -1.5, "humidity": 81},
        "wind": {"speed": 4.2},
        "weather": [{"main": "Snow", "description": "light snow"}],
    }
    result = normalize(raw, location="Oslo", provider="fake")
    assert result["location"] == "Oslo"
    assert result["temperature_c"] == -1.5
    assert result["humidity"] == 81.0
    assert result["wind_speed_kph"] == 4.2
    assert result["conditions"] == "light snow"
    assert result["provider"] == "fake"


def test_normalize_leaves_missing_fields_as_none_rather_than_guessing():
    result = normalize({}, location="Nowhere", provider="fake")
    assert result["location"] == "Nowhere"
    assert result["temperature_c"] is None
    assert result["humidity"] is None
    assert result["wind_speed_kph"] is None
    assert result["conditions"] is None


def test_normalize_ignores_unusable_values():
    raw = {"main": {"temp": "not-a-number"}, "wind": "unexpected"}
    result = normalize(raw, location="X", provider="fake")
    assert result["temperature_c"] is None
    assert result["wind_speed_kph"] is None


# --- Configured provider, exercised against a fake session -------------------


class _FakeResponse:
    def __init__(self, payload: Any, *, status: int = 200) -> None:
        self._payload = payload
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")

    def json(self) -> Any:
        return self._payload


class _FakeSession:
    """Stand-in for `requests`; records calls, returns canned payloads."""

    def __init__(self, response: _FakeResponse | Exception) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _http_provider(response) -> tuple[HttpWeatherProvider, _FakeSession]:
    session = _FakeSession(response)
    provider = HttpWeatherProvider(
        base_url="https://api.example.test/weather",
        api_key="k-123",
        timeout=7,
        session=session,
    )
    return provider, session


def test_http_provider_sends_key_and_location():
    provider, session = _http_provider(_FakeResponse({"name": "Oslo"}))
    provider.fetch_current("Oslo")
    params = session.calls[0]["params"]
    assert params["q"] == "Oslo"
    assert params["appid"] == "k-123"
    assert session.calls[0]["timeout"] == 7


def test_http_provider_normalizes_the_response():
    payload = {"name": "Oslo", "main": {"temp": 3.0, "humidity": 70}}
    provider, _ = _http_provider(_FakeResponse(payload))
    result = provider.fetch_current("Oslo")
    assert result["temperature_c"] == 3.0
    assert result["provider"] == "http"


def test_http_provider_wraps_network_failures():
    provider, _ = _http_provider(RuntimeError("connection reset"))
    with pytest.raises(ProviderError):
        provider.fetch_current("Oslo")


def test_http_provider_rejects_non_object_payload():
    provider, _ = _http_provider(_FakeResponse(["unexpected"]))
    with pytest.raises(ProviderError):
        provider.fetch_current("Oslo")


def test_http_provider_wraps_http_errors():
    provider, _ = _http_provider(_FakeResponse({}, status=500))
    with pytest.raises(ProviderError):
        provider.fetch_current("Oslo")


def test_route_returns_502_when_configured_provider_fails(app, client):
    provider, _ = _http_provider(RuntimeError("timeout"))
    app.extensions["weather_provider"] = provider
    response = client.get("/api/weather/current?location=Oslo")
    assert response.status_code == 502
    assert response.get_json()["error"]["code"] == "weather_provider_error"


def test_endpoint_saves_and_lists_snapshot_with_fake_provider(app, client):
    payload = {"name": "Oslo", "main": {"temp": -2.0, "humidity": 80}}
    provider, _ = _http_provider(_FakeResponse(payload))
    app.extensions["weather_provider"] = provider

    created = client.post("/api/weather/snapshots", json={"location": "Oslo"})
    assert created.status_code == 201
    snapshot = created.get_json()["data"]
    assert snapshot["id"] > 0
    assert snapshot["temperature_c"] == -2.0
    assert snapshot["provider"] == "http"

    listed = client.get("/api/weather/snapshots").get_json()
    assert listed["count"] == 1
    fetched = client.get(f"/api/weather/snapshots/{snapshot['id']}")
    assert fetched.status_code == 200


# --- Snapshot persistence (service level) ------------------------------------


def _snapshot(**overrides: Any) -> dict[str, Any]:
    base = {
        "location": "Oslo",
        "observed_at": "2026-11-23T12:00:00Z",
        "temperature_c": -2.0,
        "humidity": 80.0,
        "wind_speed_kph": 11.0,
        "conditions": "light snow",
        "provider": "test-double",
        "raw_response": {"source": "unit-test fixture"},
    }
    base.update(overrides)
    return base


def test_save_and_get_snapshot_roundtrip(app, connection):
    stored = weather_service.save_snapshot(connection, _snapshot())
    fetched = weather_service.get_snapshot(connection, stored["id"])
    assert fetched["location"] == "Oslo"
    assert fetched["conditions"] == "light snow"
    assert fetched["provider"] == "test-double"


def test_raw_response_is_only_returned_on_request(app, connection):
    stored = weather_service.save_snapshot(connection, _snapshot())
    assert "raw_response" not in weather_service.get_snapshot(connection, stored["id"])
    with_raw = weather_service.get_snapshot(
        connection, stored["id"], include_raw=True
    )
    assert with_raw["raw_response"] == {"source": "unit-test fixture"}


def test_snapshots_can_be_filtered_by_location(app, connection):
    weather_service.save_snapshot(connection, _snapshot(location="Oslo"))
    weather_service.save_snapshot(connection, _snapshot(location="Lisbon"))
    results = weather_service.list_snapshots(connection, location="Lisbon")
    assert [s["location"] for s in results] == ["Lisbon"]


def test_snapshot_tolerates_missing_measurements(app, connection):
    stored = weather_service.save_snapshot(
        connection,
        _snapshot(temperature_c=None, humidity=None, wind_speed_kph=None,
                  conditions=None, raw_response=None),
    )
    assert stored["temperature_c"] is None
    assert stored["location"] == "Oslo"


def test_missing_snapshot_returns_404(client):
    response = client.get("/api/weather/snapshots/999")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"
