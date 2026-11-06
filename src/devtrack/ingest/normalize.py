"""Normalize staged records into the core tables with idempotent upserts.

Each staging row is processed in its own transaction: a malformed record is marked
with its error and skipped, and the rest of the batch still loads. Rows are processed
in dependency order (people before commits, PRs before reviews, links last), so a
child never arrives before its parent.

Idempotency: every core row is keyed by (source_system, source_id). The upsert
updates a row only when its raw_hash changed, so re-running a load writes nothing.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import logging
import re
from collections import Counter
from collections.abc import Callable
from typing import Any

from devtrack import audit, ids
from devtrack.db import Database, canonical_json, utcnow

log = logging.getLogger(__name__)

ENTITY_ORDER = [
    "person",
    "feature",
    "story",
    "requirement",
    "task",
    "criterion",
    "commit",
    "pull_request",
    "pr_commits",
    "review",
    "issue",
    "build",
    "release",
    "test_report",
    "link",
    "verification",
]
MAX_PATCH_CHARS_PER_FILE = 8_000
MAX_DIFF_CHARS = 60_000
TEST_FILE = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$)")
TEST_DEF = re.compile(r"^\+\s*(?:async\s+)?def\s+(test_\w+)\s*\(")
ISSUE_REF = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b")
BOT_NAME = re.compile(r"(?i)(\[bot\]$|dependabot|pre-commit-ci|github-actions)")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def scrub_emails_in_text(text: str | None) -> str | None:
    """Commit trailers (Co-authored-by, Signed-off-by) and PR bodies carry emails.
    We never need them, so they are masked before storage."""
    return EMAIL.sub("[email]", text) if text else text


class NormalizeError(ValueError):
    """A staged record can't be normalized (missing parent, bad shape)."""


def raw_hash(raw: Any) -> str:
    return hashlib.sha256(canonical_json(raw).encode()).hexdigest()


def iso(value: str | None) -> str | None:
    """Normalize any ISO-8601 timestamp to 'YYYY-MM-DDTHH:MM:SSZ' (UTC)."""
    if value is None or value == "":
        return None
    moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Generic upsert
# ---------------------------------------------------------------------------


def upsert(db: Database, table: str, values: dict[str, Any]) -> tuple[int, bool]:
    """Insert or update by (source_system, source_id). Returns (id, written).

    ``written`` is False when the row already existed with the same raw_hash, which
    is how a rerun proves it changed nothing.
    """
    raw = values.pop("raw")
    values["raw"] = db.json(raw)
    values["raw_hash"] = raw_hash(raw)
    values["synced_at"] = utcnow()
    cols = list(values)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in ("source_system", "source_id"))
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        f" ON CONFLICT (source_system, source_id) DO UPDATE SET {updates}"
        f" WHERE {table}.raw_hash <> excluded.raw_hash"
        " RETURNING id"
    )
    row = db.query_one(sql, list(values.values()))
    if row is not None:
        return int(row["id"]), True
    existing = db.scalar(
        f"SELECT id FROM {table} WHERE source_system = ? AND source_id = ?",
        (values["source_system"], values["source_id"]),
    )
    return int(existing), False


def link(db: Database, sql: str, params: tuple[Any, ...]) -> bool:
    """Insert a link row; duplicates are ignored. Returns True if a row was added."""
    return db.execute(sql + " ON CONFLICT DO NOTHING", params) > 0


def id_by(db: Database, table: str, where: str, params: tuple[Any, ...]) -> int | None:
    value = db.scalar(f"SELECT id FROM {table} WHERE {where}", params)
    return int(value) if value is not None else None


def require_id(db: Database, table: str, where: str, params: tuple[Any, ...], what: str) -> int:
    found = id_by(db, table, where, params)
    if found is None:
        raise NormalizeError(f"{what} not found: {params}")
    return found


# ---------------------------------------------------------------------------
# People and identity resolution
# ---------------------------------------------------------------------------


