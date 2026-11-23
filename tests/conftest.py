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
    "FLASK_ENV",
    "FLASK_DEBUG",
    "SECRET_KEY",
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_weather_journal.db"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _WEATHER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Prevent a developer's local .env from leaking into the test run.
    monkeypatch.setattr("app._load_dotenv", lambda: None, raising=False)


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
