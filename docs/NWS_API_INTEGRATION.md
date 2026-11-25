# NWS API Integration

## Overview

This app fetches real current weather observations from the US National Weather Service at api.weather.gov. No API key is required, but a descriptive User-Agent header is mandatory for access. The integration uses coordinate-based lookups and provides only observed weather data—not forecasts—drawn directly from observation stations across the United States.

## NWS endpoints used

The provider makes three sequential API calls in order:

1. **GET /points/{lat},{lon}** – Retrieves point metadata containing the URL to nearby observation stations. This result is cached per rounded coordinate to avoid redundant lookups.

2. **GET {properties.observationStations}** – Lists observation stations near the given coordinates, ordered from nearest first. The provider extracts station IDs and attempts at most three before aborting.

3. **GET /stations/{stationId}/observations/latest** – Fetches the latest observation from a specific station. If unavailable or returning null measurements, the provider tries the next nearest station.

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `NWS_USER_AGENT` | Descriptive identifier for your app (required by NWS) | `WeatherReport/0.1 (set NWS_USER_AGENT with a contact)` |
| `NWS_BASE_URL` | Base URL for NWS API requests. Must be an `https://api.weather.gov` URL with no port; any other value is ignored with a logged warning and the default is used, so bad configuration never blocks startup | `https://api.weather.gov` |
| `NWS_TIMEOUT_SECONDS` | Per-request timeout in seconds. A non-numeric value is ignored with a logged warning and falls back to `10` | falls back to `WEATHER_TIMEOUT_SECONDS`, else `10` |
| `NWS_TOTAL_TIMEOUT_SECONDS` | Worker-time budget for one whole observation lookup. `0`, unset or non-numeric derives it as 3x the per-request timeout | `0` (derive) |
| `WEATHER_PROVIDER` | Explicit provider selection; empty uses legacy behavior | (empty string) |

Note that `NWS_BASE_URL` cannot be pointed at a local stub. The SSRF guard
(`ensure_nws_url`) allows only `https://api.weather.gov`, and that restriction
applies to the configured base URL as well as to every URL the API hands back.
Configuration is merely lenient about a rejected value — it warns and reverts to
the default instead of raising at startup. To exercise the provider against a
fake backend, inject a session: `NwsWeatherProvider(session=fake)`, the pattern
used throughout `tests/test_nws.py`.

## Request flow

The `NwsWeatherProvider.fetch_observation(lat, lon)` method executes the following steps:

1. **Validate and round coordinates** – Input values are parsed via `parse_coordinates()`, which rejects non-numeric or out-of-range values early before any network call. Coordinates are rounded to 4 decimal places.

2. **Fetch point metadata** – A GET request to `/points/{lat},{lon}` returns cached or fresh point data including the stations URL. This lookup is cached per (rounded) coordinate key using a bounded LRU-like cache (default 256 entries).

3. **List nearby stations** – The provider fetches the station list from the URL found in point metadata and extracts valid station identifiers (1-10 characters, alphanumeric).

4. **Attempt up to three nearest stations** – For each station ID, the provider calls `/stations/{id}/observations/latest` and falls through to the next nearest station in two cases: the station answers with an HTTP error status (any 4xx/5xx, including a 404 for a station NWS does not serve), or it answers 200 with an observation that has no timestamp or no usable measurement at all. Everything else aborts the whole request immediately rather than retrying: a timeout, an untrusted URL rejected by the SSRF guard, and a malformed or non-JSON body. If all attempted stations are exhausted, the provider raises `nws_no_observation` (502).

5. **Normalize the result** – A usable observation (one with `observed_at` + at least one of temperature/humidity/wind/description not null) is mapped to the internal shape via `normalize_observation()`, which includes all measured fields, applies unit conversions (C→F, km/h→mph), and preserves nulls for missing measurements.

## API endpoint exposed by this app

**GET /api/weather/observations?lat=<lat>&lon=<lon>**

Returns JSON with a `data` object containing the normalized observation. The
internal `raw_response` field is stripped by the route, so it never appears in
the API response.

The example below is **illustrative, not a captured response**. Its shape and
types are accurate, but do not treat the values as a recorded reading from any
particular station. Note that every numeric measurement is a JSON float, so
`wind_direction_deg`, `barometric_pressure_pa` and `visibility_m` render with a
decimal point even when the upstream value is a whole number -- `_number()`
casts to `float` unconditionally.

```json
{
  "data": {
    "location": "39.7456,-97.0892",
    "latitude": 39.7456,
    "longitude": -97.0892,
    "station_id": "KTOP",
    "observed_at": "2026-11-24T02:10:00+00:00",
    "temperature_c": 1.0,
    "temperature_f": 33.8,
    "dewpoint_c": -1.1,
    "humidity": 86.2,
    "wind_speed_kph": 11.124,
    "wind_speed_mph": 6.91,
    "wind_gust_kph": null,
    "wind_gust_mph": null,
    "wind_direction_deg": 180.0,
    "barometric_pressure_pa": 101800.0,
    "visibility_m": 16090.0,
    "description": "Clear",
    "conditions": "Clear",
    "source": "api.weather.gov",
    "provider": "nws",
    "data_type": "observation"
  }
}
```

