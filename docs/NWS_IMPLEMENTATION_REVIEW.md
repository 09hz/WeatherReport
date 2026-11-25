# NWS Implementation Review

Independent pre-commit review of the National Weather Service provider.

| | |
|---|---|
| Review date | 2026-11-23 |
| Subject | `feature/nws-weather-api`, uncommitted in worktree `.worktrees/t_683ebd3f` |
| Beads issue | `WeatherReport-27w` (left open, in_progress) |
| Baseline | `master` @ `92a177a` |
| Reviewers | 3 independent agents (security/HTTP, data correctness, tests/docs), none of which authored the implementation |
| Verification | Every finding below was re-verified by the orchestrator against the source, and the behavioural ones by direct execution probes |
| Recommendation | **REQUEST CHANGES** — safe to commit to the feature branch; three items should be closed before merge to `master` |

This report is written to the main checkout (`docs/` is untracked on `master`) so
it does not become part of the feature branch's diff. No file in the worktree
and no tracked file on `master` was modified during the review.

## Review summary

The implementation is substantially correct and in several respects better than
the spec required. The highest-risk component — the SSRF allowlist on
NWS-supplied URLs — is genuinely well built and survived a deliberate attempt to
break it. The "never invent a measurement" rule holds throughout normalization.
Persistence compatibility with the pre-existing snapshot layer was checked
column by column and holds.

No critical defects. No security defects. The three findings worth acting on are
robustness and operability issues, not correctness-of-data issues:

1. Station failover does not trigger on a malformed station response (major).
2. Only an exact `/InvalidPoint` problem type produces a client-facing 404; every
   other upstream 4xx becomes a 502 and logs a full stack trace (major).
3. `tests/conftest.py` does not scrub the new `NWS_*` / `WEATHER_PROVIDER`
   environment variables, so a developer's shell can break the suite (major).

## Files examined

New (untracked):

- `app/services/nws_provider.py` (350 lines) — provider, SSRF guard, normalization
- `tests/test_nws.py` (445 lines) — 20 mocked tests + 1 live smoke test
- `docs/NWS_API_INTEGRATION.md` (104 lines)

Modified vs `master` (+116 / -3 across 7 files):

- `app/__init__.py` — `nws_provider` extension wiring, index listing
- `app/config.py` — `WEATHER_PROVIDER`, `NWS_USER_AGENT`, `NWS_BASE_URL`, `NWS_TIMEOUT_SECONDS`
- `app/routes/weather.py` — `GET /api/weather/observations`
- `app/services/weather_provider.py` — `build_nws_provider`, `WEATHER_PROVIDER=nws` selection
- `app/validation.py` — `coordinate()`, `parse_coordinates()`
- `.env.example`, `pytest.ini`

Read for context, unmodified: `app/errors.py`, `app/services/weather_service.py`,
`app/database/schema.py`, `app/database/connection.py`, `tests/conftest.py`,
`tests/test_weather.py`.

## Confirmed findings

### MAJOR 1 — Station failover only triggers on an HTTP error status

`app/services/nws_provider.py:237-242`

The fallback loop continues only when `exc.details.get("reason") in ("http_error",)`.
`_get_json` raises `reason="malformed"` for a non-JSON or non-object body
(`:345-349`) and `reason=<ExceptionTypeName>` for connection-level failures
(`:318-321`). Both propagate out of the loop and end the whole request.

Verified by execution — station #1 returning an HTML body with HTTP 200:

```
ABORTED: weather_provider_error 502 malformed | observation calls: 1
```

Control, station #1 returning HTTP 500:

```
FELL THROUGH to: KTOP | observation calls: 2
```

So a single flaky station that serves an Akamai interstitial or resets the
connection suppresses the two remaining candidates. The inline comment at
`:238-239` ("timeouts and trust failures abort immediately") does not describe
what the condition implements. Note the spec mandated failover "until one yields
a usable observation" without enumerating transport faults, so this is a
robustness shortfall rather than a spec violation.

Fix: also continue on `malformed` and connection-level reasons; keep aborting on
`timeout`, `untrusted_url` and `invalid_url`.

### MAJOR 2 — Non-`/InvalidPoint` upstream 4xx becomes a 502 with a logged stack trace

`app/services/nws_provider.py:330-343`, `app/errors.py:22,90,97-98`

The out-of-coverage special case requires `str(problem.get("type","")).endswith("/InvalidPoint")`.
If the 404 body is not JSON (`payload` becomes `None` at `:327-328`, so `problem`
is `{}`), or carries a different problem type, control reaches `:343` and raises a
bare `ProviderError`, which inherits `status_code = 502` and `log_traceback = True`.

