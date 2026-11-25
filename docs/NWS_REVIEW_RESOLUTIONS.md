# NWS Review Resolutions

Audit trail for the findings raised in `docs/NWS_IMPLEMENTATION_REVIEW.md`
against the keyless NWS observation provider. Every entry below is closed in
Beads; the issue ID is the authoritative record and carries the full close
reason.

All test counts in this document were measured with outbound HTTP proxied to a
dead port (`HTTP_PROXY=http://127.0.0.1:9`), so a passing suite also proves no
test reaches the network.

| Issue | Severity | Area |
|---|---|---|
| WeatherReport-gxo | MAJOR 1+2 | Station failover, upstream 4xx mapping |
| WeatherReport-2rt | MAJOR 3 | Test environment isolation |
| WeatherReport-8sz | MINOR 4 | Startup resilience to bad config |
| WeatherReport-em8 | MINOR 5 | Usable observations discarded |
| WeatherReport-24d | MINOR 6 | Worker-time bounding |
| WeatherReport-d53 | MINOR 7 | SSRF guard uniformity |
| WeatherReport-7dj | MINOR 8 | Test coverage gaps |
| WeatherReport-x3l | MINOR 9 | Double rounding |
| WeatherReport-boi | MINOR 10 | Snapshot location filtering |
| WeatherReport-95x | Informational | Docstring, cache aliasing, docs |

## WeatherReport-gxo — MAJOR 1+2: station failover and upstream 4xx mapping

**Problem.** `fetch_observation()` continued the station loop only when
`details['reason'] == 'http_error'`, so a station returning a non-JSON body
(`reason='malformed'`) or failing at the connection level aborted the entire
request and suppressed the remaining candidates. Separately, an upstream 4xx
that was not exactly problem type `/InvalidPoint` fell through to a bare
`ProviderError` — reported as a 502 server fault and writing a full traceback
on an unauthenticated endpoint, making log volume caller-drivable.

**Fix.** `STATION_ABORT_REASONS = {timeout, untrusted_url, invalid_url}` now
defines the only failures that end the whole request; every other failure is
specific to one station and falls through to the next candidate. Exhaustion
reports `stations_tried`, `last_failure` and `last_upstream_status`.
`_get_json()` takes a request *kind* and maps upstream status on the kind
rather than the problem-type string, which NWS does not guarantee: a point 404
becomes `nws_point_unavailable` even with an HTML body, a point 400 becomes
`nws_invalid_point`, anything else stays a 502. A new
`AppError(log_traceback=...)` keyword marks expected upstream-HTTP errors as
non-traceback-worthy. Station 404s still read as recoverable failover signals,
not client errors.

**Verification.** 21 tests in `tests/test_nws_failover_and_errors.py`
reproducing both of the review's exact repro scenarios.

## WeatherReport-2rt — MAJOR 3: conftest does not scrub NWS_* env vars

**Problem.** `_WEATHER_ENV_VARS` omitted `WEATHER_PROVIDER`, `NWS_USER_AGENT`,
`NWS_BASE_URL` and `NWS_TIMEOUT_SECONDS`, and the scrub was opt-in via the
`clean_env` fixture. Tests that construct `Config()` directly rather than going
through the `app` fixture therefore read the developer's shell, so having
`WEATHER_PROVIDER=nws` exported in an interactive session turned a green suite
red.

**Fix.** The four variables were added, and the scrub became an autouse fixture
so isolation applies to every test regardless of how configuration is built. A
test that *wants* one of these set still does so with `monkeypatch`, which runs
after the autouse fixture. `clean_env` is retained as a no-op alias where a
test documents its env isolation explicitly.

**Verification.** Full suite green with the variables exported in the parent
shell.

## WeatherReport-8sz — MINOR 4: bad NWS_BASE_URL crashes application startup

**Problem.** `NwsWeatherProvider.__init__` ran the configured base URL through
`ensure_nws_url`, and `create_app` builds the provider unconditionally, so
`NWS_BASE_URL=http://localhost:8080` made `create_app()` raise at startup. That
contradicts `app/config.py`'s contract that bad values degrade to safe defaults
so the app always boots.