def resolve_person(
    db: Database,
    *,
    login: str | None,
    name: str | None,
    email_sha: str | None,
    source_system: str = "github",
    synthetic_key: str | None = None,
) -> int | None:
    """Find or create a person, matching on login first, then email hash.

    Git history knows a name and an email; the API knows a login (and sometimes the
    email). Matching on either key, and filling in the other when we learn it, keeps
    one row per human instead of one per data source.
    """
    if not (login or email_sha or synthetic_key):
        return None
    if synthetic_key:
        source_id = f"synthetic:{synthetic_key}"
        pid, _ = upsert(
            db,
            "people",
            {
                "login": None,
                "display_name": name or synthetic_key,
                "email_hash": None,
                "is_bot": False,
                "source_system": source_system,
                "source_id": source_id,
                "raw": {"key": synthetic_key, "display_name": name},
            },
        )
        return pid

    row = (
        db.query_one("SELECT id, login, email_hash FROM people WHERE login = ?", (login,)) if login else None
    )
    if row is None and email_sha:
        row = db.query_one("SELECT id, login, email_hash FROM people WHERE email_hash = ?", (email_sha,))
    if row is not None:
        if login and not row["login"]:
            db.execute("UPDATE people SET login = ? WHERE id = ?", (login, row["id"]))
        if email_sha and not row["email_hash"]:
            db.execute("UPDATE people SET email_hash = ? WHERE id = ?", (email_sha, row["id"]))
        return int(row["id"])

    source_id = ids.person_from_login(login) if login else f"email:{(email_sha or '')[:16]}"
    display = name or login or "unknown"
    pid, _ = upsert(
        db,
        "people",
        {
            "login": login,
            "display_name": display,
            "email_hash": email_sha,
            "is_bot": bool(BOT_NAME.search(login or "") or BOT_NAME.search(display)),
            "source_system": source_system,
            "source_id": source_id,
            "raw": {"login": login, "name": name, "email_sha256": email_sha},
        },
    )
    return pid


def _scrub_emails(raw: dict[str, Any]) -> None:
    """Replace personal emails in an API commit payload with their sha256, and mask
    any email written inside the message."""
    commit = raw.get("commit") or {}
    for who in ("author", "committer"):
        person = commit.get(who)
        if isinstance(person, dict) and person.get("email"):
            person["email_sha256"] = ids.email_hash(person.pop("email"))
    if commit.get("message"):
        commit["message"] = scrub_emails_in_text(commit["message"])


def _person_email_sha(person: dict[str, Any] | None) -> str | None:
    if not person:
        return None
    if person.get("email_sha256"):
        return str(person["email_sha256"])
    if person.get("email"):
        return ids.email_hash(str(person["email"]))
    return None


# ---------------------------------------------------------------------------
# GitHub entities
# ---------------------------------------------------------------------------


