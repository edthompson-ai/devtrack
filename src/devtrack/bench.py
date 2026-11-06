"""Query benchmark: EXPLAIN ANALYZE for the saved queries before and after indexing.

The seed is ~1,000 rows, too small for any index to matter. ``scale_up`` adds
production-like volume with set-based SQL (all rows tagged source_system='synthetic',
source_id 'scale:...'): by default ~150k commits, 30k PRs, 75k reviews, 20k tasks,
16k criteria. Run it against a throwaway database, never the demo one.

Procedure: migrate without 002_indexes, load seed, scale up, ANALYZE, time every
query (median of N runs, plans captured); then apply 002_indexes, ANALYZE, repeat.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from devtrack.db import Database, migrate
from devtrack.db.backend import MIGRATIONS_DIR
from devtrack.ingest.seed import load_seed
from devtrack.queries.saved import all_queries

log = logging.getLogger(__name__)

TIMEOUT_MS = 60_000
WORDS = ["parser", "zstd", "proxy", "pager", "ansi", "timeout", "retry", "cache", "header", "redirect"]


def _series(db: Database, n: int) -> str:
    if db.dialect == "postgres":
        return f"WITH g(i) AS (SELECT generate_series(1, {n}))"
    return f"WITH RECURSIVE g(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM g WHERE i < {n})"


def _ts(db: Database, hours_expr: str) -> str:
    if db.dialect == "postgres":
        return f"(TIMESTAMPTZ '2025-01-01 00:00:00+00' + ({hours_expr}) * INTERVAL '1 hour')"
    return f"strftime('%Y-%m-%dT%H:%M:%SZ', '2025-01-01', '+' || ({hours_expr}) || ' hours')"


def _sha(db: Database, expr: str) -> str:
    if db.dialect == "postgres":
        return f"lpad(to_hex({expr}), 40, '0')"
    return f"printf('%040x', {expr})"


def _word(expr: str) -> str:
    whens = " ".join(f"WHEN {i} THEN '{w}'" for i, w in enumerate(WORDS))
    return f"(CASE ({expr}) % {len(WORDS)} {whens} END)"


COMMON = "'synthetic', 'scale:' || '{kind}:' || i, '{{}}', 'scale'"
LINEAGE = "source_system, source_id, raw, raw_hash"


def scale_up(db: Database, factor: float = 1.0) -> dict[str, int]:
    n = {
        k: max(int(v * factor), 1)
        for k, v in {
            "people": 500,
            "features": 400,
            "stories": 4_000,
            "tasks": 20_000,
            "criteria": 16_000,
            "requirements": 1_600,
            "commits": 150_000,
            "prs": 30_000,
            "reviews": 75_000,
        }.items()
    }

    def s(count: int) -> str:
        return _series(db, count)

    def ts(hours: str) -> str:
        return _ts(db, hours)

    statements = [
        f"{s(n['people'])} INSERT INTO people (display_name, {LINEAGE})"
        f" SELECT 'Scale Person ' || i, {COMMON.format(kind='person')} FROM g",
        f"{s(n['features'])} INSERT INTO features (key, repo, title, status, created_at, {LINEAGE})"
        f" SELECT 'SCALE-F-' || i, 'scale/repo' || (i % 20), 'Feature ' || i, 'done', {ts('i')},"
        f" {COMMON.format(kind='feature')} FROM g",
        f"{s(n['stories'])} INSERT INTO stories (feature_id, key, title, status, points, started_at,"
        f" completed_at, {LINEAGE})"
        f" SELECT f.id, 'SCALE-S-' || i, 'Story ' || i,"
        f" CASE i % 4 WHEN 0 THEN 'in_progress' ELSE 'done' END, 1 + i % 8, {ts('i * 2')}, NULL,"
        f" {COMMON.format(kind='story')} FROM g JOIN features f ON f.key = 'SCALE-F-' || (1 + i % {n['features']})",
        f"{s(n['tasks'])} INSERT INTO tasks (story_id, key, repo, title, status, created_at, {LINEAGE})"
        f" SELECT st.id, 'SCALE-T-' || i, 'scale/repo' || (i % 20), 'Task ' || i,"
        f" CASE i % 5 WHEN 0 THEN 'todo' ELSE 'done' END, {ts('i')}, {COMMON.format(kind='task')}"
        f" FROM g JOIN stories st ON st.key = 'SCALE-S-' || (1 + i % {n['stories']})",
        f"{s(n['criteria'])} INSERT INTO acceptance_criteria (story_id, key, text, created_at, updated_at,"
        f" {LINEAGE})"
        f" SELECT st.id, 'SCALE-AC-' || i, 'The ' || {_word('i')} || ' handles ' || {_word('i * 7')}"
        f" || ' input number ' || i, {ts('i')}, {ts('i')}, {COMMON.format(kind='criterion')}"
        f" FROM g JOIN stories st ON st.key = 'SCALE-S-' || (1 + i % {n['stories']})",
        f"{s(n['requirements'])} INSERT INTO requirements (key, title, body, version, created_at, updated_at,"
        f" {LINEAGE})"
        f" SELECT 'SCALE-R-' || i, 'Requirement ' || i, 'Body ' || i, 1 + i % 3, {ts('i')},"
        f" {ts('i + (i % 7) * 2000')}, {COMMON.format(kind='requirement')} FROM g",
        f"{s(n['commits'])} INSERT INTO commits (repo, sha, author_id, authored_at, committed_at, message,"
        f" is_merge, additions, deletions, files_changed, diff, {LINEAGE})"
        f" SELECT 'scale/repo' || (i % 20), {_sha(db, 'i')}, p.id, {ts('i / 10')}, {ts('i / 10')},"
        f" 'Fix ' || {_word('i')} || ' handling in ' || {_word('i * 3')} || ' module (' || i || ')',"
        f" FALSE, i % 50, i % 20, 1 + i % 5, '+ changed line ' || i, {COMMON.format(kind='commit')}"
        f" FROM g JOIN people p ON p.source_system = 'synthetic'"
        f" AND p.source_id = 'scale:person:' || (1 + i % {n['people']})",
        f"{s(n['prs'])} INSERT INTO pull_requests (repo, number, title, state, author_id, created_at,"
        f" merged_at, closed_at, {LINEAGE})"
        f" SELECT 'scale/repo' || (i % 20), 100000 + i, 'PR ' || i, 'merged', NULL, {ts('i / 2')},"
        f" {ts('i / 2 + 6 + i % 90')}, {ts('i / 2 + 6 + i % 90')}, {COMMON.format(kind='pr')} FROM g",
        # links: 5 commits per PR; 80% of commits belong to a task; each criterion to 1-2 tasks
        f"{s(n['commits'])} INSERT INTO pull_request_commits (pull_request_id, commit_id)"
        f" SELECT pr.id, c.id FROM g"
        f" JOIN commits c ON c.repo = 'scale/repo' || (i % 20) AND c.sha = {_sha(db, 'i')}"
        f" JOIN pull_requests pr ON pr.repo = 'scale/repo' || ((1 + (i - 1) / 5) % 20)"
        f" AND pr.number = 100000 + (1 + (i - 1) / 5)"
        f" WHERE (1 + (i - 1) / 5) <= {n['prs']}",
        f"{s(n['commits'])} INSERT INTO commit_tasks (commit_id, task_id, link_source)"
        f" SELECT c.id, t.id, 'backlog_map' FROM g"
        f" JOIN commits c ON c.repo = 'scale/repo' || (i % 20) AND c.sha = {_sha(db, 'i')}"
        f" JOIN tasks t ON t.key = 'SCALE-T-' || (1 + (i * 13) % {n['tasks']})"
        f" WHERE i % 5 <> 0",
        f"{s(n['criteria'])} INSERT INTO task_criteria (task_id, criterion_id, link_source)"
        f" SELECT t.id, a.id, 'backlog_map' FROM g"
        f" JOIN acceptance_criteria a ON a.key = 'SCALE-AC-' || i"
        f" JOIN tasks t ON t.key = 'SCALE-T-' || (1 + (i * 3) % {n['tasks']})"
        f" WHERE i % 10 <> 0",
        f"{s(n['criteria'])} INSERT INTO requirement_criteria (requirement_id, criterion_id)"
        f" SELECT r.id, a.id FROM g JOIN acceptance_criteria a ON a.key = 'SCALE-AC-' || i"
        f" JOIN requirements r ON r.key = 'SCALE-R-' || (1 + i % {n['requirements']})",
        f"{s(n['reviews'])} INSERT INTO reviews (pull_request_id, reviewer_id, state, submitted_at, {LINEAGE})"
        f" SELECT pr.id, p.id, CASE i % 3 WHEN 0 THEN 'APPROVED' WHEN 1 THEN 'COMMENTED'"
        f" ELSE 'CHANGES_REQUESTED' END, {ts('i / 5 + 3')}, {COMMON.format(kind='review')}"
        f" FROM g JOIN pull_requests pr ON pr.repo = 'scale/repo' || ((1 + i % {n['prs']}) % 20)"
        f" AND pr.number = 100000 + (1 + i % {n['prs']})"
        f" JOIN people p ON p.source_system = 'synthetic' AND p.source_id = 'scale:person:' || (1 + (i * 31) % {n['people']})",
    ]
    for sql in statements:
        started = time.perf_counter()
        db.execute(sql)
        db.commit()
        log.info("scale step done in %.1fs: %s", time.perf_counter() - started, sql[:80])
    return n


@dataclass
class Timing:
    query: str
    title: str
    phase: str
    median_ms: float | None  # None = exceeded TIMEOUT_MS
    runs_ms: list[float]
    plan_summary: str
    plan_text: str = field(repr=False)


def _plan_nodes(node: dict[str, Any], out: list[str]) -> None:
    label = node.get("Node Type", "?")
    if node.get("Index Name"):
        label += f" using {node['Index Name']}"
    elif node.get("Relation Name"):
        label += f" on {node['Relation Name']}"
    out.append(label)
    for child in node.get("Plans", []):
        _plan_nodes(child, out)


def _explain_postgres(db: Database, sql: str, params: tuple[Any, ...]) -> tuple[float, str, str]:
    row = db.query_one("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql.rstrip().rstrip(";"), params)
    assert row is not None
    plan_json = next(iter(row.values()))
    if isinstance(plan_json, str):
        plan_json = json.loads(plan_json)
    top = plan_json[0]
    nodes: list[str] = []
    _plan_nodes(top["Plan"], nodes)
    scans = [n for n in nodes if "Scan" in n]
    text_rows = db.query(
        "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) " + sql.rstrip().rstrip(";"), params
    )
    text = "\n".join(next(iter(r.values())) for r in text_rows)
    return float(top["Execution Time"]), "; ".join(dict.fromkeys(scans)), text


def _explain_sqlite(db: Database, sql: str, params: tuple[Any, ...]) -> tuple[float, str, str]:
    plan = db.query("EXPLAIN QUERY PLAN " + sql, params)
    text = "\n".join(r["detail"] for r in plan)
    scans = [r["detail"] for r in plan if r["detail"].startswith(("SCAN", "SEARCH"))]
    conn = getattr(db, "conn", None)
    started = time.perf_counter()
    deadline = started + TIMEOUT_MS / 1000
    if conn is not None:
        # SQLite has no statement_timeout; a progress handler that returns 1 aborts the query.
        conn.set_progress_handler(lambda: 1 if time.perf_counter() > deadline else 0, 100_000)
    try:
        db.query(sql, params)
    except Exception as exc:
        if "interrupted" in str(exc):
            raise TimeoutError("statement timeout") from exc
        raise
    finally:
        if conn is not None:
            conn.set_progress_handler(None, 0)
    return (time.perf_counter() - started) * 1000, "; ".join(scans), text


EXTRA_QUERIES = {
    "fts_commits": (
        "Full-text search on commit messages",
        {
            "postgres": "SELECT id FROM commits WHERE search @@ websearch_to_tsquery('english', 'zstd proxy')",
            "sqlite": 'SELECT rowid FROM commits_fts WHERE commits_fts MATCH \'"zstd" "proxy"\'',
        },
        {
            "postgres": "SELECT id FROM commits WHERE message ILIKE '%zstd%' AND message ILIKE '%proxy%'",
            "sqlite": "SELECT id FROM commits WHERE message LIKE '%zstd%' AND message LIKE '%proxy%'",
        },
    ),
}


def _measure(
    db: Database, sql: str, params: tuple[Any, ...], runs: int
) -> tuple[float | None, list[float], str, str]:
    """Median ms over ``runs``; None when the query exceeds TIMEOUT_MS (Postgres)."""
    fn = _explain_postgres if db.dialect == "postgres" else _explain_sqlite
    samples: list[float] = []
    summary = text = ""
    for _ in range(runs):
        try:
            ms, summary, text = fn(db, sql, params)
        except Exception as exc:
            if "statement timeout" not in str(exc):
                raise
            explain = "EXPLAIN " if db.dialect == "postgres" else "EXPLAIN QUERY PLAN "
            plan = db.query(explain + sql.rstrip().rstrip(";"), params)
            text = "\n".join(str(list(r.values())[-1]) for r in plan)
            return None, samples, "timed out; estimated plan only", text
        samples.append(round(ms, 2))
        if ms > TIMEOUT_MS / 4:
            break  # one slow sample is enough to make the point; don't burn minutes
    return round(statistics.median(samples), 2), samples, summary, text


def run_queries(db: Database, phase: str, runs: int, trace_key: str) -> list[Timing]:
    timings: list[Timing] = []
    for slug, q in all_queries().items():
        params: tuple[Any, ...] = (trace_key,) if q.params else ()
        median, samples, summary, text = _measure(db, q.sql(db.dialect), params, runs)
        timings.append(Timing(slug, q.title, phase, median, samples, summary, text))
    for slug, (title, indexed, scan) in EXTRA_QUERIES.items():
        sql = (indexed if phase == "after" else scan)[db.dialect]
        median, samples, summary, text = _measure(db, sql, (), runs)
        label = "tsvector + GIN" if phase == "after" else "ILIKE scan"
        if db.dialect == "sqlite":
            label = "FTS5" if phase == "after" else "LIKE scan"
        timings.append(Timing(slug, f"{title} ({label})", phase, median, samples, summary, text))
    return timings


def analyze(db: Database) -> None:
    db.executescript("ANALYZE;")


def benchmark(db: Database, *, factor: float = 1.0, runs: int = 5) -> dict[str, Any]:
    """Run the full before/after benchmark on an EMPTY database."""
    if db.scalar(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'commits'"
        if db.dialect == "postgres"
        else "SELECT COUNT(*) FROM sqlite_master WHERE name = 'commits'"
    ):
        raise RuntimeError("benchmark needs an empty database; it adds ~400k synthetic rows")
    migrate(db, skip=["002_indexes"])
    if db.dialect == "postgres":
        db.execute(f"SET statement_timeout = {TIMEOUT_MS}")
    load_seed(db)
    sizes = scale_up(db, factor)
    analyze(db)
    trace_key = "SCALE-R-17"
    before = run_queries(db, "before", runs, trace_key)
    started = time.perf_counter()
    db.executescript((MIGRATIONS_DIR / db.dialect / "002_indexes.sql").read_text())
    index_build_s = time.perf_counter() - started
    analyze(db)
    after = run_queries(db, "after", runs, trace_key)
    counts = {
        t: int(db.scalar(f"SELECT COUNT(*) FROM {t}"))
        for t in (
            "commits",
            "pull_requests",
            "reviews",
            "tasks",
            "acceptance_criteria",
            "commit_tasks",
            "task_criteria",
            "pull_request_commits",
            "requirement_criteria",
        )
    }
    version = db.scalar("SELECT version()" if db.dialect == "postgres" else "SELECT sqlite_version()")
    return {
        "dialect": db.dialect,
        "server": str(version).split(" on ")[0] if db.dialect == "postgres" else f"SQLite {version}",
        "scale_factor": factor,
        "generated": sizes,
        "row_counts": counts,
        "runs_per_query": runs,
        "index_build_seconds": round(index_build_s, 2),
        "results": [asdict(t) for t in before + after],
    }


def write_report(result: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"query-performance.{result['dialect']}.json").write_text(json.dumps(result, indent=2))
    before = {r["query"]: r for r in result["results"] if r["phase"] == "before"}
    after = {r["query"]: r for r in result["results"] if r["phase"] == "after"}
    lines = [
        f"# Query performance ({result['dialect']}, {result['server']})",
        "",
        "Generated by `devtrack bench`. Median of "
        f"{result['runs_per_query']} runs of `EXPLAIN ANALYZE` (Postgres) or wall-clock (SQLite).",
        "",
        "Row counts: " + ", ".join(f"{k} {v:,}" for k, v in result["row_counts"].items()) + ".",
        f"Building every index in 002_indexes.sql took {result['index_build_seconds']}s.",
        "",
        "| # | Query | Before (ms) | After (ms) | Speed-up | Scans after indexing |",
        "|---|---|---:|---:|---:|---|",
    ]

    def fmt(ms: float | None) -> str:
        return f"> {TIMEOUT_MS:,} (timed out)" if ms is None else f"{ms:,.1f}"

    for i, (slug, b) in enumerate(before.items(), 1):
        a = after[slug]
        if b["median_ms"] is None and a["median_ms"]:
            speed = f"> {TIMEOUT_MS / a['median_ms']:,.0f}x"
        elif b["median_ms"] and a["median_ms"]:
            speed = f"{b['median_ms'] / a['median_ms']:,.1f}x"
        else:
            speed = "n/a"
        lines.append(
            f"| {i} | {b['title'].split(' (')[0]} | {fmt(b['median_ms'])} | {fmt(a['median_ms'])} |"
            f" {speed} | {re.sub(r'[|]', '/', a['plan_summary'])[:160]} |"
        )
    lines += ["", "## Plans", ""]
    for slug, b in before.items():
        a = after[slug]
        lines += [
            f"### {b['title']}",
            "",
            "Before:",
            "",
            "```",
            b["plan_text"].strip()[:3000],
            "```",
            "",
            "After:",
            "",
            "```",
            a["plan_text"].strip()[:3000],
            "```",
            "",
        ]
    path = out_dir / f"query-performance.{result['dialect']}.md"
    path.write_text("\n".join(lines))
    return path
