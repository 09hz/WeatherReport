# Agent Workflow Notes - 2026-11-27

Process notes from an engineering session on `/home/jesusg/PycharmProjects/WeatherReport`, a Flask weather and journal app deployed on Vercel. Issue tracking is Beads via the `bd` CLI. The operating git policy is conservative: the agent does not commit or push unless the user explicitly asks in the current request.

## Delegation tiers

- **Local model worker** — a profile named `localworker` running qwen3.5:4b through Ollama, used for cheap bounded writing tasks such as vault notes.
- **Cloud developer profile** — a `developer` profile running claude-opus-5-5, used for heavy multi-file work.
- **Orchestrating agent** — keeps all decisions, all factual claims and all verification.

## How work was routed

The routing rule: a task touching more than one larger Python file, or needing a coherent design, goes to the developer profile; lighter bounded artifacts go to local workers; decisions and final verification are never delegated.

A concrete tier-2 example: giving the rate limiter and weather cache a shared-state backend spanned `app/rate_limit.py`, `app/services/weather_cache.py`, `app/config.py`, a new `app/shared_state.py` and new tests, so it went to the developer profile. It ran inside a dedicated git worktree on its own branch rather than in the main checkout, so a loose agent could not disturb the user's working tree.

## Verification discipline

Subagent and local-worker reports were treated as self-reports, never as facts. Each claim was re-checked independently by the orchestrator. Two cases where this mattered:

1. The delegated shared-state work claimed success. Re-running the suite confirmed it, and a stronger check was available: the `redis` package was not installed in the virtualenv at all, yet the full suite and all 42 new tests still passed — that is what actually proves the optional-dependency design rather than merely asserting it. The suite was also re-run with `http_proxy` and `https_proxy` pointed at a dead port (`http://127.0.0.1:9`) to prove no live network call happens in tests.
2. A delegated agent reported finding an unrelated bug. Rather than trusting the report, the orchestrator reproduced it with a production-shaped boot and confirmed five config keys resolved to `None`. The bug was real.

## Local-worker pitfalls

The qwen3.5:4b worker appeared to write its assigned file correctly to the
absolute vault path but ALSO create an identical duplicate inside the
unrelated git repository, showing up as a stray untracked file in
`git status`. That diagnosis was **wrong**, and acting on it destroyed real
work.

The vault folder
`/home/jesusg/Documents/AgentKnowledge/ProjectVault/WeatherReport` is a
**symlink** to `/home/jesusg/PycharmProjects/WeatherReport/docs`. Both paths
were therefore the same file — `diff` reported them identical and
`stat -c '%i'` showed a single shared inode. Deleting the repo-side path as a
"stray duplicate" deleted the vault note itself, twice, and both notes had to
be restored from their recorded contents.

Two things made the false theory look confirmed. `find` does not follow
symlinks by default, so it listed only the repo path and made the vault copy
look absent. And `diff` reporting "identical" was read as evidence of copying
when it is equally consistent with one file under two names.

The real lesson is the inverse of the original one: before deleting any
apparent duplicate, run `readlink -f` on the directory and compare inodes with
`stat -c '%i %n'`. Same inode means one file, never a cleanup task. A vault
that symlinks into a repository also means delegated notes legitimately appear
in `git status`; leave them and let the repo owner decide whether that path
should be tracked or ignored.

## Git state pitfalls

The main git checkout was at one point found sitting on the agent's feature branch at an older commit, which made several expected files appear to be missing from disk. Checking `git worktree list`, `git branch -vv` and `git reflog` established what had happened and that nothing was lost; the checkout was clean, so restoring it to master was lossless. The lesson is to verify repository state with git itself before concluding that files are missing or that work was destroyed.

## Separating CLI failure modes

Platform CLI failure has three distinct modes that need separating before touching any deploy config: the binary being absent, the network being blocked, and being unauthenticated.

In this session the Vercel CLI was absent and was installed with `npm install -g vercel`, giving version 67.0.0. A direct curl to `https://api.vercel.com/v2/user` returned HTTP 403 in 0.28 seconds; a fast 403 means DNS, TLS and routing all worked and the API merely refused an unauthenticated request, so the network was never the problem. The remaining gap was authentication, which required the repo owner because `vercel login` runs an interactive device flow that prints its own one-time URL inside the session that owns it. There is no login link an agent can hand over.

## Rules to carry forward

- Route multi-file or design-bearing work to the cloud developer profile, and keep local workers to bounded single artifacts.
- Never delegate decisions, factual claims or final verification — re-check every delegated claim independently.
- Prefer verification that could only pass if the design is correct, such as running the suite with the optional dependency uninstalled and the network pointed at a dead port.
- Run `git status` after every local-worker run in any repository it could reach and delete stray duplicate files before reporting delivery.
- Separate absent binary, blocked network and missing authentication before changing any deploy configuration.

Related: [[01-delegation-playbook]] [[02-branch-and-beads-protocol]] [[04-git-policy]]
