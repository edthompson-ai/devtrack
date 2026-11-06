"""Phase 6: the question box only ever runs safe, read-only SQL."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from devtrack.db import Database, connect, migrate
from devtrack.ingest.seed import load_seed
from devtrack.nlq.safe_sql import UnsafeSQL, check_sql
from devtrack.nlq.translate import ask


@pytest.fixture
def dbs(tmp_path: Path) -> Iterator[tuple[Database, Database]]:
    url = f"sqlite:///{tmp_path / 'nlq.db'}"
    db = connect(url)
    migrate(db)
    load_seed(db)
    ro = connect(url, read_only=True)
    yield db, ro
    ro.close()
    db.close()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT key FROM features",
        "select f.key, count(*) from features f join stories s on s.feature_id = f.id group by f.key",
        "WITH x AS (SELECT id FROM commits) SELECT COUNT(*) FROM x",
        "SELECT key FROM features WHERE title LIKE '%drop table%'",  # keywords inside strings are fine
        "SELECT key FROM features;",
    ],
)
def test_validator_accepts_reads(sql: str) -> None:
    checked = check_sql(sql)
    assert checked.sql.startswith("SELECT * FROM (") and checked.sql.endswith("LIMIT 200")


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("DELETE FROM features", "only SELECT"),
        ("SELECT 1; DROP TABLE features", "one statement"),
        ("SELECT * FROM features; -- harmless?", None),  # trailing comment after ; is fine
        ("WITH d AS (DELETE FROM features RETURNING *) SELECT * FROM d", "DELETE"),
        ("SELECT * FROM staging_records", "staging_records"),
        ("SELECT * FROM features, sqlite_master", "sqlite_master"),
        ("SELECT * FROM pg_catalog.pg_user", "pg_"),
        ("SELECT pg_sleep(10)", "pg_"),
        ("SELECT load_extension('x')", "load_extension"),
        ("SELECT * FROM information_schema.tables", "information_schema"),
        ('SELECT * FROM "staging_records"', "quoted identifiers"),
        ("SELECT * INTO backup FROM features", "INTO"),
        ("PRAGMA table_info(features)", "only SELECT"),
        ("SELECT 1 /* unterminated", "unterminated"),
    ],
)
def test_validator_rejects(sql: str, reason: str | None) -> None:
    if reason is None:
        check_sql(sql)
        return
    with pytest.raises(UnsafeSQL, match=reason):
        check_sql(sql)


def test_read_only_connection_refuses_writes_even_if_validation_were_bypassed(
    dbs: tuple[Database, Database],
) -> None:
    _, ro = dbs
    with pytest.raises(sqlite3.OperationalError, match=r"readonly|read-only"):
        ro.execute("DELETE FROM features")


def test_template_questions(dbs: tuple[Database, Database]) -> None:
    db, ro = dbs
    a = ask(db, ro, "What's untested in the pager feature?")
    assert a.error is None and a.translation and a.translation.params == ("FEAT-CLICK-01",)
    assert {r["feature"] for r in a.rows} == {"FEAT-CLICK-01"}
    a = ask(db, ro, "Which criteria have no code?")
    assert {r["criterion"] for r in a.rows} == {"AC-CLICK-201-4", "AC-REQ-203-4", "AC-HTTPX-203-3"}
    a = ask(db, ro, "Trace REQ-REQ-02")
    assert a.rows and all(r["requirement"] == "REQ-REQ-02" for r in a.rows)
    a = ask(db, ro, "Which commits aren't tied to a task in psf/requests?")
    assert a.rows and {r["repo"] for r in a.rows} == {"psf/requests"}


def test_user_text_is_never_spliced_into_sql(dbs: tuple[Database, Database]) -> None:
    db, ro = dbs
    a = ask(db, ro, "What's untested in feature x' OR 1=1; DROP TABLE features; --")
    assert a.translation is not None
    assert "DROP" not in a.translation.sql and "1=1" not in a.translation.sql
    assert db.scalar("SELECT COUNT(*) FROM features") == 6


def test_unknown_question_gives_suggestions_not_a_guess(dbs: tuple[Database, Database]) -> None:
    db, ro = dbs
    a = ask(db, ro, "how is the weather")
    assert a.error and a.suggestions
