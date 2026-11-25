# Weather & Journal — Backend Foundation

A modular Flask backend and server-rendered UI for a Weather & Journal
application. It lets you record dated journal entries about places, look up
live US weather by ZIP or city, and archive weather snapshots that entries can
reference.

Two core features:

1. **Journal the weather in your area** — create, search, edit and delete
   dated entries, each optionally linked to a stored weather snapshot.
2. **Look up US weather** — live observations from the National Weather
   Service, by ZIP code, city and state, or coordinates.

## Status

| Area | State |
| --- | --- |
| App factory, config, error handling | Implemented |
| SQLite schema + connection management | Implemented |
| Journal create / read / search / edit / delete | Implemented |
| Weather snapshot save / list / read | Implemented |
| Live weather retrieval (NWS, keyless) | Implemented |
| Offline US place-name geocoding | Implemented |
| Browser UI (`/ui`) | Implemented |

The application never fabricates weather data. With no provider configured,
weather lookups return HTTP 503 rather than placeholder observations, and a
measurement NWS reports as null stays null.

## Weather provider

`WEATHER_PROVIDER=nws` selects the National Weather Service
(api.weather.gov). **NWS is keyless** — there is no API key, only a required
descriptive `NWS_USER_AGENT` with a contact address. Coverage is the US plus
PR, VI, GU, AS and MP.

NWS accepts coordinates only, so place names are resolved from a dataset
committed to the repo (`app/data/`) before the request is made. A lookup
therefore makes no geocoding request, and the app's only outbound host is
api.weather.gov. Accepted input:

```text
78701            a ZIP code (ZIP+4 is truncated)
Austin, TX       city and state code
Austin, Texas    city and state name
Austin TX        comma optional
Chicago          bare city, only when it is unique across all states
30.27,-97.74     coordinates, which skip geocoding entirely
```

An ambiguous bare city is never guessed at: `Springfield` returns a 400
listing the 24 states it is in. Regenerate the dataset with
`python scripts/build_geodata.py` — see `app/data/README.md`.

## Directory structure

```text
.
├── app/
│   ├── __init__.py              # create_app() application factory
│   ├── config.py                # env-var configuration (no hardcoded secrets)
│   ├── errors.py                # error types + uniform JSON error envelope
│   ├── validation.py            # reusable request-input validators
│   ├── data/                    # bundled offline place data (GeoNames, CC BY)
│   ├── static/css/              # brutalist.css
│   ├── templates/               # base layout + UI pages
│   ├── database/
│   │   ├── connection.py        # app-context-scoped SQLite connections
│   │   └── schema.py            # table/index DDL, idempotent initialization
│   ├── routes/
│   │   ├── health.py            # /health
│   │   ├── journal.py           # /api/journal/*
│   │   ├── weather.py           # /api/weather/*
│   │   └── ui/                  # /ui/* server-rendered pages
│   └── services/
│       ├── geocoder.py          # offline ZIP/city -> coordinates
│       ├── journal_service.py   # journal persistence + queries
│       ├── nws_provider.py      # api.weather.gov adapter (keyless)
│       ├── weather_provider.py  # provider interface, HTTP adapter, normalizer
│       └── weather_service.py   # snapshot persistence + retrieval
├── scripts/
│   └── build_geodata.py         # regenerate app/data/ from GeoNames
├── tests/
│   ├── conftest.py              # fixtures: temp DB, credential-free env
│   ├── test_app.py              # init, health check, schema
│   ├── test_geocoder.py         # place-name resolution + ambiguity
│   ├── test_journal.py          # journal CRUD + validation
│   └── test_weather.py          # provider behaviour, normalization, snapshots
├── .env.example
├── pytest.ini
├── requirements.txt
├── main.py                      # `python main.py` — run + clickable UI link
└── run.py                       # module-level app for WSGI tooling
```

