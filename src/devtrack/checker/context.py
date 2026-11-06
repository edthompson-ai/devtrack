"""Gather everything the checker needs to judge one acceptance criterion.

Context = the criterion, its story and feature, the requirement text it serves, the
tasks linked to it, and the commits behind those tasks (message + diff, oldest first,
because a later commit can supersede an earlier one), plus linked test names.

Size is bounded: the diff budget is split across commits, secret-bearing files are
dropped, and every piece of repo text is redacted before it can reach a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from devtrack.checker.guardrails import detect_injection, estimate_tokens, is_secret_file, redact_secrets
from devtrack.db import Database


@dataclass
class CommitContext:
    repo: str
    sha: str
    message: str
    diff: str
    committed_at: str
    pr_numbers: list[int]
    truncated: bool = False


@dataclass
class CriterionContext:
    key: str
    text: str
    story: str
    feature: str
    requirements: list[str]
    tasks: list[dict[str, Any]]
    commits: list[CommitContext]
    tests: list[str]
    redactions: list[str] = field(default_factory=list)
    dropped_files: list[str] = field(default_factory=list)
    injection_hits: list[str] = field(default_factory=list)

    @property
    def truncated(self) -> bool:
        return any(c.truncated for c in self.commits)

    def repo_text(self) -> str:
        """All untrusted text exactly as sent; evidence quotes are checked against it."""
        return "\n".join(f"{c.message}\n{c.diff}" for c in self.commits)


def _drop_secret_files(diff: str) -> tuple[str, list[str]]:
    """Remove whole file sections whose path looks like a secrets file."""
    kept: list[str] = []
    dropped: list[str] = []
    skipping = False
    for line in diff.splitlines():
        if line.startswith("--- a/"):
            path = line[6:]
            skipping = is_secret_file(path)
            if skipping:
                dropped.append(path)
        if not skipping:
            kept.append(line)
    return "\n".join(kept), dropped


def gather_context(db: Database, criterion_key: str, *, max_diff_tokens: int = 9_000) -> CriterionContext:
    crit = db.query_one(
        "SELECT a.id, a.key, a.text, s.title AS story, f.title AS feature"
        " FROM acceptance_criteria a JOIN stories s ON s.id = a.story_id JOIN features f ON f.id = s.feature_id"
        " WHERE a.key = ?",
        (criterion_key,),
    )
    if crit is None:
        raise KeyError(f"no acceptance criterion {criterion_key!r}")
    reqs = [
        r["body"]
        for r in db.query(
            "SELECT r.body FROM requirements r JOIN requirement_criteria rc ON rc.requirement_id = r.id"
            " WHERE rc.criterion_id = ? ORDER BY r.key",
            (crit["id"],),
        )
    ]
    tasks = db.query(
        "SELECT t.key, t.title, t.status FROM tasks t JOIN task_criteria tc ON tc.task_id = t.id"
        " WHERE tc.criterion_id = ? ORDER BY t.key",
        (crit["id"],),
    )
    rows = db.query(
        "SELECT DISTINCT c.id, c.repo, c.sha, c.message, c.diff, c.committed_at"
        " FROM commits c JOIN commit_tasks ct ON ct.commit_id = c.id"
        " JOIN task_criteria tc ON tc.task_id = ct.task_id"
        " WHERE tc.criterion_id = ? AND NOT c.is_merge ORDER BY c.committed_at, c.sha",
        (crit["id"],),
    )
    tests = [
        f"{r['path']}::{r['name']}"
        for r in db.query(
            "SELECT t.path, t.name FROM test_cases t JOIN criterion_tests ct ON ct.test_case_id = t.id"
            " WHERE ct.criterion_id = ? ORDER BY t.path, t.name",
            (crit["id"],),
        )
    ]

    ctx = CriterionContext(
        key=crit["key"],
        text=crit["text"],
        story=crit["story"],
        feature=crit["feature"],
        requirements=reqs,
        tasks=tasks,
        commits=[],
        tests=tests,
    )
    per_commit_chars = (max_diff_tokens * 4) // max(len(rows), 1)
    for r in rows:
        prs = [
            p["number"]
            for p in db.query(
                "SELECT pr.number FROM pull_requests pr JOIN pull_request_commits prc ON prc.pull_request_id = pr.id"
                " WHERE prc.commit_id = ? ORDER BY pr.number",
                (r["id"],),
            )
        ]
        diff, dropped = _drop_secret_files(r["diff"] or "")
        ctx.dropped_files += dropped
        diff, kinds = redact_secrets(diff)
        message, kinds_msg = redact_secrets(r["message"])
        ctx.redactions += kinds + kinds_msg
        truncated = len(diff) > per_commit_chars
        if truncated:
            diff = diff[:per_commit_chars] + "\n... [diff truncated to fit the context budget]"
        ctx.commits.append(
            CommitContext(r["repo"], r["sha"], message, diff, str(r["committed_at"]), prs, truncated)
        )
    ctx.injection_hits = detect_injection(ctx.repo_text())
    return ctx


def context_tokens(ctx: CriterionContext) -> int:
    return estimate_tokens(ctx.repo_text()) + estimate_tokens(ctx.text) + 200
