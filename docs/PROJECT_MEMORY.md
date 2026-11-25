# WeatherReport project memory

Durable notes for anyone (human or agent) picking this project up cold. The
Obsidian vault entry `ProjectVault/WeatherReport` is a symlink to this repo's
`docs/` directory, so every note here is both repo documentation and a vault
note. Keep that in mind: notes are shipped artifacts, not a scratchpad.

Related notes: [[UI_CLASS_CONTRACT]], [[NWS_API_INTEGRATION]],
[[NWS_IMPLEMENTATION_REVIEW]], [[NWS_REVIEW_RESOLUTIONS]].

## What the project is

A Flask application with two halves that share one SQLite database:

- **Weather** — fetches current conditions from a provider, caches them in
  process with a TTL, and can persist a *snapshot* row.
- **Journal** — dated text entries, optionally attached to a stored snapshot.

Shape of the code:

```
app/__init__.py        application factory; resolves the provider ONCE at startup
app/config.py          environment -> config mapping
app/errors.py          AppError hierarchy + Flask error handlers (JSON bodies)
app/validation.py      field validators shared by every route
app/rate_limit.py      process-wide throttle (disabled under TESTING)
app/request_log.py     request logging (disabled under TESTING)
app/database/          connection handling + schema
app/services/          weather_service, journal_service, weather_cache, providers
app/routes/            health, weather, journal, limits (JSON) + ui/ (HTML)
```

### Invariants worth knowing before you change anything

- **`GET /` is the JSON endpoint index.** Two test modules assert it. The HTML
  UI therefore lives under `/ui`, never at the root.
- **Providers are resolved at startup, not per request.** With no credentials
  the factory installs an *unconfigured* provider, so a test can never make an
  outbound call by accident. The coordinate-based observations route always
  uses the keyless NWS provider.
- **Tests are network-hostile on purpose.** A `conftest.py` autouse fixture
  scrubs provider configuration from the environment for *every* test, and
  also neutralises local dotenv loading, because a developer's shell was once
  enough to turn the suite red. Re-run suspicious tests with both proxy
  variables pointed at a dead port to prove the mocks are real.
- **The UI calls the service layer directly.** A view must not issue an HTTP
  request to its own application: it deadlocks the single-threaded dev server
  and double-counts the rate limiter.
- **Nulls are first-class.** Every weather measurement, and the observation
  timestamp, can be null. The UI renders them as an explicit placeholder dash;
  blank cells and the string `None` are both bugs.

## History you will otherwise rediscover the hard way

A merge (`2667f24`) resolved several files in favour of one side while keeping
the *other* side's callers. The result imported but was quietly hollow:
coordinate helpers deleted while the modules importing them survived, a route
dropped while the docs still advertised it, a provider branch lost so its
builder became unreachable. Commit `a74447e` repaired it.

Two lessons stuck:

1. **A green import is not a green tree.** `scripts/audit_merge_debris.py`
   exists because of this merge. It checks that every module imports, that the
   routes advertised by `GET /` match the real url_map in both directions, that
   `__all__` entries exist, that no module-level function is unreferenced, and
   that the docs do not cite routes or environment variables the code does not
   define. Its exit code is the finding count, so it works as a CI gate. **Run
   it after any merge.**
2. **A closed issue whose code is missing usually means unmerged, not lost.**
   Check `git log --all` before filing a regression.

## Current state: the brutalist UI (shipped)

Tracked in Beads as UI-0 … UI-5, all closed. Design brief from the product
owner, treated as non-negotiable and enforced by tests: black/white/grey only,
no transitions, no animations, no border-radius, no box-shadow, no gradients,
no webfonts or CDN links, no JavaScript. [[UI]] is the reader's entry point;
[[UI_CLASS_CONTRACT]] is the frozen class vocabulary.

The foundation deliberately split the UI into a package so the page sets could
be built in parallel:

```
app/routes/ui/__init__.py   blueprint at /ui, NAV, context processor
app/routes/ui/helpers.py    page_window, pager, notice_from_error,
                            field_errors, present  <- shared so every page
                            renders nulls and pagination identically
app/routes/ui/weather.py    /ui/weather*
app/routes/ui/journal.py    /ui/journal*
app/templates/base.html     masthead / nav / main / footer chrome
app/static/css/brutalist.css
```

Two conventions hold on every page: **every write is POST-then-redirect**, and
**every expected failure is a page** — views catch `AppError` and render it
with the error's own status code, because the app-wide handler answers in JSON,
which is right for the API and wrong for a browser.

### How it was built, and what that taught

The work was delegated: three Claude Code workers in parallel (stylesheet,
weather pages, journal pages) plus a local `qwen3.5:4b` worker on the doc
draft. What made it work was **disjoint file ownership plus a frozen interface
written before any of them started**. The class contract was not documentation
after the fact; it was the thing that let three authors agree without talking.

- Give each worker an explicit *you own these files* list and an explicit
  *these files belong to someone else* list. The shared ones to protect here
  are the contract, `base.html`, `helpers.py` and anything under `tests/`.
- **Never let the author of the code write its tests.** `tests/test_ui_pages.py`
  was written separately from the pages, which is the only reason it is
  evidence. Two of its tests failed first against the real code and were fixed
  rather than the code — that is how the unique `(date, location)` rule was
  discovered, and it became the 409 conflict test.
- A worker's self-report is a claim about itself. Re-run the suite and read
  the diff yourself. Also check the *guarded* files really are untouched
  (`md5sum` before and after is enough).
- Workers do not reliably stay inside their mandate: one closed its own Beads
  issue despite being told not to, while two others declined for the right
  reason. `.beads/issues.jsonl` is shared state and concurrent exports corrupt
  it, so the tracker belongs to the orchestrator alone. Filed as UI-7.
- The local 4B model's draft was structurally reusable and factually not:
  it inlined an entire document it should have linked, and misattributed a
  rationale. Delegate the skeleton, write the facts yourself.
- Three independent workers flagging the *same* three contract gaps is a
  strong signal the contract is wrong, not the workers. Filed as UI-6.

## Conventions in force

- Beads (`bd`) is the only task tracker. No markdown TODO lists, no
  `MEMORY.md`. Issues live in a local Dolt database; `.beads/issues.jsonl` is
  a passive export, never the source of truth, and must not be hand-edited.
- Close reasons are the audit trail. A good one states the *mechanism* of the
  fix, what was deliberately left alone and why, and concrete verification
  (which tests, how many, which suite result).
- Conservative git policy: an agent does not commit or push unless asked. A
  finished session normally ends with verified work sitting uncommitted and
  the issue closed — that is a *normal* end state, not an interruption.
- One commit per closed issue where the hunks allow it; every commit green.
- Interpreter is the repo's own `.venv` (Python 3.14). Shell helpers may be
  aliased interactive, so scripted file operations use the forcing flags.

## Verification commands

```
.venv/bin/python -m pytest -q                  # full suite
.venv/bin/python scripts/audit_merge_debris.py # merge-debris gate
bd ready                                       # what is actually actionable
```
