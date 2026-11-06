"""Verification: is each requirement actually verified, and on which build?

The chain this module works over:

    requirement --(method)--> criteria --(evidence)--> verification_results --> build --> release

* Every requirement names one verification method: test, analysis, inspection or
  demonstration. Only evidence of that method counts toward it. A passing unit test is
  not an inspection.
* ``test`` evidence is derived from CI: for each build, a criterion passes when every
  test linked to it ran and passed, and fails when any of them failed or errored. A
  build where a linked test was skipped or missing produces no result at all, because
  "didn't run" is not "passed".
* Analysis, inspection and demonstration evidence is a manual sign-off by a named
  person against one build (``record_verification``).
* Change control: when a requirement gets a new version, every result recorded before
  the change is invalidated (kept, with a reason, never deleted) and a re-verification
  item opens for each of its criteria. An item closes on the first valid passing
  result of the right method recorded after the change.
* Status is always relative to a target build: by default the latest main-branch build
  of the repository, or a named release.

Everything here is idempotent: ``refresh`` can run after every sync and writes nothing
when nothing changed.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

from devtrack import audit
from devtrack.db import Database, utcnow

log = logging.getLogger(__name__)

METHODS = ("test", "analysis", "inspection", "demonstration")
MANUAL_METHODS = ("analysis", "inspection", "demonstration")

CriterionStatus = Literal["failed", "verified", "reverify", "verified_older_build", "not_verified"]
RequirementStatus = Literal[
    "failed", "needs_reverification", "verified", "verified_older_build", "partial", "not_verified"
]

CRITERION_LABEL: dict[str, str] = {
    "failed": "Failed",
    "verified": "Verified",
    "reverify": "Re-verify",
    "verified_older_build": "Verified on older build",
    "not_verified": "Not verified",
}
REQUIREMENT_LABEL: dict[str, str] = {
    "failed": "Failed",
    "needs_reverification": "Needs re-verification",
    "verified": "Verified",
    "verified_older_build": "Verified on older build",
    "partial": "Partially verified",
    "not_verified": "Not verified",
}


class VerificationError(ValueError):
    """A manual sign-off that can't be recorded (unknown criterion or build, bad method)."""


