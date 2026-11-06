"""The six saved queries, loaded from ``queries/sql/*.sql``.

Each query is plain SQL that runs on both SQLite and Postgres. The only dialect
difference (date arithmetic) is written as a ``HOURS_BETWEEN(a, b)`` macro and
expanded here, so there is one source of truth per query instead of two copies
that drift apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from devtrack.db import Database
from devtrack.db.backend import Dialect

SQL_DIR = Path(__file__).parent / "sql"
# Arguments are column references (alias.column); keeping the macro that narrow
# makes the expansion obviously correct.
_HOURS = re.compile(r"HOURS_BETWEEN\(\s*([\w.]+)\s*,\s*([\w.]+)\s*\)")


@dataclass(frozen=True)
class SavedQuery:
    slug: str
    number: int
    title: str
    source: str
    params: tuple[str, ...] = ()

    def sql(self, dialect: Dialect) -> str:
        return expand(self.source, dialect)

    def run(self, db: Database, *args: Any) -> list[dict[str, Any]]:
        return db.query(self.sql(db.dialect), args)


def expand(sql: str, dialect: Dialect) -> str:
    def hours(match: re.Match[str]) -> str:
        start, end = match.group(1).strip(), match.group(2).strip()
        if dialect == "sqlite":
            return f"((julianday({end}) - julianday({start})) * 24.0)"
        return f"(EXTRACT(EPOCH FROM ({end} - {start})) / 3600.0)"

    return _HOURS.sub(hours, sql)


@cache
def all_queries() -> dict[str, SavedQuery]:
    queries: dict[str, SavedQuery] = {}
    for path in sorted(SQL_DIR.glob("*.sql")):
        number_text, slug = path.stem.split("_", 1)
        text = path.read_text()
        first = text.splitlines()[0]
        title = first.removeprefix("--").strip().split(". ", 1)[-1]
        params = ("requirement_key",) if "?" in text else ()
        queries[slug] = SavedQuery(
            slug=slug, number=int(number_text), title=title, source=text, params=params
        )
    return queries


def get(slug: str) -> SavedQuery:
    try:
        return all_queries()[slug]
    except KeyError:
        raise KeyError(f"unknown saved query {slug!r}; choose from {sorted(all_queries())}") from None
