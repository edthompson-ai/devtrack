#!/usr/bin/env python3
"""Rerun the model eval on the Anthropic API and compare it to the saved results.

For an independent reviewer. The only thing to set is ANTHROPIC_API_KEY:

    ANTHROPIC_API_KEY=sk-ant-... python scripts/reproduce_eval.py

It builds a fresh SQLite database from the seed in ./reproduce/ (nothing else is
touched), runs the keyword baseline and the model judge with prompts v1 and v2 against
the 48 labels, writes the results next to the database, and prints a side-by-side
comparison with docs/evidence/eval-results.json.

The published runs went through Claude Code's CLI; this uses the API directly, so small
differences are expected (see the README, "Reproduce the eval"). Optional overrides:
CHECKER_MODEL (default claude-sonnet-5-5), CHECKER_RUN_BUDGET_USD (default 3.00 per run
here), CHECKER_PRICE_INPUT_PER_MTOK / CHECKER_PRICE_OUTPUT_PER_MTOK (defaults 3 / 15).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from devtrack.checker.guardrails import Budget, BudgetExceeded  # noqa: E402
from devtrack.checker.judges import AnthropicClient, HeuristicJudge, Judge, LLMJudge  # noqa: E402
from devtrack.db import Database, connect, migrate  # noqa: E402
from devtrack.evals.evidence import write_evidence  # noqa: E402
from devtrack.evals.run import load_labels, result_from_db, run_eval, write_report  # noqa: E402
from devtrack.ingest.seed import DEFAULT_SEED_DIR, load_seed  # noqa: E402

SAVED = ROOT / "docs" / "evidence" / "eval-results.json"
METRICS = ("accuracy", "fail_precision", "fail_recall", "unclear_rate", "cost_usd")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if not raw else float(raw)


def fresh_db(out_dir: Path) -> Database:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "eval.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    db = connect(f"sqlite:///{path}")
    migrate(db)
    load_seed(db)
    load_labels(db, DEFAULT_SEED_DIR / "eval_labels.json")
    return db


def compare(saved: list[dict[str, Any]], new: list[dict[str, Any]]) -> str:
    def pick(runs: list[dict[str, Any]], version: str) -> dict[str, Any] | None:
        hits = [r for r in runs if r["prompt_version"] == version]
        return hits[-1] if hits else None

    def fmt(v: Any, key: str) -> str:
        if v is None:
            return "-"
        return f"${float(v):.2f}" if key == "cost_usd" else f"{float(v):.0%}"

    lines = [
        "| Prompt | Metric | Saved (Claude Code CLI) | This run (API) |",
        "|---|---|---:|---:|",
    ]
    for version in ("heuristic-v1", "v1", "v2"):
        a, b = pick(saved, version), pick(new, version)
        for key in METRICS:
            lines.append(
                f"| {version} | {key.replace('_', ' ')} | {fmt(a and a.get(key), key)} | {fmt(b and b.get(key), key)} |"
            )
    return "\n".join(lines)


def run(judges: list[Judge], out_dir: Path, budget_usd: float, prices: tuple[float, float]) -> Path:
    db = fresh_db(out_dir)
    for judge in judges:
        started = time.monotonic()
        result = run_eval(db, judge, Budget(budget_usd, *prices), notes="reproduce_eval.py (API)")
        m = result.metrics
        print(
            f"{result.prompt_version:>12} / {result.model}: accuracy {m.accuracy:.0%}, fail precision"
            f" {m.fail_precision}, fail recall {m.fail_recall}, unclear {m.unclear_rate:.0%},"
            f" cost ${result.cost_usd:.2f}, {time.monotonic() - started:.0f}s",
            flush=True,
        )
    results_path, _ = write_evidence(db, out_dir)
    last = int(db.scalar("SELECT MAX(id) FROM eval_runs"))
    write_report(db, result_from_db(db, last), out_dir / "eval-report.md")
    new_runs = json.loads(results_path.read_text())["runs"]
    saved_runs = json.loads(SAVED.read_text())["runs"]
    table = compare(saved_runs, new_runs)
    (out_dir / "comparison.md").write_text(
        "# Reproduced eval vs saved results\n\n" + table + "\n", encoding="utf-8"
    )
    print("\n" + table)
    print(
        f"\nwritten to {out_dir}/: eval-results.json, eval-verdicts.jsonl.gz, eval-report.md, comparison.md"
    )
    return out_dir


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path("reproduce"))
    args = ap.parse_args(argv)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("Set ANTHROPIC_API_KEY first: ANTHROPIC_API_KEY=sk-ant-... python scripts/reproduce_eval.py")
        return 2
    model = os.environ.get("CHECKER_MODEL") or "claude-sonnet-5-5"
    prices = (
        _env_float("CHECKER_PRICE_INPUT_PER_MTOK", 3.0),
        _env_float("CHECKER_PRICE_OUTPUT_PER_MTOK", 15.0),
    )
    judges: list[Judge] = [
        HeuristicJudge(),
        LLMJudge(AnthropicClient(api_key=key, model=model), prompt_version="v1"),
        LLMJudge(AnthropicClient(api_key=key, model=model), prompt_version="v2"),
    ]
    try:
        run(judges, args.out, _env_float("CHECKER_RUN_BUDGET_USD", 3.0), prices)
    except BudgetExceeded as exc:
        print(f"Stopped at the cost cap: {exc}. Raise CHECKER_RUN_BUDGET_USD to continue.")
        return 1
    except RuntimeError as exc:  # API errors: bad key, no credit, model not available
        print(f"The API call failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
