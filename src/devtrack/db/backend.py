"""Database access for SQLite and Postgres behind one small interface.

Application code writes SQL with ``?`` placeholders and ISO-8601 UTC timestamp strings.
The Postgres backend rewrites placeholders to ``%s`` and turns datetimes it reads back
into the same ISO strings, so query results look identical on both backends.

Deliberately thin: no ORM. The SQL is the product here (saved queries, EXPLAIN plans),
so it stays visible and reviewable.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import logging
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, Protocol

log = logging.getLogger(__name__)

Dialect = Literal["sqlite", "postgres"]
Row = dict[str, Any]
Params = Sequence[Any]

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Database(Protocol):
    """What the rest of the code needs from a database."""

    dialect: Dialect

    def execute(self, sql: str, params: Params = ()) -> int:
        """Run one statement. Returns the affected row count."""

    def executemany(self, sql: str, rows: Sequence[Params]) -> None: ...

    def query(self, sql: str, params: Params = ()) -> list[Row]: ...

    def query_one(self, sql: str, params: Params = ()) -> Row | None: ...

    def scalar(self, sql: str, params: Params = ()) -> Any: ...

    def executescript(self, sql: str) -> None: ...

    def json(self, value: Any) -> Any:
        """Wrap a Python value for a JSON/JSONB column."""

    def transaction(self) -> Any:
        """Context manager: commit on success, roll back on error."""

    def commit(self) -> None: ...

    def close(self) -> None: ...


def canonical_json(value: Any) -> str:
    """Stable JSON text: sorted keys, no whitespace. Used for hashing and storage."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _normalize_value(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, decimal.Decimal):
        return float(value)
    return value


class SQLiteDatabase:
    dialect: Dialect = "sqlite"

    def __init__(self, path: str, *, read_only: bool = False) -> None:
        self.path = path
        if read_only:
            uri = f"file:{path}?mode=ro" if path != ":memory:" else "file::memory:?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self.conn.execute("PRAGMA query_only = ON")
        else:
            self.conn = sqlite3.connect(path, check_same_thread=False)
            if path != ":memory:":
                self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._depth = 0

    def execute(self, sql: str, params: Params = ()) -> int:
        cur = self.conn.execute(sql, tuple(params))
        return cur.rowcount

    def executemany(self, sql: str, rows: Sequence[Params]) -> None:
        self.conn.executemany(sql, [tuple(r) for r in rows])

    def query(self, sql: str, params: Params = ()) -> list[Row]:
        cur = self.conn.execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]

    def query_one(self, sql: str, params: Params = ()) -> Row | None:
        cur = self.conn.execute(sql, tuple(params))
        row = cur.fetchone()
        return dict(row) if row is not None else None

    def scalar(self, sql: str, params: Params = ()) -> Any:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return None if row is None else row[0]

    def executescript(self, sql: str) -> None:
        self.conn.executescript(sql)

    def json(self, value: Any) -> Any:
        return canonical_json(value)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        # Nested use joins the outer transaction (savepoints are not needed here).
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.conn.rollback()
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.conn.commit()

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def to_pyformat(sql: str) -> str:
    """Rewrite ``?`` placeholders to ``%s`` for psycopg, leaving quoted text alone.

    Literal ``%`` must be doubled for psycopg's pyformat parser. Handles single-quoted
    strings, double-quoted identifiers, ``--`` line comments and ``/* */`` comments.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            j = i + 1
            while j < n:
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:  # escaped quote
                        j += 2
                        continue
                    break
                j += 1
            out.append(sql[i : j + 1].replace("%", "%%"))
            i = j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            out.append(sql[i:j].replace("%", "%%"))
            i = j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append(sql[i:j].replace("%", "%%"))
            i = j
        elif ch == "?":
            out.append("%s")
            i += 1
        elif ch == "%":
            out.append("%%")
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


class PostgresDatabase:
    dialect: Dialect = "postgres"

    def __init__(self, url: str, *, read_only: bool = False, statement_timeout_ms: int = 15000) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise RuntimeError(
                "Postgres needs psycopg: pip install 'psycopg[binary]' (or use a sqlite:/// URL)"
            ) from exc
        self._psycopg = psycopg
        self.conn = psycopg.connect(url, row_factory=dict_row, autocommit=False)
        with self.conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
            cur.execute("SET TIME ZONE 'UTC'")
            if read_only:
                cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        self.conn.commit()
        self._depth = 0

    def _run(self, sql: str, params: Params) -> Any:
        cur = self.conn.cursor()
        try:
            cur.execute(to_pyformat(sql) if params else sql, tuple(params) if params else None)
        except Exception:
            cur.close()
            # A failed statement aborts the whole Postgres transaction. Outside an explicit
            # transaction() block, roll back so the connection stays usable; inside one,
            # transaction() rolls back when the error reaches it.
            if self._depth == 0:
                self.conn.rollback()
            raise
        return cur

    def execute(self, sql: str, params: Params = ()) -> int:
        cur = self._run(sql, params)
        count = int(cur.rowcount)
        cur.close()
        if self._depth == 0:
            self.conn.commit()
        return count

    def executemany(self, sql: str, rows: Sequence[Params]) -> None:
        try:
            with self.conn.cursor() as cur:
                cur.executemany(to_pyformat(sql), [tuple(r) for r in rows])
        except Exception:
            if self._depth == 0:
                self.conn.rollback()
            raise
        if self._depth == 0:
            self.conn.commit()

    def query(self, sql: str, params: Params = ()) -> list[Row]:
        cur = self._run(sql, params)
        rows = [{k: _normalize_value(v) for k, v in r.items()} for r in cur.fetchall()]
        cur.close()
        if self._depth == 0:
            self.conn.commit()
        return rows

    def query_one(self, sql: str, params: Params = ()) -> Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Params = ()) -> Any:
        row = self.query_one(sql, params)
        return None if row is None else next(iter(row.values()))

    def executescript(self, sql: str) -> None:
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql)  # no params: psycopg sends it as one simple-protocol batch
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()

    def json(self, value: Any) -> Any:
        from psycopg.types.json import Jsonb

        return Jsonb(value)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.conn.rollback()
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.conn.commit()

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def connect(url: str, *, read_only: bool = False) -> Database:
    """Open a database from a URL: ``sqlite:///path.db``, ``sqlite://:memory:`` or ``postgresql://...``."""
    if url.startswith("sqlite:"):
        path = (
            url.removeprefix("sqlite:///") if url.startswith("sqlite:///") else url.removeprefix("sqlite://")
        )
        return SQLiteDatabase(path or ":memory:", read_only=read_only)
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresDatabase(url, read_only=read_only)
    raise ValueError(f"Unsupported DATABASE_URL scheme: {url.split(':', 1)[0]}")


def migrate(db: Database, *, upto: str | None = None, skip: Sequence[str] = ()) -> list[str]:
    """Apply pending migrations for this dialect, in file-name order. Returns names applied.

    ``skip`` leaves named migrations out (the benchmark runs without 002_indexes first).
    """
    db.executescript(
        "CREATE TABLE IF NOT EXISTS schema_migrations ( version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    done = {r["version"] for r in db.query("SELECT version FROM schema_migrations")}
    applied: list[str] = []
    for path in sorted((MIGRATIONS_DIR / db.dialect).glob("*.sql")):
        name = path.stem
        if upto is not None and name > upto:
            break
        if name in done or name in skip:
            continue
        log.info("applying migration %s (%s)", name, db.dialect)
        db.executescript(path.read_text())
        db.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (name, utcnow()),
        )
        db.commit()
        applied.append(name)
    return applied


def utcnow() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
