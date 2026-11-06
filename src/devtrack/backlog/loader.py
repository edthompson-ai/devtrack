"""Turn the backlog file (seed/backlog.json, AI-written for this project) into staging records.

The backlog (seed/backlog.json) is synthetic: a plausible feature -> story -> task
hierarchy with acceptance criteria and architect requirements, written against real
merged PRs in the seed snapshot. Tasks point at real commits by PR number or sha
prefix. It goes through the same staging -> normalize path as GitHub data, so the
loader only reshapes; all validation and linking happen in the normalizer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

StagedTuple = tuple[str, str, str, dict[str, Any]]  # (source_system, entity, source_id, payload)


def synthetic_sha(key: str) -> str:
    """Stable 40-char sha for a synthetic commit, so reruns produce the same key."""
    return hashlib.sha1(f"synthetic:{key}".encode()).hexdigest()


def read_backlog(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    return data


def backlog_records(backlog: dict[str, Any]) -> Iterator[StagedTuple]:
    for person in backlog.get("people", []):
        yield "synthetic", "person", f"synthetic:{person['key']}", person

    for feature in backlog["features"]:
        repo = feature["repo"]
        feature_payload = {k: v for k, v in feature.items() if k != "stories"}
        yield "backlog", "feature", feature["key"], feature_payload
        for story in feature["stories"]:
            story_payload = {k: v for k, v in story.items() if k not in ("tasks", "criteria")}
            story_payload["feature"] = feature["key"]
            yield "backlog", "story", story["key"], story_payload
            default_time = story.get("started_at") or feature["created_at"]

            for task in story.get("tasks", []):
                task_payload = {
                    k: v for k, v in task.items() if k not in ("prs", "commits", "synthetic_commits")
                }
                task_payload.update({"story": story["key"], "repo": repo})
                yield "backlog", "task", task["key"], task_payload
                for pr in task.get("prs", []):
                    yield (
                        "backlog",
                        "link",
                        f"{task['key']}->pr-{pr}",
                        {
                            "kind": "commit_task_pr",
                            "task": task["key"],
                            "repo": repo,
                            "pr": pr,
                        },
                    )
                for prefix in task.get("commits", []):
                    yield (
                        "backlog",
                        "link",
                        f"{task['key']}->sha-{prefix}",
                        {
                            "kind": "commit_task_sha",
                            "task": task["key"],
                            "repo": repo,
                            "sha_prefix": prefix,
                        },
                    )
                for key in task.get("synthetic_commits", []):
                    yield (
                        "backlog",
                        "link",
                        f"{task['key']}->synthetic-{key}",
                        {
                            "kind": "commit_task_sha",
                            "task": task["key"],
                            "repo": repo,
                            "sha_prefix": synthetic_sha(key),
                        },
                    )

            for crit in story.get("criteria", []):
                yield (
                    "backlog",
                    "criterion",
                    crit["key"],
                    {
                        "key": crit["key"],
                        "story": story["key"],
                        "text": crit["text"],
                        "created_at": crit.get("created_at", default_time),
                        "updated_at": crit.get("updated_at", crit.get("created_at", default_time)),
                    },
                )
                for task_key in crit.get("tasks", []):
                    yield (
                        "backlog",
                        "link",
                        f"{task_key}->{crit['key']}",
                        {
                            "kind": "task_criteria",
                            "task": task_key,
                            "criterion": crit["key"],
                        },
                    )
                for path, name in crit.get("tests", []):
                    yield (
                        "backlog",
                        "link",
                        f"{crit['key']}->test:{path}::{name}",
                        {
                            "kind": "criterion_test",
                            "criterion": crit["key"],
                            "repo": repo,
                            "path": path,
                            "name": name,
                        },
                    )

    for req in backlog.get("requirements", []):
        yield (
            "backlog",
            "requirement",
            req["key"],
            {k: v for k, v in req.items() if k not in ("criteria", "parent")},
        )
        if req.get("parent"):
            yield (
                "backlog",
                "link",
                f"{req['key']}->parent:{req['parent']}",
                {"kind": "requirement_parent", "requirement": req["key"], "parent": req["parent"]},
            )
        for crit_key in req.get("criteria", []):
            yield (
                "backlog",
                "link",
                f"{req['key']}->{crit_key}",
                {
                    "kind": "requirement_criteria",
                    "requirement": req["key"],
                    "criterion": crit_key,
                },
            )

    for v in backlog.get("verifications", []):
        build = v["build"]
        ref = "-".join(f"{k}{val}" for k, val in sorted(build.items()))
        yield "backlog", "verification", f"{v['criterion']}@{ref}/{v['method']}", v

    people = {p["key"]: p["display_name"] for p in backlog.get("people", [])}
    for sc in backlog.get("synthetic_commits", []):
        sha = synthetic_sha(sc["key"])
        yield (
            "synthetic",
            "commit",
            f"{sc['repo']}@{sha}",
            {
                "_repo": sc["repo"],
                "_provenance": "synthetic",
                "_synthetic_author": sc["author"],
                "sha": sha,
                "commit": {
                    "author": {"name": people.get(sc["author"], sc["author"]), "date": sc["committed_at"]},
                    "committer": {"name": people.get(sc["author"], sc["author"]), "date": sc["committed_at"]},
                    "message": sc["message"],
                },
                "author": None,
                "parents": [{"sha": "0" * 40}],
                "stats": {
                    "additions": sum(f["additions"] for f in sc["files"]),
                    "deletions": sum(f["deletions"] for f in sc["files"]),
                },
                "files": sc["files"],
            },
        )
