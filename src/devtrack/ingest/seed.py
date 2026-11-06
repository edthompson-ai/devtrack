"""Load the offline seed: real git-derived GitHub data plus the synthetic backlog.

Everything goes through staging -> normalize, the same path a live sync uses.
"""

from __future__ import annotations

import gzip
import json
import logging
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from devtrack import ids
from devtrack.backlog.loader import backlog_records, read_backlog
from devtrack.backlog.synthetic import synthetic_reviews
from devtrack.backlog.synthetic_ci import synthetic_builds, synthetic_test_reports
from devtrack.db import Database
from devtrack.ingest.normalize import link_issue_references, process_staging
from devtrack.ingest.staging import finish_batch, stage, start_batch
from devtrack.policy import load_policy_file
from devtrack.verification import refresh as refresh_verification

log = logging.getLogger(__name__)

DEFAULT_SEED_DIR = Path(__file__).resolve().parents[3] / "seed"


def snapshot_records(snapshot: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    repo = snapshot["repo"]
    for commit in snapshot["commits"]:
        yield "commit", ids.commit(repo, commit["sha"]), {**commit, "_repo": repo}
    for pr in snapshot["pulls"]:
        yield "pull_request", ids.pull_request(repo, pr["number"]), {**pr, "_repo": repo}
    for pc in snapshot["pr_commits"]:
        yield "pr_commits", ids.pr_commits(repo, pc["number"]), {"_repo": repo, **pc}
    for rel in snapshot.get("releases", []):
        yield "release", ids.release(repo, rel["tag_name"]), {**rel, "_repo": repo}


def read_snapshot(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    return data


def load_seed(db: Database, seed_dir: Path = DEFAULT_SEED_DIR) -> Counter[str]:
    totals: Counter[str] = Counter()
    snapshots = [read_snapshot(p) for p in sorted((seed_dir / "github").glob("*.json.gz"))]
    if not snapshots:
        raise FileNotFoundError(f"no seed snapshots in {seed_dir / 'github'}")

    for snap in snapshots:
        batch = start_batch(db, "github", snap["repo"])
        new, same = stage(db, batch, "github", snapshot_records(snap))
        totals["staged.new"] += new
        totals["staged.unchanged"] += same
        finish_batch(db, batch, "succeeded", {"staged_new": new, "staged_unchanged": same, "mode": "seed"})

    batch = start_batch(db, "synthetic", "ci-builds")
    builds = [b for snap in snapshots for b in synthetic_builds(snap)]
    new, same = stage(db, batch, "synthetic", builds)
    totals["staged.new"] += new
    totals["staged.unchanged"] += same
    finish_batch(db, batch, "succeeded", {"staged_new": new, "staged_unchanged": same})

    batch = start_batch(db, "synthetic", "reviews")
    reviews = [r for snap in snapshots for r in synthetic_reviews(snap["repo"], snap["pulls"])]
    new, same = stage(db, batch, "synthetic", ((e, s, p) for _, e, s, p in reviews))
    totals["staged.new"] += new
    totals["staged.unchanged"] += same
    finish_batch(db, batch, "succeeded", {"staged_new": new, "staged_unchanged": same})

    backlog = read_backlog(seed_dir / "backlog.json")
    by_system: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for system, entity, source_id, payload in backlog_records(backlog):
        by_system.setdefault(system, []).append((entity, source_id, payload))
    for system, records in by_system.items():
        batch = start_batch(db, system, "backlog")
        new, same = stage(db, batch, system, records)
        totals["staged.new"] += new
        totals["staged.unchanged"] += same
        finish_batch(db, batch, "succeeded", {"staged_new": new, "staged_unchanged": same})

    totals.update(process_staging(db))
    totals["links.issue_refs"] += link_issue_references(db)

    # Test results need builds and test links in the database, so they are a second pass.
    batch = start_batch(db, "synthetic", "ci-test-reports")
    new, same = stage(db, batch, "synthetic", list(synthetic_test_reports(db)))
    totals["staged.new"] += new
    totals["staged.unchanged"] += same
    finish_batch(db, batch, "succeeded", {"staged_new": new, "staged_unchanged": same})
    totals.update(process_staging(db))

    policy_file = seed_dir / "repo_policy.json"
    if policy_file.exists():
        totals["policy.repos"] += load_policy_file(db, policy_file)
    totals.update({f"verification.{k}": v for k, v in refresh_verification(db).items()})
    log.info("seed load: %s", dict(totals))
    return totals
