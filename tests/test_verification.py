"""Verification: method per requirement, builds and releases in the chain, change
control that invalidates evidence and opens re-verification items."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from devtrack.backlog.loader import read_backlog
from devtrack.db import Database
from devtrack.github.client import GitHubClient
from devtrack.ingest.junit import JUnitError, junit_report_payload, parse_junit
from devtrack.ingest.normalize import process_staging
from devtrack.ingest.seed import DEFAULT_SEED_DIR, load_seed
from devtrack.ingest.staging import get_watermark, stage, start_batch
from devtrack.ingest.sync import sync_repo
from devtrack.verification import (
    BuildRef,
    CriterionVerification,
    RequirementVerification,
    VerificationError,
    criterion_status,
    find_build,
    record_verification,
    refresh,
    requirement_status,
    rollup,
)
from tests.fakes import FakeGitHub, api_commit, api_run


@pytest.fixture
def loaded(db: Database) -> Database:
    load_seed(db)
    return db


def by_key(
    db: Database, *, release: str | None = None, repo: str | None = None
) -> dict[str, RequirementVerification]:
    return {r.key: r for r in requirement_status(db, release=release, repo=repo)}


def crit(req: RequirementVerification, key: str) -> CriterionVerification:
    return next(c for c in req.criteria if c.key == key)


# -- pure rules -------------------------------------------------------------------------


def _build(i: int) -> BuildRef:
    return BuildRef(i, "r", i, "a" * 40, "success", f"2026-01-0{i}T00:00:00Z", "push")


def test_status_precedence() -> None:
    target = _build(3)
    on_target = {"build_id": 3, "result": "passed"}
    older = {"build_id": 2, "result": "passed"}
    failed = {"build_id": 2, "result": "failed"}
    item = [{"version": 2, "opened_at": "x"}]
    assert criterion_status([failed, older], [], target)[0] == "failed"  # newest result failed
    assert criterion_status([on_target], [], target)[0] == "verified"
    assert criterion_status([], item, target)[0] == "reverify"
    assert criterion_status([older], [], target)[0] == "verified_older_build"
    assert criterion_status([], [], target)[0] == "not_verified"
    assert criterion_status([], [], None)[0] == "not_verified"


def test_rollup() -> None:
    assert rollup(["verified", "failed", "reverify"]) == "failed"
    assert rollup(["verified", "reverify"]) == "needs_reverification"
    assert rollup(["verified", "verified"]) == "verified"
    assert rollup(["verified", "verified_older_build"]) == "verified_older_build"
    assert rollup(["verified", "not_verified"]) == "partial"
    assert rollup(["not_verified"]) == "not_verified"
    assert rollup([]) == "not_verified"


# -- the seeded story -------------------------------------------------------------------


def test_seed_statuses_against_latest_build(loaded: Database) -> None:
    s = by_key(loaded)
    click1 = s["REQ-CLICK-01"]
    assert click1.status == "failed" and click1.target.label == "#1049"
    regression = crit(click1, "AC-CLICK-101-3")
    assert regression.status == "failed"
    assert regression.result["evidence"][0]["test"].endswith("test_tempfile_pager_closes_file_before_unlink")
    # The change to v2 opened items that later passing builds closed.
    assert not click1.open_items

    assert s["REQ-CLICK-02"].status == "partial"
    assert s["REQ-HTTPX-03"].status == "failed"
    assert crit(s["REQ-HTTPX-03"], "AC-HTTPX-102-3").result["verified_by"] == "jamal-wright"
    assert s["REQ-REQ-02"].status == "needs_reverification"
    assert s["REQ-REQ-02"].open_items == 3


def test_only_the_requirements_own_method_counts(loaded: Database) -> None:
    """REQ-CLICK-03 is verified by inspection: CI results on its criteria are ignored."""
    s = by_key(loaded)
    for c in s["REQ-CLICK-03"].criteria:
        assert c.result is None or c.result["method"] == "inspection"


def test_change_control_invalidates_and_supersedes(loaded: Database) -> None:
    rows = loaded.query(
        "SELECT v.invalidated_reason FROM verification_results v JOIN acceptance_criteria a ON a.id = v.criterion_id"
        " WHERE a.key = 'AC-REQ-202-1' AND v.method = 'analysis'"
    )
    assert rows and all(r["invalidated_reason"] == "REQ-REQ-02 changed to v3 on 2026-06-20" for r in rows)
    states = {
        (r["requirement_version"], r["status"])
        for r in loaded.query(
            "SELECT i.requirement_version, i.status FROM reverification_items i"
            " JOIN requirements r ON r.id = i.requirement_id WHERE r.key = 'REQ-REQ-02'"
        )
    }
    # The June analyses answered v2 (they sit between v2 and v3), so v2's items closed;
    # v3 then invalidated those analyses and opened its own items, which stay open.
    assert states == {(2, "closed"), (3, "open")}


def test_refresh_is_idempotent(loaded: Database) -> None:
    before = loaded.query("SELECT * FROM verification_results ORDER BY id")
    items = loaded.query("SELECT * FROM reverification_items ORDER BY id")
    stats = refresh(loaded)
    assert sum(stats.values()) == 0
    assert loaded.query("SELECT * FROM verification_results ORDER BY id") == before
    assert loaded.query("SELECT * FROM reverification_items ORDER BY id") == items


def test_status_against_a_release(loaded: Database) -> None:
    s = by_key(loaded, release="v2.34.0", repo="psf/requests")
    req3 = s["REQ-REQ-03"]
    assert req3.target.release == "v2.34.0"
    assert crit(req3, "AC-REQ-203-1").status == "verified"  # demonstrated on that release's build
    # The repo filter keeps the parents whose subtree still has something in scope.
    assert set(s) == {"SYS-01", "SUB-HTTP-01", "REQ-REQ-01", "REQ-REQ-02", "REQ-REQ-03"}
    assert s["SUB-HTTP-01"].children == ["REQ-REQ-01", "REQ-REQ-02", "REQ-REQ-03"]


def test_results_after_the_target_build_do_not_count(loaded: Database) -> None:
    """Against release 8.4.0, click results from later builds must be ignored."""
    s = by_key(loaded, release="8.4.0", repo="pallets/click")
    cutoff = s["REQ-CLICK-02"].target.finished_at
    for req in s.values():
        for c in req.criteria:
            if c.result_build:
                assert c.result_build.finished_at <= cutoff


def test_manual_signoff_closes_its_item_then_a_new_change_reopens(loaded: Database) -> None:
    latest = find_build(loaded, "psf/requests", "1051")
    record_verification(
        loaded,
        criterion_key="AC-REQ-202-1",
        build_id=latest.id,
        method="analysis",
        result="passed",
        verified_by="nina-kowalski",
        note="re-reviewed against v3",
        verified_at="2026-07-10T09:00:00Z",
    )
    refresh(loaded)
    c = crit(by_key(loaded)["REQ-REQ-02"], "AC-REQ-202-1")
    assert c.status == "verified" and not c.open_items

    # The requirement changes again: the fresh sign-off is invalidated, a v4 item opens.
    req = next(
        r for r in read_backlog(DEFAULT_SEED_DIR / "backlog.json")["requirements"] if r["key"] == "REQ-REQ-02"
    )
    req = {k: v for k, v in req.items() if k != "criteria"}
    req["revisions"] = req["revisions"] + [
        {
            "version": 4,
            "body": "Tightened again.",
            "changed_at": "2026-08-01T12:00:00Z",
            "note": "audit finding",
        }
    ]
    stage(loaded, start_batch(loaded, "backlog", "change"), "backlog", [("requirement", "REQ-REQ-02", req)])
    assert process_staging(loaded)["requirement.written"] == 1
    stats = refresh(loaded)
    assert stats["invalidated"] >= 1 and stats["items_opened"] == 3
    # v3 items for the two criteria nobody re-verified are superseded; 202-1 stays closed.
    assert stats["items_superseded"] == 2
    c = crit(by_key(loaded)["REQ-REQ-02"], "AC-REQ-202-1")
    assert c.status == "reverify" and c.open_items[0]["version"] == 4
    assert any("changed to v4" in x["reason"] for x in c.invalidated)


def test_record_verification_guards(loaded: Database) -> None:
    click = find_build(loaded, "pallets/click", "1049")
    ok: dict[str, Any] = {
        "criterion_key": "AC-CLICK-201-4",
        "build_id": click.id,
        "result": "passed",
        "verified_by": "sofia-reyes",
    }
    with pytest.raises(VerificationError, match="come from CI"):
        record_verification(loaded, method="test", **ok)
    with pytest.raises(VerificationError, match="named person"):
        record_verification(loaded, method="inspection", **{**ok, "verified_by": " "})
    other = find_build(loaded, "psf/requests", "1051")
    with pytest.raises(VerificationError, match="is in psf/requests"):
        record_verification(loaded, method="inspection", **{**ok, "build_id": other.id})
    record_verification(loaded, method="inspection", **ok)
    refresh(loaded)
    assert crit(by_key(loaded)["REQ-CLICK-03"], "AC-CLICK-201-4").status == "verified"


def test_find_build_accepts_run_number_tag_and_sha(loaded: Database) -> None:
    by_run = find_build(loaded, "pallets/click", "#1049")
    assert find_build(loaded, "pallets/click", by_run.head_sha[:10]).id == by_run.id
    assert find_build(loaded, "pallets/click", "8.5.0").release == "8.5.0"
    with pytest.raises(VerificationError):
        find_build(loaded, "pallets/click", "nope")


# -- JUnit ------------------------------------------------------------------------------

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="5">
  <testcase classname="tests.test_termui" name="test_tempfile_pager_closes_file_before_unlink" time="0.120"/>
  <testcase classname="tests.test_termui" name="test_pipepager_less_raw_mode_detection[-R]" time="0.010"/>
  <testcase classname="tests.test_termui" name="test_pipepager_less_raw_mode_detection[-r]" time="0.010">
    <failure message="assert False">trace</failure>
  </testcase>
  <testcase classname="tests.test_termui.TestPager" name="test_skip_me" time="0"><skipped message="windows only"/></testcase>
  <testcase classname="tests.test_utils.test_style" name="test_styling_invalid_color" time="0.002"><error message="boom"/></testcase>
</testsuite></testsuites>"""


