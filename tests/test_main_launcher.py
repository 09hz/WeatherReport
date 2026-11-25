"""Tests for the `python main.py` launcher.

The launcher is exercised without ever binding a long-lived server: the only
parts worth asserting are port selection, the URL shown to the user, and the
log filtering that keeps that URL as the last line of output.
"""

from __future__ import annotations

import logging
import socket

import pytest

import main


def test_ui_path_points_at_the_browser_ui():
    # GET / is the JSON endpoint index, so the printed link must not be bare.
    assert main.UI_PATH == "/ui/"


def test_find_open_port_returns_the_requested_port_when_free():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    assert main.find_open_port("127.0.0.1", free_port) == free_port


def test_find_open_port_steps_past_a_taken_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]

        chosen = main.find_open_port("127.0.0.1", port)

    assert chosen > port


def test_find_open_port_gives_up_with_a_clear_message():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]

        with pytest.raises(SystemExit) as excinfo:
            main.find_open_port("127.0.0.1", port, limit=1)

    assert "--port" in str(excinfo.value)


def test_banner_contains_a_plain_clickable_url_without_color():
    text = main.banner("http://127.0.0.1:5000/ui/", "nws", colorize=False)
    assert "http://127.0.0.1:5000/ui/" in text
    assert "nws" in text
    assert "\033" not in text


def test_banner_wraps_the_url_in_an_osc8_hyperlink_when_colorized():
    text = main.banner("http://127.0.0.1:5000/ui/", "nws", colorize=True)
    assert "\033]8;;http://127.0.0.1:5000/ui/" in text


@pytest.mark.parametrize(
    "message",
    [
        "WARNING: This is a development server. Do not use it in production.",
        " * Running on http://127.0.0.1:5000",
        "Press CTRL+C to quit",
        " * Restarting with stat",
        # Werkzeug ANSI-colours these lines; stripping must happen first.
        "\033[31m\033[1mWARNING: This is a development server.\033[0m",
        "\033[33mPress CTRL+C to quit\033[0m",
        # The dev-server warning and the addresses arrive as one record.
        "WARNING: This is a development server.\n * Running on http://x",
    ],
)
def test_startup_noise_is_dropped(message):
    record = logging.LogRecord("werkzeug", logging.INFO, "", 0, message, None, None)
    assert main._DropStartupNoise().filter(record) is False


@pytest.mark.parametrize(
    "message",
    [
        '127.0.0.1 - - [01/Jan/2026 00:00:00] "GET /ui/ HTTP/1.1" 200 -',
        "Error on request:",
        "",
    ],
)
def test_request_logging_survives_the_filter(message):
    record = logging.LogRecord("werkzeug", logging.INFO, "", 0, message, None, None)
    assert main._DropStartupNoise().filter(record) is True


def test_silence_startup_banners_disables_the_flask_banner():
    from flask import cli as flask_cli

    original = flask_cli.show_server_banner
    werkzeug_logger = logging.getLogger("werkzeug")
    filters_before = list(werkzeug_logger.filters)
    try:
        main.silence_startup_banners()
        # Signature varies across Flask versions; it must swallow anything.
        banner_fn = flask_cli.show_server_banner
        assert banner_fn("a", "b", "c", "d") is None  # type: ignore[call-arg]
        assert any(
            isinstance(f, main._DropStartupNoise) for f in werkzeug_logger.filters
        )
    finally:
        flask_cli.show_server_banner = original
        werkzeug_logger.filters = filters_before


def test_parse_args_defaults_to_loopback_5000(monkeypatch):
    monkeypatch.delenv("FLASK_HOST", raising=False)
    monkeypatch.delenv("FLASK_PORT", raising=False)
    args = main.parse_args([])
    assert (args.host, args.port) == (main.DEFAULT_HOST, main.DEFAULT_PORT)
    assert args.open_browser is False
    assert args.debug is False


def test_parse_args_reads_flask_env_vars(monkeypatch):
    monkeypatch.setenv("FLASK_HOST", "0.0.0.0")
    monkeypatch.setenv("FLASK_PORT", "8080")
    args = main.parse_args([])
    assert (args.host, args.port) == ("0.0.0.0", 8080)


def test_parse_args_flags_override_the_environment(monkeypatch):
    monkeypatch.setenv("FLASK_PORT", "8080")
    args = main.parse_args(["--port", "9001", "--open", "--debug"])
    assert args.port == 9001
    assert args.open_browser is True
    assert args.debug is True


@pytest.mark.parametrize("bind_host", ["0.0.0.0", "::", ""])
def test_wildcard_binds_are_shown_as_loopback(monkeypatch, capsys, bind_host):
    """A link to 0.0.0.0 is not clickable; the banner must show 127.0.0.1."""
    monkeypatch.setattr(main, "find_open_port", lambda host, port: port)

    served = {}

    def fake_run(self, host, port, debug, use_reloader):  # noqa: ANN001
        served.update(host=host, port=port)

    from flask import Flask

    monkeypatch.setattr(Flask, "run", fake_run)

    assert main.main(["--host", bind_host, "--port", "5999"]) == 0

    printed = capsys.readouterr().out
    assert "http://127.0.0.1:5999/ui/" in printed
    assert served == {"host": bind_host, "port": 5999}


def test_banner_is_suppressed_in_the_reloader_child(monkeypatch, capsys):
    """The reloader re-execs this module; printing twice scrolls the link off."""
    monkeypatch.setenv("WERKZEUG_RUN_MAIN", "true")
    monkeypatch.setattr(main, "find_open_port", lambda host, port: port)

    from flask import Flask

    monkeypatch.setattr(Flask, "run", lambda *a, **k: None)

    assert main.main(["--port", "5998"]) == 0
    assert capsys.readouterr().out == ""