**Fix.** `build_nws_provider()` trust-checks the configured value itself; on
failure it logs a warning naming the rejected value and the reason, then uses
`DEFAULT_BASE_URL`. The same leniency applies to a non-numeric
`NWS_TIMEOUT_SECONDS`.

**Deliberately not changed.** The SSRF guard. The issue's motivation mentioned
making the knob usable for a local stub, but that is incompatible with the
guard the same review requires: `ensure_nws_url` still allows only
`https://api.weather.gov`, and the provider constructor still hard-rejects
anything else. Only *configuration* is lenient — it warns and reverts to the
default instead of raising. Testing against a fake backend is done by injecting
a session (`NwsWeatherProvider(session=fake)`), not by repointing the base URL.

**Verification.** 40 tests in `tests/test_nws_config_fallback.py` booting
`create_app()` against 8 hostile URL forms (suffix-match, explicit port, wrong
scheme, unparseable, userinfo, and others) and asserting the guard still
rejects each one when it arrives from outside.

## WeatherReport-em8 — MINOR 5: usable observation discarded when timestamp absent

**Problem.** `_is_usable` required `observed_at`, which is only set when
`properties.timestamp` is a string. A payload carrying a temperature and a text
description but no timestamp was rejected, collapsing the whole request into
`nws_no_observation` / 502.

**Fix.** `_is_usable` now requires only that at least one measurement is
present. An undated reading is returned with `observed_at` null, leaving the
caller to decide what to do about it. A payload with no measurements at all is
still rejected — that is the condition which actually makes an observation
worthless.

**Verification.** 5 tests, including the empty-properties case and a
non-string timestamp.

## WeatherReport-24d — MINOR 6: no cumulative deadline across the request chain

**Problem.** One lookup makes up to five sequential upstream requests
(`/points`, the station list, up to three observations). Each had its own
timeout with no shared budget, so a run of slow-but-under-limit responses could
occupy a worker for the sum of the per-request limits. Per-attempt bounding was
correct; worker-time bounding was absent.

**Fix.** `fetch_observation()` fixes one deadline before the first request and
threads it through `get_point` / `_stations` / `_get_json`. Each request is
issued with `min(remaining budget, NWS_TIMEOUT_SECONDS)`, so it is never given
more time than the chain has left nor more than the configured per-request
limit. An exhausted budget raises *before* another request is issued —
`reason=timeout`, `scope=total_budget`, 504, no traceback. Because
`reason=timeout` is already in `STATION_ABORT_REASONS`, exhaustion aborts the
station loop rather than being mistaken for a per-station failure. The budget
defaults to `TOTAL_TIMEOUT_MULTIPLIER` (3) times the per-request timeout and is
overridable with `NWS_TOTAL_TIMEOUT_SECONDS`, which degrades to the derived
default on a bad value. A cached `/points` hit spends no budget.

**Verification.** 14 tests in `tests/test_nws_budget_and_guards.py` driven by
an injected fake `monotonic` clock, so they are deterministic and add no
wall-clock time; they assert the exact per-request timeout sequence
`[10, 8, 5, 2]`.

## WeatherReport-d53 — MINOR 7: unvalidated forecast URL is cached

**Problem.** `observationStations` was guarded by `ensure_nws_url` but the
adjacent `forecast` value was stored in the point cache verbatim. Nothing
fetches it today, so the issue was latent, but the guard was non-uniform at the
extraction site and the first consumer would inherit an SSRF.

**Fix.** Added `_optional_nws_url()`: the same trust check with a different
failure mode — it returns `None` instead of raising. `forecast` goes through it
at extraction, so an untrusted value never reaches the cache, while a bad value
in a field the request does not use cannot fail the lookup.
`observationStations` keeps the hard check and still raises `untrusted_url`.

**Verification.** 16 tests covering 7 hostile `forecast` values through both
the cache and the full request path, plus an explicit test that the two failure
modes are not confused.

## WeatherReport-7dj — MINOR 8: test gaps at the station step and unit branches

**Problem as filed.** The degF temperature branch, the `m_s-1` wind branch, and
the two malformed-structure guards in `get_point` and `_stations` were listed
as untested.

**Correction.** The degF and `m_s-1` branches were *not* still untested —
WeatherReport-x3l covered both while fixing the double-rounding bug. That was
verified before writing anything, rather than adding duplicate coverage.

