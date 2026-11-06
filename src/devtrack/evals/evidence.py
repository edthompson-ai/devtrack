"""Save eval runs as evidence files that live in the repository.

The database is where runs are stored; these files are what a reviewer can read
without running anything, and what the eval gate checks in CI:

* ``docs/evidence/eval-results.json``: one summary per run (metrics, cost, backend,
  and the model names the provider reported in its responses).
* ``docs/evidence/eval-verdicts.jsonl.gz``: one line per verdict in those runs, with
  the full prompt as sent, the raw response(s) as received, the model that answered,
  session or request ids, token counts and cost.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

from devtrack.db import Database

EVIDENCE = Path(__file__).resolve().parents[3] / "docs" / "evidence"


def _loads(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


def run_summaries(db: Database) -> list[dict[str, Any]]:
    runs = db.query(
        "SELECT id, prompt_version, model, started_at, n, accuracy, fail_precision, fail_recall, unclear_rate,"
        " cost_usd, notes FROM eval_runs ORDER BY id"
    )
    out = []
    for r in runs:
        seen = db.query(
            "SELECT v.model_version, COUNT(*) AS n, SUM(v.input_tokens) AS input_tokens,"
            " SUM(v.output_tokens) AS output_tokens FROM eval_results e JOIN ai_verdicts v ON v.id = e.verdict_id"
            " WHERE e.run_id = ? GROUP BY v.model_version ORDER BY v.model_version",
            (r["id"],),
        )
        calls = db.scalar(
            "SELECT COUNT(*) FROM eval_results e JOIN ai_verdicts v ON v.id = e.verdict_id"
            " WHERE e.run_id = ? AND v.raw_responses IS NOT NULL",
            (r["id"],),
        )
        out.append(
            {
                "run_id": r["id"],
                "prompt_version": r["prompt_version"],
                "model": r["model"],
                "notes": r["notes"],
                "started_at": r["started_at"],
                "n": r["n"],
                "accuracy": r["accuracy"],
                "fail_precision": r["fail_precision"],
                "fail_recall": r["fail_recall"],
                "unclear_rate": r["unclear_rate"],
                "cost_usd": round(float(r["cost_usd"] or 0), 4),
                "model_calls": int(calls or 0),
                "models_reported": seen,
            }
        )
    return out


def verdict_records(db: Database) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT e.run_id, a.key AS criterion, e.expected, e.predicted, v.id AS verdict_id, v.verdict, v.status,"
        " v.confidence, v.reasoning, v.evidence, v.flags, v.model, v.model_version, v.prompt_version,"
        " v.prompt_system, v.prompt_user, v.raw_responses, v.request_ids, v.stop_reason, v.input_tokens,"
        " v.output_tokens, v.cost_usd, v.created_at"
        " FROM eval_results e JOIN ai_verdicts v ON v.id = e.verdict_id"
        " JOIN acceptance_criteria a ON a.id = e.criterion_id ORDER BY e.run_id, a.key"
    )
    for r in rows:
        for k in ("evidence", "flags", "raw_responses", "request_ids"):
            r[k] = _loads(r[k]) if r[k] is not None else None
    return rows


def write_evidence(db: Database, out_dir: Path = EVIDENCE) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    results = out_dir / "eval-results.json"
    results.write_text(
        json.dumps(
            {
                "_about": "Eval runs against seed/eval_labels.json. Written by devtrack.evals.evidence from the"
                " database the runs were stored in. models_reported is what each response said answered.",
                "runs": run_summaries(db),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    verdicts = out_dir / "eval-verdicts.jsonl.gz"
    with gzip.open(verdicts, "wt", encoding="utf-8") as f:
        for r in verdict_records(db):
            f.write(json.dumps(r, sort_keys=True) + "\n")
    return results, verdicts
