"""UI route tests (UI-4, WeatherReport-cd8).

Covers the pages delivered by UI-2 and UI-3: every route renders HTML, the
write flows round-trip, and every error path renders as a *page* rather than a
JSON envelope or a traceback.

Network safety: `conftest.py` scrubs provider credentials for every test, so
the app-wide provider is the unconfigured one and a lookup can only raise
ProviderNotConfiguredError. Tests that need a successful lookup install a fake
provider object on `app.extensions`; nothing here can reach the network, which
is also why the suite passes with both proxy variables aimed at a dead port.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from app.services import journal_service

JSON_ENVELOPE = re.compile(r'^\s*\{\s*"error"')


class FakeProvider:
    """Stands in for a configured weather provider.

    Returns a snapshot whose measurements are all None except the location, so
    the null-rendering rule is exercised on the happy path rather than only on
    an error path.
    """

    name = "fake"
    available = True

    def __init__(self, **overrides: Any) -> None:
        self.overrides = overrides
        self.calls: list[str] = []

    def fetch_current(self, location: str) -> dict[str, Any]:
        self.calls.append(location)
        snapshot = {
            "location": location,
            "observed_at": None,
            "temperature_c": None,
            "humidity": None,
            "wind_speed_kph": None,
            "conditions": None,
            "condition_code": None,
            "provider": self.name,
            "raw_response": {"secret": "must not be rendered"},
        }
        snapshot.update(self.overrides)
        return snapshot


@pytest.fixture
def configured(app):
    """Install a working provider for the duration of one test."""
    provider = FakeProvider()
    app.extensions["weather_provider"] = provider
    return provider


def html(response) -> str:
    assert response.content_type.startswith("text/html"), response.content_type
    body = response.get_data(as_text=True)
    assert not JSON_ENVELOPE.match(body), "an error envelope leaked into a page"
    assert "Traceback" not in body
    return body


def make_entry(connection, **overrides: Any) -> dict[str, Any]:
    fields = {
        "title": "Storm over the bay",
        "entry_date": "2026-11-20",
        "location": "Boston, MA",
        "description": "Sideways rain all afternoon.",
        "weather_snapshot_id": None,
    }
    fields.update(overrides)
    return journal_service.create_entry(connection, **fields)


# --------------------------------------------------------------------------- #
# every page renders
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "/ui/",
        "/ui/weather",
        "/ui/weather/snapshots",
        "/ui/journal",
        "/ui/journal/new",
    ],
)
def test_page_renders_as_html(client, path):
    response = client.get(path)
    assert response.status_code == 200
    body = html(response)
    assert 'id="main"' in body
    assert 'class="nav"' in body


def test_root_redirects_to_ui_and_api_stays_json(client):
    """The UI owns /, so the JSON listing moved to /api."""
    assert client.get("/").status_code == 302
    assert client.get("/api").is_json


# --------------------------------------------------------------------------- #
# weather: lookup
# --------------------------------------------------------------------------- #


def test_get_lookup_page_does_not_call_the_provider(client, configured):
    """Navigating must not spend a provider call or a rate-limit slot."""
    client.get("/ui/weather?location=Boston")
    assert configured.calls == []


def test_lookup_renders_every_field_and_nulls_as_placeholder(client, configured):
    response = client.post("/ui/weather", data={"location": "Boston, MA"})
    assert response.status_code == 200
    body = html(response)
    assert configured.calls == ["Boston, MA"]
    assert "temperature_c".replace("_", " ") in body
    assert "placeholder" in body
    assert ">None<" not in body
    assert "must not be rendered" not in body  # raw_response stays internal


def test_lookup_without_a_location_renders_a_field_error(client, configured):
    response = client.post("/ui/weather", data={"location": "   "})
    assert response.status_code == 400
    body = html(response)
    assert "field--invalid" in body
    assert "field__error" in body
    assert configured.calls == []


def test_lookup_without_a_provider_renders_a_notice_not_a_traceback(client):
    """The default test environment has no credentials."""
    response = client.post("/ui/weather", data={"location": "Boston, MA"})
    assert response.status_code == 503
    body = html(response)
    assert "notice--error" in body


def test_save_snapshot_redirects_then_lists_it(client, configured):
    response = client.post(
        "/ui/weather", data={"location": "Boston, MA", "action": "save"}
    )
    assert response.status_code in (302, 303)
    assert "/ui/weather/snapshots" in response.headers["Location"]

    listed = client.get(response.headers["Location"])
    assert listed.status_code == 200
    assert "Boston, MA" in html(listed)


def test_save_snapshot_without_a_provider_is_a_page_not_a_crash(client):
    response = client.post(
        "/ui/weather", data={"location": "Boston, MA", "action": "save"}
    )
    assert response.status_code == 503
    assert "notice--error" in html(response)


# --------------------------------------------------------------------------- #
# weather: snapshot list
# --------------------------------------------------------------------------- #


def test_snapshot_list_is_empty_but_not_broken(client):
    body = html(client.get("/ui/weather/snapshots"))
    assert "table__empty" in body


def test_snapshot_list_paginates_and_reports_totals(client, configured, app):
    from app.database.connection import connect
    from app.services import weather_service

    connection = connect(app.config["DATABASE_PATH"])
    for index in range(5):
        weather_service.save_snapshot(
            connection, configured.fetch_current(f"City {index}")
        )
    connection.close()

    first = html(client.get("/ui/weather/snapshots?limit=2&offset=0"))
    assert "of 5" in first or "5" in first
    assert "pager" in first
    # at the first page the previous link must be inert
    assert "pager__link--disabled" in first

    last = html(client.get("/ui/weather/snapshots?limit=2&offset=4"))
    assert "pager__link--disabled" in last


def test_snapshot_list_filters_by_location(client, configured, app):
    from app.database.connection import connect
    from app.services import weather_service

    connection = connect(app.config["DATABASE_PATH"])
    weather_service.save_snapshot(connection, configured.fetch_current("Boston, MA"))
    weather_service.save_snapshot(connection, configured.fetch_current("Austin, TX"))
    connection.close()

    body = html(client.get("/ui/weather/snapshots?location=Boston, MA"))
    assert "Boston" in body
    assert "Austin" not in body


def test_snapshot_list_rejects_a_bad_limit_as_a_page(client):
    response = client.get("/ui/weather/snapshots?limit=banana")
    assert response.status_code == 400
    body = html(response)
    assert "notice--error" in body
    # the chrome and the filter form survive, so the reader can recover
    assert 'class="nav"' in body


# --------------------------------------------------------------------------- #
# journal: list
# --------------------------------------------------------------------------- #


def test_journal_list_shows_entries(client, connection):
    make_entry(connection, title="Hail in April")
    assert "Hail in April" in html(client.get("/ui/journal"))


def test_journal_search_and_location_filter_work_together(client, connection):
    # (date, location) is unique per entry, so vary the date as well
    make_entry(connection, title="Hail in April", location="Boston, MA",
               entry_date="2026-04-01")
    make_entry(connection, title="Hail in April", location="Austin, TX",
               entry_date="2026-04-01")
    make_entry(connection, title="Clear skies", location="Boston, MA",
               entry_date="2026-04-02")

    body = html(client.get("/ui/journal?q=Hail&location=Boston, MA"))
    assert "Boston, MA" in body
    assert "Austin" not in body
    assert "Clear skies" not in body


def test_journal_filters_survive_pagination_links(client, connection):
    for index in range(4):
        make_entry(
            connection,
            title=f"Hail {index}",
            location="Boston, MA",
            entry_date=f"2026-04-0{index + 1}",
        )

    body = html(client.get("/ui/journal?q=Hail&location=Boston, MA&limit=2"))
    # the next link must carry both filters, or page 2 silently widens
    next_links = re.findall(r'href="([^"]*offset=2[^"]*)"', body)
    assert next_links, body
    assert any("q=Hail" in link and "location=Boston" in link for link in next_links)


def test_journal_list_rejects_a_bad_offset_as_a_page(client):
    response = client.get("/ui/journal?offset=-1")
    assert response.status_code == 400
    assert "notice--error" in html(response)


# --------------------------------------------------------------------------- #
# journal: create / edit / delete round trip
# --------------------------------------------------------------------------- #


FORM = {
    "title": "First frost",
    "date": "2026-11-21",
    "location": "Boston, MA",
    "description": "Ice on every windshield.",
    "weather_snapshot_id": "",
}


def test_create_round_trips_through_the_ui(client):
    response = client.post("/ui/journal/new", data=FORM)
    assert response.status_code in (302, 303)
    assert "/ui/journal" in response.headers["Location"]
    assert "First frost" in html(client.get("/ui/journal"))


def test_edit_round_trips_through_the_ui(client, connection):
    entry = make_entry(connection)

    form = client.get(f"/ui/journal/{entry['id']}/edit")
    assert form.status_code == 200
    assert "Storm over the bay" in html(form)

    response = client.post(
        f"/ui/journal/{entry['id']}/edit", data={**FORM, "title": "Renamed"}
    )
    assert response.status_code in (302, 303)
    assert "Renamed" in html(client.get("/ui/journal"))


def test_delete_round_trips_through_the_ui(client, connection):
    entry = make_entry(connection)

    confirm = client.get(f"/ui/journal/{entry['id']}/delete")
    assert confirm.status_code == 200
    assert "Storm over the bay" in html(confirm)

    response = client.post(f"/ui/journal/{entry['id']}/delete")
    assert response.status_code in (302, 303)
    assert "Storm over the bay" not in html(client.get("/ui/journal"))


def test_delete_of_a_missing_entry_renders_a_notice(client):
    response = client.post("/ui/journal/4242/delete")
    assert response.status_code == 404
    assert "notice--error" in html(response)


def test_edit_of_a_missing_entry_renders_a_notice(client):
    response = client.get("/ui/journal/4242/edit")
    assert response.status_code == 404
    assert "notice--error" in html(response)


# --------------------------------------------------------------------------- #
# journal: validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "x" * 201),
        ("description", "x" * 10_001),
        ("date", "21-11-2026"),
        ("location", ""),
        ("weather_snapshot_id", "0"),
    ],
)
def test_invalid_field_is_marked_and_input_is_preserved(client, field, value):
    payload = {**FORM, field: value, "description": "Keep this text."}
    if field == "description":
        payload["description"] = value

    response = client.post("/ui/journal/new", data=payload)
    assert response.status_code == 400
    body = html(response)
    assert "field--invalid" in body
    assert "field__error" in body
    if field != "description":
        assert "Keep this text." in body, "the user's typing was discarded"


def test_a_rejected_create_writes_nothing(client, connection):
    client.post("/ui/journal/new", data={**FORM, "date": "not-a-date"})
    assert journal_service.count_entries(connection) == 0


def test_duplicate_date_and_location_renders_the_conflict(client, connection):
    """create_entry raises ConflictError (409) for a repeated (date, location).

    The contract moved form-level failures inside the form: a conflict is not
    attributable to one control, but it IS about this form, so it renders as
    `.form__error` above the first field rather than as a page-level notice.
    This test asserted `notice--error` before UI-6 and was updated with the
    contract, not relaxed — the status code, the message and the write count
    are all still asserted.
    """
    make_entry(connection, entry_date=FORM["date"], location=FORM["location"])

    response = client.post("/ui/journal/new", data=FORM)
    assert response.status_code == 409
    body = html(response)
    assert "form__error" in body
    assert "notice--error" not in body, "a form failure must not become a page notice"
    assert "already exists" in body
    # the message belongs inside the form, not above it
    assert body.index('class="form"') < body.index("form__error")
    assert journal_service.count_entries(connection) == 1


def test_a_field_failure_stays_on_the_field_and_not_in_the_form_error(client):
    """The two error regions must not collapse into one."""
    body = html(client.post("/ui/journal/new", data={**FORM, "date": "nope"}))
    assert "field--invalid" in body
    assert "form__error" not in body
