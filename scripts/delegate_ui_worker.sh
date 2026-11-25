#!/usr/bin/env bash
# delegate_ui_worker.sh — dispatch Claude Code workers at UI work in this repo.
#
# WeatherReport-tgr (UI-7). During the first parallel UI build the workers were
# told in prose which files they owned and which they must not touch. Two obeyed.
# The third ran `bd close` and `bd remember` on its own issue while two siblings
# were live, and `.beads/issues.jsonl` is shared state that a concurrent export
# can corrupt. Prose is advice; --disallowedTools is policy. This script is the
# policy.
#
# Guarantees, in order of how much they matter:
#   1. A worker cannot run `bd` at all. The tracker belongs to the orchestrator.
#   2. A worker cannot run a git command that moves HEAD, the index or a remote.
#   3. Guarded shared files are hashed before and after; a modification is
#      reported as a FAILURE of that worker, not quietly accepted.
#   4. Workers run concurrently only with disjoint OWNED file lists.
#
# Usage:
#   ./scripts/delegate_ui_worker.sh <name> <prompt-file> [<name> <prompt-file> ...]
#
# A prompt file is plain text. It is appended to the shared brief below, so it
# only needs the task and the worker's own "YOUR FILES" list.

set -u

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OUT=${DELEGATE_OUT:-$HOME/.hermes/cache/scratch/ui-delegation}
PY="$REPO/.venv/bin/python"

# Shared state no worker may write. Add to this list, never remove from it.
GUARDED=(
  "docs/UI_CLASS_CONTRACT.md"
  "app/templates/base.html"
  "app/routes/ui/__init__.py"
  "app/routes/ui/helpers.py"
)

# Guarded too, but bd rewrites it on a deferred export of the ORCHESTRATOR's own
# `bd` calls, so a hash change here is not by itself evidence against a worker.
# Reported separately, with the diff, instead of as a flat violation. The real
# guarantee for this file is the tool policy below: a worker cannot run bd.
VOLATILE_GUARDED=(
  ".beads/issues.jsonl"
)

# `bd` is absent on purpose: no entry here can re-enable it. Both the absolute
# and the repo-relative interpreter path are granted, because a worker told to
# run `.venv/bin/python -m pytest` will type exactly that, and a pattern that
# only matches the absolute form silently denies the worker its own test run.
ALLOWED_TOOLS="Read,Edit,Write,Glob,Grep,WebSearch,WebFetch,Bash($PY:*),Bash(.venv/bin/python:*),Bash(ls:*),Bash(cat:*),Bash(grep:*),Bash(rg:*),Bash(find:*),Bash(git diff:*),Bash(git status:*),Bash(git log:*)"
DISALLOWED_TOOLS="Bash(bd:*),Bash(git commit:*),Bash(git push:*),Bash(git checkout:*),Bash(git switch:*),Bash(git branch:*),Bash(git stash:*),Bash(git reset:*),Bash(git add:*),Bash(git merge:*),Bash(git rebase:*),Bash(git worktree:*)"

