"""The relabel tools: sampling, kappa, quote checking, and the saved two-pass result."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import relabel  # noqa: E402
import relabel_two_pass  # noqa: E402


def test_kappa() -> None:
    assert relabel.cohen_kappa([("pass", "pass"), ("fail", "fail")]) == 1.0
    assert relabel.cohen_kappa([("pass", "fail"), ("fail", "pass")]) == -1.0
    assert relabel.cohen_kappa([]) is None


def test_sample_is_stratified_and_stable() -> None:
    labels = relabel.stored_labels()
    a, b = relabel.sample(labels, 20, 7), relabel.sample(labels, 20, 7)
    assert a == b and len(set(a)) == 20
    mix = sorted(labels[k]["label"] for k in a)
    assert mix.count("fail") == 7 and mix.count("pass") == 11 and mix.count("unclear") == 2


def test_quote_check_accepts_diff_quotes_and_rejects_inventions() -> None:
    it = {
        "key": "K",
        "text": "t",
        "story": "s",
        "feature": "f",
        "requirements": [],
        "tests": [],
        "commits": [
            {"repo": "r", "sha": "abc", "prs": [], "message": "m", "diff": "+if x:\n+    import ssl"}
        ],
    }
    assert relabel_two_pass.quote_found("if x:\n    import ssl", it)
    assert not relabel_two_pass.quote_found("import requests", it)
    assert not relabel_two_pass.quote_found("", it)  # code is linked, so a quote is required


def test_saved_two_pass_result_is_consistent() -> None:
    data = json.loads((ROOT / "seed" / "relabel_two_pass.json").read_text())
    items = data["items"]
    labels = relabel.stored_labels()
    assert [i["criterion"] for i in items] == relabel.sample(labels, 20, data["sample_seed"])
    pairs = [(i["pass1"]["label"], i["pass2"]["label"]) for i in items]
    s = data["summary"]["pass1_vs_pass2"]
    assert s["agree"] == sum(a == b for a, b in pairs)
    assert s["kappa"] == relabel.cohen_kappa(pairs)
    for i in items:
        assert i["agree"] == (i["pass1"]["label"] == i["pass2"]["label"])
        assert i["final"] == (i["adjudication"]["label"] if not i["agree"] else i["pass1"]["label"])
    sessions = data["sessions"]
    assert sessions["pass1"]["session_id"] != sessions["pass2"]["session_id"], "two separate sessions"
    # No labeller or adjudicator was shown the stored labels' rationales.
    prompts = [sessions[k]["user_prompt"] for k in ("pass1", "pass2", "adjudication") if sessions[k]]
    for i in items:
        assert all(i["stored_rationale"] not in p for p in prompts)
