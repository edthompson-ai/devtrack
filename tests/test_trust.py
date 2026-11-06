"""Requirement hierarchy, the append-only audit log, and the full record per verdict."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from devtrack.audit import record, verify_chain
from devtrack.checker.guardrails import Budget
from devtrack.checker.judges import LLMJudge, LLMResult
from devtrack.checker.run import check_criterion, review_fail
from devtrack.db import Database
from devtrack.ingest.normalize import process_staging
from devtrack.ingest.seed import load_seed
from devtrack.ingest.staging import stage, start_batch
from devtrack.policy import set_classification
from devtrack.quality import run_checks
from devtrack.verification import find_build, record_verification, requirement_status, rollup_tree


@pytest.fixture
def loaded(db: Database) -> Database:
    load_seed(db)
    return db


# -- hierarchy ------------------------------------------------------------------------


def test_rollup_tree_rules() -> None:
    assert rollup_tree([], ["verified", "verified"]) == "verified"
    assert rollup_tree([], ["verified", "failed"]) == "failed"
    assert rollup_tree([], ["verified", "needs_reverification"]) == "needs_reverification"
    assert rollup_tree([], ["verified", "verified_older_build"]) == "verified_older_build"
    assert rollup_tree([], ["verified", "not_verified"]) == "partial"
    assert rollup_tree([], ["partial"]) == "partial"
    assert rollup_tree(["verified"], ["not_verified"]) == "partial"  # own criteria count too
    assert rollup_tree([], []) == "not_verified"


def test_seed_tree_rolls_failures_up(loaded: Database) -> None:
    s = {r.key: r for r in requirement_status(loaded)}
    assert [r.key for r in requirement_status(loaded)][:3] == ["SYS-01", "SUB-CLI-01", "REQ-CLICK-01"]
    assert s["SYS-01"].children == ["SUB-CLI-01", "SUB-HTTP-01"] and s["SYS-01"].depth == 0
    assert s["REQ-CLICK-01"].depth == 2 and s["REQ-CLICK-01"].parent_key == "SUB-CLI-01"
    # One failing component requirement fails its subsystem and the system.
    assert s["REQ-CLICK-01"].status == "failed"
    assert s["SUB-CLI-01"].status == "failed" and s["SYS-01"].status == "failed"
    assert s["SYS-01"].is_rollup and not s["REQ-CLICK-01"].is_rollup


def test_cycle_is_refused(loaded: Database) -> None:
    batch = start_batch(loaded, "backlog", "cycle")
    stage(
        loaded,
        batch,
        "backlog",
        [
            (
                "link",
                "SYS-01->parent:REQ-CLICK-01",
                {"kind": "requirement_parent", "requirement": "SYS-01", "parent": "REQ-CLICK-01"},
            )
        ],
    )
    stats = process_staging(loaded)
    assert stats["link.error"] == 1
    err = loaded.scalar("SELECT error FROM staging_records WHERE source_id = 'SYS-01->parent:REQ-CLICK-01'")
    assert "cycle" in err
    assert loaded.scalar("SELECT parent_id FROM requirements WHERE key = 'SYS-01'") is None
    assert all(r.ok for r in run_checks(loaded) if r.check.name == "requirement_tree_has_no_cycles")


def test_dq_check_catches_a_cycle_written_around_the_normalizer(loaded: Database) -> None:
    sys_id = loaded.scalar("SELECT id FROM requirements WHERE key = 'SYS-01'")
    leaf_id = loaded.scalar("SELECT id FROM requirements WHERE key = 'SUB-CLI-01'")
    loaded.execute("UPDATE requirements SET parent_id = ? WHERE id = ?", (leaf_id, sys_id))
    bad = next(r for r in run_checks(loaded) if r.check.name == "requirement_tree_has_no_cycles")
    assert not bad.ok


# -- audit log --------------------------------------------------------------------------


def test_seed_audit_chain_is_intact_and_covers_the_story(loaded: Database) -> None:
    check = verify_chain(loaded)
    assert check.ok and check.rows > 500
    actions = {r["action"] for r in loaded.query("SELECT DISTINCT action FROM audit_log")}
    assert {
        "requirement.created",
        "verification.recorded",
        "verification.derived",
        "verification.invalidated",
        "reverification.opened",
        "reverification.closed",
        "policy.classified",
    } <= actions
    # Who, what, when, which build: an invalidation names all four.
    row = loaded.query_one(
        "SELECT actor, effective_at, build_id, detail FROM audit_log WHERE action = 'verification.invalidated'"
        " AND entity_key = 'AC-REQ-202-1'"
    )
    assert row and row["actor"] == "change-control" and row["build_id"] is not None
    assert row["effective_at"] == "2026-06-20T16:00:00Z"
    assert json.loads(row["detail"])["reason"] == "REQ-REQ-02 changed to v3 on 2026-06-20"


def test_audit_log_refuses_update_and_delete(loaded: Database) -> None:
    with pytest.raises(Exception, match="append-only"):
        loaded.execute("UPDATE audit_log SET actor = 'someone-else' WHERE id = 1")
    with pytest.raises(Exception, match="append-only"):
        loaded.execute("DELETE FROM audit_log WHERE id = 1")
    assert verify_chain(loaded).ok


def test_tampering_is_detected_even_with_triggers_dropped(loaded: Database) -> None:
    """Someone with DDL rights drops the guard and edits a row: the chain shows where."""
    if loaded.dialect == "sqlite":
        loaded.executescript("DROP TRIGGER audit_log_no_update;")
    else:
        loaded.executescript("DROP TRIGGER audit_log_no_change ON audit_log;")
    loaded.execute("UPDATE audit_log SET actor = 'mallory' WHERE id = 10")
    check = verify_chain(loaded)
    assert not check.ok and check.first_bad_id == 10


def test_actions_are_audited_with_actor_and_build(loaded: Database) -> None:
    build = find_build(loaded, "pallets/click", "1049")
    record_verification(
        loaded,
        criterion_key="AC-CLICK-201-4",
        build_id=build.id,
        method="inspection",
        result="passed",
        verified_by="sofia-reyes",
        verified_at="2026-10-06T09:00:00Z",
    )
    set_classification(loaded, "pallets/click", "internal", set_by="security-team", note="vendored copy")
    events = loaded.query(
        "SELECT actor, action, entity_key, build_id FROM audit_log ORDER BY id DESC LIMIT 2"
    )
    assert events[0] == {
        "actor": "security-team",
        "action": "policy.classified",
        "entity_key": "pallets/click",
        "build_id": None,
    }
    assert events[1] == {
        "actor": "sofia-reyes",
        "action": "verification.recorded",
        "entity_key": "AC-CLICK-201-4",
        "build_id": build.id,
    }
    assert verify_chain(loaded).ok


def test_a_new_requirement_version_is_audited(loaded: Database) -> None:
    req = {
        "key": "REQ-HTTPX-02",
        "title": "URLs are encoded the way browsers encode them",
        "author": "maya-chen",
        "verification_method": "test",
        "revisions": [
            {"version": 1, "changed_at": "2024-10-20T16:00:00Z", "body": "Old.", "note": "initial"},
            {"version": 2, "changed_at": "2026-10-10T10:00:00Z", "body": "New.", "note": "clarified"},
        ],
    }
    stage(loaded, start_batch(loaded, "backlog", "change"), "backlog", [("requirement", "REQ-HTTPX-02", req)])
    process_staging(loaded)
    row = loaded.query_one("SELECT actor, action, detail FROM audit_log ORDER BY id DESC LIMIT 1")
    assert row and row["action"] == "requirement.changed" and row["actor"] == "maya-chen"
    assert json.loads(row["detail"])["previous_version"] == 1


def test_concurrent_writers_cannot_fork_the_chain(db: Database) -> None:
    """Two rows can't claim the same predecessor (UNIQUE prev_hash)."""
    with db.transaction():
        record(db, actor="a", action="x", entity="e", entity_key="k")
    first = db.query_one("SELECT prev_hash FROM audit_log")
    assert first
    with pytest.raises(Exception, match=r"(?i)unique"):
        db.execute(
            "INSERT INTO audit_log (recorded_at, actor, action, entity, entity_key, detail, prev_hash, hash)"
            " VALUES ('2026-01-01T00:00:00Z', 'b', 'x', 'e', 'k', '{}', ?, ?)",
            (first["prev_hash"], "f" * 64),
        )


