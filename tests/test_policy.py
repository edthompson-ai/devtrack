"""Data classification: controlled repositories never reach an external model."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from devtrack.checker.guardrails import Budget
from devtrack.checker.judges import (
    AnthropicClient,
    ApprovedInternalModelClient,
    HeuristicJudge,
    InternalModelNotConfigured,
    LLMJudge,
    LLMResult,
)
from devtrack.checker.run import check_criterion, check_many
from devtrack.db import Database
from devtrack.ingest.seed import load_seed
from devtrack.policy import (
    PolicyBlocked,
    all_policies,
    allows,
    classification_of,
    load_policy_file,
    set_classification,
)

CLICK_KEY = "AC-CLICK-101-1"


@dataclass
class RecordingLLM:
    """Records whether it was ever called. ``external`` mirrors a real client."""

    external: bool = True
    model: str = "recording-model"
    sent: list[str] = field(default_factory=list)

    def complete(self, system: str, user: str, *, max_tokens: int) -> LLMResult:
        self.sent.append(user)
        reply = {"verdict": "unclear", "evidence": [], "confidence": 0.1, "reasoning": "r"}
        return LLMResult(json.dumps(reply), 10, 10)


@dataclass
class UndeclaredLLM:
    """A client that doesn't say whether it is external."""

    model: str = "mystery"
    sent: list[str] = field(default_factory=list)

    def complete(self, system: str, user: str, *, max_tokens: int) -> LLMResult:
        self.sent.append(user)
        return LLMResult("{}", 1, 1)


def budget() -> Budget:
    return Budget(1.0, 3.0, 15.0)


@pytest.fixture
def loaded(db: Database) -> Database:
    load_seed(db)
    return db


def test_rule_table() -> None:
    assert allows("public", external=True)
    assert allows("internal", external=True)
    assert not allows("controlled", external=True)
    assert allows("controlled", external=False)


def test_unclassified_repo_fails_closed(db: Database) -> None:
    p = classification_of(db, "acme/secret")
    assert p.classification == "controlled" and not p.explicit


def test_seed_policy_is_loaded(loaded: Database) -> None:
    policies = {p.repo: p for p in all_policies(loaded)}
    assert {r: p.classification for r, p in policies.items()} == {
        "encode/httpx": "public",
        "pallets/click": "public",
        "psf/requests": "public",
    }
    assert all(p.explicit and p.set_by == "arjun-patel" for p in policies.values())


def test_controlled_repo_never_reaches_an_external_model(loaded: Database) -> None:
    set_classification(loaded, "pallets/click", "controlled", set_by="security", note="ITAR test")
    client = RecordingLLM()
    before = loaded.scalar("SELECT COUNT(*) FROM ai_verdicts")
    with pytest.raises(PolicyBlocked, match="classified 'controlled'"):
        check_criterion(loaded, CLICK_KEY, LLMJudge(client), budget())
    assert client.sent == [], "nothing may be sent"
    assert loaded.scalar("SELECT COUNT(*) FROM ai_verdicts") == before, "nothing may be stored"


def test_client_that_does_not_declare_is_treated_as_external(loaded: Database) -> None:
    set_classification(loaded, "pallets/click", "controlled", set_by="security")
    client = UndeclaredLLM()
    judge = LLMJudge(client)  # type: ignore[arg-type]
    assert judge.external
    with pytest.raises(PolicyBlocked):
        check_criterion(loaded, CLICK_KEY, judge, budget())
    assert client.sent == []


def test_real_client_is_external() -> None:
    assert AnthropicClient(api_key="k", model="m").external
    assert not HeuristicJudge().external


def test_local_judges_may_see_controlled_code(loaded: Database) -> None:
    set_classification(loaded, "pallets/click", "controlled", set_by="security")
    rec = check_criterion(loaded, CLICK_KEY, HeuristicJudge(), budget())
    assert rec.verdict in ("pass", "fail", "unclear")


def test_internal_model_passes_policy_then_says_it_is_not_configured(loaded: Database) -> None:
    set_classification(loaded, "pallets/click", "controlled", set_by="security")
    judge = LLMJudge(ApprovedInternalModelClient(url=None))
    assert not judge.external
    with pytest.raises(InternalModelNotConfigured, match="INTERNAL_MODEL_URL"):
        check_criterion(loaded, CLICK_KEY, judge, budget())
    with pytest.raises(InternalModelNotConfigured, match="not implemented"):
        ApprovedInternalModelClient(url="https://llm.internal.example").complete("s", "u", max_tokens=1)


def test_check_many_skips_and_counts_blocked(loaded: Database) -> None:
    set_classification(loaded, "pallets/click", "controlled", set_by="security")
    keys = [CLICK_KEY, "AC-REQ-101-1"]
    client = RecordingLLM()
    summary = check_many(loaded, keys, LLMJudge(client), budget())
    assert summary.blocked_policy == 1 and summary.blocked == [CLICK_KEY]
    assert summary.checked == 1
    assert all("pallets/click" not in s for s in client.sent)


def test_classification_is_validated_and_owned(db: Database, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        set_classification(db, "acme/app", "secret", set_by="x")
    with pytest.raises(ValueError):
        set_classification(db, "acme/app", "public", set_by="")
    f = tmp_path / "policy.json"
    f.write_text(json.dumps({"set_by": "sec", "repos": {"acme/app": {"classification": "internal"}}}))
    assert load_policy_file(db, f) == 1
    assert classification_of(db, "acme/app").classification == "internal"