def test_parse_junit() -> None:
    rows = {
        (r["path"], r["name"]): r for r in parse_junit(JUNIT, known_paths={"tests/test_utils/test_style.py"})
    }
    assert (
        rows[("tests/test_termui.py", "test_tempfile_pager_closes_file_before_unlink")]["outcome"] == "passed"
    )
    # Parametrized cases collapse to one test that fails if any case failed.
    raw = rows[("tests/test_termui.py", "test_pipepager_less_raw_mode_detection")]
    assert raw["outcome"] == "failed" and raw["duration_ms"] == 20 and raw["message"] == "assert False"
    assert rows[("tests/test_termui.py", "test_skip_me")]["outcome"] == "skipped"  # class dropped from path
    assert rows[("tests/test_utils/test_style.py", "test_styling_invalid_color")]["outcome"] == "error"


@pytest.mark.parametrize(
    "bad",
    [
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><testsuites/>',
        "<html><body/></html>",
        "<testsuites><testsuite/></testsuites>",
        "not xml",
    ],
)
def test_parse_junit_rejects(bad: str) -> None:
    with pytest.raises(JUnitError):
        parse_junit(bad)


def test_junit_report_fixes_the_regression(loaded: Database, tmp_path: Path) -> None:
    """A real report on a new build where the failing test passes again."""
    click = find_build(loaded, "pallets/click", "1049")
    payload = {
        "id": 77,
        "name": "CI",
        "run_number": 1050,
        "head_sha": click.head_sha,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "run_started_at": "2026-10-06T10:00:00Z",
        "updated_at": "2026-10-06T10:08:00Z",
        "_repo": "pallets/click",
    }
    stage(loaded, start_batch(loaded, "github", "ci"), "github", [("build", "pallets/click#run-77", payload)])
    process_staging(loaded)
    report = tmp_path / "junit.xml"
    linked = loaded.query(
        "SELECT DISTINCT tc.path, tc.name FROM criterion_tests ct JOIN test_cases tc ON tc.id = ct.test_case_id"
        " WHERE tc.repo = 'pallets/click'"
    )
    cases = "".join(
        f'<testcase classname="{r["path"][:-3].replace("/", ".")}" name="{r["name"]}" time="0.1"/>'
        for r in linked
    )
    report.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>", encoding="utf-8")
    body = junit_report_payload(loaded, report, repo="pallets/click", build_source_id="pallets/click#run-77")
    stage(
        loaded,
        start_batch(loaded, "github", "junit"),
        "github",
        [("test_report", "pallets/click#run-77/tests", body)],
    )
    assert process_staging(loaded)["test_report.written"] == 1
    refresh(loaded)
    s = by_key(loaded)
    assert s["REQ-CLICK-01"].target.run_number == 1050
    assert s["REQ-CLICK-01"].status == "verified"


