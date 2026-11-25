"""NWS configuration hardening: a bad NWS_BASE_URL must not stop startup.

Review MINOR 4 (WeatherReport-8sz). app/config.py's contract is that bad
configuration degrades to a safe default so the application always boots.
`build_nws_provider` therefore trust-checks the *configured* base URL and
falls back to the default with a warning, while the hard `ensure_nws_url`
check still rejects URLs that arrive from outside the process.
"""

from __future__ import annotations

import logging

import pytest

from app import create_app
from app.errors import ProviderError
from app.services.nws_provider import DEFAULT_BASE_URL, NwsWeatherProvider, ensure_nws_url
from app.services.weather_provider import build_nws_provider

HOSTILE_URLS = (
    "http://localhost:8080",  # the local-stub case from the review
    "https://api.weather.gov.evil.test",  # suffix-match attempt
    "https://api.weather.gov:8443",  # explicit port
    "https://evil.test/points",  # unrelated host
    "http://api.weather.gov",  # right host, wrong scheme
    "ftp://api.weather.gov",  # unsupported scheme
    "not a url",  # unparseable
    "https://user@api.weather.gov",  # userinfo in netloc
)


def _nws(config: dict) -> NwsWeatherProvider:
    """Build via the config path and narrow to the concrete provider type."""
    provider = build_nws_provider(config)
    assert isinstance(provider, NwsWeatherProvider)
    return provider


# --- A. build_nws_provider degrades instead of raising -----------------------


@pytest.mark.parametrize("bad_url", HOSTILE_URLS)
def test_build_nws_provider_falls_back_on_hostile_base_url(bad_url, caplog):
    with caplog.at_level(logging.WARNING, logger="app.services.weather_provider"):
        provider = _nws({"NWS_BASE_URL": bad_url})

    assert provider._base_url == DEFAULT_BASE_URL
    assert any(
        "NWS_BASE_URL" in record.message or "NWS_BASE_URL" in record.getMessage()
        for record in caplog.records
    ), "the discarded value should be reported at WARNING"


def test_build_nws_provider_keeps_a_valid_base_url(caplog):
    with caplog.at_level(logging.WARNING, logger="app.services.weather_provider"):
        provider = _nws({"NWS_BASE_URL": "https://api.weather.gov/"})

    assert provider._base_url == DEFAULT_BASE_URL  # trailing slash stripped
    assert not caplog.records, "a good URL must not warn"


def test_build_nws_provider_accepts_a_sub_path_on_the_allowed_host():
    provider = _nws({"NWS_BASE_URL": "https://api.weather.gov/alt"})
    assert provider._base_url == "https://api.weather.gov/alt"


@pytest.mark.parametrize("empty", ["", None, "   "])
def test_build_nws_provider_uses_default_when_unset(empty):
    provider = _nws({"NWS_BASE_URL": empty})
    assert provider._base_url == DEFAULT_BASE_URL


def test_build_nws_provider_falls_back_on_bad_timeout(caplog):
    with caplog.at_level(logging.WARNING, logger="app.services.weather_provider"):
        provider = _nws({"NWS_TIMEOUT_SECONDS": "soon"})

    assert provider._timeout == 10
    assert any("NWS_TIMEOUT_SECONDS" in r.getMessage() for r in caplog.records)


def test_build_nws_provider_keeps_a_valid_timeout():
    assert _nws({"NWS_TIMEOUT_SECONDS": "25"})._timeout == 25


# --- B. create_app still boots ----------------------------------------------


@pytest.mark.parametrize("bad_url", HOSTILE_URLS)
def test_create_app_boots_with_a_hostile_base_url(bad_url, db_path, monkeypatch):
    monkeypatch.setenv("WEATHER_PROVIDER", "nws")
    monkeypatch.setenv("NWS_BASE_URL", bad_url)

    app = create_app({"DATABASE_PATH": str(db_path)})

    provider = app.extensions["weather_provider"]
    assert provider.name == "nws"
    assert provider._base_url == DEFAULT_BASE_URL


def test_create_app_health_is_reachable_with_a_hostile_base_url(db_path, monkeypatch):
    """The regression: startup used to raise, so no route was reachable at all."""
    monkeypatch.setenv("WEATHER_PROVIDER", "nws")
    monkeypatch.setenv("NWS_BASE_URL", "http://localhost:8080")

    app = create_app({"DATABASE_PATH": str(db_path)})
    response = app.test_client().get("/health")

    assert response.status_code in (200, 503)


# --- C. externally supplied URLs are still hard-rejected ---------------------


@pytest.mark.parametrize("bad_url", HOSTILE_URLS)
def test_ensure_nws_url_still_rejects_untrusted_urls(bad_url):
    """The SSRF guard is unchanged; only the config path is lenient."""
    with pytest.raises(ProviderError):
        ensure_nws_url(bad_url)


@pytest.mark.parametrize("bad_url", HOSTILE_URLS)
def test_provider_constructor_still_rejects_an_explicit_bad_base_url(bad_url):
    """Direct construction is a programming error, not configuration drift."""
    with pytest.raises(ProviderError):
        NwsWeatherProvider(base_url=bad_url)
