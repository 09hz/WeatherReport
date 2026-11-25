"""Single-command entry point: `python main.py`.

Boots the Flask app, then prints one clickable link to the browser UI as the
last line of output. Werkzeug's own startup banner is suppressed so the link
stays at the bottom of the terminal where the user is looking.

Production still goes through a real WSGI server:
    gunicorn "app:create_app()"
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import socket
import sys
import webbrowser

from app import create_app

#: The browser UI lives under /ui; GET / is the JSON endpoint index.
UI_PATH = "/ui/"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000
#: How many consecutive ports to try before giving up.
PORT_SCAN_LIMIT = 20


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def find_open_port(host: str, port: int, limit: int = PORT_SCAN_LIMIT) -> int:
    """Return `port`, or the next free port after it.

    A stale dev server on 5000 is the most common reason `python main.py`
    fails for someone, and the fix is never interesting. Step past it.
    """
    for candidate in range(port, port + limit):
        if _port_is_free(host, candidate):
            return candidate
    raise SystemExit(
        f"No free port in {port}-{port + limit - 1} on {host}. "
        f"Stop the process using it or pass --port."
    )


def _supports_color(stream) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)()) and os.environ.get(
        "TERM"
    ) not in {"dumb", None}


def _hyperlink(url: str, colorize: bool) -> str:
    """Render `url` as an OSC 8 terminal hyperlink when that is safe.

    Terminals that don't understand OSC 8 ignore the escape and show the bare
    URL, and most of those still auto-link a plain http:// string anyway. Both
    paths leave the user something to click.
    """
    if not colorize:
        return url
    underline_cyan = "\033[4;36m"
    reset = "\033[0m"
    return f"\033]8;;{url}\033\\{underline_cyan}{url}{reset}\033]8;;\033\\"


def banner(url: str, provider: str, colorize: bool) -> str:
    """The block printed last, pointing at the UI."""
    bold = "\033[1m" if colorize else ""
    dim = "\033[2m" if colorize else ""
    reset = "\033[0m" if colorize else ""
    rule = "=" * 58
    return "\n".join(
        [
            "",
            rule,
            f"  {bold}WEATHER REPORT{reset}  weather provider: {provider}",
            rule,
            f"  {dim}Open the app (ctrl/cmd-click or click the link):{reset}",
            "",
            f"    {_hyperlink(url, colorize)}",
            "",
            f"  {dim}Press CTRL+C to stop the server.{reset}",
            rule,
            "",
        ]
    )


#: Werkzeug startup chatter our banner already covers. Werkzeug ANSI-colours
#: these lines, so match after stripping escape sequences.
_WERKZEUG_STARTUP_NOISE = (
    "WARNING: This is a development server",
    "* Running on",
    "Press CTRL+C to quit",
    "* Restarting with",
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class _DropStartupNoise(logging.Filter):
    """Suppress the startup lines so the clickable link is the last output.

    Request logging is untouched: only these banner lines are dropped, and a
    werkzeug release that reworded them just means the banner moves back up
    the screen, not that anything breaks.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        plain = _ANSI_RE.sub("", record.getMessage())
        first_line = plain.splitlines()[0].strip() if plain.strip() else ""
        return not first_line.startswith(_WERKZEUG_STARTUP_NOISE)


def silence_startup_banners() -> None:
    """Stop Flask and Werkzeug printing their own banners above ours."""
    try:
        from flask import cli as flask_cli
    except ImportError:  # pragma: no cover - flask is a hard dependency
        pass
    else:
        flask_cli.show_server_banner = lambda *args, **kwargs: None
    logging.getLogger("werkzeug").addFilter(_DropStartupNoise())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py", description="Run the Weather Report web app."
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("FLASK_HOST", DEFAULT_HOST),
        help=f"Interface to bind (default {DEFAULT_HOST}).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("FLASK_PORT", DEFAULT_PORT)),
        help=f"Port to bind (default {DEFAULT_PORT}; steps forward if taken).",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        dest="open_browser",
        help="Also launch the default browser at the UI.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Force Flask debug mode (auto-reload + tracebacks).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    app = create_app()
    debug = args.debug or bool(app.config.get("DEBUG"))
    port = find_open_port(args.host, args.port)

    # A bound 0.0.0.0 is not a URL anyone can click; point at loopback.
    display_host = "127.0.0.1" if args.host in {"0.0.0.0", "::", ""} else args.host
    url = f"http://{display_host}:{port}{UI_PATH}"

    provider = getattr(
        app.extensions.get("weather_provider"), "name", "unconfigured"
    )

    silence_startup_banners()

    # The reloader re-execs this module, which would print the banner twice and
    # leave the first copy scrolled off. Print only in the serving process.
    is_reload_child = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    if not is_reload_child:
        print(banner(url, provider, _supports_color(sys.stdout)), flush=True)
        if args.open_browser:
            webbrowser.open_new_tab(url)

    try:
        app.run(host=args.host, port=port, debug=debug, use_reloader=debug)
    except KeyboardInterrupt:
        print("\nServer stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