**Fix.** The genuinely missing coverage was the malformed-structure guards:
`get_point` rejecting `properties` that is null / list / str / int / bool and a
payload with no `properties` key; `_stations` rejecting `features` that is
null / dict / str / int and a payload with no `features` key. Added beyond the
criteria: individual junk entries inside a well-formed `features` list are
skipped rather than fatal (12 shapes, including a non-matching, an over-long
and a non-string `stationIdentifier`), and a good feature mixed in among junk
is still used.

**Verification.** 24 tests in `tests/test_nws_budget_and_guards.py`.

## WeatherReport-x3l — MINOR 9: double rounding on non-canonical input units

**Problem.** `_temperature_c` rounded a degF input to 2 decimals and `c_to_f`
rounded again, so 33 degF round-tripped to 33.01 degF; the `m_s-1` wind path
had the same shape. Output could disagree with the upstream reading by 0.01.

**Fix.** `_temperature_c` / `_speed_kph` return a `_Reading(value, converted)`
carrying the *exact* canonical-unit value plus whether a conversion was
applied. `normalize_observation` derives the paired `_f` / `_mph` figure from
the full-precision value, so a converted reading round-trips to what NWS sent.

**Refinement over the literal criteria.** The criteria said conversions should
round once at output; a value *already* in canonical units is passed through
unrounded instead, because rounding a passthrough would make our output
disagree with the source for no reason. An existing test asserting
`wind_speed_kph == 16.09344` caught this and was correct.

**Verification.** 23 tests parametrized over 8 degF and 6 `m_s-1` values plus
the gust and dewpoint paths.

## WeatherReport-boi — MINOR 10: snapshot location filter misses the submitted string

**Problem.** `location` was rebuilt as `f"{latitude},{longitude}"` from rounded
floats, so a snapshot POSTed as `"45.50000, -122.60"` was stored as
`"45.5,-122.6"` while `list_snapshots` filtered on exact string equality — a
client listing by the string it had just submitted got zero rows.

**Fix.** Two sides, because fixing only one leaves a hole. `canonical_location()`
returns the canonical `"lat,lon"` form of a coordinate pair, or `None` for
free text. `save_snapshot` stores the canonical form, so storage cannot
fragment into un-joinable groups by spelling. `list_snapshots` matches both the
submitted string and its canonical form, which covers odd spellings and rows
written before this behaviour existed. A non-coordinate location keeps exact,
case-sensitive matching rather than guessing at place names.

**Known limit.** Pre-existing non-canonical rows are matched by their own
string, not retro-canonicalized; that would need a migration and is out of
scope.

**Verification.** 36 tests in `tests/test_snapshot_location_filter.py`,
including pagination through both the `IN (?, ?)` and the exact-match branch.
An early version of the fix canonicalized only the query side; the tests caught
that a canonical query then missed a verbatim-stored row, which is what
prompted the write-side change.

## WeatherReport-95x — Informational cleanup

Six items:

1. **Stale docstring.** `weather_provider.py` claimed the application never
   makes outbound requests without credentials. The keyless NWS route always
   does. The docstring now states that selecting `WEATHER_PROVIDER=nws` *is*
   the opt-in, that there is no key to withhold, and that `ensure_nws_url`
   confines the traffic to `api.weather.gov`.
2. **Cache aliasing.** `get_point` returned the cached dict by reference, so a
   caller mutating it corrupted shared state. It now copies on read *and* on
   the cache-miss return path.
3. **Status failing open.** `int(getattr(response, 'status_code', 200) or 200)`
   meant a missing or falsy status read as 200. A status that is missing,
   non-numeric, or outside 100–599 now raises `malformed`. The range check
   matters: a present-but-falsy `0` passes a `None`-only check.
4. **Docs example types.** `wind_direction_deg`, `barometric_pressure_pa` and
   `visibility_m` render as floats, because `_number()` casts unconditionally.
5. **Docs provenance.** The example is labelled illustrative rather than "a
   real reading from KTOP", since its provenance was only ever in the issue
   tracker.
6. **Weak SSRF assertion.** The test asserted only the exception type.
   `ProviderError` is raised for ordinary upstream failures too, so it would
   have passed even if the guard stopped running. It is now parametrized over
   `(url, reason)` and asserts `details['reason']`.
