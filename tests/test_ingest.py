"""Phase 2: staging -> normalize is idempotent, incremental, and isolates bad records."""

from __future__ import annotations

import json
import random
import re

from devtrack.db import Database
from devtrack.github.client import GitHubClient
from devtrack.ingest.normalize import process_staging
from devtrack.ingest.seed import load_seed
from devtrack.ingest.staging import get_watermark, stage, start_batch
from devtrack.ingest.sync import sync_repo
from tests.fakes import FakeGitHub, api_commit

CORE = [
    "people",
    "features",
    "stories",
    "tasks",
    "acceptance_criteria",
    "requirements",
    "commits",
    "pull_requests",
    "reviews",
    "test_cases",
    "task_criteria",
    "commit_tasks",
    "builds",
    "releases",
    "test_runs",
    "verification_results",
    "reverification_items",
    "repo_policies",
    "requirement_criteria",
    "pull_request_commits",
    "criterion_tests",
]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def counts(db: Database) -> dict[str, int]:
    return {t: int(db.scalar(f"SELECT COUNT(*) FROM {t}")) for t in CORE}


def client_for(fake: FakeGitHub) -> GitHubClient:
    return GitHubClient(token="test", session=fake, sleep=lambda s: None, rng=random.Random(0))


# -- seed ------------------------------------------------------------------------


def test_seed_loads_every_entity_without_errors(db: Database) -> None:
    stats = load_seed(db)
    assert not [k for k in stats if k.endswith(".error")], stats
    c = counts(db)
    assert c["features"] == 6 and c["stories"] == 17 and c["acceptance_criteria"] == 64
    assert c["pull_requests"] == 135 and c["commits"] >= 220 and c["reviews"] > 0
    assert c["commit_tasks"] > 0 and c["task_criteria"] > 0 and c["criterion_tests"] == 19
    # Release tags are real; builds and test results are synthetic and flagged.
    assert c["releases"] == 12 and c["builds"] == 147 and c["test_runs"] > 0
    assert db.scalar("SELECT COUNT(*) FROM builds WHERE source_system <> 'synthetic'") == 0
    assert db.scalar("SELECT COUNT(*) FROM releases WHERE build_id IS NULL") == 0
    assert c["verification_results"] > 0 and c["reverification_items"] > 0


def test_rerunning_the_seed_changes_nothing(db: Database) -> None:
    load_seed(db)
    before = counts(db)
    synced = db.scalar("SELECT MAX(synced_at) FROM commits")
    stats = load_seed(db)
    assert stats["staged.new"] == 0
    assert not [k for k in stats if k.endswith(".written")], "a rerun must not rewrite rows"
    assert counts(db) == before
    assert db.scalar("SELECT MAX(synced_at) FROM commits") == synced


def test_changed_record_updates_in_place(db: Database) -> None:
    load_seed(db)
    row = db.query_one("SELECT id, source_id, raw FROM pull_requests ORDER BY id LIMIT 1")
    assert row
    raw = row["raw"] if isinstance(row["raw"], dict) else json.loads(row["raw"])
    raw["title"] = "Renamed upstream"
    batch = start_batch(db, "github", raw["_repo"])
    stage(db, batch, "github", [("pull_request", row["source_id"], raw)])
    stats = process_staging(db)
    assert stats["pull_request.written"] == 1
    assert db.scalar("SELECT title FROM pull_requests WHERE id = ?", (row["id"],)) == "Renamed upstream"
    assert db.scalar("SELECT COUNT(*) FROM pull_requests WHERE source_id = ?", (row["source_id"],)) == 1


def test_bad_record_is_isolated(db: Database) -> None:
    batch = start_batch(db, "backlog", "test")
    stage(
        db,
        batch,
        "backlog",
        [
            (
                "story",
                "ST-NOPE",
                {"key": "ST-NOPE", "feature": "FEAT-MISSING", "title": "x", "status": "backlog"},
            ),
            ("person", "synthetic:ok", {"key": "ok", "display_name": "Okay Person"}),
        ],
    )
    stats = process_staging(db)
    assert stats["story.error"] == 1 and stats["person.written"] == 1
    err = db.scalar("SELECT error FROM staging_records WHERE source_id = 'ST-NOPE'")
    assert "feature not found" in err
    assert db.scalar("SELECT processed_at FROM staging_records WHERE source_id = 'ST-NOPE'") is None


def test_no_personal_email_is_stored(db: Database) -> None:
    load_seed(db)
    fake = FakeGitHub()
    fake.add_repo("acme/app")["commits"].append(
        api_commit("b" * 40, "2026-10-01T10:00:00Z", "Initial", email="real.person@acme.com")
    )
    sync_repo(db, client_for(fake), "acme/app")
    for table in ("commits", "people", "pull_requests"):
        for r in db.query(f"SELECT raw FROM {table}"):
            text = r["raw"] if isinstance(r["raw"], str) else json.dumps(r["raw"])
            assert not EMAIL.search(text), f"email found in {table}.raw: {EMAIL.search(text)}"
    assert db.scalar("SELECT COUNT(*) FROM people WHERE email_hash IS NOT NULL") > 0


