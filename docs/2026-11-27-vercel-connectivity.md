# Vercel Connectivity Check - 2026-11-27

## What was broken

Vercel CLI was not on PATH at session start, preventing any vercel command execution from completing.

## What was installed

1. `npm install -g vercel`
2. `Vercel CLI 67.0.0 (native)`
3. `/home/jesusg/.hermes/tools/node-26.12.1-linux-x64/bin/vercel`

## Evidence

- `curl https://api.vercel.com/v2/user` returned HTTP 403 in 0.28s
- Network path and TLS work but unauthenticated requests are refused
- `vercel whoami` prints "Logged out."
- No `.vercel/` directory exists in the repo to link a project

## Beads touched

- WeatherReport-mww — "Install Vercel CLI and verify platform connectivity" — CLOSED this session.
- WeatherReport-cd7 — "Authenticate Vercel CLI and link the WeatherReport project" — OPEN, P1, waiting on the repo owner.
- WeatherReport-2fh — "Share rate-limit and weather-cache state across serverless instances" — P3, CLAIMED and in progress this session.

## Still blocked

- `vercel login` is interactive and cannot run without the repo owner.
- Authentication requires a VERCEL_TOKEN which does not exist in the environment.

Related: [[00-role-charter]] [[02-branch-and-beads-protocol]]

---

**Status update (later the same day).** The two blockers above were both cleared:
the owner completed `vercel login` via the device flow, the project was linked
as `ktp11/weather-report`, and WeatherReport-cd7 and WeatherReport-2fh are now
closed. Reading those logs is what exposed the real production outage — see
[[2026-11-27-vercel-outage-postmortem]].

**Provenance note.** This file was restored from its exact recorded contents
after being deleted by mistake: this vault folder is a symlink to the
repository's `docs/` directory, so the note also appeared as an untracked file
in `git status` and was wrongly treated as a stray duplicate. See
[[2026-11-27-agent-workflow-notes]].
