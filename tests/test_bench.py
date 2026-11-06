"""Phase 3: the benchmark runs end to end (tiny scale, SQLite) and writes a report."""

from __future__ import annotations

from pathlib import Path

from devtrack.bench import benchmark, write_report
from devtrack.db import connect


def test_benchmark_before_and_after(tmp_path: Path) -> None:
    db = connect(f"sqlite:///{tmp_path / 'bench.db'}")
    result = benchmark(db, factor=0.01, runs=1)
    phases = {(r["query"], r["phase"]) for r in result["results"]}
    assert len(phases) == 14  # 6 saved queries + FTS, before and after
    after_q6 = next(r for r in result["results"] if r["query"] == "full_trace" and r["phase"] == "after")
    assert "requirements" in after_q6["plan_text"] or "USING" in after_q6["plan_text"]
    report = write_report(result, tmp_path)
    text = report.read_text()
    assert "| 6 | Full trace" in text and "Speed-up" in text
    db.close()