def test_patches_live_in_diff_not_raw(db: Database) -> None:
    load_seed(db)
    row = db.query_one("SELECT raw, diff FROM commits WHERE diff <> '' LIMIT 1")
    assert row and "@@" in row["diff"]
    raw = row["raw"] if isinstance(row["raw"], dict) else json.loads(row["raw"])
    assert all("patch" not in f for f in raw["files"])


# -- live sync against a fake GitHub ----------------------------------------------


def fake_repo() -> FakeGitHub:
    fake = FakeGitHub(page_size=2)
    repo = fake.add_repo("acme/app")
    repo["commits"] += [
        api_commit("1" * 40, "2026-10-01T10:00:00Z", "Add parser (fixes #5)"),
        api_commit(
            "2" * 40,
            "2026-10-02T10:00:00Z",
            "Add tests",
            files=[
                {
                    "filename": "tests/test_parser.py",
                    "additions": 3,
                    "deletions": 0,
                    "patch": "@@ -0,0 +1,3 @@\n+def test_parses_empty():\n+    assert parse('') == []\n",
                }
            ],
        ),
        api_commit("3" * 40, "2026-10-03T10:00:00Z", "Docs"),
    ]
    repo["pulls"].append(
        {
            "number": 7,
            "title": "Parser",
            "body": "Closes #5",
            "state": "closed",
            "user": {"login": "octocat"},
            "created_at": "2026-10-01T09:00:00Z",
            "updated_at": "2026-10-03T12:00:00Z",
            "merged_at": "2026-10-03T12:00:00Z",
            "closed_at": "2026-10-03T12:00:00Z",
            "merge_commit_sha": "3" * 40,
            "head": {"ref": "parser"},
            "base": {"ref": "main"},
        }
    )
    repo["pr_commits"][7] = ["1" * 40, "2" * 40]
    repo["reviews"][7] = [
        {
            "id": 99,
            "user": {"login": "reviewer1"},
            "state": "APPROVED",
            "body": "ship it",
            "submitted_at": "2026-10-03T11:00:00Z",
        }
    ]
    repo["issues"] += [
        {
            "number": 5,
            "title": "Parser crashes on empty input",
            "state": "closed",
            "created_at": "2026-09-30T08:00:00Z",
            "updated_at": "2026-10-03T12:00:00Z",
            "closed_at": "2026-10-03T12:00:00Z",
            "assignee": None,
        },
        {"number": 7, "title": "Parser", "pull_request": {}, "updated_at": "2026-10-03T12:00:00Z"},
    ]
    return fake


def test_sync_loads_all_entities_and_links(db: Database) -> None:
    fake = fake_repo()
    stats = sync_repo(db, client_for(fake), "acme/app")
    assert not [k for k in stats if k.endswith(".error")], stats
    assert db.scalar("SELECT COUNT(*) FROM commits WHERE repo = 'acme/app'") == 3
    assert db.scalar("SELECT COUNT(*) FROM pull_request_commits") == 2
    assert db.scalar("SELECT COUNT(*) FROM reviews") == 1
    # issue #5 became a task; PR #7 (also returned by /issues) did not
    assert db.scalar("SELECT COUNT(*) FROM tasks WHERE source_system = 'github'") == 1
    # "fixes #5" in a commit and "Closes #5" in the PR body both link to the issue task
    sources = {r["link_source"] for r in db.query("SELECT link_source FROM commit_tasks")}
    assert sources == {"message_ref", "pr_ref"}
    assert db.scalar("SELECT name FROM test_cases") == "test_parses_empty"
    assert get_watermark(db, "github", "acme/app", "commits") == "2026-10-03T10:00:00Z"
    assert get_watermark(db, "github", "acme/app", "pulls") == "2026-10-03T12:00:00Z"


def test_second_sync_is_incremental(db: Database) -> None:
    fake = fake_repo()
    sync_repo(db, client_for(fake), "acme/app")
    fake.calls.clear()
    fake.repos["acme/app"]["commits"].append(api_commit("4" * 40, "2026-10-05T10:00:00Z", "Fix typo"))
    stats = sync_repo(db, client_for(fake), "acme/app")

    commit_list_call = next(q for p, q in fake.calls if p.endswith("/commits"))
    assert commit_list_call["since"] == "2026-10-03T09:50:00Z"  # watermark minus 10 minute overlap
    detail_calls = [p for p, _ in fake.calls if "/commits/" in p]
    assert detail_calls == ["/repos/acme/app/commits/" + "4" * 40], "only the new commit's detail is fetched"
    assert stats["commit.written"] == 1
    assert db.scalar("SELECT COUNT(*) FROM commits WHERE repo = 'acme/app'") == 4
    assert get_watermark(db, "github", "acme/app", "commits") == "2026-10-05T10:00:00Z"


def test_git_seed_person_and_api_person_resolve_to_one_row(db: Database) -> None:
    """Git history knows an email, the API knows a login; one human must stay one row."""
    fake = FakeGitHub()
    fake.add_repo("acme/app")["commits"] += [
        api_commit("5" * 40, "2026-10-01T10:00:00Z", "a", login=None, email="dev@acme.com"),
        api_commit("6" * 40, "2026-10-02T10:00:00Z", "b", login="devlogin", email="dev@acme.com"),
    ]
    sync_repo(db, client_for(fake), "acme/app")
    people = db.query("SELECT login, email_hash FROM people")
    assert len(people) == 1 and people[0]["login"] == "devlogin" and people[0]["email_hash"]