Verified by execution — upstream 404 with `type=.../problems/SomethingElse`:

```
code= weather_provider_error status_code= 502 log_traceback= True
```

Two consequences: a caller-caused condition is reported as a server fault,
against the spec's requirement that out-of-coverage surface as a clear
client-facing error; and every occurrence writes a full traceback via
`app.logger.exception`, on an unauthenticated endpoint, so upstream 4xx noise is
caller-drivable log volume.

Fix: map an upstream 404 (and a 400 on `/points/...`) to a 4xx client error
keyed on the request kind rather than on the problem-type string, and set
`log_traceback = False` on expected upstream-HTTP provider errors.

### MAJOR 3 — Test environment is not isolated from the new env vars

`tests/conftest.py:22-29`

`_WEATHER_ENV_VARS` scrubs `WEATHER_API_KEY`, `WEATHER_API_BASE_URL`,
`DATABASE_PATH`, `FLASK_ENV`, `FLASK_DEBUG`, `SECRET_KEY` — but not
`WEATHER_PROVIDER`, `NWS_USER_AGENT`, `NWS_BASE_URL` or `NWS_TIMEOUT_SECONDS`.

Verified by execution, same worktree, same code:

```
WEATHER_PROVIDER=nws          python -m pytest  ->  8 failed, 99 passed
NWS_BASE_URL=http://localhost:9  python -m pytest  ->  1 failed, 47 passed, 59 errors
```

A green suite therefore depends on the developer's shell being clean. This is a
reproducibility hazard, not a network leak: `fetch_current` rejects a
non-`lat,lon` location before any request.

Fix: append the four variables to `_WEATHER_ENV_VARS`.

### MINOR 4 — A bad `NWS_BASE_URL` crashes application startup

`app/__init__.py:45-47` → `app/services/weather_provider.py:146-157` → `app/services/nws_provider.py:192`

`NwsWeatherProvider.__init__` passes the configured base URL through
`ensure_nws_url`, and `create_app` builds the NWS provider unconditionally.
Verified: `NWS_BASE_URL=http://localhost:8080 create_app()` →
`STARTUP FAILED: ProviderError Refusing to follow a URL outside api.weather.gov.`

This contradicts `app/config.py`'s stated contract that missing or bad values
fall back to safe defaults "so the app always boots", and it makes the
`NWS_BASE_URL` knob unusable for a local stub. Fix: validate in
`build_nws_provider`, fall back to the default with a warning, and keep the hard
`ensure_nws_url` check for externally supplied URLs only.

### MINOR 5 — A usable observation is discarded when `properties.timestamp` is absent

`app/services/nws_provider.py:142,170-174`

`_is_usable` requires a non-`None` `observed_at`, and `observed_at` is only set
when `properties.timestamp` is a `str`. Verified: a station payload with
`temperature 1.0 degC` and `textDescription "Clear"` but no timestamp is
rejected, and after three attempts the request fails
`nws_no_observation` / 502 rather than returning the measurements with
`observed_at: null`. Conservative and never invents data, but lossy.

### MINOR 6 — No cumulative deadline across the request chain

`app/services/nws_provider.py:220-253,298-310`

One API call makes up to five sequential upstream requests, each with its own
`timeout=self._timeout` (default 10 s) and no shared budget. Responses that are
slow but under the per-request limit can occupy a worker for roughly 50 s — and
`requests`' scalar timeout applies separately to connect and read. Per-attempt
bounding is correct; worker-time bounding is absent. (Derived from the code, not
measured.)

### MINOR 7 — Unvalidated `forecast` URL is cached

`app/services/nws_provider.py:270`

`observationStations` is guarded by `ensure_nws_url` at `:269`; the adjacent
`forecast` value is stored verbatim. Nothing fetches it today (confirmed by
repo-wide grep), so this is latent, but it makes the guard non-uniform at the
extraction site and the first consumer inherits an SSRF.

### MINOR 8 — Test gap at the station step, exactly where the abort/retry branch lives

`tests/test_nws.py:325,356`

Both the timeout test and the malformed-body test inject their failure at
`POINT_URL`; confirmed by grep that `requests.exceptions.Timeout` and
`json_error=True` appear nowhere else. The `reason in ("http_error",)` branch at
`nws_provider.py:240` is therefore exercised only with `http_error`, so widening
or narrowing it would not fail any test — see MAJOR 1.

Also untested: the `degF` temperature branch (`:107-108`), the `m_s-1` wind
branch (`:122-123`), and the two malformed-structure guards (`:264`, `:286`).