Layering is strict: routes handle HTTP and validation, services hold logic and
SQL, the database package owns connections and schema. Services never import
Flask request/response objects, so they are directly unit-testable.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env     # optional — the app starts fine without it
python main.py           # prints a clickable link to the UI
```

`main.py` is the entry point for using the app: it starts the server and
prints the browser URL as the last line of output, so the link is the thing
you see. It steps to the next free port if 5000 is already taken.

```bash
python main.py --open            # also launch the default browser
python main.py --port 8080       # pick a port
python main.py --host 0.0.0.0    # expose on the LAN (link shows 127.0.0.1)
python main.py --debug           # auto-reload + tracebacks
```

`run.py` remains the bare WSGI-style entry point (`app = create_app()`) for
tooling that imports a module-level app. Production uses a real WSGI server:

```bash
gunicorn "app:create_app()"
```

Requires Python 3.10+. No API credentials are needed to start the app or run
the tests.

## Configuration

All configuration comes from environment variables (loaded from `.env` when
`python-dotenv` is installed). See `.env.example`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `FLASK_ENV` | `development` | Environment name; `test`/`testing` enables TESTING |
| `FLASK_DEBUG` | `false` | Flask debug mode |
| `FLASK_HOST` / `FLASK_PORT` | `127.0.0.1` / `5000` | Dev server bind address |
| `DATABASE_PATH` | `instance/weather_journal.db` | SQLite file; relative paths resolve from the project root |
| `WEATHER_API_KEY` | _unset_ | Provider credential |
| `WEATHER_API_BASE_URL` | _unset_ | Provider current-conditions endpoint |
| `WEATHER_TIMEOUT_SECONDS` | `10` | Outbound request timeout |
| `SECRET_KEY` | _unset_ | Flask secret; a random ephemeral key is generated if absent — **set it explicitly in production** |

Security notes:

- No credential is hardcoded; `.env` and `*.db` are gitignored.
- The literal placeholders from `.env.example` (e.g. `YOUR_WEATHER_API_KEY_HERE`)
  are treated as "not configured", so a copied-but-unedited `.env` cannot
  trigger outbound requests.
- All SQL uses parameterized queries.

## Enabling weather retrieval

`app/services/weather_provider.py` defines the `WeatherProvider` protocol
(`name`, `available`, `fetch_current(location)`). Two implementations ship:

- `UnconfiguredWeatherProvider` — the default; raises `ProviderNotConfiguredError`
  and performs no network I/O.
- `HttpWeatherProvider` — a generic REST adapter. Set `WEATHER_API_KEY` and
  `WEATHER_API_BASE_URL` and it is selected automatically at startup.

The adapter's defaults match an OpenWeatherMap-style current-weather endpoint
(`?q=<location>&appid=<key>&units=metric`). For another provider, either pass
different `query_param`/`key_param` values or implement the protocol and return
it from `build_provider`. `normalize()` maps a provider payload to the internal
dictionary shape and leaves absent fields as `None` rather than guessing.

## API

All responses are JSON. Success payloads are wrapped in `data`; collections add
`count`. Errors use a single envelope:

```json
{"error": {"code": "validation_error", "message": "...", "details": {"field": "title"}}}
```

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/` | Service metadata and endpoint list |
| GET | `/health` | `200` healthy, `503` if schema/DB unavailable |
| GET | `/api/weather/provider` | Whether external lookups are enabled |
| GET | `/api/weather/current?location=` | Live conditions; `503` when unconfigured, `502` on provider failure |
| POST | `/api/weather/snapshots` | `{"location": "Oslo"}` — fetch and persist |
| GET | `/api/weather/snapshots?location=&limit=` | Saved snapshots, newest first |
| GET | `/api/weather/snapshots/<id>` | Single snapshot |
| POST | `/api/journal/entries` | Requires `title`, `date` (ISO-8601), `location`, `description`; optional `weather_snapshot_id` |
| GET | `/api/journal/entries?q=&location=&limit=&offset=` | Search/filter, newest first (`limit` max 200) |
| GET | `/api/journal/entries/<id>` | Single entry |
| DELETE | `/api/journal/entries/<id>` | `204` on success |

Status codes: `400` validation, `404` not found, `405` wrong method,
`502` provider failure, `503` provider not configured, `500` unexpected.

Example:

```bash
curl -X POST localhost:5000/api/journal/entries \
  -H 'Content-Type: application/json' \
  -d '{"title":"Rainy walk","date":"2026-11-23","location":"Amsterdam","description":"Grey skies."}'

curl 'localhost:5000/api/journal/entries?q=Amsterdam'
```

## Database schema

Created automatically at startup, idempotently.

- `weather_snapshots` — `id`, `location`, `observed_at`, `temperature_c`,
  `humidity`, `wind_speed_kph`, `conditions`, `provider`, `raw_response` (JSON
  text, withheld from API responses unless requested), `created_at`.
- `journal_entries` — `id`, `title`, `entry_date`, `location`, `description`,
  `weather_snapshot_id` → `weather_snapshots(id)` `ON DELETE SET NULL`,
  `created_at`, `updated_at`.

Indexed on snapshot location, and entry date and location. `PRAGMA
foreign_keys` is enabled per connection.

## Testing

```bash
.venv/bin/python -m pytest
```

64 tests cover app initialization, the health check, schema creation and
idempotency, journal CRUD/search/pagination, validation and error envelopes,
and weather behaviour with and without credentials. Each test runs against a
temporary SQLite file with weather env vars stripped; the configured-provider
cases use a fake HTTP session, so the suite never makes a network request.

## Next steps

- Journal update (`PATCH`) and richer search.
- A concrete provider implementation plus response caching.
- Pagination metadata (total counts) and request logging.
- Frontend.