# -- full record per verdict ------------------------------------------------------------


@dataclass
class ProviderLike:
    """Behaves like the real client: reports model version, request id and stop reason."""

    replies: list[str]
    model: str = "claude-test"
    external: bool = True
    calls: int = 0
    sent: list[str] = field(default_factory=list)

    def complete(self, system: str, user: str, *, max_tokens: int) -> LLMResult:
        self.calls += 1
        self.sent.append(user)
        return LLMResult(
            self.replies.pop(0),
            1200,
            150,
            model_version="claude-test-20261001",
            request_id=f"req_{self.calls}",
            stop_reason="end_turn",
        )


def _reply(verdict: str) -> str:
    return json.dumps({"verdict": verdict, "evidence": [], "confidence": 0.4, "reasoning": "r"})


def test_verdict_stores_prompt_response_and_model_version(loaded: Database) -> None:
    client = ProviderLike(["not json at all", _reply("unclear")])
    rec = check_criterion(loaded, "AC-REQ-101-1", LLMJudge(client), Budget(1.0, 3.0, 15.0))
    row = loaded.query_one("SELECT * FROM ai_verdicts WHERE id = ?", (rec.id,))
    assert row
    assert row["prompt_system"] and "AC-REQ-101-1" in row["prompt_user"]
    assert row["prompt_user"] == client.sent[0], "the stored prompt is exactly what was sent"
    assert json.loads(row["raw_responses"]) == ["not json at all", _reply("unclear")]  # repair kept
    assert json.loads(row["request_ids"]) == ["req_1", "req_2"]
    assert row["model_version"] == "claude-test-20261001" and row["stop_reason"] == "end_turn"
    audit = loaded.query_one("SELECT actor, detail FROM audit_log WHERE action = 'verdict.created'")
    assert audit and audit["actor"] == "checker:claude-test"
    assert json.loads(audit["detail"])["model_version"] == "claude-test-20261001"


def test_review_is_audited(loaded: Database) -> None:
    client = ProviderLike([_reply("fail")])
    rec = check_criterion(loaded, "AC-REQ-101-1", LLMJudge(client), Budget(1.0, 3.0, 15.0))
    assert rec.status == "pending_review"
    review_fail(loaded, rec.id, decision="rejected", reviewer="maya-chen", note="the test covers it")
    row = loaded.query_one("SELECT actor, detail FROM audit_log WHERE action = 'verdict.reviewed'")
    assert row and row["actor"] == "maya-chen"
    assert json.loads(row["detail"])["decision"] == "rejected"
