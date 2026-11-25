"""Application configuration loaded exclusively from environment variables.

No credential is ever hardcoded here. Missing values fall back to safe,
non-functional defaults so the app always boots without real secrets.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE_VALUES


def _env_str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name) or default)
    except ValueError:
        return default


# Placeholder markers shipped in .env.example. Treated as "not configured" so a
# copied-but-unedited .env cannot trigger real outbound requests.
_PLACEHOLDERS = {
    "",
    "your_weather_api_key_here",
    "your_weather_provider_url_here",
    "replace_with_random_secret",
    "changeme",
}


def _is_placeholder(value: str) -> bool:
    return value.strip().lower() in _PLACEHOLDERS


@dataclass(frozen=True)
class Config:
    """Immutable snapshot of the runtime configuration."""

    env: str = field(default_factory=lambda: _env_str("FLASK_ENV", "development"))
    debug: bool = field(default_factory=lambda: _env_bool("FLASK_DEBUG", False))
    secret_key: str = field(default_factory=lambda: _env_str("SECRET_KEY"))
    database_path: str = field(
        default_factory=lambda: _env_str("DATABASE_PATH", "instance/weather_journal.db")
    )
    weather_api_key: str = field(default_factory=lambda: _env_str("WEATHER_API_KEY"))
    weather_api_base_url: str = field(
        default_factory=lambda: _env_str("WEATHER_API_BASE_URL")
    )
    weather_timeout_seconds: int = field(
        default_factory=lambda: _env_int("WEATHER_TIMEOUT_SECONDS", 10)
    )
    # Optional explicit provider selection ("nws"); empty keeps legacy behaviour.
    weather_provider: str = field(
        default_factory=lambda: _env_str("WEATHER_PROVIDER").lower()
    )
    # NWS (api.weather.gov) is keyless but requires a descriptive User-Agent.
    nws_user_agent: str = field(default_factory=lambda: _env_str("NWS_USER_AGENT"))
    nws_base_url: str = field(
        default_factory=lambda: _env_str("NWS_BASE_URL", "https://api.weather.gov")
    )
    nws_timeout_seconds: int = field(
        default_factory=lambda: _env_int(
            "NWS_TIMEOUT_SECONDS", _env_int("WEATHER_TIMEOUT_SECONDS", 10)
        )
    )
    # Worker-time bound for one whole observation lookup (up to five
    # sequential upstream requests). 0 means "derive it from the per-request
    # timeout", which is what the provider does by default.
    nws_total_timeout_seconds: int = field(
        default_factory=lambda: _env_int("NWS_TOTAL_TIMEOUT_SECONDS", 0)
    )

    @property
    def resolved_database_path(self) -> Path:
        path = Path(self.database_path).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path

    @property
    def weather_provider_configured(self) -> bool:
        """External weather calls stay disabled until both values are real."""
        return not _is_placeholder(self.weather_api_key) and not _is_placeholder(
            self.weather_api_base_url
        )

    @property
    def testing(self) -> bool:
        return self.env.lower() in {"test", "testing"}

    def to_flask_mapping(self) -> dict:
        """Translate into the keys Flask itself consumes."""
        return {
            "ENV": self.env,
            "DEBUG": self.debug,
            "TESTING": self.testing,
            # An ephemeral key keeps dev/test usable; production must set one.
            "SECRET_KEY": self.secret_key or secrets.token_urlsafe(32),
            "DATABASE_PATH": str(self.resolved_database_path),
            "WEATHER_API_KEY": self.weather_api_key,
            "WEATHER_API_BASE_URL": self.weather_api_base_url,
            "WEATHER_TIMEOUT_SECONDS": self.weather_timeout_seconds,
            "WEATHER_PROVIDER_CONFIGURED": self.weather_provider_configured,
            "WEATHER_PROVIDER": self.weather_provider,
            "NWS_USER_AGENT": self.nws_user_agent,
            "NWS_BASE_URL": self.nws_base_url,
            "NWS_TIMEOUT_SECONDS": self.nws_timeout_seconds,
            "NWS_TOTAL_TIMEOUT_SECONDS": self.nws_total_timeout_seconds,
            "JSON_SORT_KEYS": False,
        }


def load_config() -> Config:
    """Read the current environment into a Config instance."""
    return Config()
