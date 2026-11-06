"""The eval gate: CI fails if fail detection drops below the keyword baseline.

"Fail detection" is the checker's job that matters most: catching criteria whose code
doesn't meet them. It is measured by fail recall (share of real fails caught) and fail
precision (share of flagged fails that are real). The floor is the offline keyword
heuristic, recorded in ``docs/eval-baseline.json``.

Three checks, cheapest first:

1. **Baseline still holds** (offline, runs on SQLite and Postgres in CI). The heuristic
   is re-run on a freshly seeded database. It is deterministic, so any drop means the
   pipeline under it changed: ingestion, linking, context building, guardrails or the
   labels. That is exactly the kind of silent regression a gate should catch.
2. **Recorded model runs beat the baseline.** Every model run saved in
   ``docs/evidence/eval-results.json`` must match or beat the baseline on fail recall
   and fail precision. A prompt or model change that makes things worse can't be
   committed with a green build.
3. **Live model run** (optional, only with ``--live`` and an API key in CI secrets):
   runs the current prompt against the labels and applies the same rule.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devtrack.checker.guardrails import Budget
from devtrack.checker.judges import HeuristicJudge, Judge
from devtrack.db import Database
from devtrack.evals.run import run_eval

DOCS = Path(__file__).resolve().parents[3] / "docs"
BASELINE = DOCS / "eval-baseline.json"
RESULTS = DOCS / "evidence" / "eval-results.json"


@dataclass
class GateResult:
    name: str
    ok: bool
    detail: str


def _floor(baseline: dict[str, Any]) -> tuple[float, float]:
    return float(baseline["fail_recall"]), float(baseline["fail_precision"])


def _meets(metrics: dict[str, Any], floor: tuple[float, float]) -> bool:
    recall, precision = metrics.get("fail_recall"), metrics.get("fail_precision")
    return recall is not None and precision is not None and recall >= floor[0] and precision >= floor[1]


def _fmt(m: dict[str, Any]) -> str:
    return f"fail recall {m.get('fail_recall')}, fail precision {m.get('fail_precision')}"


def check_baseline(db: Database, baseline: dict[str, Any]) -> GateResult:
    result = run_eval(db, HeuristicJudge(), Budget(0.0, 0.0, 0.0), notes="eval gate")
    got = {"fail_recall": result.metrics.fail_recall, "fail_precision": result.metrics.fail_precision}
    ok = _meets(got, _floor(baseline))
    return GateResult("baseline re-run", ok, f"heuristic now {_fmt(got)}; floor {_fmt(baseline)}")


def check_recorded(results: list[dict[str, Any]], baseline: dict[str, Any]) -> list[GateResult]:
    floor = _floor(baseline)
    out = []
    for r in results:
        if r.get("model") == "heuristic":
            continue
        ok = _meets(r, floor)
        out.append(GateResult(f"recorded run {r['prompt_version']} / {r['model']}", ok, _fmt(r)))
    return out


def check_live(db: Database, judge: Judge, budget: Budget, baseline: dict[str, Any]) -> GateResult:
    result = run_eval(db, judge, budget, notes="eval gate (live)")
    got = {"fail_recall": result.metrics.fail_recall, "fail_precision": result.metrics.fail_precision}
    return GateResult(
        f"live run {judge.prompt_version} / {judge.model}", _meets(got, _floor(baseline)), _fmt(got)
    )


def run_gate(
    db: Database,
    *,
    live_judge: Judge | None = None,
    budget: Budget | None = None,
    baseline_path: Path = BASELINE,
    results_path: Path = RESULTS,
) -> list[GateResult]:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    gates = [check_baseline(db, baseline)]
    if results_path.exists():
        gates += check_recorded(json.loads(results_path.read_text(encoding="utf-8"))["runs"], baseline)
    if live_judge is not None:
        gates.append(check_live(db, live_judge, budget or Budget(2.0, 3.0, 15.0), baseline))
    return gates