def test_skipped_linked_test_is_not_evidence(loaded: Database) -> None:
    build = find_build(loaded, "pallets/click", "1049")
    loaded.execute(
        "UPDATE test_runs SET outcome = 'skipped' WHERE build_id = ? AND name = 'test_wrap_text_visible_width'",
        (build.id,),
    )
    loaded.execute(
        "DELETE FROM verification_results WHERE build_id = ? AND source = 'test_runs'"
        " AND criterion_id = (SELECT id FROM acceptance_criteria WHERE key = 'AC-CLICK-102-1')",
        (build.id,),
    )
    refresh(loaded)
    c = crit(by_key(loaded)["REQ-CLICK-02"], "AC-CLICK-102-1")
    assert c.status == "verified_older_build"


# -- live sync of builds and releases ---------------------------------------------------


def test_sync_reads_actions_runs_and_releases(db: Database) -> None:
    fake = FakeGitHub(page_size=2)
    repo = fake.add_repo("acme/app")
    repo["commits"] += [
        api_commit("1" * 40, "2026-10-01T10:00:00Z", "Add parser"),
        api_commit("2" * 40, "2026-10-02T10:00:00Z", "Fix parser"),
    ]
    repo["runs"] += [
        api_run(501, 10, "1" * 40, "2026-10-01T10:05:00Z"),
        api_run(502, 11, "2" * 40, "2026-10-02T10:05:00Z", conclusion="failure"),
        api_run(503, 12, "2" * 40, "2026-10-02T09:30:00Z", status="in_progress"),
        api_run(504, 3, "2" * 40, "2026-10-02T10:06:00Z", name="Docs", path=".github/workflows/docs.yaml"),
    ]
    repo["releases"].append(
        {
            "tag_name": "v1.0",
            "draft": False,
            "published_at": "2026-10-02T12:00:00Z",
            "target_commitish": "main",
            "html_url": "https://github.com/acme/app/releases/tag/v1.0",
        }
    )
    repo["tags"]["v1.0"] = "2" * 40
    client = GitHubClient(token="t", session=fake, sleep=lambda s: None, rng=random.Random(0))
    stats = sync_repo(db, client, "acme/app", workflow="tests.yaml")
    assert not [k for k in stats if k.endswith(".error")], stats
    builds = db.query("SELECT workflow, run_number, status, commit_id FROM builds ORDER BY run_number")
    assert [(b["run_number"], b["status"]) for b in builds] == [
        (10, "success"),
        (11, "failure"),
        (12, "in_progress"),
    ]
    assert all(b["commit_id"] is not None for b in builds)  # linked to the commit it built
    rel = db.query_one("SELECT target_sha, build_id FROM releases WHERE tag = 'v1.0'")
    assert rel and rel["target_sha"] == "2" * 40 and rel["build_id"] is not None
    # The run still in progress holds the build watermark back to its own start time,
    # so the next sync fetches it again and sees how it finished.
    assert get_watermark(db, "github", "acme/app", "builds") == "2026-10-02T09:30:00Z"

    # Next sync: the release is known, so its tag is not resolved again.
    fake.calls.clear()
    sync_repo(db, client, "acme/app", workflow="tests.yaml")
    assert not [p for p, _ in fake.calls if p.endswith("/commits/v1.0")]
    runs_call = next(q for p, q in fake.calls if p.endswith("/runs"))
    assert runs_call["created"] == ">=2026-10-02T09:20:00Z"


def test_seed_builds_are_flagged_synthetic_and_releases_real(loaded: Database) -> None:
    raw = loaded.scalar("SELECT raw FROM builds LIMIT 1")
    assert (raw if isinstance(raw, dict) else json.loads(raw))["_provenance"] == "synthetic"
    assert loaded.scalar("SELECT COUNT(*) FROM releases WHERE source_system = 'github'") == 12
