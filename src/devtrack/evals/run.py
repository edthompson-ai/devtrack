"""Evaluate a judge against labelled ground truth (seed/eval_labels.json).

Metrics (the positive class for precision and recall is "fail", because a missed
fail is the costly error: unbuilt work marked done):
  * accuracy        share of criteria where predicted == label
  * fail precision  of the criteria the checker called "fail", how many really fail
  * fail recall     of the criteria that really fail, how many the checker caught
  * unclear rate    share of predictions that were "unclear" (sent to a person)
Every run is stored per (prompt version, model) so changes are measured, not guessed.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from devtrack.checker.guardrails import Budget
from devtrack.checker.judges import Judge
from devtrack.checker.run import check_criterion
from devtrack.db import Database, utcnow
from devtrack.policy import PolicyBlocked

LABELS = ("pass", "fail", "unclear")

log = logging.getLogger(__name__)


def load_labels(db: Database, path: Path) -> int:
    data = json.loads(path.read_text())
    n = 0
    with db.transaction():
        for item in data["labels"]:
            crit_id = db.scalar("SELECT id FROM acceptance_criteria WHERE key = ?", (item["criterion"],))
            if crit_id is None:
                raise KeyError(f"label for unknown criterion {item['criterion']}")
            db.execute(
                "INSERT INTO eval_labels (criterion_id, label, rationale, labeled_by, labeled_at)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT (criterion_id) DO UPDATE SET label = excluded.label,"
                " rationale = excluded.rationale, labeled_by = excluded.labeled_by, labeled_at = excluded.labeled_at",
                (crit_id, item["label"], item["rationale"], data["labeled_by"], data["labeled_at"]),
            )
            n += 1
    return n


@dataclass
class Metrics:
    n: int
    accuracy: float
    fail_precision: float | None
    fail_recall: float | None
    unclear_rate: float
    confusion: dict[str, dict[str, int]]

    @staticmethod
    def compute(pairs: list[tuple[str, str]]) -> Metrics:
        """pairs = [(expected, predicted), ...]"""
        n = len(pairs)
        confusion = {e: {p: 0 for p in LABELS} for e in LABELS}
        for exp, pred in pairs:
            confusion[exp][pred] += 1
        correct = sum(confusion[label][label] for label in LABELS)
        predicted_fail = sum(confusion[e]["fail"] for e in LABELS)
        actual_fail = sum(confusion["fail"].values())
        tp = confusion["fail"]["fail"]
        return Metrics(
            n=n,
            accuracy=round(correct / n, 3) if n else 0.0,
            fail_precision=round(tp / predicted_fail, 3) if predicted_fail else None,
            fail_recall=round(tp / actual_fail, 3) if actual_fail else None,
            unclear_rate=round(sum(confusion[e]["unclear"] for e in LABELS) / n, 3) if n else 0.0,
            confusion=confusion,
        )


@dataclass
class EvalResult:
    run_id: int
    prompt_version: str
    model: str
    metrics: Metrics
    cost_usd: float
    rows: list[dict[str, Any]] = field(default_factory=list)


def tag_error(text: str, expected: str, predicted: str, flags: list[str]) -> str:
    """A coarse reason for a miss, so the error analysis groups similar failures."""
    t = text.lower()
    if "injection_suspected" in flags:
        return "prompt injection in repo content"
    if re.search(r"\b(ms|seconds?|under \d|within \d|windows|performance)\b", t):
        return "runtime or platform claim"
    if re.search(r"\b(doc|docs|documentation|guide|changelog|history|advisory)\b", t):
        return "docs or changelog claim"
    if re.search(r"\btests?\b", t):
        return "test claim"
    if expected == "fail" and predicted == "pass":
        return "partly met: code touches the area but misses a detail"
    if expected == "pass" and predicted != "pass":
        return "met, but evidence uses different words than the criterion"
    return "other"


def run_eval(db: Database, judge: Judge, budget: Budget, *, notes: str = "") -> EvalResult:
    labelled = db.query(
        "SELECT a.key, a.text, l.label FROM eval_labels l JOIN acceptance_criteria a ON a.id = l.criterion_id"
        " ORDER BY a.key"
    )
    if not labelled:
        raise RuntimeError("no eval labels loaded; run `devtrack eval --load-labels` first")
    spent_before = budget.spent_usd
    rows: list[dict[str, Any]] = []
    for item in labelled:
        try:
            rec = check_criterion(db, item["key"], judge, budget)
        except PolicyBlocked as exc:
            log.warning("eval skipped %s: %s", item["key"], exc)
            continue
        rows.append(
            {
                "criterion": item["key"],
                "text": item["text"],
                "expected": item["label"],
                "predicted": rec.verdict,
                "confidence": rec.confidence,
                "reasoning": rec.reasoning,
                "flags": rec.flags,
                "verdict_id": rec.id,
                "model": rec.model,
            }
        )
    metrics = Metrics.compute([(r["expected"], r["predicted"]) for r in rows])
    cost = budget.spent_usd - spent_before
    with db.transaction():
        run_id = int(
            db.scalar(
                "INSERT INTO eval_runs (prompt_version, model, started_at, n, accuracy, fail_precision, fail_recall,"
                " unclear_rate, cost_usd, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (
                    judge.prompt_version,
                    judge.model,
                    utcnow(),
                    metrics.n,
                    metrics.accuracy,
                    metrics.fail_precision,
                    metrics.fail_recall,
                    metrics.unclear_rate,
                    round(cost, 6),
                    notes or None,
                ),
            )
        )
        for r in rows:
            crit_id = db.scalar("SELECT id FROM acceptance_criteria WHERE key = ?", (r["criterion"],))
            db.execute(
                "INSERT INTO eval_results (run_id, criterion_id, expected, predicted, verdict_id) VALUES (?, ?, ?, ?, ?)",
                (run_id, crit_id, r["expected"], r["predicted"], r["verdict_id"]),
            )
    return EvalResult(run_id, judge.prompt_version, judge.model, metrics, cost, rows)


def result_from_db(db: Database, run_id: int) -> EvalResult:
    """Rebuild a stored run (for reports written after the fact)."""
    run = db.query_one("SELECT * FROM eval_runs WHERE id = ?", (run_id,))
    if run is None:
        raise KeyError(f"no eval run {run_id}")
    rows = db.query(
        "SELECT a.key AS criterion, a.text, e.expected, e.predicted, v.confidence, v.reasoning, v.flags,"
        " v.id AS verdict_id, v.model FROM eval_results e JOIN acceptance_criteria a ON a.id = e.criterion_id"
        " JOIN ai_verdicts v ON v.id = e.verdict_id WHERE e.run_id = ? ORDER BY a.key",
        (run_id,),
    )
    for r in rows:
        r["flags"] = json.loads(r["flags"]) if isinstance(r["flags"], str) else r["flags"]
    metrics = Metrics.compute([(r["expected"], r["predicted"]) for r in rows])
    return EvalResult(
        int(run["id"]), run["prompt_version"], run["model"], metrics, float(run["cost_usd"]), rows
    )


TWO_PASS = Path(__file__).resolve().parents[3] / "seed" / "relabel_two_pass.json"


def label_check_section(path: Path = TWO_PASS) -> list[str]:
    """Summarise the two-pass label check (scripts/relabel_two_pass.py), if it has run."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    s = data["summary"]
    pp, fs = s["pass1_vs_pass2"], s["final_vs_stored"]

    def kappa(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.2f}"

    sessions = [x for x in data["sessions"].values() if x]
    models = sorted({str(x["model_reported"]) for x in sessions})
    out = [
        "",
        "## Label check: two independent AI passes",
        "",
        "Labels are AI-generated with a two-pass independent agreement check. Ed specified the process"
        "; a third independent AI pass adjudicated the disagreements, which are listed for human review.",
        "",
        f"The {pp['n']} criteria in the relabel sample were labelled twice from scratch, each pass in its own"
        " fresh Claude Code headless session with no tools and no access to the repository, the stored labels"
        " or the other pass. A third fresh session adjudicated each disagreement without seeing the stored"
        f" labels. Model reported: {', '.join(models)}. Reported cost: ${s['cost_usd_reported']:.2f}."
        " Details, prompts and raw responses: `seed/relabel_two_pass.json`.",
        "",
        "| Comparison | Agreement | Cohen's kappa |",
        "|---|---:|---:|",
        f"| Pass 1 vs pass 2 | {pp['agree']}/{pp['n']} ({pp['percent']}%) | {kappa(pp['kappa'])} |",
        f"| Adjudicated vs stored labels | {fs['agree']}/{fs['n']} ({fs['percent']}%) | {kappa(fs['kappa'])} |",
        "",
    ]
    nf = s["quotes_not_found"]
    if nf["pass1"] or nf["pass2"]:
        out += [
            f"Quotes not found verbatim in the item: pass 1 {', '.join(nf['pass1']) or 'none'};"
            f" pass 2 {', '.join(nf['pass2']) or 'none'}.",
            "",
        ]
    dis = [i for i in data["items"] if not i["agree"]]
    out += [f"### Disagreements between the passes ({len(dis)})", ""]
    if dis:
        out += [
            "| Criterion | Pass 1 | Pass 2 | Adjudicated | Stored | Adjudication note |",
            "|---|---|---|---|---|---|",
        ]
        for i in dis:

            def cell(p: dict[str, Any]) -> str:
                return f"**{p['label']}**: {p['reason']}".replace("|", "/").replace("\n", " ")

            note = (i["adjudication"] or {}).get("note", "").replace("|", "/")
            out.append(
                f"| {i['criterion']} | {cell(i['pass1'])} | {cell(i['pass2'])} | **{i['final']}** |"
                f" {i['stored']} | {note} |"
            )
    else:
        out.append("None: both passes gave every criterion the same label.")
    changed = [i for i in data["items"] if i["final"] != i["stored"]]
    out += ["", f"### Where the adjudicated label differs from the stored label ({len(changed)})", ""]
    if changed:
        out += ["| Criterion | Stored | Adjudicated | Stored rationale |", "|---|---|---|---|"]
        for i in changed:
            out.append(
                f"| {i['criterion']} | {i['stored']} | {i['final']} | {i['stored_rationale'].replace('|', '/')} |"
            )
    else:
        out.append("None.")
    if data.get("observations"):
        out += ["", "### Observations", ""] + [f"- {o}" for o in data["observations"]]
    out += [
        "",
        "Caveat: every labeller here is a Claude model, the same family as the judge. This measures how stable"
        " the AI labels are, not whether they are right. A person relabelling the sample (`relabel.html`) is"
        " still the stronger check.",
    ]
    return out


