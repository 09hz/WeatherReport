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
| `NWS_BASE_URL` | Base URL for NWS API requests | `https://api.weather.gov` |
| `NWS_TIMEOUT_SECONDS` | Request timeout in seconds | falls back to `WEATHER_TIMEOUT_SECONDS`, else `10` |
| `WEATHER_PROVIDER` | Explicit provider selection; empty uses legacy behavior | (empty string) |

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
the API response. The example below is a real reading from KTOP (Topeka):

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
    "wind_direction_deg": 180,
    "barometric_pressure_pa": 101800,
    "visibility_m": 16090,
    "description": "Clear",
    "conditions": "Clear",
    "source": "api.weather.gov",
    "provider": "nws",
    "data_type": "observation"
  }
}
```

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
