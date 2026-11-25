"""Guardrails for delegating work to autonomous workers (UI-7, WeatherReport-tgr).

`scripts/delegate_ui_worker.sh` exists because prose instructions did not hold:
a worker told in plain English not to touch the issue tracker ran `bd close`
and `bd remember` on its own issue while two sibling workers were live, and
`.beads/issues.jsonl` is shared state that a concurrent export can corrupt.

These tests assert the policy the script encodes, so a later edit cannot
quietly re-open the hole. They are about the dispatcher, not the app — fast,
and they never launch a worker.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "delegate_ui_worker.sh"
TEXT = SCRIPT.read_text()


def _assignment(name: str) -> str:
    """The right-hand side of a top-level NAME="..." assignment."""
    match = re.search(rf'^{name}="([^"]*)"', TEXT, re.M)
    assert match, f"{name} is no longer a simple quoted assignment in {SCRIPT.name}"
    return match.group(1)


def test_the_dispatcher_exists_and_is_executable():
    assert SCRIPT.exists()
    assert os.access(SCRIPT, os.X_OK), "a dispatcher nobody can run is not a guardrail"


def test_it_is_valid_shell():
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - bash is present on every target
        pytest.skip("bash not available")
    result = subprocess.run(
        [bash, "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_bd_is_never_grantable_to_a_worker():
    """The tracker belongs to the orchestrator. This is the whole issue."""
    assert "Bash(bd:*)" in _assignment("DISALLOWED_TOOLS")
    assert "bd" not in _assignment("ALLOWED_TOOLS").replace("Bash(", "")


@pytest.mark.parametrize(
    "subcommand",
    ["commit", "push", "checkout", "switch", "branch", "stash", "reset", "add",
     "merge", "rebase", "worktree"],
)
def test_state_changing_git_is_denied(subcommand):
    """A --yolo worker has moved a checkout onto another branch mid-task before."""
    assert f"Bash(git {subcommand}:*)" in _assignment("DISALLOWED_TOOLS")


@pytest.mark.parametrize(
    "subcommand", ["git diff", "git status", "git log"]
)
def test_read_only_git_is_still_allowed(subcommand):
    """A worker that cannot read its own diff cannot check its work."""
    assert f"Bash({subcommand}:*)" in _assignment("ALLOWED_TOOLS")


def test_every_shared_file_is_guarded():
    """Files two workers would both want to edit, hashed before and after."""
    guarded = re.search(r"GUARDED=\(\n(.*?)\n\)", TEXT, re.S)
    volatile = re.search(r"VOLATILE_GUARDED=\(\n(.*?)\n\)", TEXT, re.S)
    assert guarded, "the GUARDED list moved; the hash check may be dead"
    assert volatile, "the VOLATILE_GUARDED list moved"
    listed = set(re.findall(r'"([^"]+)"', guarded.group(1)))
    listed |= set(re.findall(r'"([^"]+)"', volatile.group(1)))
    for path in (
        "docs/UI_CLASS_CONTRACT.md",
        "app/templates/base.html",
        "app/routes/ui/__init__.py",
        "app/routes/ui/helpers.py",
        ".beads/issues.jsonl",
    ):
        assert path in listed, f"{path} is shared state and must be guarded"
    root = SCRIPT.parent.parent
    for path in listed:
        assert (root / path).exists(), f"guarding {path}, which does not exist"


def test_the_beads_export_is_treated_as_volatile_not_as_proof():
    """bd re-exports on its own schedule.

    The first run of this dispatcher reported a VIOLATION on
    .beads/issues.jsonl that was really bd's deferred export of the
    orchestrator's own `bd update --claim`. A guard that cries wolf gets
    ignored, so that file is reported separately, with the diff, and the real
    guarantee is the denied `bd` tool.
    """
    volatile = re.search(r"VOLATILE_GUARDED=\(\n(.*?)\n\)", TEXT, re.S)
    assert volatile, "the VOLATILE_GUARDED list moved"
    assert ".beads/issues.jsonl" in volatile.group(1)
    assert "NOTE: volatile guarded state changed" in TEXT
    # and it must still be impossible for a worker to touch it
    assert "Bash(bd:*)" in _assignment("DISALLOWED_TOOLS")


def test_the_workers_interpreter_is_actually_grantable():
    """A worker typing the path it was given must not be denied.

    The first run denied `.venv/bin/python -m pytest` because only the
    absolute interpreter path was allowed, so both workers were blocked from
    running the suite they were told to run.
    """
    allowed = _assignment("ALLOWED_TOOLS")
    assert "Bash(.venv/bin/python:*)" in allowed
    assert "Bash($PY:*)" in allowed


def test_denied_tool_calls_are_surfaced():
    """A worker reaching past its mandate must be visible, not silent."""
    assert "permission_denials" in TEXT
    assert "denied tool calls" in TEXT


def test_a_guarded_modification_is_reported_as_a_violation():
    assert "VIOLATION" in TEXT
    assert 'if [ "$BEFORE" = "$AFTER" ]' in TEXT


def test_the_brief_tells_the_worker_the_tracker_is_not_its_own():
    """Policy stops a tool call; the brief stops the worker from trying."""
    assert "cannot run bd" in TEXT
    assert "orchestrator owns" in TEXT


def test_the_brief_forbids_editing_tests_to_pass():
    """The implementer must never be the author of its own verification."""
    assert "Do not edit any test to make it pass" in TEXT
    assert "never accept the implementer's own tests" in TEXT


def test_the_brief_warns_about_the_doubled_quiet_flag():
    """pytest.ini already sets -q; -q -q hides the summary the worker must report."""
    assert "pytest.ini already sets" in TEXT


def test_it_refuses_an_odd_number_of_arguments():
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover
        pytest.skip("bash not available")
    result = subprocess.run(
        [bash, str(SCRIPT), "only-a-name"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_it_refuses_an_unreadable_prompt_file(tmp_path):
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover
        pytest.skip("bash not available")
    missing = tmp_path / "nope.txt"
    result = subprocess.run(
        [bash, str(SCRIPT), "worker", str(missing)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "DELEGATE_OUT": str(tmp_path / "out")},
    )
    assert result.returncode == 2
    assert "not readable" in result.stderr
    # and it must bail BEFORE spending a worker
    assert not (tmp_path / "out" / "worker.json").exists()
