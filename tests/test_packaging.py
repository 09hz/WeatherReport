"""Packaging guards.

Vercel's Python builder resolves dependencies with uv from pyproject.toml and
IGNORES requirements.txt whenever a pyproject.toml exists. A runtime dependency
added only to requirements.txt therefore passes every local test and then
crashes the deployment at import -- which is exactly how production broke in
WeatherReport-4u0 (ModuleNotFoundError: No module named 'flask' on a build that
reported success in 2s).

The two files are kept in sync by hand, so this pins the invariant
(WeatherReport-r6g).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"


def _normalise(spec: str) -> str:
    """Compare on content, not incidental whitespace or case."""
    return spec.replace(" ", "").lower()


def _requirements_specs() -> set[str]:
    lines = REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    return {
        _normalise(line)
        for raw in lines
        if (line := raw.strip()) and not line.startswith("#")
    }


def _pyproject_specs() -> tuple[set[str], set[str]]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data["project"]
    runtime = {_normalise(dep) for dep in project["dependencies"]}
    optional = {
        _normalise(dep)
        for group in project.get("optional-dependencies", {}).values()
        for dep in group
    }
    return runtime, optional


def test_pyproject_declares_a_project_table_with_dependencies():
    """Without this table uv installs NOTHING and the deploy dies at import."""
    runtime, _ = _pyproject_specs()
    assert runtime, "pyproject.toml [project].dependencies must not be empty"
    assert any("flask" in dep for dep in runtime)


def test_requirements_and_pyproject_do_not_drift():
    runtime, optional = _pyproject_specs()
    declared = runtime | optional
    from_requirements = _requirements_specs()

    missing_from_pyproject = from_requirements - declared
    assert not missing_from_pyproject, (
        "These are in requirements.txt but NOT in pyproject.toml, so Vercel "
        f"will not install them: {sorted(missing_from_pyproject)}"
    )

    missing_from_requirements = declared - from_requirements
    assert not missing_from_requirements, (
        "These are in pyproject.toml but not requirements.txt, so a local "
        f"install diverges from the deployment: {sorted(missing_from_requirements)}"
    )


def test_vercel_entrypoint_is_declared():
    """Vercel cannot detect the app factory without this (FLASK_ENTRYPOINT_NOT_FOUND)."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert data["tool"]["vercel"]["entrypoint"] == "run:app"


def test_setuptools_package_discovery_is_pinned():
    """The flat layout makes auto-discovery abort; the selection must be explicit."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    include = data["tool"]["setuptools"]["packages"]["find"]["include"]
    assert "app*" in include