`observed_at` is nullable. A station that reports measurements without a
timestamp is still returned, with `observed_at` set to `null`; only a payload
carrying no measurements at all is rejected.

Values already in canonical units (`degC`, `km_h-1`) are passed through
exactly as NWS sent them. Values needing conversion (`degF`, `m_s-1`) are
rounded to two decimals once, at output, and the paired `_f`/`_mph` field is
derived from the full-precision figure -- so a reading round-trips to the
number NWS actually reported.

## Failure handling

Station failover tries at most three nearby stations for a single point lookup.
Failures fall into two modes. Recoverable failures — an HTTP error status, a
non-JSON or non-object response body, a connection-level failure such as a
reset, or no usable measurement — skip that station and try the next one.
Unskippable failures — a timeout, an exhausted chain budget, and a URL that
fails the trust check (it must never be retried elsewhere) — abort the whole
request immediately. Exhausting every candidate yields `nws_no_observation`
with HTTP 502, whose details carry `stations_tried`, `last_failure` and
`last_upstream_status`.

A missing timestamp is *not* a failure: the observation is returned with
`observed_at` set to `null`.

| Failure | Behaviour |
|---|---|
| HTTP error status, non-JSON/non-object body, connection failure, no usable measurement | Skip this station, try the next one |
| Timeout, exhausted total budget, untrusted URL | Abort immediately, no retry |

## Timeouts and the chain budget

A single lookup makes up to five sequential upstream requests: `/points`, the
station list, and up to three station observations. These are bounded twice:

- **Per request** by `NWS_TIMEOUT_SECONDS`. This bounds one round trip.
- **Per chain** by `NWS_TOTAL_TIMEOUT_SECONDS`, a wall-clock deadline fixed
  before the first request. This bounds how long one API call can occupy a
  worker, which per-request limits alone do not: five requests each finishing
  just inside a 10s limit is a 50s request.

Each request is issued with `min(remaining budget, NWS_TIMEOUT_SECONDS)`, so a
request is never given more time than the chain has left and never more than
the configured per-request limit. When the budget is gone the provider raises
**before** issuing another request, so an over-budget chain cannot buy itself
another round trip. That error is `weather_provider_timeout` with HTTP 504 and
`details.scope == "total_budget"`, distinguishing it from a single request that
timed out on its own.

A cached `/points` hit issues no request and therefore spends no budget.

The upstream HTTP status is mapped using the *kind* of request being made
(point, stations, observation), not the problem-type string in the response
body, because NWS does not guarantee a JSON problem body.

| Upstream condition | Request kind | HTTP status | Error code |
|---|---|---|---|
| 404 (no coverage for this point) | point | 404 | `nws_point_unavailable` |
| 400 (coordinates rejected) | point | 400 | `nws_invalid_point` |
| Any other status | point / stations / observation | 502 | `weather_provider_error` |
| Timeout | any | 504 | `weather_provider_timeout` |

All of these are expected conditions and deliberately do **not** write a stack
trace to the log: the endpoint is unauthenticated, so traceback volume would
otherwise be driven by the caller. Error details always include `reason`,
`upstream_status` and `request_kind`.

## Limitations

- **US coverage only** – Points outside the NWS coverage area (e.g., London, UK) return HTTP 404 with error code `nws_point_unavailable`.

- **Coordinate rounding** – All input coordinates are rounded to 4 decimal places before lookup.

- **Null measurements preserved** – Missing or null observations are returned as JSON `null`; nothing is guessed, averaged, or interpolated.

- **Current conditions only** – The endpoint returns the single latest observation reported by a nearby station (`data_type: "observation"`). It is not a forecast, and it is not a history API: there is no way to ask for an earlier time, and the reading can already be up to an hour old depending on the station's reporting interval.

- **HTTPS to api.weather.gov only** – Only URLs matching `https://api.weather.gov` are accepted; any other scheme or host triggers an error (SSRF protection).

## Running the tests

**Mocked tests (default, no internet needed)**  
The normal test suite uses a fake HTTP session and never contacts the live API. Run with:
```bash
python -m pytest tests/test_nws.py
```

**Live smoke test against real NWS API (opt-in)**  
To test against the actual weather service, set `NWS_LIVE_TESTS=1` and provide a User-Agent identifying your app:
```bash
NWS_LIVE_TESTS=1 \
NWS_USER_AGENT="YourApp (your-contact@example.com)" \
python -m pytest tests/test_nws.py -m live
```

The `live` marker is deselected by default through `addopts = -q -m "not live"` in `pytest.ini`, and passing `-m live` on the command line overrides that default. The test additionally carries a `skipif` on `NWS_LIVE_TESTS`, so even an explicit `-m live` run skips it unless `NWS_LIVE_TESTS=1` is set.
