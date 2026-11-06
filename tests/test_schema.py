"""Phase 1: the schema enforces what the data model promises."""

from __future__ import annotations

from typing import Any

import pytest

from devtrack.db import Database, migrate

CORE_TABLES = {
    "features",
    "stories",
    "tasks",
    "acceptance_criteria",
    "requirements",
    "commits",
    "pull_requests",
    "reviews",
    "people",
}
LINK_TABLES = {
    "task_criteria",
    "commit_tasks",
    "requirement_criteria",
    "pull_request_commits",
    "criterion_tests",
}
SHA = "a" * 40


def table_names(db: Database) -> set[str]:
    if db.dialect == "sqlite":
        rows = db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    else:
        rows = db.query(
            "SELECT table_name AS name FROM information_schema.tables WHERE table_schema = 'public'"
        )
    return {r["name"] for r in rows}


def columns(db: Database, table: str) -> set[str]:
    if db.dialect == "sqlite":
        return {r["name"] for r in db.query(f"PRAGMA table_info({table})")}
    rows = db.query("SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,))
    return {r["column_name"] for r in rows}


def integrity_errors(db: Database) -> tuple[type[BaseException], ...]:
    if db.dialect == "sqlite":
        import sqlite3

        return (sqlite3.IntegrityError,)
    import psycopg

    return (psycopg.errors.IntegrityError,)


def common(db: Database, source_id: str, system: str = "backlog") -> dict[str, Any]:
    return {
        "source_system": system,
        "source_id": source_id,
        "raw": db.json({"id": source_id}),
        "raw_hash": "h",
    }


def insert(db: Database, table: str, values: dict[str, Any]) -> int:
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    return int(
        db.scalar(f"INSERT INTO {table} ({cols}) VALUES ({marks}) RETURNING id", list(values.values()))
    )


def seed_story(db: Database) -> int:
    feature = insert(
        db,
        "features",
        {
            "key": "FEAT-1",
            "repo": "pallets/click",
            "title": "Shell completion",
            "status": "in_progress",
            "created_at": "2026-09-01T00:00:00Z",
            **common(db, "FEAT-1"),
        },
    )
    return insert(
        db,
        "stories",
        {
            "feature_id": feature,
            "key": "ST-1",
            "title": "Complete option values",
            "status": "in_progress",
            **common(db, "ST-1"),
        },
    )


def test_all_tables_exist(db: Database) -> None:
    names = table_names(db)
    assert names >= CORE_TABLES
    assert names >= LINK_TABLES
    assert {
        "staging_records",
        "sync_state",
        "ingest_batches",
        "ai_verdicts",
        "verdict_reviews",
        "eval_labels",
        "eval_runs",
        "eval_results",
        "embeddings",
    } <= names


@pytest.mark.parametrize("table", sorted(CORE_TABLES))
def test_every_core_table_has_lineage_columns(db: Database, table: str) -> None:
    assert {"source_id", "source_system", "synced_at", "raw", "raw_hash"} <= columns(db, table)


def test_migrate_is_idempotent(db: Database) -> None:
    assert migrate(db) == []


def test_source_key_is_unique(db: Database) -> None:
    seed_story(db)
    with pytest.raises(integrity_errors(db)):
        insert(
            db,
            "features",
            {
                "key": "FEAT-OTHER",
                "repo": "pallets/click",
                "title": "Dup",
                "status": "planned",
                "created_at": "2026-09-01T00:00:00Z",
                **common(db, "FEAT-1"),
            },
        )


def test_foreign_keys_are_enforced(db: Database) -> None:
    with pytest.raises(integrity_errors(db)):
        insert(
            db,
            "stories",
            {"feature_id": 999, "key": "ST-X", "title": "Orphan", "status": "backlog", **common(db, "ST-X")},
        )


def test_backlog_task_must_have_a_story(db: Database) -> None:
    with pytest.raises(integrity_errors(db)):
        insert(
            db,
            "tasks",
            {
                "key": "TASK-1",
                "repo": "pallets/click",
                "title": "Floating",
                "status": "todo",
                "created_at": "2026-09-01T00:00:00Z",
                **common(db, "TASK-1"),
            },
        )


def test_github_issue_task_may_float(db: Database) -> None:
    task_id = insert(
        db,
        "tasks",
        {
            "key": "GH-pallets/click-12",
            "repo": "pallets/click",
            "title": "Issue",
            "status": "todo",
            "issue_number": 12,
            "created_at": "2026-09-01T00:00:00Z",
            **common(db, "pallets/click#issue-12", "github"),
        },
    )
    assert task_id > 0


def test_merged_pull_request_needs_merged_at(db: Database) -> None:
    with pytest.raises(integrity_errors(db)):
        insert(
            db,
            "pull_requests",
            {
                "repo": "pallets/click",
                "number": 1,
                "title": "x",
                "state": "merged",
                **common(db, "pallets/click#pr-1", "github"),
            },
        )


def test_commit_sha_must_be_full_length(db: Database) -> None:
    with pytest.raises(integrity_errors(db)):
        insert(
            db,
            "commits",
            {
                "repo": "pallets/click",
                "sha": "abc123",
                "committed_at": "2026-09-01T00:00:00Z",
                "message": "short sha",
                **common(db, "pallets/click@abc123", "github"),
            },
        )


def test_link_tables_reject_duplicates(db: Database) -> None:
    story = seed_story(db)
    task = insert(
        db,
        "tasks",
        {
            "story_id": story,
            "key": "TASK-1",
            "repo": "pallets/click",
            "title": "t",
            "status": "todo",
            "created_at": "2026-09-01T00:00:00Z",
            **common(db, "TASK-1"),
        },
    )
    crit = insert(
        db,
        "acceptance_criteria",
        {
            "story_id": story,
            "key": "AC-1",
            "text": "Completes option values",
            "created_at": "2026-09-01T00:00:00Z",
            "updated_at": "2026-09-01T00:00:00Z",
            **common(db, "AC-1"),
        },
    )
    db.execute(
        "INSERT INTO task_criteria (task_id, criterion_id, link_source) VALUES (?, ?, 'backlog_map')",
        (task, crit),
    )
    with pytest.raises(integrity_errors(db)):
        db.execute(
            "INSERT INTO task_criteria (task_id, criterion_id, link_source) VALUES (?, ?, 'manual')",
            (task, crit),
        )


def test_fail_verdict_cannot_be_final(db: Database) -> None:
    story = seed_story(db)
    crit = insert(
        db,
        "acceptance_criteria",
        {
            "story_id": story,
            "key": "AC-1",
            "text": "x",
            "created_at": "2026-09-01T00:00:00Z",
            "updated_at": "2026-09-01T00:00:00Z",
            **common(db, "AC-1"),
        },
    )
    with pytest.raises(integrity_errors(db)):
        db.execute(
            "INSERT INTO ai_verdicts (criterion_id, verdict, confidence, reasoning, model, prompt_version,"
            " input_hash, status) VALUES (?, 'fail', 0.9, 'r', 'm', 'v1', 'h', 'final')",
            (crit,),
        )


def test_connection_survives_a_failed_statement(db: Database) -> None:
    """On Postgres a failed statement aborts the transaction; the backend must roll back
    so the next statement works (a bad NL->SQL query must not break the dashboard)."""
    with pytest.raises(Exception, match=r"(?i)no such table|does not exist"):
        db.query("SELECT * FROM table_that_does_not_exist")
    assert db.scalar("SELECT COUNT(*) FROM features") == 0
