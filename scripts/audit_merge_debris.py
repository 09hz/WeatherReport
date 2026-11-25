"""Audit the tree for merge debris: phantom references and dead code.

Written for WeatherReport after merge 2667f24 produced a tree that did not
import. The merge's failure mode was mixing one side's callers with the other
side's definitions, so this looks for exactly that shape of breakage:

  1. every module imports
  2. routes advertised by GET / exist in the url_map, and vice versa
  3. every name in a module's __all__ actually exists
  4. module-level functions that nothing references (build_provider lost its
     nws branch this way, leaving build_nws_provider unreachable)
  5. docs referencing routes or env vars the code does not define

Run: python scripts/audit_merge_debris.py
Exit code is the number of findings, so it is usable as a gate.
"""

from __future__ import annotations

import ast
import importlib
import os
import pkgutil
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

findings: list[tuple[str, str]] = []


def report(kind: str, message: str) -> None:
    findings.append((kind, message))


# --- 1. every module imports -------------------------------------------------

import app  # noqa: E402

for mod in pkgutil.walk_packages(app.__path__, "app."):
    try:
        importlib.import_module(mod.name)
    except Exception as exc:  # noqa: BLE001 - reporting, not handling
        report("import", f"{mod.name}: {type(exc).__name__}: {exc}")

# --- 2. advertised routes vs the real url_map -------------------------------

from app import create_app  # noqa: E402

application = create_app({"TESTING": True})
client = application.test_client()

real_rules = {
    (rule.rule, method)
    for rule in application.url_map.iter_rules()
    for method in sorted((rule.methods or set()) - {"HEAD", "OPTIONS"})
}
real_paths = {rule for rule, _ in real_rules}

advertised = client.get("/").get_json().get("endpoints", [])
for entry in advertised:
    parts = entry.split()
    method, path = parts[0], parts[1]
    path = path.split("?")[0]
    # the index writes <location> / <id>; the url_map writes converters
    normalized = re.sub(r"<[^>]+>", "<X>", path)
    candidates = {re.sub(r"<[^>]+>", "<X>", p) for p in real_paths}
    if normalized not in candidates:
        report("phantom-route", f"GET / advertises {method} {path}, not in url_map")

api_paths = {
    p for p in real_paths if p.startswith("/api/") and "<" not in p
}
advertised_paths = {
    re.sub(r"<[^>]+>", "<X>", e.split()[1].split("?")[0]) for e in advertised
}
for path in sorted(api_paths):
    if path not in advertised_paths:
        report("unadvertised-route", f"{path} exists but GET / does not list it")

# --- 3. __all__ entries that do not exist -----------------------------------

for py in sorted((ROOT / "app").rglob("*.py")):
    if "__pycache__" in py.parts:
        continue
    tree = ast.parse(py.read_text())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and
                any(getattr(t, "id", None) == "__all__" for t in node.targets)):
            continue
        if not isinstance(node.value, (ast.List, ast.Tuple)):
            continue
        names = [
            e.value for e in node.value.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
        rel = py.relative_to(ROOT)
        dotted = str(rel.with_suffix("")).replace(os.sep, ".").removesuffix(".__init__")
        try:
            module = importlib.import_module(dotted)
        except Exception:
            continue
        for name in names:
            if not hasattr(module, name):
                report("stale-__all__", f"{rel}: __all__ exports missing {name!r}")

# --- 4. unreferenced module-level functions ---------------------------------
# Decorated functions are excluded: a Flask view is referenced by its route
# decorator, not by a call site, so flagging it would be noise.

defined: dict[str, Path] = {}
for py in sorted((ROOT / "app").rglob("*.py")):
    if "__pycache__" in py.parts:
        continue
    tree = ast.parse(py.read_text())
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_") and not node.decorator_list:
                defined[node.name] = py.relative_to(ROOT)

haystack = "\n".join(
    p.read_text()
    for p in list((ROOT / "app").rglob("*.py")) + list((ROOT / "tests").rglob("*.py"))
    if "__pycache__" not in p.parts
)
for name, where in sorted(defined.items()):
    # one occurrence is the definition itself
    if len(re.findall(rf"\b{re.escape(name)}\b", haystack)) <= 1:
        report("dead-code", f"{where}: {name}() is defined but never referenced")

# --- 5. docs referencing routes / env vars that do not exist ----------------

env_vars = set(re.findall(r'_env_(?:str|int|bool)\(\s*"([A-Z_]+)"',
                          (ROOT / "app" / "config.py").read_text()))
flask_keys = set(re.findall(r'"([A-Z_]+)":', (ROOT / "app" / "config.py").read_text()))
known_env = env_vars | flask_keys | {
    "HTTP_PROXY", "HTTPS_PROXY", "NWS_LIVE_TESTS", "FLASK_APP", "FLASK_ENV",
    "FLASK_DEBUG", "PYTHONPATH", "WEATHER_PROVIDER",
}

for doc in sorted((ROOT / "docs").glob("*.md")):
    text = doc.read_text()
    # Markdown filenames and heading SHOUT_CASE are not env vars. Neither are
    # Obsidian wikilink targets: docs/ is a symlinked vault, so [[NOTE_NAME]]
    # is a note reference, not an environment variable.
    text_no_filenames = re.sub(r"[A-Z0-9_]+\.md", "", text)
    text_no_filenames = re.sub(r"\[\[[^\]]+\]\]", "", text_no_filenames)
    for path in sorted(set(re.findall(r"/api/[a-z0-9/_<>-]+", text))):
        clean = re.sub(r"<[^>]+>", "<X>", path.rstrip("/.,`)"))
        candidates = {re.sub(r"<[^>]+>", "<X>", p) for p in real_paths}
        if clean not in candidates:
            report("doc-phantom-route", f"{doc.name}: references {path}")
    for var in sorted(set(re.findall(r"\b([A-Z][A-Z0-9_]{4,})\b", text_no_filenames))):
        if var.startswith("NWS_") or var.startswith("WEATHER_"):
            if var not in known_env:
                report("doc-phantom-env", f"{doc.name}: references {var}")

# --- output ------------------------------------------------------------------

if not findings:
    print("audit clean: no merge debris found")
    raise SystemExit(0)

by_kind: dict[str, list[str]] = {}
for kind, message in findings:
    by_kind.setdefault(kind, []).append(message)
for kind in sorted(by_kind):
    print(f"\n{kind.upper()} ({len(by_kind[kind])})")
    for message in sorted(by_kind[kind]):
        print(f"  - {message}")
print(f"\n{len(findings)} finding(s)")
raise SystemExit(len(findings))
