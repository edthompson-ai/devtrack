"""Shared fixtures.

Every test that takes ``db`` runs on in-memory SQLite. Set TEST_DATABASE_URL to a
throwaway Postgres database (and install psycopg) to run the same tests on Postgres
as well: the fixture wipes and re-migrates the public schema for each test.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from devtrack.db import Database, connect, migrate

PG_URL = os.environ.get("TEST_DATABASE_URL")
BACKENDS = ["sqlite"] + (["postgres"] if PG_URL else [])


def _fresh(backend: str) -> Database:
    if backend == "sqlite":
        db = connect("sqlite://:memory:")
    else:
        assert PG_URL
        db = connect(PG_URL)
        db.executescript("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    migrate(db)
    return db


@pytest.fixture(params=BACKENDS)
def db(request: pytest.FixtureRequest) -> Iterator[Database]:
    database = _fresh(request.param)
    yield database
    database.close()


@pytest.fixture
def sqlite_db() -> Iterator[Database]:
    database = _fresh("sqlite")
    yield database
    database.close()
