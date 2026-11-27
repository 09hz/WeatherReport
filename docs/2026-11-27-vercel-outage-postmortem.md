# Vercel Outage Postmortem - 2026-11-27

## Symptom

The Vercel production deployment of project `ktp11/weather-report` (production URL https://weather-report-sable-ten.vercel.app) returned "page unavailable". The earlier deployment id was `dpl_AYz2FBQZVY6RnrdePuuDdhq4ZQv8`, runtime `python3.12`.

## How the code was ruled out

The committed code was booted in a deployment-shaped environment: empty env via `env -i`, run from outside the repo, with `VERCEL=1` set.

It imported fine and served:

- `/` → 200
- `/health` → 200, database connected
- `/ui/`, `/ui/weather`, `/ui/journal` → 200 `text/html`

So the application code was healthy and the fault was platform-side.

## Root cause

The runtime log, obtained with:

```bash
vercel logs --scope ktp11 --project prj_PhCabsKM84OyaQD8ZaAFdpRw4QY0
```

showed every request dying at import:

```
ModuleNotFoundError: No module named 'flask'
Python process exited with exit status: 1
```

The build log, obtained with `vercel inspect --logs`, showed why:

```
No Python version specified in .python-version, pyproject.toml, or Pipfile.lock. Using python version: 3.12
Using uv 0.13.5 (x86_64-unknown-linux-gnu)
Installing required dependencies from pyproject.toml...
Build Completed in /vercel/output [2s]
```

When a `pyproject.toml` exists, Vercel's Python builder resolves dependencies from it using uv, and `requirements.txt` is ignored. The project's `pyproject.toml` contained a `[tool.vercel]` table but **no `[project]` table**, so the dependency install step installed nothing. The build still reported success, in about 2 seconds. A roughly 2-second successful Python build is the diagnostic tell, because real dependency resolution takes longer.

## Why the docs made it worse

`docs/VERCEL_DEPLOYMENT.md` asserted the exact opposite. It said the file intentionally has no `[project]` table "so dependency resolution stays on requirements.txt". That written assumption is what produced the outage. The doc has since been corrected and now quotes the build log as evidence.

## The fix

`pyproject.toml` gained:

- a `[project]` table with `name`, `version`, `requires-python = ">=3.12"`, and the four runtime dependencies: `Flask>=3.0,<4.0`, `python-dotenv>=1.0`, `requests>=2.31`, `psycopg[binary]>=3.1,<4.0`
- a `[project.optional-dependencies]` `dev` entry holding `pytest>=8.0`
- a `[build-system]` using setuptools
- `[tool.setuptools.packages.find]` with `include = ["app*"]`, because the flat layout (`app/ docs/ scripts/ src/ tasks/ tests/`) otherwise makes setuptools auto-discovery abort with "Multiple top-level packages discovered"

## How it was verified

In this order:

1. The builder was replicated locally with no Vercel account involved: `uv venv --python 3.12` then `uv pip install .`, which installed flask 3.1.3 and psycopg and let the app cold-start on 3.12.
2. A preview deployment was made and checked.
3. Production was deployed and verified as an anonymous visitor with plain `curl`: `/ui/` 200, `/ui/weather` 200, `/health` 200, `/` 200, with `/health` returning:

```json
{"database":{"connected":true,"missing_tables":[]},"environment":"production","status":"ok","weather_provider":{"available":true,"name":"nws"}}
```

A real National Weather Service lookup for `Denver,CO` returned live observations from station KBKF.

## Second bug found in the same session

Tracked as **WeatherReport-9vj**.

`Config.to_flask_mapping()` in `app/config.py` defined rate-limit, request-log and weather-cache fields but never emitted them, so every `app.config.get` call returned `None` and rate limiting was silently off in every real deployment despite its declared default of `True`. An explicit `RATE_LIMIT_REQUESTS=7` was ignored.

Seven keys were added to the mapping. `REQUEST_LOG_PATH` had to ship together with `REQUEST_LOG_ENABLED` because `request_log.py` indexes it directly and would raise `KeyError`. Five regression tests were added.

Before enabling a previously-dead limiter in production, per-client keying was checked: `limiter_key` uses `client_ip()`, which honours the left-most `X-Forwarded-For` entry that Vercel sets, so buckets are per visitor rather than one shared proxy bucket. This was proved by exhausting one forwarded IP and confirming a second still received 200. After the production deploy the headers `x-ratelimit-limit: 100`, `x-ratelimit-remaining: 95` and `x-ratelimit-window: 60` were observed live, which could not have appeared before the fix.

## Lessons

- A build that succeeds proves nothing about what it installed; treat an implausibly fast successful build (a ~2s Python build) as a failure signal and read the build log for the install step.
- Deployment docs are claims, not evidence — pin every assertion about builder behaviour to a quoted build log line, or assume it is wrong.
- When the platform offers several dependency manifests, determine which one the builder actually reads, because the presence of a higher-precedence file silently disables the others.
- A config field that is declared but never emitted to the runtime mapping is a dead feature; cover the mapping itself with regression tests so defaults cannot silently resolve to `None`.

Related: [[2026-11-27-vercel-connectivity]] [[2026-11-27-shared-serverless-state]]
