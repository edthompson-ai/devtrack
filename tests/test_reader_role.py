"""Postgres only: the question box's role can read the allow-listed tables and nothing else.

The role is created by docker compose (scripts/docker/initdb); here the test creates it,
re-applies the grant migrations, and switches to it with SET ROLE.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from devtrack.db import Database

MIGRATIONS = Path(__file__).resolve().parents[1] / "src" / "devtrack" / "db" / "migrations" / "postgres"


@pytest.fixture
def as_reader(db: Database) -> Iterator[Database]:
    if db.dialect != "postgres":
        pytest.skip("roles are a Postgres feature")
    db.executescript(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'devtrack_reader') THEN"
        " CREATE ROLE devtrack_reader NOLOGIN; END IF; END $$;"
    )
    for name in ("005_reader_role.sql", "006_verification.sql", "007_trust.sql"):
        sql = (MIGRATIONS / name).read_text()
        db.executescript(sql[sql.rindex("DO $$") :])  # just the grant block at the end
    db.commit()
    db.executescript("SET ROLE devtrack_reader;")
    yield db
    db.executescript("RESET ROLE;")


def test_reader_can_read_allowed_tables(as_reader: Database) -> None:
    for table in ("features", "acceptance_criteria", "verification_results", "audit_log", "ai_verdicts"):
        as_reader.query(f"SELECT COUNT(*) FROM {table}")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM staging_records",
        "SELECT COUNT(*) FROM sync_state",
        "DELETE FROM features",
        "INSERT INTO audit_log (recorded_at, actor, action, entity, entity_key, prev_hash, hash)"
        " VALUES ('x', 'a', 'b', 'c', 'd', 'e', 'f')",
    ],
)
def test_reader_is_refused_everything_else(as_reader: Database, sql: str) -> None:
    with pytest.raises(Exception, match="permission denied"):
        as_reader.query(sql) if sql.startswith("SELECT") else as_reader.execute(sql)