def history(db: Database) -> list[dict[str, Any]]:
    return db.query(
        "SELECT id, prompt_version, model, started_at, n, accuracy, fail_precision, fail_recall, unclear_rate,"
        " cost_usd, notes FROM eval_runs ORDER BY id"
    )


def write_report(db: Database, latest: EvalResult, path: Path) -> Path:
    def pct(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.0%}"

    labelled_by = db.scalar("SELECT labeled_by FROM eval_labels LIMIT 1") or "unknown"
    lines = [
        "# Evaluation report",
        "",
        "Ground truth: `seed/eval_labels.json` (48 criteria; see its `_about` for how they were labelled).",
        f"Labelled by: **{labelled_by}**.",
        "Positive class for precision and recall: **fail** (unbuilt work marked done is the costly mistake).",
        "",
        "## Runs by prompt version",
        "",
        "| Run | Prompt | Model requested | Model that answered | n | Accuracy | Fail precision | Fail recall"
        " | Unclear rate | Cost | How it ran |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for h in history(db):
        answered = db.query(
            "SELECT DISTINCT v.model_version FROM eval_results e JOIN ai_verdicts v ON v.id = e.verdict_id"
            " WHERE e.run_id = ? AND v.model_version IS NOT NULL AND v.model_version NOT LIKE 'rule:%'"
            " ORDER BY 1",
            (h["id"],),
        )
        lines.append(
            f"| {h['id']} | {h['prompt_version']} | {h['model']} |"
            f" {', '.join(str(a['model_version']) for a in answered) or '-'} | {h['n']} | {pct(h['accuracy'])} |"
            f" {pct(h['fail_precision'])} | {pct(h['fail_recall'])} | {pct(h['unclear_rate'])} |"
            f" ${float(h['cost_usd']):.4f} | {h['notes'] or ''} |"
        )
    m = latest.metrics
    lines += [
        "",
        f"## Latest run ({latest.prompt_version}, {latest.model})",
        "",
        "Confusion matrix (rows = human label, columns = checker verdict):",
        "",
        "| label \\ verdict | pass | fail | unclear |",
        "|---|---:|---:|---:|",
    ]
    for exp in LABELS:
        c = m.confusion[exp]
        lines.append(f"| **{exp}** | {c['pass']} | {c['fail']} | {c['unclear']} |")
    misses = [r for r in latest.rows if r["expected"] != r["predicted"]]
    reasons = Counter(tag_error(r["text"], r["expected"], r["predicted"], r["flags"]) for r in misses)
    lines += ["", f"## What it gets wrong ({len(misses)} of {m.n})", ""]
    for reason, count in reasons.most_common():
        lines.append(f"- **{reason}**: {count}")
    lines += [
        "",
        "| Criterion | Label | Verdict | Reason group | Checker's reasoning |",
        "|---|---|---|---|---|",
    ]
    for r in misses:
        why = r["reasoning"].replace("|", "/").replace("\n", " ")[:220]
        lines.append(
            f"| {r['criterion']} | {r['expected']} | {r['predicted']} |"
            f" {tag_error(r['text'], r['expected'], r['predicted'], r['flags'])} | {why} |"
        )
    lines += label_check_section()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path
