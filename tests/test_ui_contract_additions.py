"""Tests for the UI-6 contract additions (WeatherReport-b8i).

UI-6 added three classes and three explicit decisions to the frozen contract.
These tests assert the parts a reader cannot verify by eye: that the markup
carries the accessibility mechanism the contract demands and not just the
class, that the stylesheet actually implements the new names, and that the
decisions recorded as "this workaround is the intended answer" stay true.

Written by the orchestrator, not by the workers that implemented them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services import journal_service

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "docs" / "UI_CLASS_CONTRACT.md"
CSS = (ROOT / "app" / "static" / "css" / "brutalist.css").read_text()
NEW_CLASSES = ("table-scroll", "table__cell--actions", "btn--disabled", "form__error")


def make_entry(connection, **overrides):
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
# the contract and the stylesheet agree
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", NEW_CLASSES)
def test_new_class_is_in_the_contract(name):
    assert f"| `{name}` |" in CONTRACT.read_text()


@pytest.mark.parametrize("name", NEW_CLASSES)
def test_new_class_is_styled(name):
    """UI-1's standing rule: every contracted class is styled, used or not."""
    assert f".{name}" in CSS


def test_the_stylesheet_still_honours_the_prohibitions():
    """UI-6 was additive; it must not have smuggled in colour or motion."""
    body = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    for forbidden in (
        "border-radius", "box-shadow", "transition", "animation", "gradient",
        "@import", "http",
    ):
        assert forbidden not in body


# --------------------------------------------------------------------------- #
# table-scroll: the overflow must be reachable, not just visually scrollable
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["/ui/journal", "/ui/weather/snapshots"])
def test_wide_table_is_wrapped_in_a_reachable_scroll_region(client, path):
    body = client.get(path).get_data(as_text=True)
    assert "table-scroll" in body

    wrapper = re.search(r'<div class="table-scroll"[^>]*>', body)
    assert wrapper, "table-scroll must be the wrapper element"
    opening = wrapper.group(0)
    # a div that scrolls but cannot be focused is unreachable by keyboard
    assert 'tabindex="0"' in opening
    # and an unlabelled focus stop is a trap the reader cannot identify
    assert 'role="region"' in opening
    assert "aria-label=" in opening


@pytest.mark.parametrize("path", ["/ui/journal", "/ui/weather/snapshots"])
def test_the_scroll_region_actually_wraps_the_table(client, path):
    body = client.get(path).get_data(as_text=True)
    wrapper_at = body.index('class="table-scroll"')
    table_at = body.index('<table class="table"', wrapper_at)
    close_at = body.index("</div>", wrapper_at)
    assert wrapper_at < table_at < close_at


def test_tbody_stays_unclassed_as_the_contract_decided(client, connection):
    """An explicit UI-6 decision: no table__body hook."""
    make_entry(connection)
    body = client.get("/ui/journal").get_data(as_text=True)
    assert "<tbody>" in body
    assert "table__body" not in body


# --------------------------------------------------------------------------- #
# table__cell--actions
# --------------------------------------------------------------------------- #


def test_row_controls_live_in_an_actions_cell(client, connection):
    entry = make_entry(connection)
    body = client.get("/ui/journal").get_data(as_text=True)
    assert "table__cell--actions" in body

    cell = re.search(
        r'<td class="[^"]*table__cell--actions[^"]*">(.*?)</td>', body, re.S
    )
    assert cell, "the actions cell must be a td carrying the modifier"
    assert f"/ui/journal/{entry['id']}/edit" in cell.group(1)
    assert f"/ui/journal/{entry['id']}/delete" in cell.group(1)


def test_the_actions_column_header_matches_its_cells(client, connection):
    make_entry(connection)
    body = client.get("/ui/journal").get_data(as_text=True)
    assert body.count("table__cell--actions") >= 2, "header and body cell"


# --------------------------------------------------------------------------- #
# inert controls: the class must never be the only mechanism
# --------------------------------------------------------------------------- #


def _inert_markup(body: str) -> list[str]:
    return re.findall(r"<(?:a|button|span)[^>]*--disabled[^>]*>", body)


@pytest.mark.parametrize(
    "path",
    ["/ui/journal?limit=2", "/ui/weather/snapshots?limit=2", "/ui/journal", "/ui/"],
)
def test_no_inert_control_is_clickable(client, connection, path):
    """btn--disabled and pager__link--disabled both carry this rule."""
    for index in range(3):
        make_entry(
            connection, title=f"Entry {index}", entry_date=f"2026-04-0{index + 1}"
        )

    body = client.get(path).get_data(as_text=True)
    for element in _inert_markup(body):
        if element.startswith("<a"):
            assert "href" not in element, f"inert anchor is still clickable: {element}"
            assert 'aria-disabled="true"' in element
        elif element.startswith("<button"):
            assert "disabled" in element
        # a <span> is inert by construction: the contract's preferred form


def test_the_first_page_has_an_inert_previous_end(client, connection):
    for index in range(3):
        make_entry(
            connection, title=f"Entry {index}", entry_date=f"2026-04-0{index + 1}"
        )
    body = client.get("/ui/journal?limit=2").get_data(as_text=True)
    assert "pager__link--disabled" in body
    # the contract now requires a span, never an <a> with a dead href
    assert re.search(r'<span class="[^"]*pager__link--disabled', body)
    assert not re.search(r'<a[^>]*pager__link--disabled', body)


# --------------------------------------------------------------------------- #
# the two error regions stay distinct (see also tests/test_ui_pages.py)
# --------------------------------------------------------------------------- #


def test_a_form_level_failure_renders_inside_the_form(client, connection):
    make_entry(connection, entry_date="2026-11-21", location="Boston, MA")
    response = client.post(
        "/ui/journal/new",
        data={
            "title": "First frost",
            "date": "2026-11-21",
            "location": "Boston, MA",
            "description": "Ice.",
            "weather_snapshot_id": "",
        },
    )
    assert response.status_code == 409
    body = response.get_data(as_text=True)

    form_at = body.index('<form class="form"')
    error_at = body.index('class="form__error"')
    fields_at = body.index('class="field')
    assert form_at < error_at < fields_at, "form__error belongs above the first field"
    assert 'role="alert"' in body[error_at - 60 : error_at + 60]


def test_a_page_level_failure_is_still_a_notice(client):
    """UI-6 must not have collapsed notice--error into form__error."""
    body = client.get("/ui/journal?offset=-1").get_data(as_text=True)
    assert "notice--error" in body
    assert "form__error" not in body
