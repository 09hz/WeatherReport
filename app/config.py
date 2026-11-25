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
    weather_cache_ttl_seconds: int = field(
        default_factory=lambda: _env_int("WEATHER_CACHE_TTL_SECONDS", 300)
    )
    weather_cache_max_entries: int = field(
        default_factory=lambda: _env_int("WEATHER_CACHE_MAX_ENTRIES", 256)
    )
    request_log_enabled: bool = field(
        default_factory=lambda: _env_bool("REQUEST_LOG_ENABLED", True)
    )
    request_log_path: str = field(
        default_factory=lambda: _env_str("REQUEST_LOG_PATH", ".beads/logs/app.log")
    )
    rate_limit_enabled: bool = field(
        default_factory=lambda: _env_bool("RATE_LIMIT_ENABLED", True)
    )
    rate_limit_requests: int = field(
        default_factory=lambda: _env_int("RATE_LIMIT_REQUESTS", 100)
    )
    rate_limit_window_seconds: int = field(
        default_factory=lambda: _env_int("RATE_LIMIT_WINDOW_SECONDS", 60)
    )

    @property
    def resolved_request_log_path(self) -> Path:
        path = Path(self.request_log_path).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path

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
            "WEATHER_CACHE_TTL_SECONDS": self.weather_cache_ttl_seconds,
            "WEATHER_CACHE_MAX_ENTRIES": self.weather_cache_max_entries,
            # Auditing and throttling are process-wide side effects, so a
            # test run opts in explicitly rather than inheriting them.
            "REQUEST_LOG_ENABLED": self.request_log_enabled and not self.testing,
            "REQUEST_LOG_PATH": str(self.resolved_request_log_path),
            "RATE_LIMIT_ENABLED": self.rate_limit_enabled and not self.testing,
            "RATE_LIMIT_REQUESTS": self.rate_limit_requests,
            "RATE_LIMIT_WINDOW_SECONDS": self.rate_limit_window_seconds,
            "JSON_SORT_KEYS": False,
        }


def load_config() -> Config:
    """Read the current environment into a Config instance."""
    return Config()
