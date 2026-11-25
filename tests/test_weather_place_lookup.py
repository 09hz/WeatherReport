"""Tests for place-name resolution in the weather service and UI.

No test here touches the network: a fake provider records the location it was
handed, which is precisely what the resolution step is responsible for.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.errors import ValidationError
from app.services import weather_service


class RecordingProvider:
    """Fake provider that records what location it was asked for."""

    name = "recorder"
    available = True

    def __init__(self, *, requires_coordinates: bool) -> None:
        self.requires_coordinates = requires_coordinates
        self.calls: list[str] = []

    def fetch_current(self, location: str) -> dict[str, Any]:
        self.calls.append(location)
        return {
            "location": location,
            "temperature_c": 11.0,
            "conditions": "Clear",
            "provider": self.name,
        }


class LegacyProvider:
    """A provider predating requires_coordinates, i.e. it omits the flag."""

    name = "legacy"
    available = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch_current(self, location: str) -> dict[str, Any]:
        self.calls.append(location)
        return {"location": location, "provider": self.name}


# --------------------------------------------------------------------------- #
# resolve_location
# --------------------------------------------------------------------------- #


def test_place_name_is_geocoded_for_a_coordinate_only_provider():
    provider = RecordingProvider(requires_coordinates=True)
    resolved = weather_service.resolve_location("Austin, TX", provider)
    assert resolved.place is not None
    assert resolved.place.label == "Austin, TX"
    # The provider must receive coordinates, not the typed text.
    lat, _, lon = resolved.provider_location.partition(",")
    assert 29.5 < float(lat) < 31.0
    assert -98.5 < float(lon) < -97.0


def test_coordinates_bypass_the_geocoder_entirely():
    provider = RecordingProvider(requires_coordinates=True)
    resolved = weather_service.resolve_location("45.5,-122.6", provider)
    assert resolved.provider_location == "45.5,-122.6"
    assert resolved.place is None


def test_place_name_is_passed_through_for_a_provider_that_geocodes_itself():
    provider = RecordingProvider(requires_coordinates=False)
    resolved = weather_service.resolve_location("Austin, TX", provider)
    assert resolved.provider_location == "Austin, TX"
    assert resolved.place is None


def test_a_provider_without_the_flag_is_treated_as_passthrough():
    """Fakes in the existing suite omit requires_coordinates; they must work."""
    resolved = weather_service.resolve_location("anywhere", LegacyProvider())
    assert resolved.provider_location == "anywhere"
    assert resolved.place is None


# --------------------------------------------------------------------------- #
# fetch_current
# --------------------------------------------------------------------------- #


def test_fetch_current_labels_the_snapshot_with_the_resolved_place():
    provider = RecordingProvider(requires_coordinates=True)
    snapshot = weather_service.fetch_current("78701", provider)

    assert snapshot["location"] == "Austin, TX"
    assert snapshot["place"]["matched"] == "zip"
    assert snapshot["place"]["label"] == "Austin, TX"
    # The typed query is preserved so a surprising match is auditable.
    assert snapshot["query"] == "78701"
    assert snapshot["coordinates"] == provider.calls[0]


def test_fetch_current_keeps_coordinates_as_the_location_when_given_them():
    provider = RecordingProvider(requires_coordinates=True)
    snapshot = weather_service.fetch_current("45.5,-122.6", provider)
    assert snapshot["location"] == "45.5,-122.6"
    assert snapshot["coordinates"] == "45.5,-122.6"
    assert "place" not in snapshot
    assert "query" not in snapshot


def test_fetch_current_adds_nothing_for_a_passthrough_provider():
    provider = RecordingProvider(requires_coordinates=False)
    snapshot = weather_service.fetch_current("Austin, TX", provider)
    assert snapshot["location"] == "Austin, TX"
    assert "place" not in snapshot
    assert "coordinates" not in snapshot


def test_fetch_current_does_not_mutate_the_provider_payload():
    """The cache stores what the provider returned; labelling must not edit it."""

    payload = {"location": "x", "provider": "recorder"}

    class SharedPayloadProvider:
        name = "shared"
        available = True
        requires_coordinates = True

        def fetch_current(self, location: str) -> dict[str, Any]:
            return payload

    weather_service.fetch_current("78701", SharedPayloadProvider())
    assert payload == {"location": "x", "provider": "recorder"}


def test_an_ambiguous_place_fails_before_the_provider_is_called():
    provider = RecordingProvider(requires_coordinates=True)
    with pytest.raises(ValidationError):
        weather_service.fetch_current("Springfield", provider)
    assert provider.calls == []


# --------------------------------------------------------------------------- #
# through the HTTP layer
# --------------------------------------------------------------------------- #


@pytest.fixture
def nws_client(app):
    """A client whose provider is a coordinate-only recorder."""
    provider = RecordingProvider(requires_coordinates=True)
    app.extensions["weather_provider"] = provider
    client = app.test_client()
    client.recorder = provider  # type: ignore[attr-defined]
    return client


def test_api_current_accepts_a_city_and_state(nws_client):
    response = nws_client.get("/api/weather/current?location=Austin,%20TX")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["location"] == "Austin, TX"
    assert data["place"]["matched"] == "city"


def test_api_current_accepts_a_zip(nws_client):
    response = nws_client.get("/api/weather/current?location=78701")
    assert response.status_code == 200
    assert response.get_json()["data"]["location"] == "Austin, TX"


def test_api_current_reports_ambiguity_as_a_400(nws_client):
    response = nws_client.get("/api/weather/current?location=Springfield")
    assert response.status_code == 400
    message = response.get_json()["error"]["message"]
    assert "Add the state" in message


def test_api_current_reports_an_unknown_place_as_a_404(nws_client):
    response = nws_client.get("/api/weather/current?location=Nowhereville,%20TX")
    assert response.status_code == 404


def test_ui_weather_resolves_a_place_name_and_shows_what_it_matched(nws_client):
    response = nws_client.post(
        "/ui/weather", data={"location": "78701", "action": "lookup"}
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "resolved to" in body
    assert "Austin, TX" in body
    # The resolved-place dict must not leak into the measurement grid.
    assert "&#39;matched&#39;" not in body


def test_ui_weather_form_hint_documents_what_it_accepts(nws_client):
    body = nws_client.get("/ui/weather").get_data(as_text=True)
    assert "ZIP code" in body
    assert "Austin, TX" in body


def test_ui_weather_shows_an_ambiguous_place_once_on_the_field(nws_client):
    response = nws_client.post(
        "/ui/weather", data={"location": "Springfield", "action": "lookup"}
    )
    assert response.status_code == 400
    body = response.get_data(as_text=True)
    assert "field--invalid" in body
    # Previously rendered as BOTH a page notice and a field error.
    assert body.count("Add the state") == 1
    assert "notice--error" not in body


def test_ui_weather_still_shows_a_page_notice_for_a_non_field_error(app):
    """A provider failure has no field, so it must stay a page notice."""

    class FailingProvider:
        name = "failing"
        available = False
        requires_coordinates = False

        def fetch_current(self, location: str) -> dict[str, Any]:
            from app.errors import ProviderNotConfiguredError

            raise ProviderNotConfiguredError()

    app.extensions["weather_provider"] = FailingProvider()
    response = app.test_client().post(
        "/ui/weather", data={"location": "anywhere", "action": "lookup"}
    )
    assert response.status_code == 503
    assert "notice--error" in response.get_data(as_text=True)


def test_saved_snapshot_is_stored_under_the_place_label(nws_client):
    response = nws_client.post(
        "/ui/weather", data={"location": "78701", "action": "save"}
    )
    assert response.status_code == 302
    # A reader filtering their snapshot history should see "Austin, TX",
    # not a coordinate pair they never typed.
    assert "Austin" in response.headers["Location"]

    listed = nws_client.get("/api/weather/snapshots").get_json()["data"]
    assert [row["location"] for row in listed] == ["Austin, TX"]
