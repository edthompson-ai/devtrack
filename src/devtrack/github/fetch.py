"""Fetch repository data from the GitHub REST API as staging records.

Each function yields ``StagedRecord``s whose payload is the API's own JSON (unchanged),
so the staging table holds exactly what GitHub returned. Incremental sync:
  * commits: ``since=<watermark>``; commit details (files and patches) are only fetched
    for shas we don't already have, because each detail costs one request.
  * issues:  ``since=<watermark>`` filters on updated_at server-side.
  * pulls:   no ``since`` parameter exists, so we sort by updated desc and stop at the
    first PR older than the watermark.
  * reviews and PR commit lists: fetched for PRs that changed since the watermark.
  * builds:  GitHub Actions workflow runs, ``created=>=<watermark>``.
  * releases: the list is short and has no ``since``; only tags we don't have yet are
    resolved to their commit (one request each).
The watermark is moved back by a small overlap so clock skew and in-flight updates on
GitHub's side can't make us miss a record; the upserts make the overlap harmless.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from devtrack import ids
from devtrack.github.client import GitHubClient

log = logging.getLogger(__name__)

OVERLAP = dt.timedelta(minutes=10)


@dataclass(frozen=True)
class StagedRecord:
    entity: str
    source_id: str
    payload: dict[str, Any]


def with_overlap(watermark: str | None) -> str | None:
    if watermark is None:
        return None
    moment = dt.datetime.fromisoformat(watermark.replace("Z", "+00:00")) - OVERLAP
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_commits(
    client: GitHubClient,
    repo: str,
    *,
    since: str | None,
    known_shas: set[str],
    max_items: int,
) -> Iterator[StagedRecord]:
    params: dict[str, Any] = {}
    if since:
        params["since"] = since
    for item in client.paginate(f"repos/{repo}/commits", params, max_items=max_items):
        sha = item["sha"]
        if sha in known_shas:
            continue
        detail = client.get_json(f"repos/{repo}/commits/{sha}")
        detail["_repo"] = repo
        yield StagedRecord("commit", ids.commit(repo, sha), detail)


def fetch_pulls(
    client: GitHubClient, repo: str, *, since: str | None, max_items: int
) -> Iterator[StagedRecord]:
    def older_than_watermark(pr: dict[str, Any]) -> bool:
        return since is not None and pr["updated_at"] < since

    params = {"state": "all", "sort": "updated", "direction": "desc"}
    for pr in client.paginate(f"repos/{repo}/pulls", params, max_items=max_items, stop=older_than_watermark):
        number = pr["number"]
        pr["_repo"] = repo
        yield StagedRecord("pull_request", ids.pull_request(repo, number), pr)

        shas = [c["sha"] for c in client.paginate(f"repos/{repo}/pulls/{number}/commits")]
        yield StagedRecord(
            "pr_commits", ids.pr_commits(repo, number), {"_repo": repo, "number": number, "shas": shas}
        )
        for review in client.paginate(f"repos/{repo}/pulls/{number}/reviews"):
            review["_repo"] = repo
            review["_pr_number"] = number
            yield StagedRecord("review", ids.review(repo, number, review["id"]), review)


def fetch_issues(
    client: GitHubClient, repo: str, *, since: str | None, max_items: int
) -> Iterator[StagedRecord]:
    params: dict[str, Any] = {"state": "all", "sort": "updated", "direction": "desc"}
    if since:
        params["since"] = since
    for issue in client.paginate(f"repos/{repo}/issues", params, max_items=max_items):
        if "pull_request" in issue:  # the issues endpoint also returns PRs; skip them
            continue
        issue["_repo"] = repo
        yield StagedRecord("issue", ids.issue(repo, issue["number"]), issue)


def fetch_builds(
    client: GitHubClient, repo: str, *, since: str | None, max_items: int, workflow: str | None = None
) -> Iterator[StagedRecord]:
    """Workflow runs. ``workflow`` (a file name like ``tests.yaml``) limits the fetch to
    the workflow that runs the test suite; unset, every workflow's runs are kept."""
    path = f"repos/{repo}/actions/workflows/{workflow}/runs" if workflow else f"repos/{repo}/actions/runs"
    params: dict[str, Any] = {}
    if since:
        params["created"] = f">={since}"
    for run in client.paginate(path, params, max_items=max_items, items_key="workflow_runs"):
        run["_repo"] = repo
        yield StagedRecord("build", ids.workflow_run(repo, run["id"]), run)


def fetch_releases(
    client: GitHubClient, repo: str, *, known_tags: set[str], max_items: int
) -> Iterator[StagedRecord]:
    for rel in client.paginate(f"repos/{repo}/releases", max_items=max_items):
        if rel.get("draft") or rel["tag_name"] in known_tags:
            continue
        # The release object names a branch or sha in target_commitish, which is not the
        # tagged commit once the branch moves on. Resolve the tag itself.
        commit = client.get_json(f"repos/{repo}/commits/{rel['tag_name']}")
        rel["_repo"] = repo
        rel["_target_sha"] = commit["sha"]
        yield StagedRecord("release", ids.release(repo, rel["tag_name"]), rel)
