"""Validate SQL from the question box before it runs.

This is the first of two layers. The second, and the one that really holds, is the
database: questions run on a read-only connection (SQLite ``mode=ro`` plus
``query_only``; Postgres read-only transactions and, in docker compose, a role that
only has SELECT). The validator exists to give clear errors early and to stop
things a read-only connection still allows (expensive functions, reading files,
peeking at tables outside the allow-list, unbounded result sets).

Rules: exactly one statement; it starts with SELECT or WITH; only allow-listed
tables; no denied keywords or functions; a LIMIT is always applied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ALLOWED_TABLES = frozenset(
    {
        "features",
        "stories",
        "tasks",
        "acceptance_criteria",
        "requirements",
        "requirement_revisions",
        "commits",
        "pull_requests",
        "reviews",
        "people",
        "test_cases",
        "task_criteria",
        "commit_tasks",
        "requirement_criteria",
        "pull_request_commits",
        "criterion_tests",
        "ai_verdicts",
        "verdict_reviews",
        "counted_verdicts",
        "eval_labels",
        "eval_runs",
        "eval_results",
        "builds",
        "releases",
        "test_runs",
        "verification_results",
        "reverification_items",
        "repo_policies",
    }
)
DENIED_WORDS = frozenset(
    {
        "insert",
        "update",
        "delete",
        "merge",
        "upsert",
        "replace",
        "drop",
        "create",
        "alter",
        "truncate",
        "grant",
        "revoke",
        "attach",
        "detach",
        "pragma",
        "vacuum",
        "reindex",
        "analyze",
        "copy",
        "call",
        "execute",
        "exec",
        "do",
        "set",
        "reset",
        "begin",
        "commit",
        "rollback",
        "savepoint",
        "release",
        "lock",
        "listen",
        "notify",
        "load",
        "prepare",
        "deallocate",
        "declare",
        "fetch",
        "move",
        "into",
    }
)
DENIED_FUNCTIONS = re.compile(
    r"\b(pg_\w+|lo_\w+|dblink\w*|current_setting|set_config|load_extension|readfile|writefile|"
    r"sqlite_\w+|fts\w*_\w+|randomblob|zeroblob|generate_series|repeat|query_to_xml\w*|"
    r"inet_\w+|version)\s*\(",
    re.I,
)
# Tables that exist but are off limits to questions (raw payloads, internals, search
# shadow tables). Any mention anywhere in the query is rejected, which also covers
# comma joins (FROM a, b) that a FROM/JOIN scan alone would miss.
FORBIDDEN_TABLES = frozenset(
    {
        "staging_records",
        "ingest_batches",
        "sync_state",
        "schema_migrations",
        "embeddings",
        "criteria_fts",
        "commits_fts",
    }
)
FORBIDDEN_PREFIXES = ("pg_", "sqlite_", "information_schema", "criteria_fts_", "commits_fts_")
MAX_ROWS = 200


class UnsafeSQL(ValueError):
    pass


@dataclass(frozen=True)
class CheckedSQL:
    sql: str  # the statement as validated, wrapped in a row limit
    tables: frozenset[str]


def _strip(sql: str) -> tuple[str, str]:
    """Return (code_only, code_with_literals) with comments removed. In code_only,
    string literals and quoted identifiers are blanked so their contents can't be
    mistaken for keywords."""
    code, keep = [], []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j == -1:
                raise UnsafeSQL("unterminated comment")
            i = j + 2
            continue
        if ch in ("'", '"'):
            j = i + 1
            while j < n:
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                raise UnsafeSQL("unterminated quoted text")
            code.append(" '' " if ch == "'" else ' "x" ')
            keep.append(sql[i : j + 1])
            i = j + 1
            continue
        code.append(ch)
        keep.append(ch)
        i += 1
    return "".join(code), "".join(keep)


def check_sql(sql: str, *, max_rows: int = MAX_ROWS) -> CheckedSQL:
    if not sql or not sql.strip():
        raise UnsafeSQL("empty query")
    if len(sql) > 8000:
        raise UnsafeSQL("query too long")
    code, kept = _strip(sql)
    code, kept = code.strip().rstrip(";").strip(), kept.strip().rstrip(";").strip()
    if ";" in code:
        raise UnsafeSQL("only one statement is allowed")
    lowered = code.lower()
    first = lowered.split(None, 1)[0] if lowered else ""
    if first not in ("select", "with"):
        raise UnsafeSQL("only SELECT queries are allowed")
    words = set(re.findall(r"[a-z_]+", lowered))
    bad = sorted(words & DENIED_WORDS)
    if bad:
        raise UnsafeSQL(f"not allowed: {', '.join(bad).upper()}")
    if DENIED_FUNCTIONS.search(code):
        raise UnsafeSQL(f"function not allowed: {DENIED_FUNCTIONS.search(code).group(1)}")  # type: ignore[union-attr]
    if '"x"' in code:
        raise UnsafeSQL("quoted identifiers are not allowed")
    identifiers = set(re.findall(r"[a-z_][a-z0-9_]*", lowered))
    forbidden = sorted(i for i in identifiers if i in FORBIDDEN_TABLES or i.startswith(FORBIDDEN_PREFIXES))
    if forbidden:
        raise UnsafeSQL(f"table not allowed: {', '.join(forbidden)}")
    cte_names = {
        m.lower()
        for m in re.findall(r"(?i)(?:with|,)\s*(?:recursive\s+)?([a-z_]\w*)\s+(?:\([^)]*\)\s*)?as\s*\(", code)
    }
    referenced = {t.lower() for t in re.findall(r"(?i)\b(?:from|join)\s+([a-z_][\w.]*)", code)}
    unknown = {t for t in referenced - cte_names if t.split(".")[-1] not in ALLOWED_TABLES or "." in t}
    if unknown:
        raise UnsafeSQL(f"table not allowed: {', '.join(sorted(unknown))}")
    wrapped = f"SELECT * FROM (\n{kept}\n) AS nlq_result LIMIT {int(max_rows)}"
    return CheckedSQL(wrapped, frozenset(referenced - cte_names))