### MINOR 9 — Double rounding on non-canonical input units

`app/services/nws_provider.py:97-109,67-68`

`_temperature_c` rounds a `degF` input to 2 decimals, and `c_to_f` then rounds
again, so 33 °F round-trips to 33.01 °F. Same pattern for `m_s-1` wind. Output
can disagree with the upstream reading by 0.01. No fabrication.

### MINOR 10 — Snapshot `location` is the reformatted coordinate pair

`app/services/nws_provider.py:145`, `app/services/weather_service.py:62,94`

`location` is rebuilt as `f"{latitude},{longitude}"` from the rounded floats, so
a snapshot POSTed as `45.50000, -122.60` is stored as `45.5,-122.6`, while
`list_snapshots` filters on exact string equality. A client listing by the string
it submitted gets zero rows.

### INFORMATIONAL

- `nws_provider.py:323` — `int(getattr(response, "status_code", 200) or 200)` fails
  open: a missing or falsy status is treated as 200, and a non-numeric status
  raises outside both `try` blocks and reaches the catch-all 500 handler. No
  impact with real `requests` objects.
- `weather_provider.py:3-6` — the module docstring still promises no outbound
  request without credentials; the keyless NWS route always makes them.
- The example payload in `docs/NWS_API_INTEGRATION.md:64-66` renders
  `wind_direction_deg`, `barometric_pressure_pa` and `visibility_m` as integers,
  but `_number` casts every measurement to `float`. Verified: the real response
  serializes `180.0` and `16090.0`.
- `docs/NWS_API_INTEGRATION.md:46` labels the example "a real reading from KTOP".
  The values do come from a recorded live run (`WeatherReport-27w` notes: KTOP
  1.0 C / 33.8 F, 11.124 kph / 6.91 mph), but that provenance lives in the issue
  tracker, not the repo. Worth a provenance line or relabelling as illustrative.
- `data_type` is not persisted — `weather_snapshots` has no such column, so the
  observation/forecast distinction survives only as `provider='nws'`.
- `nws_provider.py:258-281` — `get_point` returns the cached dict by reference;
  a future caller mutating it corrupts shared state.
- `nws_provider.py:222-223` — the station list is re-fetched on every request even
  on a point-cache hit, so caching removes only one of two metadata round-trips.
- `tests/test_nws.py:388-393` — the SSRF test asserts only the exception type, not
  `details['reason']`; the companion `session.urls() == [POINT_URL]` assertion is
  what actually makes it a real regression trap.

## Verified correct

Stated explicitly because these are the areas most likely to harbour a serious
defect, and they do not:

- **SSRF guard** (`nws_provider.py:40-64`). The combined scheme + `hostname` +
  lowercased `netloc` + no-port predicate was probed offline against every bypass
  class in the brief and rejected all of them: userinfo
  (`https://api.weather.gov@evil.com`), explicit ports including `:443`, trailing
  dot, punycode and Cyrillic homoglyph hosts, scheme-relative `//`, IP literals,
  suffix confusion. The `netloc` check is what closes the userinfo hole a
  hostname-only check would leave open. A `urlsplit`-vs-`requests` parser
  differential on ASCII control characters fails *closed* (urllib3 raises
  `InvalidURL`, caught at `:318-321`). `allow_redirects=False` (`:309`) means no
  3xx can escape the allowlist, and the guard is applied both at extraction
  (`:269`, `:284`) and inside `_get_json` (`:299`), so no path reaches
  `client.get` unguarded.
- **No invented data.** No default, average, interpolation or fallback value
  exists anywhere in `normalize_observation`. Unit codes are checked before
  conversion and an unexpected `unitCode` yields `None` rather than a
  mis-converted number (`:83-124`); `_number` rejects booleans; a non-Mapping
  `properties` degrades to `{}`.
- **Conversion arithmetic.** `c_to_f` and `kph_to_mph` with `KPH_PER_MPH =
  1.609344`, spot-checked at 0/100/-40 °C and 1.609344/100 kph.
- **Coordinate validation** (`app/validation.py:139-174`) rejects `None`,
  whitespace, `bool`, non-numeric types, unparseable strings, `nan`/`inf`/`1e400`
  and out-of-range values with a 400 before any network call — `parse_coordinates`
  is the first statement of `fetch_observation`. Rounding to 4 decimals never
  emits scientific notation, so the NWS 301 `AdjustPointPrecision` redirect
  cannot be triggered.
