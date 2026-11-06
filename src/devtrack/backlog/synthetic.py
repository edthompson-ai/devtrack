"""Synthetic code reviews for the offline seed.

Git history has no reviews, so the seed attaches one to three reviews to each real PR,
written by the backlog's fictional reviewers. They are clearly marked
(source_system = 'synthetic'), deterministic (seeded by PR number, so reruns produce
identical rows), and timed between the PR's first commit and its merge so that review
turnaround numbers are plausible. A live `devtrack sync` loads real reviews instead.
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Iterator
from typing import Any

from devtrack import ids

REVIEWERS = [
    ("jamal-wright", "Jamal Wright"),
    ("nina-kowalski", "Nina Kowalski"),
    ("sofia-reyes", "Sofia Reyes"),
]
COMMENTS = {
    "APPROVED": ["Looks good to me.", "LGTM, nice test coverage.", "Approving; thanks for the docs update."],
    "CHANGES_REQUESTED": ["Please add a regression test.", "Can we keep the old name as a deprecated alias?"],
    "COMMENTED": ["Question about the Windows path here.", "Small nit on naming, not blocking."],
}


def _parse(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _fmt(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def synthetic_reviews(
    repo: str, pulls: list[dict[str, Any]]
) -> Iterator[tuple[str, str, str, dict[str, Any]]]:
    for pr in pulls:
        if not pr.get("merged_at"):
            continue
        rng = random.Random(f"{repo}#{pr['number']}")
        merged = _parse(pr["merged_at"])
        if pr.get("created_at"):
            opened = _parse(pr["created_at"])
        else:
            # Open time unknown (squash merge): place reviews in a plausible window
            # of 4 to 96 hours before the merge.
            opened = merged - dt.timedelta(hours=rng.uniform(4, 96))
        span = max((merged - opened).total_seconds(), 3600.0)
        reviewers = rng.sample(REVIEWERS, k=rng.randint(1, 3))
        for i, (key, name) in enumerate(reviewers):
            last = i == len(reviewers) - 1
            state = "APPROVED" if last else rng.choice(["COMMENTED", "CHANGES_REQUESTED", "APPROVED"])
            # Reviews land somewhere in the open window; the approval comes last, before merge.
            fraction = 0.9 if last else rng.uniform(0.1, 0.8)
            submitted = opened + dt.timedelta(seconds=span * fraction)
            review_id = f"syn{pr['number']}{i}"
            yield (
                "synthetic",
                "review",
                ids.review(repo, pr["number"], review_id),
                {
                    "id": review_id,
                    "_repo": repo,
                    "_pr_number": pr["number"],
                    "_synthetic_reviewer": key,
                    "_reviewer_name": name,
                    "_provenance": "synthetic",
                    "user": None,
                    "state": state,
                    "body": rng.choice(COMMENTS[state]),
                    "submitted_at": _fmt(min(submitted, merged)),
                },
            )
