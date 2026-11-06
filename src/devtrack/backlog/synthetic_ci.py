"""Synthetic CI builds and test results for the offline seed.

Git has no CI history, so the seed simulates one, shaped exactly like what the live
path ingests: GitHub Actions workflow runs (builds) and per-test results (the same
payload `devtrack ingest-junit` produces from a JUnit XML report).

* One build per merged PR (event "push" on main), and one per release tag (event
  "release"), in time order. Deterministic: seeded by repo and PR/tag.
* Test results only for the tests linked to acceptance criteria, in every build at or
  after the commit that introduced the test.
* One scripted regression, so the seed contains a requirement that fails on the latest
  build: click's ``test_tempfile_pager_closes_file_before_unlink`` starts failing on
  1 October 2026.

Every record is flagged ``_provenance: synthetic`` and lands with
source_system = 'synthetic'.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import random
from collections.abc import Iterator
from typing import Any

from devtrack.db import Database

# (repo, test path, test name, failing from) -- a regression on purpose.
SCRIPTED_FAILURES = [
    (
        "pallets/click",
        "tests/test_termui.py",
        "test_tempfile_pager_closes_file_before_unlink",
        "2026-10-01T00:00:00Z",
    ),
]
FAILURE_MESSAGE = (
    "AssertionError: temp file still open at unlink: PermissionError: [WinError 32] "
    "The process cannot access the file because it is being used by another process"
)


def _parse(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _fmt(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_source_id(repo: str, run_number: int) -> str:
    return f"{repo}#build-{run_number}"


def _run_id(repo: str, key: str) -> int:
    return int(hashlib.sha1(f"{repo}:{key}".encode()).hexdigest()[:10], 16)


def _fails(repo: str, when: str) -> bool:
    return any(r == repo and when >= start for r, _, _, start in SCRIPTED_FAILURES)


def synthetic_builds(snapshot: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    repo = snapshot["repo"]
    events: list[tuple[str, str, str, str]] = []  # (time, sha, event, key)
    for pr in snapshot["pulls"]:
        if pr.get("merged_at") and pr.get("merge_commit_sha"):
            events.append((pr["merged_at"], pr["merge_commit_sha"], "push", f"pr-{pr['number']}"))
    for rel in snapshot.get("releases", []):
        events.append((rel["published_at"], rel["target_sha"], "release", f"rel-{rel['tag_name']}"))
    events.sort()
    for i, (when, sha, event, key) in enumerate(events, start=1):
        rng = random.Random(f"{repo}:{key}")
        run_number = 1000 + i
        started = _parse(when) + dt.timedelta(minutes=1)
        finished = started + dt.timedelta(minutes=rng.randint(6, 14))
        conclusion = "failure" if _fails(repo, _fmt(started)) else "success"
        yield (
            "build",
            build_source_id(repo, run_number),
            {
                "id": _run_id(repo, key),
                "name": "CI",
                "run_number": run_number,
                "head_sha": sha,
                "head_branch": "main" if event == "push" else key.removeprefix("rel-"),
                "event": event,
                "status": "completed",
                "conclusion": conclusion,
                "run_started_at": _fmt(started),
                "updated_at": _fmt(finished),
                "html_url": None,
                "_repo": repo,
                "_key": key,
                "_provenance": "synthetic",
            },
        )


def synthetic_test_reports(db: Database) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """One report per synthetic build, covering the criterion-linked tests that existed
    when the build ran. Reads builds and test links from the database, so run it after
    the first normalization pass."""
    tests = db.query(
        "SELECT DISTINCT tc.repo, tc.path, tc.name, c.committed_at AS introduced_at"
        " FROM criterion_tests ct JOIN test_cases tc ON tc.id = ct.test_case_id"
        " JOIN commits c ON c.id = tc.commit_id"
    )
    builds = db.query(
        "SELECT repo, source_id, started_at FROM builds WHERE source_system = 'synthetic' ORDER BY repo, run_number"
    )
    for b in builds:
        started = str(b["started_at"])
        results = []
        for t in tests:
            if t["repo"] != b["repo"] or str(t["introduced_at"]) > started:
                continue
            failing = any(
                r == t["repo"] and p == t["path"] and n == t["name"] and started >= since
                for r, p, n, since in SCRIPTED_FAILURES
            )
            rng = random.Random(f"{b['source_id']}:{t['path']}::{t['name']}")
            results.append(
                {
                    "path": t["path"],
                    "name": t["name"],
                    "outcome": "failed" if failing else "passed",
                    "duration_ms": rng.randint(4, 900),
                    "message": FAILURE_MESSAGE if failing else None,
                }
            )
        if results:
            yield (
                "test_report",
                f"{b['source_id']}/tests",
                {
                    "_repo": b["repo"],
                    "build": b["source_id"],
                    "tests": sorted(results, key=lambda r: (r["path"], r["name"])),
                    "_provenance": "synthetic",
                },
            )