def build_diff(files: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    total = 0
    for f in files:
        patch = f.get("patch")
        if patch is None:
            continue
        if len(patch) > MAX_PATCH_CHARS_PER_FILE:
            patch = patch[:MAX_PATCH_CHARS_PER_FILE] + "\n... [truncated]"
        block = f"--- a/{f['filename']}\n+++ b/{f['filename']}\n{patch}\n"
        if total + len(block) > MAX_DIFF_CHARS:
            parts.append(f"... [{len(files) - len(parts)} more files truncated]\n")
            break
        parts.append(block)
        total += len(block)
    return "".join(parts)


def normalize_commit(db: Database, system: str, source_id: str, payload: dict[str, Any]) -> bool:
    repo = payload["_repo"]
    sha = payload["sha"]
    info = payload["commit"]
    author = info.get("author") or {}
    committer = info.get("committer") or {}
    login = (payload.get("author") or {}).get("login")
    synthetic_author = payload.get("_synthetic_author")
    author_id = resolve_person(
        db,
        login=login,
        name=author.get("name"),
        email_sha=_person_email_sha(author),
        source_system="synthetic" if synthetic_author else system,
        synthetic_key=synthetic_author,
    )
    files = payload.get("files") or []
    raw = copy.deepcopy(payload)
    _scrub_emails(raw)
    for f in raw.get("files") or []:
        f.pop("patch", None)  # patches live in the diff column; no need to store them twice
    stats = payload.get("stats") or {}
    commit_id, written = upsert(
        db,
        "commits",
        {
            "repo": repo,
            "sha": sha,
            "author_id": author_id,
            "authored_at": iso(author.get("date")),
            "committed_at": iso(committer.get("date") or author.get("date")),
            "message": scrub_emails_in_text(info.get("message")) or "",
            "is_merge": len(payload.get("parents") or []) > 1,
            "additions": stats.get("additions"),
            "deletions": stats.get("deletions"),
            "files_changed": len(files),
            "diff": build_diff(files),
            "source_system": system,
            "source_id": source_id,
            "raw": raw,
        },
    )
    for f in files:
        path = f["filename"]
        if not TEST_FILE.search(path):
            continue
        for line in (f.get("patch") or "").splitlines():
            match = TEST_DEF.match(line)
            if match:
                upsert(
                    db,
                    "test_cases",
                    {
                        "repo": repo,
                        "path": path,
                        "name": match.group(1),
                        "commit_id": commit_id,
                        "source_system": system,
                        "source_id": ids.test_case(repo, path, match.group(1)),
                        "raw": {"repo": repo, "path": path, "name": match.group(1), "sha": sha},
                    },
                )
    return written


def normalize_pull_request(db: Database, system: str, source_id: str, payload: dict[str, Any]) -> bool:
    repo = payload["_repo"]
    user = payload.get("user") or {}
    git_author = payload.get("_git_author") or {}
    author_id = resolve_person(
        db,
        login=user.get("login"),
        name=user.get("login") or git_author.get("name"),
        email_sha=git_author.get("email_sha256"),
    )
    merged_at = iso(payload.get("merged_at"))
    state = "merged" if merged_at else payload.get("state", "open")
    raw = copy.deepcopy(payload)
    raw["body"] = scrub_emails_in_text(raw.get("body"))
    _, written = upsert(
        db,
        "pull_requests",
        {
            "repo": repo,
            "number": payload["number"],
            "title": payload["title"],
            "body": raw["body"],
            "state": state,
            "author_id": author_id,
            "created_at": iso(payload.get("created_at")),
            "merged_at": merged_at,
            "closed_at": iso(payload.get("closed_at")),
            "merge_commit_sha": payload.get("merge_commit_sha"),
            "head_ref": (payload.get("head") or {}).get("ref"),
            "base_ref": (payload.get("base") or {}).get("ref"),
            "source_system": system,
            "source_id": source_id,
            "raw": raw,
        },
    )
    return written


def normalize_pr_commits(db: Database, system: str, source_id: str, payload: dict[str, Any]) -> bool:
    repo, number = payload["_repo"], payload["number"]
    pr_id = require_id(db, "pull_requests", "repo = ? AND number = ?", (repo, number), "pull request")
    added = False
    missing = 0
    for sha in payload["shas"]:
        commit_id = id_by(db, "commits", "repo = ? AND sha = ?", (repo, sha))
        if commit_id is None:
            missing += 1  # outside the fetch window; the next sync that loads it can link it
            continue
        added |= link(
            db,
            "INSERT INTO pull_request_commits (pull_request_id, commit_id) VALUES (?, ?)",
            (pr_id, commit_id),
        )
    if missing:
        log.info("%s#%s: %d member commits not loaded yet", repo, number, missing)
    return added


def normalize_review(db: Database, system: str, source_id: str, payload: dict[str, Any]) -> bool:
    if not payload.get("submitted_at"):
        return False  # pending reviews have no submission time and aren't real yet
    repo, number = payload["_repo"], payload["_pr_number"]
    pr_id = require_id(db, "pull_requests", "repo = ? AND number = ?", (repo, number), "pull request")
    synthetic_key = payload.get("_synthetic_reviewer")
    user = payload.get("user") or {}
    reviewer_id = resolve_person(
        db,
        login=user.get("login"),
        name=payload.get("_reviewer_name") or user.get("login"),
        email_sha=None,
        source_system="synthetic" if synthetic_key else system,
        synthetic_key=synthetic_key,
    )
    if reviewer_id is None:
        raise NormalizeError(f"review {source_id} has no reviewer")
    _, written = upsert(
        db,
        "reviews",
        {
            "pull_request_id": pr_id,
            "reviewer_id": reviewer_id,
            "state": payload["state"],
            "body": payload.get("body"),
            "submitted_at": iso(payload["submitted_at"]),
            "source_system": system,
            "source_id": source_id,
            "raw": payload,
        },
    )
    return written


def normalize_issue(db: Database, system: str, source_id: str, payload: dict[str, Any]) -> bool:
    repo, number = payload["_repo"], payload["number"]
    assignee = payload.get("assignee") or {}
    assignee_id = resolve_person(db, login=assignee.get("login"), name=assignee.get("login"), email_sha=None)
    _, written = upsert(
        db,
        "tasks",
        {
            "story_id": None,
            "key": f"GH-{repo}-{number}",
            "repo": repo,
            "title": payload["title"],
            "status": "closed" if payload.get("state") == "closed" else "todo",
            "issue_number": number,
            "assignee_id": assignee_id,
            "created_at": iso(payload["created_at"]),
            "closed_at": iso(payload.get("closed_at")),
            "source_system": system,
            "source_id": source_id,
            "raw": payload,
        },
    )
    return written


# ---------------------------------------------------------------------------
# Backlog entities (synthetic hierarchy, see backlog/loader.py)
# ---------------------------------------------------------------------------


def _person_key(db: Database, key: str | None) -> int | None:
    if not key:
        return None
    return id_by(db, "people", "source_system = 'synthetic' AND source_id = ?", (f"synthetic:{key}",))


def normalize_person(db: Database, system: str, source_id: str, payload: dict[str, Any]) -> bool:
    _, written = upsert(
        db,
        "people",
        {
            "login": None,
            "display_name": payload["display_name"],
            "email_hash": None,
            "is_bot": False,
            "source_system": system,
            "source_id": source_id,
            "raw": payload,
        },
    )
    return written


def normalize_feature(db: Database, system: str, source_id: str, payload: dict[str, Any]) -> bool:
    _, written = upsert(
        db,
        "features",
        {
            "key": payload["key"],
            "repo": payload["repo"],
            "title": payload["title"],
            "description": payload.get("description"),
            "status": payload["status"],
            "owner_id": _person_key(db, payload.get("owner")),
            "created_at": iso(payload["created_at"]),
            "source_system": system,
            "source_id": source_id,
            "raw": payload,
        },
    )
    return written


def normalize_story(db: Database, system: str, source_id: str, payload: dict[str, Any]) -> bool:
    feature_id = require_id(db, "features", "key = ?", (payload["feature"],), "feature")
    _, written = upsert(
        db,
        "stories",
        {
            "feature_id": feature_id,
            "key": payload["key"],
            "title": payload["title"],
            "description": payload.get("description"),
            "status": payload["status"],
            "points": payload.get("points"),
            "started_at": iso(payload.get("started_at")),
            "completed_at": iso(payload.get("completed_at")),
            "source_system": system,
            "source_id": source_id,
            "raw": payload,
        },
    )
    return written


def normalize_task(db: Database, system: str, source_id: str, payload: dict[str, Any]) -> bool:
    story_id = require_id(db, "stories", "key = ?", (payload["story"],), "story")
    _, written = upsert(
        db,
        "tasks",
        {
            "story_id": story_id,
            "key": payload["key"],
            "repo": payload["repo"],
            "title": payload["title"],
            "status": payload["status"],
            "issue_number": payload.get("issue_number"),
            "assignee_id": _person_key(db, payload.get("assignee")),
            "created_at": iso(payload["created_at"]),
            "closed_at": iso(payload.get("closed_at")),
            "source_system": system,
            "source_id": source_id,
            "raw": payload,
        },
    )
    return written


def normalize_criterion(db: Database, system: str, source_id: str, payload: dict[str, Any]) -> bool:
    story_id = require_id(db, "stories", "key = ?", (payload["story"],), "story")
    _, written = upsert(
        db,
        "acceptance_criteria",
        {
            "story_id": story_id,
            "key": payload["key"],
            "text": payload["text"],
            "created_at": iso(payload["created_at"]),
            "updated_at": iso(payload["updated_at"]),
            "source_system": system,
            "source_id": source_id,
            "raw": payload,
        },
    )
    return written


def normalize_requirement(db: Database, system: str, source_id: str, payload: dict[str, Any]) -> bool:
    revisions = sorted(payload["revisions"], key=lambda r: r["version"])
    latest = revisions[-1]
    before = db.scalar("SELECT version FROM requirements WHERE key = ?", (payload["key"],))
    req_id, written = upsert(
        db,
        "requirements",
        {
            "key": payload["key"],
            "title": payload["title"],
            "body": latest["body"],
            "version": latest["version"],
            "author_id": _person_key(db, payload.get("author")),
            "created_at": iso(revisions[0]["changed_at"]),
            "updated_at": iso(latest["changed_at"]),
            "verification_method": payload.get("verification_method", "test"),
            "source_system": system,
            "source_id": source_id,
            "raw": payload,
        },
    )
    for rev in revisions:
        db.execute(
            "INSERT INTO requirement_revisions (requirement_id, version, body, change_note, changed_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (requirement_id, version) DO UPDATE SET body = excluded.body,"
            " change_note = excluded.change_note, changed_at = excluded.changed_at",
            (req_id, rev["version"], rev["body"], rev.get("note"), iso(rev["changed_at"])),
        )
    if before is None or int(before) != int(latest["version"]):
        audit.record(
            db,
            actor=payload.get("author") or system,
            action="requirement.created" if before is None else "requirement.changed",
            entity="requirement",
            entity_key=payload["key"],
            effective_at=iso(latest["changed_at"]),
            detail={"version": latest["version"], "previous_version": before, "note": latest.get("note")},
        )
    return written


def normalize_link(db: Database, system: str, source_id: str, payload: dict[str, Any]) -> bool:
    kind = payload["kind"]
    if kind == "task_criteria":
        task = require_id(db, "tasks", "key = ?", (payload["task"],), "task")
        crit = require_id(db, "acceptance_criteria", "key = ?", (payload["criterion"],), "criterion")
        return link(
            db,
            "INSERT INTO task_criteria (task_id, criterion_id, link_source) VALUES (?, ?, 'backlog_map')",
            (task, crit),
        )
    if kind == "requirement_criteria":
        req = require_id(db, "requirements", "key = ?", (payload["requirement"],), "requirement")
        crit = require_id(db, "acceptance_criteria", "key = ?", (payload["criterion"],), "criterion")
        return link(
            db, "INSERT INTO requirement_criteria (requirement_id, criterion_id) VALUES (?, ?)", (req, crit)
        )
    if kind == "requirement_parent":
        child = require_id(db, "requirements", "key = ?", (payload["requirement"],), "requirement")
        parent = require_id(db, "requirements", "key = ?", (payload["parent"],), "parent requirement")
        # Walk up from the proposed parent: reaching the child would make a cycle.
        node: int | None = parent
        while node is not None:
            if node == child:
                raise NormalizeError(f"{payload['requirement']} -> {payload['parent']} would create a cycle")
            found = db.scalar("SELECT parent_id FROM requirements WHERE id = ?", (node,))
            node = int(found) if found is not None else None
        return (
            db.execute(
                "UPDATE requirements SET parent_id = ? WHERE id = ? AND (parent_id IS NULL OR parent_id <> ?)",
                (parent, child, parent),
            )
            > 0
        )
    if kind == "criterion_test":
        crit = require_id(db, "acceptance_criteria", "key = ?", (payload["criterion"],), "criterion")
        test = require_id(
            db,
            "test_cases",
            "repo = ? AND path = ? AND name = ?",
            (payload["repo"], payload["path"], payload["name"]),
            "test case",
        )
        return link(
            db,
            "INSERT INTO criterion_tests (criterion_id, test_case_id, link_source)"
            " VALUES (?, ?, 'backlog_map')",
            (crit, test),
        )
    if kind == "commit_task_pr":
        task = require_id(db, "tasks", "key = ?", (payload["task"],), "task")
        pr = require_id(
            db, "pull_requests", "repo = ? AND number = ?", (payload["repo"], payload["pr"]), "pull request"
        )
        added = False
        commit_ids = db.query("SELECT commit_id FROM pull_request_commits WHERE pull_request_id = ?", (pr,))
        if not commit_ids:
            raise NormalizeError(f"PR {payload['repo']}#{payload['pr']} has no loaded commits")
        for row in commit_ids:
            added |= link(
                db,
                "INSERT INTO commit_tasks (commit_id, task_id, link_source) VALUES (?, ?, 'backlog_map')",
                (row["commit_id"], task),
            )
        return added
    if kind == "commit_task_sha":
        task = require_id(db, "tasks", "key = ?", (payload["task"],), "task")
        matches = db.query(
            "SELECT id FROM commits WHERE repo = ? AND sha LIKE ?",
            (payload["repo"], payload["sha_prefix"] + "%"),
        )
        if len(matches) != 1:
            raise NormalizeError(f"sha prefix {payload['sha_prefix']} matched {len(matches)} commits")
        return link(
            db,
            "INSERT INTO commit_tasks (commit_id, task_id, link_source) VALUES (?, ?, 'backlog_map')",
            (matches[0]["id"], task),
        )
    raise NormalizeError(f"unknown link kind {kind!r}")


# ---------------------------------------------------------------------------
# Builds, releases, test results, manual verifications
# ---------------------------------------------------------------------------

BUILD_STATUS = {
    "success": "success",
    "failure": "failure",
    "cancelled": "cancelled",
    "timed_out": "failure",
    "startup_failure": "failure",
    "action_required": "cancelled",
    "skipped": "cancelled",
    "neutral": "success",
    "stale": "cancelled",
}


def normalize_build(db: Database, system: str, source_id: str, payload: dict[str, Any]) -> bool:
    """A CI run. Same shape for a real GitHub Actions run and a synthetic seed build."""
    repo = payload["_repo"]
    sha = payload["head_sha"]
    if payload.get("status") != "completed":
        status = "in_progress"
    else:
        status = BUILD_STATUS.get(str(payload.get("conclusion")), "failure")
    _, written = upsert(
        db,
        "builds",
        {
            "repo": repo,
            "workflow": payload.get("name") or "CI",
            "run_number": int(payload["run_number"]),
            "head_sha": sha,
            "commit_id": id_by(db, "commits", "repo = ? AND sha = ?", (repo, sha)),
            "branch": payload.get("head_branch"),
            "event": payload.get("event") or "push",
            "status": status,
            "started_at": iso(payload.get("run_started_at") or payload.get("created_at")),
            "finished_at": iso(payload.get("updated_at")) if status != "in_progress" else None,
            "url": payload.get("html_url"),
            "source_system": system,
            "source_id": source_id,
            "raw": payload,
        },
    )
    return written


def _build_for_sha(db: Database, repo: str, sha: str) -> int | None:
    row = db.query_one(
        # Finished builds first (NULLs sort differently in SQLite and Postgres).
        "SELECT id FROM builds WHERE repo = ? AND head_sha = ?"
        " ORDER BY (finished_at IS NULL), finished_at DESC, id DESC LIMIT 1",
        (repo, sha),
    )
    return None if row is None else int(row["id"])


def normalize_release(db: Database, system: str, source_id: str, payload: dict[str, Any]) -> bool:
    repo = payload["_repo"]
    target = payload.get("_target_sha") or payload.get("target_sha")
    if not target or len(target) != 40:
        raise NormalizeError(f"release {payload.get('tag_name')} has no resolved target commit")
    _, written = upsert(
        db,
        "releases",
        {
            "repo": repo,
            "tag": payload["tag_name"],
            "target_sha": target,
            "build_id": _build_for_sha(db, repo, target),
            "released_at": iso(payload.get("published_at") or payload.get("created_at")),
            "url": payload.get("html_url"),
            "source_system": system,
            "source_id": source_id,
            "raw": payload,
        },
    )
    return written


def normalize_test_report(db: Database, system: str, source_id: str, payload: dict[str, Any]) -> bool:
    """Per-test outcomes for one build (from a JUnit report or the synthetic seed)."""
    repo = payload["_repo"]
    build_id = id_by(db, "builds", "source_id = ?", (payload["build"],)) if payload.get("build") else None
    if build_id is None and payload.get("head_sha"):
        build_id = _build_for_sha(db, repo, payload["head_sha"])
    if build_id is None:
        raise NormalizeError(f"test report {source_id}: build not found (sync builds first)")
    added = False
    for t in payload["tests"]:
        case_id = id_by(db, "test_cases", "repo = ? AND path = ? AND name = ?", (repo, t["path"], t["name"]))
        changed = db.execute(
            "INSERT INTO test_runs (build_id, test_case_id, path, name, outcome, duration_ms, message)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (build_id, path, name) DO UPDATE SET outcome = excluded.outcome,"
            " duration_ms = excluded.duration_ms, message = excluded.message, test_case_id = excluded.test_case_id"
            " WHERE test_runs.outcome <> excluded.outcome"
            " OR test_runs.test_case_id IS DISTINCT FROM excluded.test_case_id",
            (build_id, case_id, t["path"], t["name"], t["outcome"], t.get("duration_ms"), t.get("message")),
        )
        added |= changed > 0
    return added


def resolve_build_ref(db: Database, repo: str, ref: dict[str, Any]) -> int:
    """A build reference in authored seed data: {"pr": 3877}, {"release": "v2.34.0"} or {"sha": "..."}."""
    if "release" in ref:
        found = db.scalar("SELECT build_id FROM releases WHERE repo = ? AND tag = ?", (repo, ref["release"]))
    elif "pr" in ref:
        sha = db.scalar(
            "SELECT merge_commit_sha FROM pull_requests WHERE repo = ? AND number = ?", (repo, ref["pr"])
        )
        found = _build_for_sha(db, repo, str(sha)) if sha else None
    else:
        found = _build_for_sha(db, repo, str(ref.get("sha", "")))
    if found is None:
        raise NormalizeError(f"no build for {ref} in {repo}")
    return int(found)


def normalize_verification(db: Database, system: str, source_id: str, payload: dict[str, Any]) -> bool:
    """A manual sign-off (analysis, inspection or demonstration) against one build."""
    crit = db.query_one(
        "SELECT a.id, f.repo FROM acceptance_criteria a JOIN stories s ON s.id = a.story_id"
        " JOIN features f ON f.id = s.feature_id WHERE a.key = ?",
        (payload["criterion"],),
    )
    if crit is None:
        raise NormalizeError(f"criterion {payload['criterion']} not found")
    build_id = resolve_build_ref(db, crit["repo"], payload["build"])
    changed = db.execute(
        "INSERT INTO verification_results (criterion_id, build_id, method, result, source, evidence,"
        " verified_by, verified_at, note) VALUES (?, ?, ?, ?, 'manual', ?, ?, ?, ?)"
        " ON CONFLICT (criterion_id, build_id, method, source) DO UPDATE SET result = excluded.result,"
        " verified_by = excluded.verified_by, verified_at = excluded.verified_at, note = excluded.note"
        " WHERE verification_results.result <> excluded.result OR verification_results.note <> excluded.note",
        (
            crit["id"],
            build_id,
            payload["method"],
            payload["result"],
            db.json([]),
            payload["by"],
            iso(payload["at"]),
            payload.get("note"),
        ),
    )
    if changed:
        audit.record(
            db,
            actor=payload["by"],
            action="verification.recorded",
            entity="criterion",
            entity_key=payload["criterion"],
            build_id=build_id,
            effective_at=iso(payload["at"]),
            detail={"method": payload["method"], "result": payload["result"], "note": payload.get("note")},
        )
    return changed > 0


Handler = Callable[[Database, str, str, dict[str, Any]], bool]
HANDLERS: dict[str, Handler] = {
    "person": normalize_person,
    "feature": normalize_feature,
    "story": normalize_story,
    "requirement": normalize_requirement,
    "task": normalize_task,
    "criterion": normalize_criterion,
    "commit": normalize_commit,
    "pull_request": normalize_pull_request,
    "pr_commits": normalize_pr_commits,
    "review": normalize_review,
    "issue": normalize_issue,
    "build": normalize_build,
    "release": normalize_release,
    "test_report": normalize_test_report,
    "link": normalize_link,
    "verification": normalize_verification,
}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def process_staging(db: Database, *, batch_id: int | None = None) -> Counter[str]:
    """Normalize every unprocessed staging row in dependency order."""
    stats: Counter[str] = Counter()
    order = {name: i for i, name in enumerate(ENTITY_ORDER)}
    where = "processed_at IS NULL" + (" AND batch_id = ?" if batch_id is not None else "")
    params: tuple[Any, ...] = (batch_id,) if batch_id is not None else ()
    rows = db.query(
        f"SELECT id, source_system, entity, source_id, payload FROM staging_records WHERE {where}", params
    )
    rows.sort(key=lambda r: (order[r["entity"]], r["id"]))
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, str):
            import json

            payload = json.loads(payload)
        handler = HANDLERS[row["entity"]]
        try:
            with db.transaction():
                written = handler(db, row["source_system"], row["source_id"], payload)
                db.execute(
                    "UPDATE staging_records SET processed_at = ?, error = NULL WHERE id = ?",
                    (utcnow(), row["id"]),
                )
            stats[f"{row['entity']}.{'written' if written else 'unchanged'}"] += 1
        except Exception as exc:  # one bad record must not stop the load
            message = f"{type(exc).__name__}: {exc}"
            log.warning(
                "staging row %s (%s %s) failed: %s", row["id"], row["entity"], row["source_id"], message
            )
            db.execute("UPDATE staging_records SET error = ? WHERE id = ?", (message[:1000], row["id"]))
            db.commit()
            stats[f"{row['entity']}.error"] += 1
    return stats


