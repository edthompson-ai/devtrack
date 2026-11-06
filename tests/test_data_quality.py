"""Phase 2: data-quality checks pass on the loaded data and catch real problems."""

from __future__ import annotations

import pytest

from devtrack.db import Database
from devtrack.ingest.seed import load_seed
from devtrack.quality import CHECKS, run_checks


@pytest.fixture
def loaded(db: Database) -> Database:
    load_seed(db)
    return db


def test_all_checks_pass_on_seed(loaded: Database) -> None:
    failures = {r.check.name: r.sample for r in run_checks(loaded) if not r.ok}
    assert failures == {}


@pytest.mark.parametrize("check", [c.name for c in CHECKS])
def test_each_check_runs(loaded: Database, check: str) -> None:
    (result,) = run_checks(loaded, [c for c in CHECKS if c.name == check])
    assert result.ok, result.sample


def test_orphan_check_catches_dangling_link(sqlite_db: Database) -> None:
    load_seed(sqlite_db)
    sqlite_db.execute("PRAGMA foreign_keys = OFF")  # simulate a load done without FK enforcement
    sqlite_db.execute(
        "INSERT INTO commit_tasks (commit_id, task_id, link_source) VALUES (999999, 1, 'manual')"
    )
    sqlite_db.commit()
    bad = {r.check.name for r in run_checks(sqlite_db) if not r.ok}
    assert "no_orphans:commit_tasks.commit_id" in bad


def test_required_check_catches_blank_title(sqlite_db: Database) -> None:
    load_seed(sqlite_db)
    sqlite_db.execute("UPDATE stories SET title = '   ' WHERE id = 1")
    sqlite_db.commit()
    bad = {r.check.name for r in run_checks(sqlite_db) if not r.ok}
    assert bad == {"required:stories.title"}
