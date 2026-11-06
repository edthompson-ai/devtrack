"""Data-quality checks. Each check is a query that returns the offending rows, so an
empty result means the check passed. Used by the test suite, `devtrack dq`, and the
dashboard, so the same definition of "clean" applies everywhere.

Foreign keys already make orphans impossible to insert; these checks are the
belt-and-braces layer (they also catch problems in a database built with FKs off,
or loaded by other tools) plus the rules a foreign key can't express.
"""

from __future__ import annotations

from dataclasses import dataclass

from devtrack.db import Database


@dataclass(frozen=True)
class Check:
    name: str
    description: str
    sql: str


def _orphan(child: str, fk: str, parent: str, *, nullable: bool = False) -> Check:
    null_ok = f" AND c.{fk} IS NOT NULL" if nullable else ""
    return Check(
        f"no_orphans:{child}.{fk}",
        f"every {child}.{fk} points at an existing {parent}",
        f"SELECT c.id AS id FROM {child} c LEFT JOIN {parent} p ON p.id = c.{fk} WHERE p.id IS NULL{null_ok}",
    )


def _link_orphan(table: str, col: str, parent: str) -> Check:
    return Check(
        f"no_orphans:{table}.{col}",
        f"every {table}.{col} points at an existing {parent}",
        f"SELECT l.{col} AS id FROM {table} l LEFT JOIN {parent} p ON p.id = l.{col} WHERE p.id IS NULL",
    )


def _no_dup_links(table: str, a: str, b: str) -> Check:
    return Check(
        f"no_duplicate_links:{table}",
        f"each ({a}, {b}) pair appears once in {table}",
        f"SELECT {a}, {b}, COUNT(*) AS n FROM {table} GROUP BY {a}, {b} HAVING COUNT(*) > 1",
    )


def _required(table: str, column: str) -> Check:
    return Check(
        f"required:{table}.{column}",
        f"{table}.{column} is present and not blank",
        f"SELECT id FROM {table} WHERE {column} IS NULL OR TRIM({column}) = ''",
    )


CHECKS: list[Check] = [
    _orphan("stories", "feature_id", "features"),
    _orphan("tasks", "story_id", "stories", nullable=True),
    _orphan("acceptance_criteria", "story_id", "stories"),
    _orphan("reviews", "pull_request_id", "pull_requests"),
    _orphan("reviews", "reviewer_id", "people"),
    _orphan("commits", "author_id", "people", nullable=True),
    _orphan("pull_requests", "author_id", "people", nullable=True),
    _link_orphan("task_criteria", "task_id", "tasks"),
    _link_orphan("task_criteria", "criterion_id", "acceptance_criteria"),
    _link_orphan("commit_tasks", "commit_id", "commits"),
    _link_orphan("commit_tasks", "task_id", "tasks"),
    _link_orphan("requirement_criteria", "requirement_id", "requirements"),
    _link_orphan("requirement_criteria", "criterion_id", "acceptance_criteria"),
    _link_orphan("pull_request_commits", "pull_request_id", "pull_requests"),
    _link_orphan("pull_request_commits", "commit_id", "commits"),
    _no_dup_links("task_criteria", "task_id", "criterion_id"),
    _no_dup_links("commit_tasks", "commit_id", "task_id"),
    _no_dup_links("requirement_criteria", "requirement_id", "criterion_id"),
    _no_dup_links("pull_request_commits", "pull_request_id", "commit_id"),
    _no_dup_links("criterion_tests", "criterion_id", "test_case_id"),
    _required("features", "title"),
    _required("stories", "title"),
    _required("tasks", "title"),
    _required("acceptance_criteria", "text"),
    _required("requirements", "body"),
    _required("commits", "message"),
    _required("pull_requests", "title"),
    _required("people", "display_name"),
    Check(
        "backlog_tasks_have_story",
        "planned (non-GitHub) tasks sit under a story",
        "SELECT id FROM tasks WHERE source_system <> 'github' AND story_id IS NULL",
    ),
    Check(
        "merged_prs_have_merge_time",
        "a merged PR has merged_at",
        "SELECT id FROM pull_requests WHERE state = 'merged' AND merged_at IS NULL",
    ),
    Check(
        "stories_have_criteria",
        "every story has at least one acceptance criterion",
        "SELECT s.id FROM stories s LEFT JOIN acceptance_criteria a ON a.story_id = s.id"
        " GROUP BY s.id HAVING COUNT(a.id) = 0",
    ),
    Check(
        "no_failed_staging_rows",
        "every staged record normalized without error",
        "SELECT id, entity, source_id, error FROM staging_records WHERE error IS NOT NULL",
    ),
    Check(
        "requirement_tree_has_no_cycles",
        "walking up from any requirement never comes back to it",
        "WITH RECURSIVE up(start_id, node_id, depth) AS ("
        " SELECT id, parent_id, 1 FROM requirements WHERE parent_id IS NOT NULL"
        " UNION ALL SELECT u.start_id, r.parent_id, u.depth + 1 FROM up u JOIN requirements r ON r.id = u.node_id"
        " WHERE r.parent_id IS NOT NULL AND u.node_id <> u.start_id AND u.depth < 50)"
        " SELECT DISTINCT start_id AS id FROM up WHERE node_id = start_id",
    ),
    Check(
        "verification_on_own_repo_build",
        "a verification result points at a build of the criterion's own repository",
        "SELECT v.id FROM verification_results v JOIN builds b ON b.id = v.build_id"
        " JOIN acceptance_criteria a ON a.id = v.criterion_id JOIN stories s ON s.id = a.story_id"
        " JOIN features f ON f.id = s.feature_id WHERE b.repo <> f.repo",
    ),
    Check(
        "no_duplicate_source_keys",
        "commit (repo, sha) is unique even across source systems",
        "SELECT repo, sha, COUNT(*) AS n FROM commits GROUP BY repo, sha HAVING COUNT(*) > 1",
    ),
]


@dataclass(frozen=True)
class CheckResult:
    check: Check
    failures: int
    sample: list[dict[str, object]]

    @property
    def ok(self) -> bool:
        return self.failures == 0


def run_checks(db: Database, checks: list[Check] | None = None) -> list[CheckResult]:
    results = []
    for check in checks or CHECKS:
        rows = db.query(check.sql)
        results.append(CheckResult(check, len(rows), rows[:5]))
    return results
