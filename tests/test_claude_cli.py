"""The Claude Code headless backend (`claude -p --output-format json`), with a fake runner."""

from __future__ import annotations

import json
from typing import Any

import pytest

from devtrack.checker.guardrails import Budget
from devtrack.checker.judges import ClaudeCodeCLIClient, ClaudeCodeCLIError, LLMJudge
from devtrack.checker.run import check_criterion
from devtrack.db import Database
from devtrack.ingest.seed import load_seed


def cli_json(result: str, *, model: str = "claude-sonnet-5-5", is_error: bool = False) -> str:
    """The shape `claude -p --output-format json` prints (trimmed to what we read)."""
    return json.dumps(
        {
            "type": "result",
            "subtype": "success" if not is_error else "error_during_execution",
            "is_error": is_error,
            "result": result,
            "session_id": "sess-123",
            "stop_reason": "end_turn",
            "total_cost_usd": 0.0215,
            "usage": {
                "input_tokens": 3,
                "cache_read_input_tokens": 13000,
                "cache_creation_input_tokens": 900,
                "output_tokens": 210,
            },
            "modelUsage": {model: {"inputTokens": 3, "outputTokens": 210}},
        }
    )


class FakeRunner:
    def __init__(self, outputs: list[tuple[int, str, str] | Exception]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[list[str], str, str]] = []

    def __call__(self, argv: list[str], stdin: str, cwd: str, timeout: float) -> tuple[int, str, str]:
        self.calls.append((argv, stdin, cwd))
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def client(runner: FakeRunner) -> ClaudeCodeCLIClient:
    return ClaudeCodeCLIClient(model="claude-sonnet-5-5", runner=runner, sleep=lambda s: None, workdir="/tmp")


def reply(verdict: str = "unclear") -> str:
    return json.dumps({"verdict": verdict, "evidence": [], "confidence": 0.5, "reasoning": "r"})


def test_argv_replaces_the_system_prompt_and_disables_tools() -> None:
    runner = FakeRunner([(0, cli_json(reply()), "")])
    c = client(runner)
    res = c.complete("SYSTEM TEXT", "USER TEXT", max_tokens=800)
    argv, stdin, _ = runner.calls[0]
    assert argv[:4] == ["claude", "-p", "--output-format", "json"]
    assert argv[argv.index("--system-prompt") + 1] == "SYSTEM TEXT"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
    assert {"--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"} <= set(argv)
    assert stdin == "USER TEXT", "the prompt goes on stdin, not the command line"
    assert res.model_version == "claude-sonnet-5-5" and res.request_id == "sess-123"
    assert res.input_tokens == 13903 and res.output_tokens == 210 and res.reported_cost_usd == 0.0215


def test_retries_then_succeeds() -> None:
    runner = FakeRunner([(1, "", "rate limited"), TimeoutError("slow"), (0, cli_json(reply()), "")])
    assert client(runner).complete("s", "u", max_tokens=1).text == reply()
    assert len(runner.calls) == 3


def test_reported_error_and_garbage_raise_after_retries() -> None:
    runner = FakeRunner([(0, cli_json("overloaded", is_error=True), ""), (0, "not json", ""), (0, "", "")])
    with pytest.raises(ClaudeCodeCLIError):
        client(runner).complete("s", "u", max_tokens=1)


def test_cli_is_an_external_model() -> None:
    assert client(FakeRunner([])).external


def test_verdict_records_cli_model_session_and_reported_cost(db: Database) -> None:
    load_seed(db)
    runner = FakeRunner([(0, cli_json(reply("unclear"), model="claude-sonnet-5-5"), "")])
    budget = Budget(1.0, 3.0, 15.0)
    rec = check_criterion(db, "AC-REQ-101-1", LLMJudge(client(runner)), budget)
    row: dict[str, Any] | None = db.query_one("SELECT * FROM ai_verdicts WHERE id = ?", (rec.id,))
    assert row
    assert row["model_version"] == "claude-sonnet-5-5"
    assert json.loads(row["request_ids"]) == ["sess-123"]
    assert row["prompt_user"] == runner.calls[0][1]
    assert json.loads(row["raw_responses"]) == [reply("unclear")]
    assert row["cost_usd"] == pytest.approx(0.0215) and budget.spent_usd == pytest.approx(0.0215)
