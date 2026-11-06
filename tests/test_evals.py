"""Phase 5: metric math, label loading, stored runs per prompt version, and the report."""

from __future__ import annotations

from pathlib import Path

import pytest

from devtrack.checker.guardrails import Budget
from devtrack.checker.judges import HeuristicJudge
from devtrack.db import Database
from devtrack.evals.run import Metrics, history, load_labels, run_eval, write_report
from devtrack.ingest.seed import DEFAULT_SEED_DIR, load_seed


def test_metrics_math() -> None:
    pairs = [
        ("fail", "fail"),
        ("fail", "pass"),
        ("pass", "fail"),
        ("pass", "pass"),
        ("unclear", "unclear"),
        ("pass", "unclear"),
    ]
    m = Metrics.compute(pairs)
    assert m.n == 6
    assert m.accuracy == pytest.approx(3 / 6, abs=1e-3)
    assert m.fail_precision == pytest.approx(1 / 2, abs=1e-3)  # 2 predicted fail, 1 right
    assert m.fail_recall == pytest.approx(1 / 2, abs=1e-3)  # 2 real fails, 1 caught
    assert m.unclear_rate == pytest.approx(2 / 6, abs=1e-3)
    assert Metrics.compute([("pass", "pass")]).fail_precision is None  # no fail predicted


def test_eval_run_is_stored_and_reported(db: Database, tmp_path: Path) -> None:
    load_seed(db)
    assert load_labels(db, DEFAULT_SEED_DIR / "eval_labels.json") == 48
    result = run_eval(db, HeuristicJudge(), Budget(1.0, 3.0, 15.0), notes="baseline")
    assert result.metrics.n == 48
    assert sum(sum(row.values()) for row in result.metrics.confusion.values()) == 48
    assert db.scalar("SELECT COUNT(*) FROM eval_results WHERE run_id = ?", (result.run_id,)) == 48
    second = run_eval(db, HeuristicJudge(), Budget(1.0, 3.0, 15.0))
    assert second.metrics == result.metrics, "same judge, same inputs: identical (and fully cached) results"
    assert [h["prompt_version"] for h in history(db)] == ["heuristic-v1", "heuristic-v1"]
    report = write_report(db, result, tmp_path / "eval.md").read_text()
    assert "Confusion matrix" in report and "What it gets wrong" in report
