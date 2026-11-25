"""Weather caching, pagination metadata and canonical condition codes
(TASK-003, 004, 008).

No test performs a real network request: the provider is always a local
double that counts its own calls.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.weather_cache import WeatherCache, cache_key, fetch_with_cache
from app.services.weather_provider import (
    CANONICAL_CONDITIONS,
    classify_conditions,
    normalize,
)


class _CountingProvider:
    """Provider double: records every fetch so cache hits are provable."""

    name = "test-double"
    available = True

    def __init__(self, conditions: str = "light rain") -> None:
        self.calls: list[str] = []
        self._conditions = conditions

    def fetch_current(self, location: str) -> dict[str, Any]:
        self.calls.append(location)
        return {
            "location": location,
            "observed_at": "2026-11-24T12:00:00Z",
            "temperature_c": 7.5,
            "humidity": 72.0,
            "wind_speed_kph": 14.0,
            "conditions": self._conditions,
            "condition_code": classify_conditions(self._conditions),
            "provider": self.name,
            "raw_response": {"source": "unit-test double", "call": len(self.calls)},
        }


@pytest.fixture
def provider(app):
    double = _CountingProvider()
    app.extensions["weather_provider"] = double
    return double


class _ManualClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- Cache unit behaviour -----------------------------------------------------


def test_cache_returns_a_stored_payload():
    cache = WeatherCache(ttl_seconds=60)
    cache.set("Oslo", {"temperature_c": 1.0})
    payload, age = cache.get("Oslo")
    assert payload == {"temperature_c": 1.0}
    assert age is not None and age >= 0


def test_cache_key_is_case_and_whitespace_insensitive():
    assert cache_key("  New   YORK ") == "new york"
    cache = WeatherCache(ttl_seconds=60)
    cache.set("New York", {"temperature_c": 2.0})
    assert cache.get("  new york ")[0] is not None


def test_cache_expires_entries_after_the_ttl():
    clock = _ManualClock()
    cache = WeatherCache(ttl_seconds=30, clock=clock)
    cache.set("Oslo", {"temperature_c": 1.0})
    clock.advance(29)
    assert cache.get("Oslo")[0] is not None
    clock.advance(2)
    assert cache.get("Oslo")[0] is None


def test_cache_counts_hits_and_misses():
    cache = WeatherCache(ttl_seconds=60)
    cache.get("Oslo")
    cache.set("Oslo", {"a": 1})
    cache.get("Oslo")
    stats = cache.stats()
    assert (stats["hits"], stats["misses"], stats["entries"]) == (1, 1, 1)
    assert stats["hit_rate"] == 0.5


def test_zero_ttl_disables_caching():
    cache = WeatherCache(ttl_seconds=0)
    cache.set("Oslo", {"a": 1})
    assert cache.enabled is False
    assert cache.get("Oslo")[0] is None


def test_cache_evicts_the_oldest_entry_at_capacity():
    clock = _ManualClock()
    cache = WeatherCache(ttl_seconds=600, max_entries=2, clock=clock)
    cache.set("A", {"n": 1})
    clock.advance(1)
    cache.set("B", {"n": 2})
    clock.advance(1)
    cache.set("C", {"n": 3})
    assert cache.get("A")[0] is None
    assert cache.get("C")[0] is not None
    assert cache.stats()["evictions"] == 1


def test_cache_stores_a_copy_not_a_reference():
    cache = WeatherCache(ttl_seconds=60)
    payload = {"temperature_c": 1.0}
    cache.set("Oslo", payload)
    payload["temperature_c"] = 99.0
    stored, _ = cache.get("Oslo")
    assert stored is not None
    assert stored["temperature_c"] == 1.0


def test_invalidate_drops_one_location_or_everything():
    cache = WeatherCache(ttl_seconds=60)
    cache.set("A", {"n": 1})
    cache.set("B", {"n": 2})
    cache.invalidate("A")
    assert cache.get("A")[0] is None and cache.get("B")[0] is not None
    cache.invalidate()
    assert cache.get("B")[0] is None


def test_fetch_with_cache_calls_the_fetcher_once_per_window():
    cache = WeatherCache(ttl_seconds=60)
    calls: list[str] = []

    def fetch(location: str) -> dict[str, Any]:
        calls.append(location)
        return {"location": location}

    first, meta_first = fetch_with_cache(cache, "Oslo", fetch)
    second, meta_second = fetch_with_cache(cache, "oslo", fetch)
    assert first == second
    assert calls == ["Oslo"]
    assert meta_first["hit"] is False and meta_second["hit"] is True


def test_a_failed_fetch_is_not_cached():
    cache = WeatherCache(ttl_seconds=60)

    def boom(location: str) -> dict[str, Any]:
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError):
        fetch_with_cache(cache, "Oslo", boom)
    assert cache.get("Oslo")[0] is None


# --- Cache through the HTTP layer --------------------------------------------


def test_repeat_request_is_served_from_cache(client, provider):
    first = client.get("/api/weather/current?location=Oslo")
    second = client.get("/api/weather/current?location=Oslo")
    assert first.status_code == second.status_code == 200
    assert first.get_json()["cache"]["hit"] is False
    assert second.get_json()["cache"]["hit"] is True
    # The acceptance criterion: one outbound call, two answers.
    assert provider.calls == ["Oslo"]


def test_cache_metadata_is_present_on_every_response(client, provider):
    meta = client.get("/api/weather/current?location=Oslo").get_json()["cache"]
    assert meta["ttl_seconds"] > 0
    assert meta["enabled"] is True
    assert "age_seconds" in meta and "expires_in_seconds" in meta


def test_distinct_locations_each_hit_the_provider(client, provider):
    client.get("/api/weather/current?location=Oslo")
    client.get("/api/weather/current?location=Lisbon")
    assert provider.calls == ["Oslo", "Lisbon"]


def test_cached_endpoint_serves_without_refetching(client, provider):
    client.get("/api/weather/current?location=Oslo")
    response = client.get("/api/weather/cached/Oslo")
    assert response.status_code == 200
    body = response.get_json()
    assert body["data"]["location"] == "Oslo"
    assert body["cache"]["hit"] is True
    assert provider.calls == ["Oslo"]


def test_cached_endpoint_404s_when_nothing_is_cached(client, provider):
    response = client.get("/api/weather/cached/Reykjavik")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"
    assert provider.calls == []


def test_cached_endpoint_never_leaks_the_raw_payload(client, provider):
    client.get("/api/weather/current?location=Oslo")
    body = client.get("/api/weather/cached/Oslo").get_json()
    assert "raw_response" not in body["data"]


def test_expired_entry_is_refetched(app, client, provider):
    clock = _ManualClock()
    app.extensions["weather_cache"] = WeatherCache(ttl_seconds=5, clock=clock)
    client.get("/api/weather/current?location=Oslo")
    clock.advance(10)
    client.get("/api/weather/current?location=Oslo")
    assert provider.calls == ["Oslo", "Oslo"]


def test_provider_status_exposes_cache_stats(client, provider):
    client.get("/api/weather/current?location=Oslo")
    stats = client.get("/api/weather/provider").get_json()["data"]["cache"]
    assert stats["entries"] == 1
    assert stats["enabled"] is True


def test_snapshot_post_also_uses_the_cache(client, provider):
    client.get("/api/weather/current?location=Oslo")
    response = client.post("/api/weather/snapshots", json={"location": "Oslo"})
    assert response.status_code == 201
    assert response.get_json()["cache"]["hit"] is True
    assert provider.calls == ["Oslo"]


def test_unconfigured_provider_is_not_cached_as_a_failure(client):
    assert client.get("/api/weather/current?location=Oslo").status_code == 503
    assert client.get("/api/weather/cached/Oslo").status_code == 404


# --- TASK-004: snapshot pagination metadata ----------------------------------


def test_snapshot_list_reports_total_count_and_has_more(client, provider, connection):
    from app.services import weather_service

    for index in range(4):
        weather_service.save_snapshot(
            connection,
            {
                "location": f"City {index}",
                "observed_at": None,
                "temperature_c": 1.0,
                "humidity": None,
                "wind_speed_kph": None,
                "conditions": "clear",
                "provider": "test-double",
                "raw_response": None,
            },
        )
    page = client.get("/api/weather/snapshots?limit=3&offset=0").get_json()
    assert page["count"] == 3
    assert page["total_count"] == 4
    assert page["has_more"] is True

    last = client.get("/api/weather/snapshots?limit=3&offset=3").get_json()
    assert last["count"] == 1
    assert last["has_more"] is False


def test_snapshot_total_count_respects_the_location_filter(client, connection):
    from app.services import weather_service

    for location in ("Oslo", "Oslo", "Lisbon"):
        weather_service.save_snapshot(
            connection,
            {
                "location": location,
                "observed_at": None,
                "temperature_c": 1.0,
                "humidity": None,
                "wind_speed_kph": None,
                "conditions": "clear",
                "provider": "test-double",
                "raw_response": None,
            },
        )
    page = client.get("/api/weather/snapshots?location=Oslo").get_json()
    assert page["total_count"] == 2


# --- TASK-008: canonical condition codes -------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("clear sky", "clear"),
        ("Sunny", "clear"),
        ("FAIR", "clear"),
        ("partly cloudy", "cloudy"),
        ("Partly Sunny", "cloudy"),
        ("mostly cloudy", "cloudy"),
        ("overcast", "cloudy"),
        ("broken clouds", "cloudy"),
        ("light rain", "rain"),
        ("Heavy Rain Showers", "rain"),
        ("drizzle", "rain"),
        ("light snow", "snow"),
        ("Blizzard conditions", "snow"),
        ("snow flurries", "snow"),
        ("wintry mix", "snow"),
        ("freezing rain", "sleet"),
        ("Ice Pellets", "sleet"),
        ("sleet", "sleet"),
        ("small hail", "hail"),
        ("thunderstorm", "thunderstorm"),
        ("T-Storms likely", "thunderstorm"),
        ("Rain and thunder", "thunderstorm"),
        ("patchy fog", "fog"),
        ("mist", "fog"),
        ("Haze", "fog"),
        ("windy", "windy"),
        ("Breezy", "windy"),
        ("", "unknown"),
        ("   ", "unknown"),
        (None, "unknown"),
        ("completely unintelligible text", "unknown"),
    ],
)
def test_classify_conditions_maps_varied_phrasings(text, expected):
    assert classify_conditions(text) == expected


def test_every_classification_is_in_the_canonical_vocabulary():
    samples = [
        "clear",
        "partly cloudy",
        "rain",
        "snow",
        "sleet",
        "hail",
        "thunder",
        "fog",
        "windy",
        "???",
    ]
    assert all(classify_conditions(s) in CANONICAL_CONDITIONS for s in samples)


def test_classification_is_punctuation_and_case_insensitive():
    assert (
        classify_conditions("LIGHT-RAIN;")
        == classify_conditions("light rain")
        == "rain"
    )


def test_classification_accepts_a_list_of_tokens():
    assert classify_conditions(["Light", "Snow"]) == "snow"


def test_hazardous_conditions_win_over_generic_ones():
    # A thunderstorm that also rains is a thunderstorm, not plain rain.
    assert classify_conditions("rain with thunderstorms") == "thunderstorm"
    assert classify_conditions("freezing rain") == "sleet"


def test_normalize_adds_a_condition_code():
    raw = {
        "name": "Oslo",
        "main": {"temp": -1.5, "humidity": 81},
        "weather": [{"main": "Snow", "description": "light snow"}],
    }
    result = normalize(raw, location="Oslo", provider="fake")
    assert result["conditions"] == "light snow"
    assert result["condition_code"] == "snow"


def test_normalize_does_not_invent_a_condition():
    result = normalize({"name": "Oslo"}, location="Oslo", provider="fake")
    assert result["conditions"] is None
    assert result["condition_code"] == "unknown"


def test_stored_snapshots_expose_a_condition_code(client, connection):
    from app.services import weather_service

    stored = weather_service.save_snapshot(
        connection,
        {
            "location": "Oslo",
            "observed_at": None,
            "temperature_c": -2.0,
            "humidity": None,
            "wind_speed_kph": None,
            "conditions": "Heavy Thunderstorms",
            "provider": "test-double",
            "raw_response": None,
        },
    )
    assert stored["condition_code"] == "thunderstorm"
    body = client.get(f"/api/weather/snapshots/{stored['id']}").get_json()["data"]
    assert body["condition_code"] == "thunderstorm"