def _loads(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


# ---------------------------------------------------------------------------
# 1. Test-derived results
# ---------------------------------------------------------------------------


def derive_test_verifications(db: Database) -> int:
    """Turn per-build test runs into per-build criterion results. Returns rows written."""
    rows = db.query(
        "SELECT ct.criterion_id, a.key AS criterion_key, tc.path, tc.name, tr.build_id, tr.outcome, tr.message,"
        " b.finished_at"
        " FROM criterion_tests ct"
        " JOIN acceptance_criteria a ON a.id = ct.criterion_id"
        " JOIN test_cases tc ON tc.id = ct.test_case_id"
        " JOIN test_runs tr ON tr.test_case_id = tc.id"
        " JOIN builds b ON b.id = tr.build_id"
        " WHERE b.finished_at IS NOT NULL"
    )
    linked: dict[int, int] = {
        int(r["criterion_id"]): int(r["n"])
        for r in db.query("SELECT criterion_id, count(*) AS n FROM criterion_tests GROUP BY criterion_id")
    }
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
    finished: dict[int, str] = {}
    for r in rows:
        grouped.setdefault((int(r["criterion_id"]), int(r["build_id"])), []).append(r)
        finished[int(r["build_id"])] = str(r["finished_at"])

    written = 0
    with db.transaction():
        for (crit, build), runs in sorted(grouped.items()):
            outcomes = [r["outcome"] for r in runs]
            if any(o in ("failed", "error") for o in outcomes):
                result = "failed"
            elif len(runs) == linked.get(crit, 0) and all(o == "passed" for o in outcomes):
                result = "passed"
            else:
                continue  # a linked test was skipped or didn't run: no evidence either way
            evidence = sorted(
                (
                    {"test": f"{r['path']}::{r['name']}", "outcome": r["outcome"]}
                    | ({"message": r["message"]} if r["outcome"] != "passed" and r["message"] else {})
                    for r in runs
                ),
                key=lambda e: e["test"],
            )
            changed = db.execute(
                "INSERT INTO verification_results (criterion_id, build_id, method, result, source, evidence,"
                " verified_by, verified_at) VALUES (?, ?, 'test', ?, 'test_runs', ?, 'ci', ?)"
                " ON CONFLICT (criterion_id, build_id, method, source) DO UPDATE SET"
                " result = excluded.result, evidence = excluded.evidence, verified_at = excluded.verified_at"
                " WHERE verification_results.result <> excluded.result"
                " OR verification_results.evidence <> excluded.evidence",
                (crit, build, result, db.json(evidence), finished[build]),
            )
            if changed:
                written += changed
                audit.record(
                    db,
                    actor="ci",
                    action="verification.derived",
                    entity="criterion",
                    entity_key=str(runs[0]["criterion_key"]),
                    build_id=build,
                    effective_at=finished[build],
                    detail={"method": "test", "result": result, "tests": len(runs)},
                )
    return written


# ---------------------------------------------------------------------------
# 2. Manual sign-offs
# ---------------------------------------------------------------------------


def record_verification(
    db: Database,
    *,
    criterion_key: str,
    build_id: int,
    method: str,
    result: str,
    verified_by: str,
    note: str | None = None,
    verified_at: str | None = None,
) -> int:
    """Record an analysis, inspection or demonstration result against one build.

    Test results come only from CI, so ``method='test'`` is refused here.
    """
    if method not in MANUAL_METHODS:
        raise VerificationError(
            f"method must be one of {', '.join(MANUAL_METHODS)}; test results come from CI"
        )
    if result not in ("passed", "failed"):
        raise VerificationError("result must be 'passed' or 'failed'")
    if not verified_by.strip():
        raise VerificationError("a sign-off needs a named person")
    crit = db.query_one(
        "SELECT a.id, f.repo FROM acceptance_criteria a JOIN stories s ON s.id = a.story_id"
        " JOIN features f ON f.id = s.feature_id WHERE a.key = ?",
        (criterion_key,),
    )
    if crit is None:
        raise VerificationError(f"unknown criterion {criterion_key}")
    build_repo = db.scalar("SELECT repo FROM builds WHERE id = ?", (build_id,))
    if build_repo is None:
        raise VerificationError(f"unknown build {build_id}")
    if build_repo != crit["repo"]:
        raise VerificationError(f"build {build_id} is in {build_repo}, criterion is in {crit['repo']}")
    with db.transaction():
        db.execute(
            "INSERT INTO verification_results (criterion_id, build_id, method, result, source, evidence,"
            " verified_by, verified_at, note) VALUES (?, ?, ?, ?, 'manual', ?, ?, ?, ?)"
            " ON CONFLICT (criterion_id, build_id, method, source) DO UPDATE SET result = excluded.result,"
            " verified_by = excluded.verified_by, verified_at = excluded.verified_at, note = excluded.note,"
            " invalidated_at = NULL, invalidated_reason = NULL",
            (crit["id"], build_id, method, result, db.json([]), verified_by, verified_at or utcnow(), note),
        )
        audit.record(
            db,
            actor=verified_by,
            action="verification.recorded",
            entity="criterion",
            entity_key=criterion_key,
            build_id=build_id,
            effective_at=verified_at,
            detail={"method": method, "result": result, "note": note},
        )
        vid = db.scalar(
            "SELECT id FROM verification_results WHERE criterion_id = ? AND build_id = ? AND method = ?"
            " AND source = 'manual'",
            (crit["id"], build_id, method),
        )
    return int(vid)


# ---------------------------------------------------------------------------
# 3. Change control
# ---------------------------------------------------------------------------


def apply_change_control(db: Database) -> Counter[str]:
    """Invalidate evidence that predates a requirement change and track re-verification.

    For every requirement revision with version >= 2:
      * results for its criteria recorded before the change are invalidated, and
      * one re-verification item per criterion is opened (once).
    Then every open item is closed by the earliest valid passing result of the
    requirement's method recorded at or after the change.
    """
    stats: Counter[str] = Counter()
    revisions = db.query(
        "SELECT r.id AS requirement_id, r.key, r.verification_method AS method, rv.version, rv.changed_at"
        " FROM requirement_revisions rv JOIN requirements r ON r.id = rv.requirement_id"
        " WHERE rv.version >= 2 ORDER BY rv.changed_at, r.key"
    )
    with db.transaction():
        for rev in revisions:
            reason = f"{rev['key']} changed to v{rev['version']} on {str(rev['changed_at'])[:10]}"
            stale = db.query(
                "SELECT v.id, v.build_id, v.method, v.result, a.key FROM verification_results v"
                " JOIN acceptance_criteria a ON a.id = v.criterion_id"
                " WHERE v.invalidated_at IS NULL AND v.verified_at < ?"
                " AND v.criterion_id IN (SELECT criterion_id FROM requirement_criteria WHERE requirement_id = ?)"
                " ORDER BY v.id",
                (rev["changed_at"], rev["requirement_id"]),
            )
            for v in stale:
                db.execute(
                    "UPDATE verification_results SET invalidated_at = ?, invalidated_reason = ? WHERE id = ?",
                    (rev["changed_at"], reason, v["id"]),
                )
                audit.record(
                    db,
                    actor="change-control",
                    action="verification.invalidated",
                    entity="criterion",
                    entity_key=v["key"],
                    build_id=int(v["build_id"]),
                    effective_at=str(rev["changed_at"]),
                    detail={
                        "requirement": rev["key"],
                        "version": rev["version"],
                        "method": v["method"],
                        "result": v["result"],
                        "reason": reason,
                    },
                )
            stats["invalidated"] += len(stale)
            criteria = db.query(
                "SELECT rc.criterion_id, a.key FROM requirement_criteria rc"
                " JOIN acceptance_criteria a ON a.id = rc.criterion_id WHERE rc.requirement_id = ?"
                " ORDER BY a.key",
                (rev["requirement_id"],),
            )
            for c in criteria:
                opened = db.execute(
                    "INSERT INTO reverification_items (requirement_id, requirement_version, criterion_id,"
                    " opened_at, status) VALUES (?, ?, ?, ?, 'open')"
                    " ON CONFLICT (requirement_id, requirement_version, criterion_id) DO NOTHING",
                    (rev["requirement_id"], rev["version"], c["criterion_id"], rev["changed_at"]),
                )
                if opened:
                    stats["items_opened"] += 1
                    audit.record(
                        db,
                        actor="change-control",
                        action="reverification.opened",
                        entity="criterion",
                        entity_key=c["key"],
                        effective_at=str(rev["changed_at"]),
                        detail={"requirement": rev["key"], "version": rev["version"]},
                    )

        # Each item is answered by evidence recorded inside its own window: from its
        # change up to the next version's change. Evidence after the next change
        # answers the newer item, and the older one is marked superseded.
        open_items = db.query(
            "SELECT i.id, i.criterion_id, i.opened_at, i.requirement_version, r.key AS requirement,"
            " r.verification_method AS method, (SELECT key FROM acceptance_criteria WHERE id = i.criterion_id)"
            " AS criterion,"
            " (SELECT min(n.opened_at) FROM reverification_items n WHERE n.requirement_id = i.requirement_id"
            "  AND n.criterion_id = i.criterion_id AND n.requirement_version > i.requirement_version) AS next_at"
            " FROM reverification_items i JOIN requirements r ON r.id = i.requirement_id"
            " WHERE i.status = 'open'"
        )
        for item in open_items:
            # Inside the window, a result answers this item even if the *next* change
            # later invalidated it: it was valid evidence for this version. For the
            # newest item the result must still be valid.
            params: tuple[Any, ...] = (item["criterion_id"], item["method"], item["opened_at"])
            if item["next_at"] is None:
                window = " AND invalidated_at IS NULL"
            else:
                window = " AND verified_at < ? AND (invalidated_at IS NULL OR invalidated_at >= ?)"
                params += (item["next_at"], item["next_at"])
            closer = db.query_one(
                "SELECT id, build_id, verified_at FROM verification_results"
                " WHERE criterion_id = ? AND method = ? AND result = 'passed'"
                f" AND verified_at >= ?{window} ORDER BY verified_at, id LIMIT 1",
                params,
            )
            if closer is not None:
                db.execute(
                    "UPDATE reverification_items SET status = 'closed', closed_at = ?,"
                    " closed_by_verification_id = ? WHERE id = ?",
                    (closer["verified_at"], closer["id"], item["id"]),
                )
                stats["items_closed"] += 1
                audit.record(
                    db,
                    actor="change-control",
                    action="reverification.closed",
                    entity="criterion",
                    entity_key=item["criterion"],
                    build_id=int(closer["build_id"]),
                    effective_at=str(closer["verified_at"]),
                    detail={
                        "requirement": item["requirement"],
                        "version": item["requirement_version"],
                        "closed_by_verification_id": int(closer["id"]),
                    },
                )
            elif item["next_at"] is not None:
                db.execute(
                    "UPDATE reverification_items SET status = 'superseded', closed_at = ? WHERE id = ?",
                    (item["next_at"], item["id"]),
                )
                stats["items_superseded"] += 1
                audit.record(
                    db,
                    actor="change-control",
                    action="reverification.superseded",
                    entity="criterion",
                    entity_key=item["criterion"],
                    effective_at=str(item["next_at"]),
                    detail={"requirement": item["requirement"], "version": item["requirement_version"]},
                )
    return stats


def refresh(db: Database) -> Counter[str]:
    """Derive test results, then apply change control. Safe to run after every sync."""
    stats: Counter[str] = Counter()
    stats["test_results"] += derive_test_verifications(db)
    stats.update(apply_change_control(db))
    log.info("verification refresh: %s", dict(stats))
    return stats


# ---------------------------------------------------------------------------
# 4. Status against a target build
# ---------------------------------------------------------------------------


@dataclass
class BuildRef:
    id: int
    repo: str
    run_number: int
    head_sha: str
    status: str
    finished_at: str | None
    event: str
    release: str | None = None
    synthetic: bool = False

    @property
    def label(self) -> str:
        return f"#{self.run_number}" + (f" ({self.release})" if self.release else "")


@dataclass
class CriterionVerification:
    key: str
    text: str
    status: CriterionStatus
    result: dict[str, Any] | None = None  # the result the status is based on
    result_build: BuildRef | None = None
    open_items: list[dict[str, Any]] = field(default_factory=list)
    invalidated: list[dict[str, Any]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return CRITERION_LABEL[self.status]


@dataclass
class RequirementVerification:
    key: str
    title: str
    version: int
    method: str
    repo: str | None
    target: BuildRef | None
    status: RequirementStatus
    criteria: list[CriterionVerification]
    parent_key: str | None = None
    depth: int = 0
    children: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return REQUIREMENT_LABEL[self.status]

    @property
    def is_rollup(self) -> bool:
        """A parent with no criteria of its own: its status comes from its children."""
        return bool(self.children) and not self.criteria

    @property
    def counts(self) -> Counter[str]:
        return Counter(c.status for c in self.criteria)

    @property
    def open_items(self) -> int:
        return sum(len(c.open_items) for c in self.criteria)


def _build_refs(db: Database) -> dict[int, BuildRef]:
    rows = db.query(
        "SELECT b.id, b.repo, b.run_number, b.head_sha, b.status, b.finished_at, b.event, b.source_system,"
        " (SELECT min(rl.tag) FROM releases rl WHERE rl.build_id = b.id) AS release"
        " FROM builds b"
    )
    return {
        int(r["id"]): BuildRef(
            id=int(r["id"]),
            repo=r["repo"],
            run_number=int(r["run_number"]),
            head_sha=r["head_sha"],
            status=r["status"],
            finished_at=None if r["finished_at"] is None else str(r["finished_at"]),
            event=r["event"],
            release=r["release"],
            synthetic=r["source_system"] == "synthetic",
        )
        for r in rows
    }


def target_build(db: Database, repo: str, release: str | None = None) -> BuildRef | None:
    """The build status is computed against: a release's build, or the latest finished
    main-branch build."""
    refs = _build_refs(db)
    if release is not None:
        bid = db.scalar("SELECT build_id FROM releases WHERE repo = ? AND tag = ?", (repo, release))
        return refs.get(int(bid)) if bid is not None else None
    bid = db.scalar(
        "SELECT id FROM builds WHERE repo = ? AND branch = 'main' AND event = 'push' AND finished_at IS NOT NULL"
        " ORDER BY finished_at DESC, run_number DESC LIMIT 1",
        (repo,),
    )
    return refs.get(int(bid)) if bid is not None else None


def criterion_status(
    results: list[dict[str, Any]], open_items: list[dict[str, Any]], target: BuildRef | None
) -> tuple[CriterionStatus, dict[str, Any] | None]:
    """Pure precedence rule, kept separate so it is easy to test.

    ``results`` are the valid results of the requirement's method on builds at or before
    the target, newest build first.
    """
    latest = results[0] if results else None
    if latest is not None and latest["result"] == "failed":
        return "failed", latest
    if latest is not None and target is not None and int(latest["build_id"]) == target.id:
        return "verified", latest
    if open_items:
        return "reverify", None
    if latest is not None:
        return "verified_older_build", latest
    return "not_verified", None


def rollup(statuses: list[CriterionStatus]) -> RequirementStatus:
    if not statuses:
        return "not_verified"
    s = set(statuses)
    if "failed" in s:
        return "failed"
    if "reverify" in s:
        return "needs_reverification"
    if s == {"verified"}:
        return "verified"
    if s <= {"verified", "verified_older_build"}:
        return "verified_older_build"
    if s & {"verified", "verified_older_build"}:
        return "partial"
    return "not_verified"


# A requirement's status, as seen by its parent. Partial stays partial.
_AS_CHILD: dict[str, str] = {
    "failed": "failed",
    "needs_reverification": "reverify",
    "verified": "verified",
    "verified_older_build": "verified_older_build",
    "partial": "partial",
    "not_verified": "not_verified",
}


def rollup_tree(own: list[CriterionStatus], children: list[RequirementStatus]) -> RequirementStatus:
    """Combine a requirement's own criteria with its children's statuses.

    Same precedence as ``rollup``: any failure fails the parent, any open
    re-verification blocks it, and it is verified only when everything under it is.
    """
    s = set(own) | {_AS_CHILD[c] for c in children}
    if not s:
        return "not_verified"
    if "failed" in s:
        return "failed"
    if "reverify" in s:
        return "needs_reverification"
    if s == {"verified"}:
        return "verified"
    if s <= {"verified", "verified_older_build"}:
        return "verified_older_build"
    if s & {"verified", "verified_older_build", "partial"}:
        return "partial"
    return "not_verified"


def requirement_status(
    db: Database, *, release: str | None = None, repo: str | None = None
) -> list[RequirementVerification]:
    """Verification status of every requirement against its repository's target build."""
    refs = _build_refs(db)
    reqs = db.query(
        "SELECT r.id, r.key, r.title, r.version, r.verification_method AS method, r.parent_id,"
        " (SELECT min(f.repo) FROM requirement_criteria rc JOIN acceptance_criteria a ON a.id = rc.criterion_id"
        "  JOIN stories s ON s.id = a.story_id JOIN features f ON f.id = s.feature_id"
        "  WHERE rc.requirement_id = r.id) AS repo"
        " FROM requirements r ORDER BY r.key"
    )
    crit_rows = db.query(
        "SELECT rc.requirement_id, a.id, a.key, a.text FROM requirement_criteria rc"
        " JOIN acceptance_criteria a ON a.id = rc.criterion_id ORDER BY a.key"
    )
    result_rows = db.query(
        "SELECT id, criterion_id, build_id, method, result, source, evidence, verified_by, verified_at, note,"
        " invalidated_at, invalidated_reason FROM verification_results"
    )
    item_rows = db.query(
        "SELECT requirement_id, requirement_version, criterion_id, opened_at FROM reverification_items"
        " WHERE status = 'open'"
    )
    targets: dict[str, BuildRef | None] = {}
    out: list[RequirementVerification] = []
    for req in reqs:
        if repo is not None and req["repo"] is not None and req["repo"] != repo:
            continue
        r_repo = req["repo"]
        if r_repo not in targets:
            targets[r_repo] = target_build(db, r_repo, release) if r_repo else None
        target = targets[r_repo]
        cutoff = target.finished_at if target else None
        criteria: list[CriterionVerification] = []
        for c in (c for c in crit_rows if c["requirement_id"] == req["id"]):
            mine = [r for r in result_rows if r["criterion_id"] == c["id"]]
            valid = [
                r
                for r in mine
                if r["method"] == req["method"]
                and r["invalidated_at"] is None
                and (cutoff is None or str(refs[int(r["build_id"])].finished_at or "") <= cutoff)
            ]
            valid.sort(
                key=lambda r: (str(refs[int(r["build_id"])].finished_at or ""), str(r["verified_at"])),
                reverse=True,
            )
            items = [
                {"version": i["requirement_version"], "opened_at": str(i["opened_at"])}
                for i in item_rows
                if i["requirement_id"] == req["id"] and i["criterion_id"] == c["id"]
            ]
            status, basis = criterion_status(valid, items, target)
            if basis is not None:
                basis = {**basis, "evidence": _loads(basis["evidence"])}
            criteria.append(
                CriterionVerification(
                    key=c["key"],
                    text=c["text"],
                    status=status,
                    result=basis,
                    result_build=refs[int(basis["build_id"])] if basis else None,
                    open_items=items,
                    invalidated=[
                        {
                            "build": refs[int(r["build_id"])].label,
                            "method": r["method"],
                            "result": r["result"],
                            "reason": r["invalidated_reason"],
                        }
                        for r in mine
                        if r["invalidated_at"] is not None and r["method"] == req["method"]
                    ],
                )
            )
        out.append(
            RequirementVerification(
                key=req["key"],
                title=req["title"],
                version=int(req["version"]),
                method=req["method"],
                repo=r_repo,
                target=target,
                status=rollup([c.status for c in criteria]),
                criteria=criteria,
            )
        )
    return _arrange_tree(out, {int(r["id"]): r for r in reqs})


def _arrange_tree(
    flat: list[RequirementVerification], rows: dict[int, dict[str, Any]]
) -> list[RequirementVerification]:
    """Roll statuses up the hierarchy and return the requirements in tree order
    (parents before children), dropping parents with nothing under them in scope."""
    by_key = {r.key: r for r in flat}
    key_of = {i: str(r["key"]) for i, r in rows.items()}
    for row in rows.values():
        child = by_key.get(str(row["key"]))
        parent_id = row.get("parent_id")
        if child is None or parent_id is None:
            continue
        child.parent_key = key_of[int(parent_id)]
        if child.parent_key in by_key:
            by_key[child.parent_key].children.append(child.key)

    def settle(key: str, depth: int) -> None:
        node = by_key[key]
        node.depth = depth
        node.children.sort()
        for c in node.children:
            settle(c, depth + 1)
        # Children with nothing in scope (no criteria, no children left) don't count.
        node.children = [c for c in node.children if by_key[c].criteria or by_key[c].children]
        if node.children:
            node.status = rollup_tree(
                [c.status for c in node.criteria], [by_key[c].status for c in node.children]
            )

    roots = sorted(k for k, r in by_key.items() if r.parent_key is None or r.parent_key not in by_key)
    for root in roots:
        settle(root, 0)

    ordered: list[RequirementVerification] = []

    def walk(key: str) -> None:
        node = by_key[key]
        # A parent whose children are all out of scope (repo filter) has nothing to show.
        if node.criteria or node.children:
            ordered.append(node)
        for c in node.children:
            walk(c)

    for root in roots:
        walk(root)
    return ordered


def criterion_verification(db: Database, criterion_key: str) -> list[tuple[str, CriterionVerification]]:
    """The criterion's status under each requirement it belongs to (for the trace page)."""
    out = []
    for req in requirement_status(db):
        for c in req.criteria:
            if c.key == criterion_key:
                out.append((req.key, c))
    return out


def find_build(db: Database, repo: str, ref: str) -> BuildRef:
    """Resolve what a person types: a build's source id, a run number, a release tag or a
    commit sha (prefix of 7+ characters)."""
    refs = _build_refs(db)
    rows = db.query("SELECT id FROM builds WHERE repo = ? AND source_id = ?", (repo, ref))
    if not rows and ref.lstrip("#").isdigit():
        rows = db.query(
            "SELECT id FROM builds WHERE repo = ? AND run_number = ?", (repo, int(ref.lstrip("#")))
        )
        if len(rows) > 1:
            raise VerificationError(
                f"run number {ref} exists in several workflows of {repo}; use the build id"
            )
    if not rows:
        rows = db.query(
            "SELECT build_id AS id FROM releases WHERE repo = ? AND tag = ? AND build_id IS NOT NULL",
            (repo, ref),
        )
    if not rows and len(ref) >= 7 and all(ch in "0123456789abcdef" for ch in ref.lower()):
        rows = db.query(
            "SELECT id FROM builds WHERE repo = ? AND head_sha LIKE ?"
            " ORDER BY (finished_at IS NULL), finished_at DESC, id DESC LIMIT 1",
            (repo, ref.lower() + "%"),
        )
    if not rows:
        raise VerificationError(f"no build {ref!r} in {repo}")
    return refs[int(rows[0]["id"])]


def recent_builds(db: Database, limit: int = 30) -> list[BuildRef]:
    refs = _build_refs(db)
    return sorted(refs.values(), key=lambda b: (b.finished_at or "", b.run_number), reverse=True)[:limit]
