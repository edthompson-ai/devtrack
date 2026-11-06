#!/usr/bin/env python3
"""Re-label a sample of the eval set yourself and measure how often you agree.

The 48 ground-truth labels in seed/eval_labels.json were written during the build by
Claude Code, not by a person. Before anyone trusts an accuracy number computed against
them, a human should label a sample blind and we should know how often the two agree.
That agreement is the ceiling on what any checker can be shown to achieve.

You see exactly what the AI checker sees: the criterion, its story and requirement,
and the redacted commit messages and diffs linked to it. The stored label stays hidden
until you have labelled every item.

Three ways to use it:

    # 1. On any device with a browser (an iPad works): build a self-contained page,
    #    label in the page, and the page shows the agreement when you finish.
    python scripts/relabel.py export --n 20 --out relabel.html

    # 2. In a terminal: label one item at a time.
    python scripts/relabel.py cli --n 20 --labeler "Your Name"

    # 3. Score a labels file saved from the page (or from the CLI).
    python scripts/relabel.py score my-labels.json

Agreement is reported as raw agreement and Cohen's kappa (agreement corrected for
chance). Rough reading of kappa: below 0.4 the labels are too noisy to evaluate
against; 0.6 to 0.8 is good; above 0.8 is very good.
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from devtrack.checker.context import gather_context  # noqa: E402
from devtrack.db import Database, connect, migrate  # noqa: E402
from devtrack.ingest.seed import DEFAULT_SEED_DIR, load_seed  # noqa: E402

LABELS = ("pass", "fail", "unclear")
TEMPLATE = Path(__file__).with_name("relabel_page.html")
MAX_DIFF_CHARS = 6_000


# -- sampling and items -------------------------------------------------------------------


def stored_labels(path: Path = DEFAULT_SEED_DIR / "eval_labels.json") -> dict[str, dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {item["criterion"]: item for item in data["labels"]}


def sample(labels: dict[str, dict[str, str]], n: int, seed: int) -> list[str]:
    """Stratified by stored label, so a 20-item sample keeps the set's mix of pass,
    fail and unclear. Deterministic for a given seed."""
    rng = random.Random(seed)
    by_label: dict[str, list[str]] = {
        lab: sorted(k for k, v in labels.items() if v["label"] == lab) for lab in LABELS
    }
    total = len(labels)
    quota = {lab: round(n * len(keys) / total) for lab, keys in by_label.items()}
    while sum(quota.values()) > n:
        quota[max(quota, key=lambda k: quota[k])] -= 1
    while sum(quota.values()) < n:
        quota[max(by_label, key=lambda k: len(by_label[k]) - quota[k])] += 1
    picked = [k for lab, keys in by_label.items() for k in rng.sample(keys, min(quota[lab], len(keys)))]
    rng.shuffle(picked)
    return picked


def seeded_db() -> Database:
    path = Path(tempfile.mkdtemp()) / "relabel.db"
    db = connect(f"sqlite:///{path}")
    migrate(db)
    load_seed(db)
    return db


def item(db: Database, key: str) -> dict[str, Any]:
    ctx = gather_context(db, key)
    commits = []
    budget = MAX_DIFF_CHARS
    for c in ctx.commits:
        diff = c.diff[:budget]
        budget = max(0, budget - len(diff))
        commits.append(
            {
                "repo": c.repo,
                "sha": c.sha[:10],
                "prs": c.pr_numbers,
                "message": c.message.strip(),
                "diff": diff
                + ("\n... [diff shortened for the labelling page]" if len(diff) < len(c.diff) else ""),
            }
        )
    return {
        "key": key,
        "text": ctx.text,
        "story": ctx.story,
        "feature": ctx.feature,
        "requirements": ctx.requirements,
        "tests": ctx.tests,
        "commits": commits,
    }


# -- scoring --------------------------------------------------------------------------------


@dataclass
class Agreement:
    n: int
    agree: int
    kappa: float | None
    confusion: dict[str, dict[str, int]]  # rows: stored label, columns: yours
    disagreements: list[dict[str, str]]

    @property
    def rate(self) -> float:
        return self.agree / self.n if self.n else 0.0


def cohen_kappa(pairs: list[tuple[str, str]]) -> float | None:
    n = len(pairs)
    if not n:
        return None
    observed = sum(a == b for a, b in pairs) / n
    left = Counter(a for a, _ in pairs)
    right = Counter(b for _, b in pairs)
    expected = sum(left[lab] * right[lab] for lab in LABELS) / (n * n)
    if expected == 1:
        return 1.0 if observed == 1 else None
    return round((observed - expected) / (1 - expected), 3)


def score(mine: dict[str, str], stored: dict[str, dict[str, str]]) -> Agreement:
    keys = [k for k in mine if k in stored]
    pairs = [(stored[k]["label"], mine[k]) for k in keys]
    confusion = {a: {b: 0 for b in LABELS} for a in LABELS}
    for a, b in pairs:
        confusion[a][b] += 1
    return Agreement(
        n=len(pairs),
        agree=sum(a == b for a, b in pairs),
        kappa=cohen_kappa(pairs),
        confusion=confusion,
        disagreements=[
            {
                "criterion": k,
                "stored": stored[k]["label"],
                "yours": mine[k],
                "stored_rationale": stored[k]["rationale"],
            }
            for k in keys
            if stored[k]["label"] != mine[k]
        ],
    )


def print_score(result: Agreement) -> None:
    kappa = "n/a" if result.kappa is None else f"{result.kappa:.2f}"
    print(f"\nAgreement: {result.agree}/{result.n} ({result.rate:.0%}), Cohen's kappa {kappa}")
    print("\nrows = stored label, columns = yours")
    print("          " + "".join(f"{b:>9}" for b in LABELS))
    for a in LABELS:
        print(f"{a:>9} " + "".join(f"{result.confusion[a][b]:>9}" for b in LABELS))
    if result.disagreements:
        print("\nWhere you disagree:")
        for d in result.disagreements:
            print(
                f"  {d['criterion']}: stored {d['stored']}, you said {d['yours']}. Stored reason: {d['stored_rationale']}"
            )


# -- commands -------------------------------------------------------------------------------


def cmd_export(args: argparse.Namespace) -> None:
    labels = stored_labels()
    keys = sample(labels, args.n, args.seed)
    db = seeded_db()
    items = [item(db, k) for k in keys]
    hidden = {k: {"label": labels[k]["label"], "rationale": labels[k]["rationale"]} for k in keys}
    payload = {
        "seed": args.seed,
        "items": items,
        # Not secret, only out of sight: decoded by the page after the last label.
        "answers": base64.b64encode(json.dumps(hidden).encode()).decode(),
    }
    page = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__RELABEL_DATA__*/null", json.dumps(payload).replace("</", "<\\/")
    )
    if not args.bare:  # a standalone file needs its own document head; a hosted page gets one
        page = (
            '<!doctype html>\n<html lang="en"><meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            "<style>body{margin:0}[hidden]{display:none!important}</style>\n" + page + "\n</html>\n"
        )
    Path(args.out).write_text(page, encoding="utf-8")
    print(f"wrote {args.out}: {len(items)} items (seed {args.seed}). Open it in any browser.")


def cmd_cli(args: argparse.Namespace) -> None:
    labels = stored_labels()
    keys = sample(labels, args.n, args.seed)
    db = seeded_db()
    mine: dict[str, str] = {}
    notes: dict[str, str] = {}
    for i, key in enumerate(keys, 1):
        it = item(db, key)
        print("\n" + "=" * 78)
        print(f"[{i}/{len(keys)}] {key}  ({it['feature']} / {it['story']})")
        print(f"\nCriterion: {it['text']}")
        for req in it["requirements"]:
            print(f"Requirement: {req}")
        if it["tests"]:
            print(f"Linked tests: {', '.join(it['tests'])}")
        if not it["commits"]:
            print("\n(no commits are linked to this criterion)")
        for c in it["commits"]:
            print(f"\n--- {c['repo']} {c['sha']} PR {c['prs']}\n{c['message']}\n{c['diff']}")
        answer = ""
        while answer not in ("p", "f", "u"):
            answer = (
                input("\nDoes the linked code meet the criterion? [p]ass / [f]ail / [u]nclear: ")
                .strip()
                .lower()[:1]
            )
        mine[key] = {"p": "pass", "f": "fail", "u": "unclear"}[answer]
        note = input("Optional note (enter to skip): ").strip()
        if note:
            notes[key] = note
    out = Path(args.out or f"relabels-{args.labeler.replace(' ', '-').lower()}.json")
    out.write_text(
        json.dumps({"labeler": args.labeler, "seed": args.seed, "labels": mine, "notes": notes}, indent=2)
    )
    print(f"\nsaved {out}")
    print_score(score(mine, labels))


def cmd_score(args: argparse.Namespace) -> None:
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    print_score(score(data["labels"], stored_labels()))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("export", "cli"):
        sp = sub.add_parser(name)
        sp.add_argument("--n", type=int, default=20)
        sp.add_argument("--seed", type=int, default=7)
        sp.add_argument("--out", default="relabel.html" if name == "export" else None)
        if name == "cli":
            sp.add_argument("--labeler", required=True)
        else:
            sp.add_argument("--bare", action="store_true", help="page body only, for hosting as an artifact")
    sub.add_parser("score").add_argument("file")
    args = ap.parse_args(argv)
    {"export": cmd_export, "cli": cmd_cli, "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    main()
