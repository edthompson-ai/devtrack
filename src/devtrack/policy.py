"""Data classification per repository, and which models may see each one.

Classifications:
  * ``public``     -- open source; any configured model may see it.
  * ``internal``   -- company code, not export-controlled; external models allowed
                      under the company's normal vendor agreement.
  * ``controlled`` -- export-controlled (ITAR/EAR) or otherwise restricted. Its code,
                      diffs and criteria must never leave the boundary: only a model
                      marked as an approved internal deployment may judge it.

The policy fails closed: a repository with no row in ``repo_policies`` is treated as
controlled, so forgetting to classify a new repository blocks it rather than leaking it.

The check runs in ``checker.run.check_criterion`` before any context is built or sent,
so a blocked criterion never reaches a prompt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from devtrack import audit
from devtrack.db import Database, utcnow

Classification = Literal["public", "internal", "controlled"]
CLASSIFICATIONS: tuple[Classification, ...] = ("public", "internal", "controlled")
DEFAULT_CLASSIFICATION: Classification = "controlled"  # fail closed


class PolicyBlocked(PermissionError):
    """The chosen model is not allowed to see this repository's data."""

    def __init__(self, repo: str, classification: str, model: str) -> None:
        super().__init__(
            f"{repo} is classified '{classification}': its data may not be sent to the external model "
            f"'{model}'. Use an approved internal model (INTERNAL_MODEL_URL) or reclassify the repository."
        )
        self.repo = repo
        self.classification = classification
        self.model = model


@dataclass(frozen=True)
class RepoPolicy:
    repo: str
    classification: Classification
    note: str | None
    set_by: str | None
    set_at: str | None
    explicit: bool  # False when no row exists and the fail-closed default applies


def classification_of(db: Database, repo: str) -> RepoPolicy:
    row = db.query_one(
        "SELECT repo, classification, note, set_by, set_at FROM repo_policies WHERE repo = ?", (repo,)
    )
    if row is None:
        return RepoPolicy(
            repo, DEFAULT_CLASSIFICATION, "not classified: treated as controlled", None, None, False
        )
    return RepoPolicy(
        row["repo"], row["classification"], row["note"], row["set_by"], str(row["set_at"]), True
    )


def allows(classification: str, *, external: bool) -> bool:
    """Only controlled data is restricted, and only from external models."""
    return not (classification == "controlled" and external)


def enforce(db: Database, repo: str, *, model: str, external: bool) -> RepoPolicy:
    policy = classification_of(db, repo)
    if not allows(policy.classification, external=external):
        raise PolicyBlocked(repo, policy.classification, model)
    return policy


def set_classification(
    db: Database, repo: str, classification: str, *, set_by: str, note: str | None = None
) -> None:
    if classification not in CLASSIFICATIONS:
        raise ValueError(f"classification must be one of {', '.join(CLASSIFICATIONS)}")
    if not set_by.strip():
        raise ValueError("a classification needs a named owner")
    before = db.scalar("SELECT classification FROM repo_policies WHERE repo = ?", (repo,))
    changed = db.execute(
        "INSERT INTO repo_policies (repo, classification, note, set_by, set_at) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (repo) DO UPDATE SET classification = excluded.classification, note = excluded.note,"
        " set_by = excluded.set_by, set_at = excluded.set_at"
        " WHERE repo_policies.classification <> excluded.classification"
        " OR repo_policies.note IS DISTINCT FROM excluded.note",
        (repo, classification, note, set_by, utcnow()),
    )
    if changed and before != classification:
        audit.record(
            db,
            actor=set_by,
            action="policy.classified",
            entity="repository",
            entity_key=repo,
            detail={"classification": classification, "previous": before, "note": note},
        )


def load_policy_file(db: Database, path: Path) -> int:
    """Apply a policy file ({"set_by": ..., "repos": {repo: {classification, note}}})."""
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    repos = data.get("repos", {})
    with db.transaction():
        for repo, entry in repos.items():
            set_classification(
                db, repo, entry["classification"], set_by=data["set_by"], note=entry.get("note")
            )
    return len(repos)


def all_policies(db: Database) -> list[RepoPolicy]:
    """Every repository the database knows about, with its effective classification."""
    repos = {
        str(r["repo"])
        for r in db.query(
            "SELECT repo FROM features UNION SELECT repo FROM commits UNION SELECT repo FROM repo_policies"
        )
    }
    return [classification_of(db, repo) for repo in sorted(repos)]
