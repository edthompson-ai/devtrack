"""The eval gate fails when fail detection drops below the keyword baseline."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from devtrack.checker.guardrails import Budget
from devtrack.checker.judges import LLMJudge, LLMResult
from devtrack.db import Database
from devtrack.evals.evidence import write_evidence
from devtrack.evals.gate import BASELINE, check_recorded, run_gate
from devtrack.evals.run import load_labels, run_eval
from devtrack.ingest.seed import DEFAULT_SEED_DIR, load_seed


@pytest.fixture
def labelled(db: Database) -> Database:
    load_seed(db)
    load_labels(db, DEFAULT_SEED_DIR / "eval_labels.json")
    return db


def write(tmp: Path, name: str, data: object) -> Path:
    p = tmp / name
    p.write_text(json.dumps(data))
    return p


def test_committed_baseline_is_what_the_heuristic_scores(labelled: Database, tmp_path: Path) -> None:
    gates = run_gate(labelled, results_path=tmp_path / "none.json")
    assert [g.ok for g in gates] == [True], gates[0].detail
    baseline = json.loads(BASELINE.read_text())
    assert f"fail recall {baseline['fail_recall']}" in gates[0].detail


def test_gate_fails_when_detection_drops(labelled: Database, tmp_path: Path) -> None:
    """Simulate a regression by raising the floor above what the pipeline delivers."""
    floor = write(tmp_path, "baseline.json", {"fail_recall": 0.9, "fail_precision": 0.9})
    gates = run_gate(labelled, baseline_path=floor, results_path=tmp_path / "none.json")
    assert not gates[0].ok


def test_recorded_model_runs_must_beat_the_baseline() -> None:
    baseline = {"fail_recall": 0.562, "fail_precision": 0.643}
    runs = [
        {
            "prompt_version": "heuristic-v1",
            "model": "heuristic",
            "fail_recall": 0.562,
            "fail_precision": 0.643,
        },
        {"prompt_version": "v1", "model": "m", "fail_recall": 0.75, "fail_precision": 0.70},
        {"prompt_version": "v2", "model": "m", "fail_recall": 0.50, "fail_precision": 0.90},
        {"prompt_version": "v3", "model": "m", "fail_recall": None, "fail_precision": None},
    ]
    results = check_recorded(runs, baseline)
    assert [r.ok for r in results] == [True, False, False]  # heuristic row skipped


def test_live_run_and_evidence_files(labelled: Database, tmp_path: Path) -> None:
    class AlwaysFail:
        model = "always-fail"
        external = False

        def complete(self, system: str, user: str, *, max_tokens: int) -> LLMResult:
            return LLMResult(
                json.dumps({"verdict": "fail", "evidence": [], "confidence": 0.9, "reasoning": "x"}),
                10,
                5,
                model_version="always-fail-1",
            )

    # Calling everything "fail" catches every real fail but floods reviewers: the
    # precision floor is what stops a degenerate judge from passing the gate.
    gates = run_gate(
        labelled,
        live_judge=LLMJudge(AlwaysFail()),
        budget=Budget(1.0, 3.0, 15.0),
        results_path=tmp_path / "none.json",
    )
    live = gates[-1]
    assert "fail recall 1.0" in live.detail and not live.ok

    run_eval(labelled, LLMJudge(AlwaysFail()), Budget(1.0, 3.0, 15.0))
    results, verdicts = write_evidence(labelled, tmp_path)
    runs = json.loads(results.read_text())["runs"]
    assert runs[-1]["models_reported"][0]["model_version"] in ("always-fail-1", "rule:no-linked-code")
    with gzip.open(verdicts, "rt") as f:
        lines = [json.loads(line) for line in f]
    called = [r for r in lines if r["raw_responses"]]
    assert called and all(r["prompt_user"] and r["model_version"] == "always-fail-1" for r in called)
