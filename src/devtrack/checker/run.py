"""Run the AI checker for acceptance criteria and record every verdict.

Pipeline for one criterion:
  gather context (redacted, size-bounded)
  -> no linked code? record "fail" without calling a model
  -> cache hit on (input hash, model, prompt version)? reuse the stored verdict
  -> budget check -> judge -> post-checks (evidence must be verbatim, injection
     downgrade, unsupported pass) -> store in ai_verdicts.
Every "fail" is stored as pending_review and only counts once a person approves it.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from devtrack import audit
from devtrack.checker.context import CriterionContext, context_tokens, gather_context
from devtrack.checker.guardrails import Budget, BudgetExceeded, verify_quotes
from devtrack.checker.judges import EvidenceItem, Judge, Judgement, JudgeOutput
from devtrack.checker.prompt import RenderedPrompt, render, versions
from devtrack.db import Database
from devtrack.policy import PolicyBlocked, enforce

log = logging.getLogger(__name__)

NO_CODE_MODEL = "rule:no-linked-code"


@dataclass
class VerdictRecord:
    id: int
    criterion: str
    verdict: str
    confidence: float
    reasoning: str
    evidence: list[dict[str, str]]
    flags: list[str]
    model: str
    prompt_version: str
    status: str
    cost_usd: float
    cached: bool = False


@dataclass
class RunSummary:
    checked: int = 0
    cached: int = 0
    skipped_budget: int = 0
    blocked_policy: int = 0
    blocked: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    verdicts: dict[str, int] = field(default_factory=dict)
    records: list[VerdictRecord] = field(default_factory=list)


def input_hash(prompt_text: str, model: str, version: str) -> str:
    return hashlib.sha256(f"{version}\x00{model}\x00{prompt_text}".encode()).hexdigest()


def apply_guardrails(ctx: CriterionContext, judgement: Judgement) -> JudgeOutput:
    """Deterministic post-checks. They don't trust the model to police itself."""
    out = judgement.output
    flags = judgement.flags
    verified, rejected = verify_quotes([e.quote for e in out.evidence], ctx.repo_text())
    evidence = [e for e in out.evidence if e.quote in verified]
    verdict, confidence, reasoning = out.verdict, out.confidence, out.reasoning
    if rejected:
        flags.append("evidence_not_found")
    if verdict == "pass" and not evidence:
        verdict, confidence = "unclear", min(confidence, 0.4)
        flags.append("unsupported_pass")
        reasoning += " [Guardrail: a pass needs at least one quote found verbatim in the diffs.]"
    if ctx.injection_hits:
        flags.append("injection_suspected")
        if verdict == "pass":
            verdict, confidence = "unclear", min(confidence, 0.5)
            reasoning += " [Guardrail: the repository text contains instructions aimed at an AI reviewer; a person must check this.]"
    if ctx.truncated:
        flags.append("context_truncated")
    if ctx.redactions:
        flags.append("secrets_redacted")
    return JudgeOutput(verdict=verdict, confidence=confidence, reasoning=reasoning, evidence=evidence)


def _cached(db: Database, key: str, model: str, version: str) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM ai_verdicts WHERE input_hash = ? AND model = ? AND prompt_version = ?"
        " ORDER BY id DESC LIMIT 1",
        (key, model, version),
    )


def _record(row: dict[str, Any], criterion: str, cached: bool) -> VerdictRecord:
    def loads(v: Any) -> Any:
        return json.loads(v) if isinstance(v, str) else v

    return VerdictRecord(
        id=int(row["id"]),
        criterion=criterion,
        verdict=row["verdict"],
        confidence=float(row["confidence"]),
        reasoning=row["reasoning"],
        evidence=loads(row["evidence"]),
        flags=loads(row["flags"]),
        model=row["model"],
        prompt_version=row["prompt_version"],
        status=row["status"],
        cost_usd=float(row["cost_usd"]),
        cached=cached,
    )


def _store(
    db: Database, crit_id: int, criterion_key: str, out: JudgeOutput, j: Judgement, key: str, cost: float
) -> int:
    """Store the verdict with its full exchange, and audit it, in one transaction."""
    status = "pending_review" if out.verdict == "fail" else "final"
    with db.transaction():
        vid = _insert_verdict(db, crit_id, out, j, key, cost, status)
        audit.record(
            db,
            actor=f"checker:{j.model}",
            action="verdict.created",
            entity="criterion",
            entity_key=criterion_key,
            detail={
                "verdict_id": vid,
                "verdict": out.verdict,
                "status": status,
                "prompt_version": j.prompt_version,
                "model_version": j.model_version,
                "flags": sorted(set(j.flags)),
            },
        )
    return vid


def _insert_verdict(
    db: Database, crit_id: int, out: JudgeOutput, j: Judgement, key: str, cost: float, status: str
) -> int:
    return int(
        db.scalar(
            "INSERT INTO ai_verdicts (criterion_id, verdict, confidence, reasoning, evidence, flags, model,"
            " prompt_version, input_hash, input_tokens, output_tokens, cost_usd, status,"
            " prompt_system, prompt_user, raw_responses, model_version, request_ids, stop_reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                crit_id,
                out.verdict,
                out.confidence,
                out.reasoning,
                db.json([e.model_dump() for e in out.evidence]),
                db.json(sorted(set(j.flags))),
                j.model,
                j.prompt_version,
                key,
                j.input_tokens,
                j.output_tokens,
                round(cost, 6),
                status,
                j.prompt_system,
                j.prompt_user,
                json.dumps(j.raw_responses) if j.raw_responses else None,
                j.model_version or j.model,
                json.dumps(j.request_ids) if j.request_ids else None,
                j.stop_reason,
            ),
        )
    )


