"""Shared pytest fixtures.

Each test gets a fresh app bound to a temporary SQLite file, and the
environment is scrubbed of weather credentials so no outbound request is
possible unless a test explicitly injects a fake provider.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import create_app  # noqa: E402
from app.database.connection import connect  # noqa: E402

_WEATHER_ENV_VARS = (
    "WEATHER_API_KEY",
    "WEATHER_API_BASE_URL",
    "DATABASE_PATH",
    "DATABASE_URL",
    "FLASK_ENV",
    "FLASK_DEBUG",
    "SECRET_KEY",
    "WEATHER_PROVIDER",
    "NWS_USER_AGENT",
    "NWS_BASE_URL",
    "NWS_TIMEOUT_SECONDS",
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_weather_journal.db"


@pytest.fixture(autouse=True)
def _isolate_weather_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub provider configuration from the environment for EVERY test.

    Autouse on purpose. Scrubbing only inside `clean_env` left the tests that
    build a `Config()` directly (rather than through the `app` fixture)
    reading the developer's shell, so `WEATHER_PROVIDER=nws` in an
    interactive session turned a green suite red. Isolation must not be
    opt-in. A test that wants one of these variables set still sets it with
    monkeypatch, which runs after this fixture.
    """
    for name in _WEATHER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Prevent a developer's local .env from leaking into the test run.
    monkeypatch.setattr("app._load_dotenv", lambda: None, raising=False)


@pytest.fixture
def clean_env(_isolate_weather_env) -> None:
    """Kept for explicitness where a test documents its env isolation."""
    return None


@pytest.fixture
def app(clean_env, db_path: Path):
    application = create_app(
        {"TESTING": True, "ENV": "testing", "DATABASE_PATH": str(db_path)}
    )
    yield application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def connection(db_path: Path):
    """Direct connection to the same database the app uses."""
    conn = connect(db_path)
    yield conn
    conn.close()
