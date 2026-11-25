# NWS Provider Code Review

Drafted by the local model (`qwen3.5:4b`, profile `localworker`) and corrected
against `tests/test_nws.py`. Claims the local pass got wrong are kept below as
"not a defect" so the record shows what was checked.

## Summary

`app/services/nws_provider.py` implements the api.weather.gov observation
backend: coordinate point lookup -> nearest-station discovery -> latest
observation, behind the existing `WeatherProvider` protocol. It adds an SSRF
guard, an LRU point cache under a lock, and unit-aware measurement parsing.
`fetch_current("lat,lon")` keeps the generic `/current` and snapshot routes
working; `fetch_observation(lat, lon)` backs `GET /api/weather/observations`.

## Correctness concerns

- `_temperature_c` accepts only `wmoUnit:degC`/`degF` and `_speed_kph` only
  `km_h-1`/`m_s-1`; anything else returns `None` instead of mis-converting.
  Deliberate, and covered by `test_unexpected_unit_code_is_not_silently_converted`.
- `get_point` passes `props.get("observationStations")` straight into
  `ensure_nws_url`, which raises `invalid_url` on missing/null. Correct
  behaviour, but the error says "missing or invalid URL" without naming the
  field — minor diagnosability nit.
- `allow_redirects=False` means an NWS `301 AdjustPointPrecision` surfaces as
  `http_error`. Acceptable because `parse_coordinates` rounds to 4 decimals
  before the request, which is exactly what prevents that 301.
- Station fall-through fires only on `details["reason"] == "http_error"` or an
  unusable 200 body; timeout, untrusted URL and malformed body abort
  immediately. Intentional, and matches `docs/NWS_API_INTEGRATION.md`.
- `app/__init__.py` constructs an NWS provider at startup even when
  `WEATHER_PROVIDER` is not `nws`. Harmless (no network at construction), but
  it is an eager object nobody may use.

## Error handling

- 404 + `type=.../InvalidPoint` is special-cased to `nws_point_unavailable`
  (404); every other upstream status becomes a `ProviderError` carrying
  `upstream_status` plus the problem+json `type`/`title`/`detail`.
- Timeouts map to `weather_provider_timeout` (504), distinct from other
  failures.
- A non-JSON body (the Akamai "Access Denied" HTML case) raises `malformed`
  rather than crashing on `.json()`.
- Exhausting `MAX_STATION_ATTEMPTS` raises `nws_no_observation` (502) with the
  list of stations tried.
- No retry/backoff on transient upstream errors; station fall-through is the
  only recovery path. Fine for a single-observation read.

## Test coverage gaps

Verified against `tests/test_nws.py` (28 tests + 1 `live`-marked test):

- NOT a defect — SSRF guard, `normalize_observation` with null/missing values,
  unexpected unit codes, zero stations, non-JSON bodies, bounded attempts and
  LRU cache eviction are all already tested.
- Real gap: no test for a non-string / malformed `stationIdentifier` in
  `_stations` (the regex filter path).
- Real gap: no concurrency test around the point-cache lock.
- Real gap: `forecast`/`grid_*` fields returned by `get_point` are never
  asserted on.
- The one live test is `-m live` deselected by default and additionally
  skipped unless `NWS_LIVE_TESTS=1`.