def check_criterion(
    db: Database,
    criterion_key: str,
    judge: Judge,
    budget: Budget,
    *,
    force: bool = False,
    max_diff_tokens: int = 9_000,
) -> VerdictRecord:
    # Policy first: a blocked criterion must not even have its context assembled.
    repo = db.scalar(
        "SELECT f.repo FROM acceptance_criteria a JOIN stories s ON s.id = a.story_id"
        " JOIN features f ON f.id = s.feature_id WHERE a.key = ?",
        (criterion_key,),
    )
    if repo is None:
        raise KeyError(f"unknown criterion {criterion_key}")
    enforce(db, str(repo), model=judge.model, external=judge.external)

    ctx = gather_context(db, criterion_key, max_diff_tokens=max_diff_tokens)
    crit_id = int(db.scalar("SELECT id FROM acceptance_criteria WHERE key = ?", (criterion_key,)))

    uses_prompt = judge.prompt_version in versions()
    prompt: RenderedPrompt | None = render(ctx, judge.prompt_version) if uses_prompt else None
    cache_text = (
        prompt.cache_text()
        if prompt
        else json.dumps({"text": ctx.text, "repo": ctx.repo_text(), "tests": ctx.tests}, sort_keys=True)
    )

    if not ctx.commits:
        # Nothing to judge: no model call, no cost. Still a "fail" that a person confirms.
        key = input_hash(cache_text, NO_CODE_MODEL, judge.prompt_version)
        hit = None if force else _cached(db, key, NO_CODE_MODEL, judge.prompt_version)
        if hit:
            return _record(hit, criterion_key, cached=True)
        j = Judgement(
            JudgeOutput(
                verdict="fail",
                confidence=1.0,
                reasoning="No commits are linked to this criterion through its tasks, so nothing implements it.",
            ),
            NO_CODE_MODEL,
            judge.prompt_version,
            flags=["no_linked_code"],
        )
        vid = _store(db, crit_id, criterion_key, j.output, j, key, 0.0)
        db.commit()
        return _record(
            db.query_one("SELECT * FROM ai_verdicts WHERE id = ?", (vid,)) or {}, criterion_key, False
        )

    key = input_hash(cache_text, judge.model, judge.prompt_version)
    if not force:
        hit = _cached(db, key, judge.model, judge.prompt_version)
        if hit:
            return _record(hit, criterion_key, cached=True)

    est_in = context_tokens(ctx) + (len(prompt.system) // 4 if prompt else 0)
    max_out = getattr(judge, "max_output_tokens", 0)
    budget.reserve(est_in, max_out)
    judgement = judge.judge(ctx, prompt)
    cost = budget.record(judgement.input_tokens, judgement.output_tokens, judgement.reported_cost_usd)
    final = apply_guardrails(ctx, judgement)
    vid = _store(db, crit_id, criterion_key, final, judgement, key, cost)
    db.commit()
    row = db.query_one("SELECT * FROM ai_verdicts WHERE id = ?", (vid,))
    assert row is not None
    return _record(row, criterion_key, cached=False)


def check_many(
    db: Database, keys: list[str], judge: Judge, budget: Budget, *, force: bool = False
) -> RunSummary:
    summary = RunSummary()
    for key in keys:
        try:
            rec = check_criterion(db, key, judge, budget, force=force)
        except BudgetExceeded as exc:
            log.warning("budget reached, stopping: %s", exc)
            summary.skipped_budget = len(keys) - summary.checked - summary.blocked_policy
            break
        except PolicyBlocked as exc:
            log.warning("skipped %s: %s", key, exc)
            summary.blocked_policy += 1
            summary.blocked.append(key)
            continue
        summary.checked += 1
        summary.cached += rec.cached
        summary.cost_usd += 0.0 if rec.cached else rec.cost_usd
        summary.verdicts[rec.verdict] = summary.verdicts.get(rec.verdict, 0) + 1
        summary.records.append(rec)
    return summary


class ReviewError(ValueError):
    pass


def review_fail(db: Database, verdict_id: int, *, decision: str, reviewer: str, note: str = "") -> None:
    """A person confirms (approved) or overturns (rejected) a pending "fail"."""
    if decision not in ("approved", "rejected"):
        raise ReviewError("decision must be 'approved' or 'rejected'")
    if not reviewer.strip():
        raise ReviewError("a reviewer name is required")
    row = db.query_one("SELECT verdict, status FROM ai_verdicts WHERE id = ?", (verdict_id,))
    if row is None:
        raise ReviewError(f"no verdict {verdict_id}")
    if row["verdict"] != "fail" or row["status"] != "pending_review":
        raise ReviewError(f"verdict {verdict_id} is {row['verdict']}/{row['status']}, not a pending fail")
    with db.transaction():
        db.execute(
            "INSERT INTO verdict_reviews (verdict_id, decision, reviewer, note) VALUES (?, ?, ?, ?)",
            (verdict_id, decision, reviewer.strip(), note or None),
        )
        db.execute("UPDATE ai_verdicts SET status = ? WHERE id = ?", (decision, verdict_id))
        key = db.scalar(
            "SELECT a.key FROM ai_verdicts v JOIN acceptance_criteria a ON a.id = v.criterion_id WHERE v.id = ?",
            (verdict_id,),
        )
        audit.record(
            db,
            actor=reviewer.strip(),
            action="verdict.reviewed",
            entity="criterion",
            entity_key=str(key),
            detail={"verdict_id": verdict_id, "decision": decision, "note": note or None},
        )


def to_evidence(items: list[dict[str, str]]) -> list[EvidenceItem]:
    return [EvidenceItem(**i) for i in items]
