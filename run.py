"""Development entry point: `python run.py`.

Use a WSGI server (gunicorn/waitress) in production, e.g.
    gunicorn "app:create_app()"
"""

from __future__ import annotations

import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(
        host=os.environ.get("FLASK_HOST", "127.0.0.1"),
        port=int(os.environ.get("FLASK_PORT", "5000")),
        debug=app.config["DEBUG"],
    )
