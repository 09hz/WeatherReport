# Shared Serverless State - 2026-11-27

## The problem
`app/rate_limit.py` and `app/services/weather_cache.py` each kept state in a process-local dict. On a serverless platform every function instance holds its own copy, so `RATE_LIMIT_REQUESTS` acted as a per-instance guard instead of a deployment-wide quota, and the cache hit rate dropped. Tracked as bead WeatherReport-2fh, which is now CLOSED.

## The design
* A new module `app/shared_state.py` holding a backend seam
* The seam exposes only the operations the callers need: `take_tokens` (atomic token-bucket consume with expiry), `get` / `set` (value with a TTL), and `delete` / `clear`
* The in-process backend is the default and keeps the old behaviour exactly
* Setting the `REDIS_URL` environment variable selects a shared Redis backend, and then `RATE_LIMIT_REQUESTS` becomes a deployment-wide quota
* All Redis keys live under the prefix `weather-report:`

## Files changed
* `app/shared_state.py` (new, 445 lines)
* `tests/test_shared_state.py` (new, 614 lines)
* `app/rate_limit.py`
* `app/services/weather_cache.py`
* `app/__init__.py`
* `app/config.py`
* `requirements.txt`
* `docs/VERCEL_DEPLOYMENT.md`

## Verification
* `python -m pytest -q` passed with zero failures
* The suite was re-run with `http_proxy=http://127.0.0.1:9` and `https_proxy=http://127.0.0.1:9`, and still passed, which proves no live network call happens during tests
* The `redis` package is NOT installed in the virtualenv, and the whole suite still passes. This proves Redis is genuinely an optional dependency and the in-process backend is the real default

## Bug found, not fixed
Filed as bead WeatherReport-9vj, priority P2, still OPEN. `Config.to_flask_mapping()` in `app/config.py` never emits the `RATE_LIMIT_*`, `REQUEST_LOG_*` or `WEATHER_CACHE_*` values. A production-shaped boot was run to check this. `RATE_LIMIT_ENABLED`, `RATE_LIMIT_REQUESTS`, `WEATHER_CACHE_TTL_SECONDS` and `REQUEST_LOG_ENABLED` all came back as `None`. So rate limiting is silently off in a real deployment even though the default in the code is True, and an explicit `RATE_LIMIT_REQUESTS=7` was ignored. It was deliberately NOT fixed, because fixing it switches rate limiting on by default in production and the repo owner must decide that first.

## Git state
* The work is on branch `feature/shared-serverless-state` in a git worktree
* Nothing was committed and nothing was pushed
* The main checkout stayed on branch `master` at commit fb9e446

Related: [[2026-11-27-vercel-connectivity]] [[04-git-policy]]

---

**Status update (later the same day).** WeatherReport-9vj was subsequently fixed
and closed: the seven missing keys were added to `to_flask_mapping()`, with
`REQUEST_LOG_PATH` shipped alongside `REQUEST_LOG_ENABLED` because
`request_log.py` indexes it directly. Per-visitor keying was proved safe before
enabling the dormant limiter, and the live `x-ratelimit-*` headers now confirm
it is enforcing in production.

This branch still has not been merged. Note that it branches from `fb9e446`,
which predates `pyproject.toml`, so every push to it produces a FAILING Vercel
preview (`FLASK_ENTRYPOINT_NOT_FOUND`); rebasing onto master fixes that.

**Provenance note.** This file was restored from its exact recorded contents
after being deleted by mistake — this vault folder is a symlink to the
repository's `docs/` directory, so the note also appeared as an untracked file
in `git status` and was wrongly treated as a stray duplicate. See
[[2026-11-27-agent-workflow-notes]].