- **SQLite persistence compatibility**, checked key by key against
  `save_snapshot` and `schema.py`: neither NOT NULL column can receive `None`, no
  dict or list is ever bound to a TEXT/REAL column, every key the writer reads is
  present, and `raw_response` goes through `serialize_raw`'s `json.dumps(...,
  default=str)`.
- **Bounded, thread-safe point cache**: `OrderedDict` LRU under a `threading.Lock`,
  eviction at `cache_size`, `max(1, ...)` floors, network I/O outside the lock.
- **Bounded station attempts**: `stations[:3]`, so no request storm per call.
  Station identifiers are regex-validated and `quote(..., safe='')`-encoded.
- **Distinct non-crashing outcomes** for timeout (`504`,
  `weather_provider_timeout`), zero stations (`404`, `nws_no_stations`), exhausted
  candidates (`502`, `nws_no_observation`, with `stations_tried`), and non-JSON
  bodies.
- **Error envelope** is the existing `{"error": {code, message, details}}` shape;
  existing routes and status codes are unchanged.
- **Network isolation of the default suite**: all 20 mocked tests get their
  provider through `_provider()`, which always injects a `_FakeSession` that
  raises on any unrouted URL. The three tests that build a session-less provider
  only read `.name`/`.available`. The three route tests overwrite
  `app.extensions["nws_provider"]` before issuing a request.

## Test results

All runs in the worktree, no source file modified, interpreter
`.venv/bin/python`.

| Run | Result |
|---|---|
| `python -m pytest` (worktree) | **107 passed, 1 deselected** |
| Same, with `http(s)_proxy` pointed at a dead port | **107 passed, 1 deselected** — confirms mocks, no real egress |
| `pytest --collect-only` | live test deselected by default (`addopts -m "not live"`) |
| `pytest -m live` without `NWS_LIVE_TESTS=1` | **1 skipped, 107 deselected** — correctly double-gated |
| `python -m pytest` on `master` | **67 passed** — baseline preserved, no tracked file modified |
| `WEATHER_PROVIDER=nws python -m pytest` | 8 failed, 99 passed — see MAJOR 3 |
| `NWS_BASE_URL=http://localhost:9 python -m pytest` | 1 failed, 47 passed, 59 errors — see MAJOR 3 / MINOR 4 |

Coverage of the 15 behaviours the issue required: all present. `coverage` is not
installed in the venv, so no line-coverage percentage was measured; coverage was
assessed by reading the tests against the code.

## Remaining risks

- **Availability under partial NWS degradation** (MAJOR 1): one misbehaving
  station fails a request that had two working candidates left.
- **Operational noise and misleading status codes** (MAJOR 2): caller-driven
  upstream 4xx produce 502s and stack traces on an unauthenticated endpoint.
- **Worker exhaustion under a slow-but-not-failing upstream** (MINOR 6): no
  cumulative deadline; roughly 50 s worst case per request at default settings.
- **Unrate-limited upstream amplification**: `/api/weather/observations` is
  unauthenticated and the 4-decimal cache key (~11 m) is trivially enumerable, so
  the 256-entry LRU does not protect api.weather.gov from a caller walking
  coordinates. NWS access is courtesy-based; a rate limit belongs here before
  this is exposed publicly.
- **Latent SSRF on the cached `forecast` URL** (MINOR 7) if a future change
  fetches it.
- **Default User-Agent**: with `NWS_USER_AGENT` unset the provider sends
  `WeatherReport/0.1 (set NWS_USER_AGENT with a contact)`, which carries no
  contact and does not meet NWS's stated guidance.
- Reviewer conclusions about live NWS behaviour (e.g. whether every
  out-of-coverage point really returns the `/InvalidPoint` type) rest on code
  reading; no network calls were made during this review.

## Recommendation

**REQUEST CHANGES.**

This is not a block and not a quality objection to the bulk of the work — the
security-critical component is sound, the data handling is honest, and the test
suite is a genuine regression trap rather than decoration. Committing the branch
as it stands is safe and loses nothing.

Before merging to `master`, close:

1. MAJOR 1 — fail over on malformed/transport station failures, and fix the
   comment at `nws_provider.py:238-239` to match.
2. MAJOR 2 — map upstream 404/400 to a client error and stop logging tracebacks
   for expected upstream HTTP failures.
3. MAJOR 3 — add `WEATHER_PROVIDER` and the three `NWS_*` variables to
   `_WEATHER_ENV_VARS` in `tests/conftest.py`.
4. MINOR 8 (cheap, and it is what let MAJOR 1 through) — add station-step timeout
   and malformed-body tests asserting a single observation request.

The remaining minors and informationals are reasonable follow-up issues and
should not gate the merge.
