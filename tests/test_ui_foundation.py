"""UI-0 foundation tests (WeatherReport-kvj).

Scope is deliberately the shell only: the blueprint is mounted, the layout
renders, the stylesheet is served, and mounting the UI did not change the JSON
contract at GET /. Per-page coverage belongs to UI-4 (WeatherReport-cd8).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CONTRACT = Path(__file__).resolve().parent.parent / "docs" / "UI_CLASS_CONTRACT.md"
STYLESHEET = (
    Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "brutalist.css"
)


def test_root_redirects_to_the_ui(client):
    """The root now belongs to the UI; the JSON listing lives at /api."""
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/ui/")

    api = client.get("/api")
    assert api.is_json
    assert api.get_json()["name"] == "weather-journal"


def test_ui_root_renders_html(client):
    response = client.get("/ui/")
    assert response.status_code == 200
    assert response.content_type.startswith("text/html")
    body = response.get_data(as_text=True)
    assert "<!DOCTYPE html>" in body
    assert 'class="page"' in body
    assert "/static/css/brutalist.css" in body


def test_ui_root_without_trailing_slash_redirects(client):
    assert client.get("/ui").status_code in (301, 308)


@pytest.mark.parametrize(
    "path",
    ["/ui/", "/ui/weather", "/ui/weather/snapshots", "/ui/journal"],
)
def test_every_foundation_route_renders(client, path):
    response = client.get(path)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    # the shared chrome, so a page can never render without navigation
    assert 'class="masthead"' in body
    assert 'class="nav"' in body
    assert 'id="main"' in body


def test_nav_marks_exactly_one_link_active(client):
    body = client.get("/ui/journal").get_data(as_text=True)
    assert body.count("nav__link--active") == 1


def test_unconfigured_provider_is_announced_not_hidden(client):
    """The test env has no credentials, so the warning must be visible."""
    body = client.get("/ui/").get_data(as_text=True)
    assert "notice--warn" in body
    assert "No weather provider configured" in body


def test_stylesheet_is_served(client):
    response = client.get("/static/css/brutalist.css")
    assert response.status_code == 200
    assert "css" in response.content_type


def _css_without_comments() -> str:
    """Strip /* ... */ so the file's own rationale is not read as a violation."""
    return re.sub(r"/\*.*?\*/", "", STYLESHEET.read_text(), flags=re.S)


@pytest.mark.parametrize(
    "forbidden",
    ["border-radius", "box-shadow", "transition", "animation", "gradient",
     "@import", "http"],
)
def test_stylesheet_honours_the_brutalist_constraints(forbidden):
    """UI-1 replaces this file; the constraints survive the replacement."""
    assert forbidden not in _css_without_comments()


def _template_classes() -> set[str]:
    templates = (Path(__file__).resolve().parent.parent / "app" / "templates").rglob(
        "*.html"
    )
    used: set[str] = set()
    for template in templates:
        # Drop Jinja statements/expressions first: a conditional class list
        # ({% if %} ... {% endif %}) otherwise leaks keywords into the tokens.
        markup = re.sub(r"\{[{%#].*?[}%#]\}", " ", template.read_text(), flags=re.S)
        for attr in re.findall(r'class="([^"]*)"', markup):
            used.update(attr.split())
    return used


def test_templates_only_use_contracted_class_names():
    """The contract is the shared vocabulary; drift breaks parallel UI work."""
    contracted = set(re.findall(r"^\| `([a-z0-9_-]+)` \|", CONTRACT.read_text(), re.M))
    assert "panel" in contracted, "contract table failed to parse"
    assert _template_classes() - contracted == set()