def link_issue_references(db: Database) -> int:
    """Link commits to GitHub-issue tasks they reference ("fixes #12" in a commit
    message, or in the body of the PR that contains the commit)."""
    added = 0
    tasks = {
        (r["repo"], r["issue_number"]): r["id"]
        for r in db.query("SELECT id, repo, issue_number FROM tasks WHERE issue_number IS NOT NULL")
    }
    if not tasks:
        return 0
    for c in db.query("SELECT id, repo, message FROM commits"):
        for num in ISSUE_REF.findall(c["message"] or ""):
            task = tasks.get((c["repo"], int(num)))
            if task and link(
                db,
                "INSERT INTO commit_tasks (commit_id, task_id, link_source) VALUES (?, ?, 'message_ref')",
                (c["id"], task),
            ):
                added += 1
    for pr in db.query("SELECT id, repo, body FROM pull_requests WHERE body IS NOT NULL"):
        for num in ISSUE_REF.findall(pr["body"] or ""):
            task = tasks.get((pr["repo"], int(num)))
            if not task:
                continue
            for row in db.query(
                "SELECT commit_id FROM pull_request_commits WHERE pull_request_id = ?", (pr["id"],)
            ):
                if link(
                    db,
                    "INSERT INTO commit_tasks (commit_id, task_id, link_source) VALUES (?, ?, 'pr_ref')",
                    (row["commit_id"], task),
                ):
                    added += 1
    db.commit()
    return added