BRIEF=$(cat <<EOF
PROJECT: WeatherReport, a Flask weather+journal JSON API with a server-rendered
UI mounted at /ui. Interpreter: $PY (run tests as: $PY -m pytest).
Repository root: $REPO

READ THESE FIRST. They are the contract for your task:
  docs/UI_CLASS_CONTRACT.md   FROZEN list of every allowed CSS class
  docs/UI.md                  how the UI is meant to behave
  app/routes/ui/__init__.py   the blueprint, NAV, context processor
  app/routes/ui/helpers.py    page_window/pager/notice_from_error/field_errors/present
  app/templates/base.html     the layout pages extend
  tests/test_ui_foundation.py the gate you must keep green
  tests/test_ui_pages.py      the behaviour you must not regress

HARD DESIGN CONSTRAINTS (product owner, non-negotiable, enforced by tests):
  black / white / grey ONLY - no colour of any kind
  no transitions, no animations, no border-radius, no box-shadow, no gradients
  no webfonts, no CDN links, no external URLs, no @import, no JavaScript

HARD PROCESS RULES:
  - You may ONLY create or edit the files listed under YOUR FILES. Touching any
    other file is a task failure; the orchestrator hashes the shared files and
    checks. These are guarded and are someone else's right now:
      ${GUARDED[*]}
  - Use ONLY class names already in docs/UI_CLASS_CONTRACT.md. If one is
    missing, use the closest existing class and REPORT the gap; do not edit the
    contract. The orchestrator owns it.
  - No inline style= attributes and no <style> blocks.
  - Never render a null API value as a blank or as the string "None": use the
    .placeholder span. helpers.present() already marks null fields.
  - You cannot run bd and you cannot run git commands that change state. The
    orchestrator owns the issue tracker and every commit. Do not ask for an
    exception; report what you did and let it close the issue.
  - Do not edit any test to make it pass. If a test is genuinely wrong, say so
    in your final answer and leave it failing.
  - Run \`$PY -m pytest\` before finishing. Note that pytest.ini already sets
    -q, so do NOT add another -q or the summary line is suppressed.
  - End your final answer with: files changed, the pytest summary line you
    actually saw, and any contract gaps you hit.
EOF
)

if [ $# -lt 2 ] || [ $(( $# % 2 )) -ne 0 ]; then
  echo "usage: $0 <name> <prompt-file> [<name> <prompt-file> ...]" >&2
  exit 2
fi

mkdir -p "$OUT"
cd "$REPO" || exit 1

hash_guarded() {
  for path in "${GUARDED[@]}"; do
    [ -e "$path" ] && md5sum "$path"
  done
}

hash_volatile() {
  for path in "${VOLATILE_GUARDED[@]}"; do
    [ -e "$path" ] && md5sum "$path"
  done
}

BEFORE=$(hash_guarded)
BEFORE_VOLATILE=$(hash_volatile)

run_worker() {
  local name="$1" prompt="$2"
  claude -p "$prompt" \
    --permission-mode acceptEdits \
    --allowedTools "$ALLOWED_TOOLS" \
    --disallowedTools "$DISALLOWED_TOOLS" \
    --max-turns 60 --output-format json \
    > "$OUT/$name.json" 2> "$OUT/$name.err"
  echo "$name exit=$?" >> "$OUT/status.txt"
}

: > "$OUT/status.txt"
names=()
while [ $# -gt 0 ]; do
  name="$1"; file="$2"; shift 2
  if [ ! -r "$file" ]; then
    echo "prompt file not readable: $file" >&2
    exit 2
  fi
  names+=("$name")
  run_worker "$name" "$BRIEF

$(cat "$file")" &
done
wait

AFTER=$(hash_guarded)
AFTER_VOLATILE=$(hash_volatile)

echo
echo "=== workers ==="
cat "$OUT/status.txt"
echo
echo "=== denied tool calls (a worker reaching past its mandate) ==="
for name in "${names[@]}"; do
  denied=$("$PY" - "$OUT/$name.json" <<'PYEOF'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception as exc:
    print(f"  (unreadable transcript: {exc})")
    raise SystemExit
for entry in data.get("permission_denials") or []:
    command = (entry.get("tool_input") or {}).get("command", "")
    print(f"  {entry.get('tool_name')}: {command[:120]}")
PYEOF
)
  echo "$name:"
  [ -n "$denied" ] && echo "$denied" || echo "  none"
done
echo
echo "=== guarded files ==="
if [ "$BEFORE" = "$AFTER" ]; then
  echo "unchanged (${#GUARDED[@]} files)"
else
  echo "VIOLATION: a worker modified guarded shared state:"
  diff <(echo "$BEFORE") <(echo "$AFTER")
  echo "Review before accepting any of this work."
fi
if [ "$BEFORE_VOLATILE" != "$AFTER_VOLATILE" ]; then
  echo
  echo "NOTE: volatile guarded state changed (${VOLATILE_GUARDED[*]})."
  echo "bd re-exports this on its own schedule, so this is expected after any"
  echo "orchestrator bd call. Confirm it is yours:"
  git --no-pager diff --stat -- "${VOLATILE_GUARDED[@]}"
  echo "A worker cannot have caused it unless 'denied tool calls' above shows bd."
fi
echo
echo "=== verify next (the orchestrator's job, not a worker's) ==="
cat <<EOF
  $PY -m pytest
  $PY scripts/audit_merge_debris.py
  git diff --stat
  # then write the tests yourself - never accept the implementer's own tests
EOF
echo "transcripts: $OUT/<name>.json"
