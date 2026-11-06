"""Read a JUnit XML report (what pytest --junitxml, and most CI test runners, write)
into the test_report payload the normalizer stores as test_runs.

pytest writes ``classname="tests.test_termui"`` and ``file="tests/test_termui.py"``
(the ``file`` attribute only with junit_family=xunit1 or legacy). The path is taken
from ``file`` when present, otherwise rebuilt from the dotted classname; a class inside
the module (``tests.test_termui.TestPager``) is dropped back to its module.

Reports come from CI machines, so they are treated as untrusted input: the standard
library parser doesn't fetch external entities, and any DOCTYPE is refused outright,
which also rules out entity-expansion ("billion laughs") documents.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from devtrack.db import Database


class JUnitError(ValueError):
    """The file is not a JUnit report we can read."""


def _path_from_classname(classname: str, known_paths: set[str]) -> str:
    parts = classname.split(".")
    for cut in range(len(parts), 0, -1):  # longest module prefix that is a known file
        candidate = "/".join(parts[:cut]) + ".py"
        if candidate in known_paths:
            return candidate
    return "/".join(parts[:-1] if len(parts) > 1 and parts[-1][:1].isupper() else parts) + ".py"


def _outcome(case: ET.Element) -> tuple[str, str | None]:
    for tag, outcome in (("error", "error"), ("failure", "failed"), ("skipped", "skipped")):
        child = case.find(tag)
        if child is not None:
            lines = (child.text or "").strip().splitlines()
            message = child.get("message") or (lines[0] if lines else None)
            return outcome, (message[:2000] if message else None)
    return "passed", None


def parse_junit(text: str, *, known_paths: set[str] | None = None) -> list[dict[str, Any]]:
    if "<!DOCTYPE" in text[:2000].upper():
        raise JUnitError("DOCTYPE declarations are not accepted in test reports")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise JUnitError(f"not valid XML: {exc}") from exc
    if root.tag not in ("testsuites", "testsuite"):
        raise JUnitError(f"expected <testsuites> or <testsuite>, found <{root.tag}>")
    known = known_paths or set()
    results: dict[tuple[str, str], dict[str, Any]] = {}
    for case in root.iter("testcase"):
        name = case.get("name")
        if not name:
            continue
        name = name.split("[", 1)[0]  # parametrized ids collapse to the test function
        path = case.get("file") or _path_from_classname(case.get("classname", ""), known)
        outcome, message = _outcome(case)
        seconds = float(case.get("time") or 0)
        key = (path, name)
        prev = results.get(key)
        # A parametrized test passes only if every parameter set passed.
        rank = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}
        if prev is None or rank[outcome] > rank[prev["outcome"]]:
            results[key] = {
                "path": path,
                "name": name,
                "outcome": outcome,
                "duration_ms": int(seconds * 1000) + (prev["duration_ms"] if prev else 0),
                "message": message,
            }
        else:
            prev["duration_ms"] += int(seconds * 1000)
    if not results:
        raise JUnitError("the report has no test cases")
    return sorted(results.values(), key=lambda r: (r["path"], r["name"]))


def junit_report_payload(db: Database, path: Path, *, repo: str, build_source_id: str) -> dict[str, Any]:
    known = {str(r["path"]) for r in db.query("SELECT DISTINCT path FROM test_cases WHERE repo = ?", (repo,))}
    return {
        "_repo": repo,
        "build": build_source_id,
        "tests": parse_junit(path.read_text(encoding="utf-8"), known_paths=known),
        "_provenance": f"junit:{path.name}",
    }
