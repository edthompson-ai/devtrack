"""Live, incremental sync from the GitHub REST API.

For each repo: read the watermarks, fetch only what changed (see github/fetch.py),
stage it, normalize it, then advance the watermarks. Watermarks move only after the
batch has been processed, so a crash means the next run re-fetches the same window
instead of silently skipping it. Re-fetched records are harmless: identical payloads
are no-ops in staging, and changed ones upsert.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable, Iterator
from typing import Any

from devtrack.db import Database
from devtrack.github.client import GitHubClient
from devtrack.github.fetch import (
    StagedRecord,
    fetch_builds,
    fetch_commits,
    fetch_issues,
    fetch_pulls,
    fetch_releases,
    with_overlap,
)
from devtrack.ingest.normalize import iso, link_issue_references, process_staging
from devtrack.ingest.staging import finish_batch, get_watermark, set_watermark, stage, start_batch
from devtrack.verification import refresh as refresh_verification

log = logging.getLogger(__name__)

SOURCE = "github"


class _HighWater:
    """Watches records stream past and remembers the newest timestamp per entity."""

    def __init__(self) -> None:
        self.newest: dict[str, str] = {}
        self.oldest_running_build: str | None = None

    def observe(self, records: Iterable[StagedRecord]) -> Iterator[tuple[str, str, dict[str, Any]]]:
        for rec in records:
            stamp = self._stamp(rec)
            if stamp is not None:
                key = {
                    "commit": "commits",
                    "pull_request": "pulls",
                    "issue": "issues",
                    "build": "builds",
                }.get(rec.entity)
                if key and stamp > self.newest.get(key, ""):
                    self.newest[key] = stamp
            if rec.entity == "build" and rec.payload.get("status") != "completed":
                started = iso(rec.payload.get("created_at"))
                if started and (self.oldest_running_build is None or started < self.oldest_running_build):
                    self.oldest_running_build = started
            yield rec.entity, rec.source_id, rec.payload

    def marks(self) -> dict[str, str]:
        """Watermarks to store. A run still in progress holds the build watermark back,
        so the next sync fetches it again and records how it finished."""
        out = dict(self.newest)
        if self.oldest_running_build is not None and "builds" in out:
            out["builds"] = min(out["builds"], self.oldest_running_build)
        return out

    @staticmethod
    def _stamp(rec: StagedRecord) -> str | None:
        p = rec.payload
        if rec.entity == "commit":
            return iso((p.get("commit") or {}).get("committer", {}).get("date"))
        if rec.entity in ("pull_request", "issue"):
            return iso(p.get("updated_at"))
        if rec.entity == "build" and p.get("status") == "completed":
            # Only finished runs move the watermark: an in-progress run must be fetched
            # again until it completes.
            return iso(p.get("created_at"))
        return None


def sync_repo(
    db: Database, client: GitHubClient, repo: str, *, max_items: int = 300, workflow: str | None = None
) -> Counter[str]:
    batch = start_batch(db, SOURCE, repo)
    stats: Counter[str] = Counter()
    try:
        marks = {e: get_watermark(db, SOURCE, repo, e) for e in ("commits", "pulls", "issues", "builds")}
        known = {r["sha"] for r in db.query("SELECT sha FROM commits WHERE repo = ?", (repo,))}
        tags = {r["tag"] for r in db.query("SELECT tag FROM releases WHERE repo = ?", (repo,))}
        high = _HighWater()

        def everything() -> Iterator[StagedRecord]:
            yield from fetch_commits(
                client, repo, since=with_overlap(marks["commits"]), known_shas=known, max_items=max_items
            )
            yield from fetch_pulls(client, repo, since=with_overlap(marks["pulls"]), max_items=max_items)
            yield from fetch_issues(client, repo, since=with_overlap(marks["issues"]), max_items=max_items)
            yield from fetch_builds(
                client, repo, since=with_overlap(marks["builds"]), max_items=max_items, workflow=workflow
            )
            yield from fetch_releases(client, repo, known_tags=tags, max_items=max_items)

        new, same = stage(db, batch, SOURCE, high.observe(everything()))
        stats["staged.new"], stats["staged.unchanged"] = new, same
        # Process every unprocessed row, not just this batch: records that failed in an
        # earlier run (fixed parent now present, or code fixed) get retried here.
        stats.update(process_staging(db))
        stats["links.issue_refs"] = link_issue_references(db)
        stats.update({f"verification.{k}": v for k, v in refresh_verification(db).items()})

        errors = sum(v for k, v in stats.items() if k.endswith(".error"))
        for entity, stamp in high.marks().items():
            set_watermark(db, SOURCE, repo, entity, stamp)
        stats["requests"] = client.requests_made
        finish_batch(db, batch, "succeeded", {**stats, "record_errors": errors})
        log.info("%s synced: %s", repo, dict(stats))
        return stats
    except Exception:
        finish_batch(db, batch, "failed", dict(stats))
        raise


def sync_all(
    db: Database,
    client: GitHubClient,
    repos: Iterable[str],
    *,
    max_items: int = 300,
    workflow: str | None = None,
) -> Counter[str]:
    total: Counter[str] = Counter()
    for repo in repos:
        total.update(sync_repo(db, client, repo, max_items=max_items, workflow=workflow))
    return total
