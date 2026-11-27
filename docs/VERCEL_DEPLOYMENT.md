# Deploying WeatherReport on Vercel

## Why the first deploy failed

> Found main.py but it does not define a top-level "app" Flask instance.

Vercel's Python framework detection scans a fixed set of filenames
(`app.py`, `index.py`, `main.py`, `server.py`, `wsgi.py`, `asgi.py`) for a
**module-level** Flask instance. This project uses an application factory
(`app/__init__.py::create_app`), and `main.py` is a dev launcher that builds
the app inside `main()` — so there is nothing at module level to find.

`run.py` already exposes `app = create_app()` at import time, so the fix is to
point the platform at it. `pyproject.toml`:

```toml
[tool.vercel]
entrypoint = "run:app"
```

That file intentionally has **no `[project]` table**, so dependency resolution
stays on `requirements.txt`. Adding one would make the build try to install
this repo as a package, and setuptools auto-discovery fails on the flat
`app/ tests/ scripts/ src/` layout.

## The second failure the entrypoint fix would have exposed

`create_app()` calls `database.init_app(app)`, which opens SQLite and runs the
schema DDL **at import time**. A Vercel deployment's filesystem is read-only
except `/tmp`, so a cold start would have raised before serving anything.
Request logging had the same problem (`.beads/logs/app.log`).

`app/config.py` therefore derives serverless-safe defaults from the `VERCEL`
environment variable that the platform sets automatically:

| Setting | Local default | Default when `VERCEL` is set |
| --- | --- | --- |
| `DATABASE_PATH` | `instance/weather_journal.db` | `/tmp/weather_journal.db` |
| `REQUEST_LOG_PATH` | `.beads/logs/app.log` | `/tmp/weather_report_app.log` |

Explicit environment variables still win over both.

## Storage: /tmp is a demo, not persistence

`/tmp` on a serverless function is per-instance and ephemeral. Journal entries
written there vanish when the instance recycles, and two concurrent instances
do not see each other's data.

For durable storage set **`DATABASE_URL`** to a Postgres connection string
(Vercel Postgres, Neon, Supabase — anything that speaks Postgres). The app then
uses the Postgres backend instead of SQLite; see `app/database/postgres.py` for
the dialect seam. SQLite remains the default for local development, and nothing
in `app/services` changes between backends.

```bash
vercel env add DATABASE_URL production   # postgresql://user:pw@host/db?sslmode=require
vercel env add SECRET_KEY production     # openssl rand -base64 32
```

Verify after deploy:

```bash
curl -s https://<deployment>/health | python -m json.tool
# {"database": {"connected": true, "missing_tables": []}, "status": "ok", ...}
```

`/health` returns 503 with the missing tables listed when storage is wrong, so
it is the single check worth running first.

## Environment variables

Non-secret runtime values are committed in `vercel.json` so a fresh deploy is
functional without dashboard clicks:

| Variable | Value | Why |
| --- | --- | --- |
| `WEATHER_PROVIDER` | `nws` | Without it `build_provider` returns the unconfigured provider and every weather request fails. NWS needs no API key. |
| `NWS_USER_AGENT` | contact string | `api.weather.gov` requires a descriptive User-Agent. **Change the placeholder email to a real contact.** |
| `DATABASE_PATH` | `/tmp/weather_journal.db` | Fallback when `DATABASE_URL` is unset. |
| `REQUEST_LOG_ENABLED` | `false` | Per-instance log files are write-only noise on serverless. |
| `FLASK_ENV` / `FLASK_DEBUG` | `production` / `false` | Never ship the debugger. |

Secrets go in the dashboard or `vercel env add`, never in `vercel.json`:

| Variable | Needed for |
| --- | --- |
| `SECRET_KEY` | Stable signing key. Unset, each cold start invents one. |
| `DATABASE_URL` | Durable Postgres storage (above). |

## Other serverless caveats

- **Rate limiting** (`app/rate_limit.py`) counts in a process-local dict, so the
  configured limit applies *per instance*, not per deployment. Treat
  `RATE_LIMIT_REQUESTS` as a per-instance guard, not a global quota.
- **The weather cache** is per instance for the same reason; expect a lower hit
  rate than locally.
- **Bundle size**: `.vercelignore` keeps `.venv/`, `.beads/`, `tests/`,
  `instance/` and editor state out of the upload. Without it the local 37 MB
  virtualenv ships on every deploy.

## Local verification used before deploying

```bash
# Boot exactly as the deployment does, with no .env in reach.
cd /tmp && env -i PATH="$PATH" PYTHONPATH=<repo> VERCEL=1 \
  WEATHER_PROVIDER=nws NWS_USER_AGENT="(example.com, you@example.com)" \
  SECRET_KEY=... python -c "import run; print(run.app.test_client().get('/health').json)"

# Postgres backend against a real server
PG_TEST_URL="postgresql://..." pytest tests/test_postgres_backend.py
```
