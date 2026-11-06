"""Judges turn a criterion's context into a raw verdict.

* ``LLMJudge`` renders a versioned prompt and asks a model through ``LLMClient``.
* ``HeuristicJudge`` is the offline baseline: keyword coverage plus a few rules. It
  costs nothing, needs no key and gives the evals a floor to beat.

Both return a ``Judgement``; guardrails, caching and storage live in ``run.py`` so
they apply to every judge the same way.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import requests
from pydantic import BaseModel, Field, ValidationError, field_validator

from devtrack.checker.context import CriterionContext
from devtrack.checker.prompt import RenderedPrompt, render
from devtrack.embeddings.hashing import tokens

log = logging.getLogger(__name__)

Verdict = Literal["pass", "fail", "unclear"]


class EvidenceItem(BaseModel):
    quote: str = Field(min_length=1, max_length=2000)
    source: str = ""


class JudgeOutput(BaseModel):
    """The JSON contract the model must return. Anything else is rejected."""

    verdict: Verdict
    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=10)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1, max_length=4000)

    @field_validator("verdict", mode="before")
    @classmethod
    def lower(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v


@dataclass
class Judgement:
    output: JudgeOutput
    model: str
    prompt_version: str
    input_tokens: int = 0
    output_tokens: int = 0
    flags: list[str] = field(default_factory=list)
    # The full exchange, stored with the verdict so it can be re-read and reproduced.
    prompt_system: str | None = None
    prompt_user: str | None = None
    raw_responses: list[str] = field(default_factory=list)
    model_version: str | None = None  # what the provider says answered, e.g. a dated snapshot
    request_ids: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    reported_cost_usd: float | None = None


class Judge(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def external(self) -> bool:
        """True when judging sends criterion and code text outside the company boundary.
        The data-classification policy uses this to keep controlled repos away from
        external models."""
        ...

    @property
    def prompt_version(self) -> str: ...

    def judge(self, ctx: CriterionContext, prompt: RenderedPrompt | None) -> Judgement: ...


# ---------------------------------------------------------------------------
# LLM client
# ---------------------------------------------------------------------------


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    model_version: str | None = None
    request_id: str | None = None
    stop_reason: str | None = None
    # Cost as reported by the backend, when it reports one (the Claude Code CLI does).
    # None means: compute it from tokens and the configured prices.
    reported_cost_usd: float | None = None


class LLMClient(Protocol):
    model: str
    external: bool

    def complete(self, system: str, user: str, *, max_tokens: int) -> LLMResult: ...


@dataclass
class AnthropicClient:
    """Minimal Messages API client: one request, retries on 429/5xx, usage reported."""

    api_key: str = field(repr=False)
    model: str
    timeout: float = 120.0
    max_attempts: int = 4
    url: str = "https://api.anthropic.com/v1/messages"
    session: Any = None
    external: bool = True  # a third-party API: never allowed to see controlled data

    def __post_init__(self) -> None:
        self.session = self.session or requests.Session()

    def complete(self, system: str, user: str, *, max_tokens: int) -> LLMResult:
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        for attempt in range(self.max_attempts):
            resp = self.session.post(self.url, json=body, headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
                usage = data.get("usage", {})
                return LLMResult(
                    text,
                    int(usage.get("input_tokens", 0)),
                    int(usage.get("output_tokens", 0)),
                    model_version=data.get("model"),
                    request_id=resp.headers.get("request-id") or data.get("id"),
                    stop_reason=data.get("stop_reason"),
                )
            if resp.status_code in (429, 500, 502, 503, 529) and attempt < self.max_attempts - 1:
                wait = float(resp.headers.get("retry-after", 0)) or random.uniform(0, 2**attempt)
                log.warning("model API %s; retrying in %.1fs", resp.status_code, wait)
                time.sleep(wait)
                continue
            raise RuntimeError(f"model API error {resp.status_code}: {resp.text[:300]}")
        raise RuntimeError("model API: retries exhausted")


class ClaudeCodeCLIError(RuntimeError):
    """The Claude Code CLI failed, timed out, or returned something we can't read."""


@dataclass
class ClaudeCodeCLIClient:
    """Run the judge through Claude Code in headless mode (``claude -p``).

    This is how the published evals were run: on a Claude subscription, with no API
    key. Each call is one fresh, non-persisted session with:
      * our system prompt replacing Claude Code's (``--system-prompt``),
      * every tool disabled (``--tools ""``), no MCP servers, no slash commands,
      * an empty working directory, so no project CLAUDE.md is picked up,
      * ``--output-format json``, which reports the model that answered, token usage,
        an API-equivalent cost and a session id.

    Differences from the API path, stated plainly: the CLI still adds some context of
    its own (environment details and similar; about 13k tokens of cached input per
    call in our runs), it can't cap output tokens or set temperature, and its reported
    cost is a list-price equivalent, not what the subscription bills. Set
    ``CHECKER_BACKEND=api`` with ``ANTHROPIC_API_KEY`` to reproduce on the API.
    """

    model: str
    executable: str = "claude"
    timeout: float = 300.0
    max_attempts: int = 3
    external: bool = True  # still a model hosted by a third party
    runner: Any = None  # injectable for tests: (argv, stdin, cwd, timeout) -> (returncode, stdout, stderr)
    sleep: Any = None
    workdir: str | None = None

    def __post_init__(self) -> None:
        import subprocess
        import tempfile

        def run(argv: list[str], stdin: str, cwd: str, timeout: float) -> tuple[int, str, str]:
            proc = subprocess.run(argv, input=stdin, capture_output=True, text=True, cwd=cwd, timeout=timeout)
            return proc.returncode, proc.stdout, proc.stderr

        self.runner = self.runner or run
        self.sleep = self.sleep or time.sleep
        self.workdir = self.workdir or tempfile.mkdtemp(prefix="devtrack-judge-")

    def argv(self, system: str) -> list[str]:
        return [
            self.executable,
            "-p",
            "--output-format",
            "json",
            "--model",
            self.model,
            "--system-prompt",
            system,
            "--tools",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
        ]

    def complete(self, system: str, user: str, *, max_tokens: int) -> LLMResult:
        last_error = ""
        for attempt in range(self.max_attempts):
            try:
                code, out, err = self.runner(self.argv(system), user, self.workdir, self.timeout)
            except Exception as exc:  # timeout or the binary is missing
                last_error = f"{type(exc).__name__}: {exc}"
                code, out, err = -1, "", last_error
            if code == 0 and out.strip():
                try:
                    data = json.loads(out)
                except json.JSONDecodeError:
                    last_error = f"unreadable CLI output: {out[:200]}"
                else:
                    if not data.get("is_error") and data.get("subtype") == "success":
                        return self._result(data)
                    last_error = (
                        f"CLI reported an error: {str(data.get('result') or data.get('subtype'))[:300]}"
                    )
            else:
                last_error = last_error or f"exit {code}: {(err or out)[:300]}"
            if attempt < self.max_attempts - 1:
                wait = 5.0 * (2**attempt)
                log.warning("claude CLI call failed (%s); retrying in %.0fs", last_error, wait)
                self.sleep(wait)
        raise ClaudeCodeCLIError(last_error)

    @staticmethod
    def _result(data: dict[str, Any]) -> LLMResult:
        usage = data.get("usage") or {}
        models = sorted((data.get("modelUsage") or {}).keys())
        input_tokens = (
            int(usage.get("input_tokens", 0))
            + int(usage.get("cache_read_input_tokens", 0))
            + int(usage.get("cache_creation_input_tokens", 0))
        )
        return LLMResult(
            str(data.get("result") or ""),
            input_tokens,
            int(usage.get("output_tokens", 0)),
            model_version=",".join(models) or None,  # the model(s) that actually answered
            request_id=data.get("session_id"),
            stop_reason=data.get("stop_reason"),
            reported_cost_usd=float(data["total_cost_usd"])
            if data.get("total_cost_usd") is not None
            else None,
        )


class InternalModelNotConfigured(RuntimeError):
    """The approved internal model endpoint isn't set up in this deployment."""


@dataclass
class ApprovedInternalModelClient:
    """Placeholder for a model deployed inside the controlled boundary (on-prem or a
    government-cloud tenancy approved for export-controlled data).

    It is the only client allowed to judge ``controlled`` repositories, because
    ``external`` is False. The transport is deliberately not implemented: the real one
    depends on the approved deployment (endpoint, auth, network path), and guessing it
    here would be worse than failing loudly. Wire it up by implementing ``complete``
    against ``INTERNAL_MODEL_URL``; everything else (prompts, guardrails, budget,
    caching, review) already works with any ``LLMClient``.
    """

    url: str | None
    model: str = "internal-approved"
    external: bool = False

    def complete(self, system: str, user: str, *, max_tokens: int) -> LLMResult:
        if not self.url:
            raise InternalModelNotConfigured(
                "no approved internal model: set INTERNAL_MODEL_URL to the endpoint approved for controlled data"
            )
        raise InternalModelNotConfigured(
            f"the transport for the approved internal model at {self.url} is not implemented in this build"
        )


_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


def parse_output(text: str) -> JudgeOutput:
    """Parse the model reply. Tolerates a code fence or a sentence around the JSON
    object; anything that still doesn't validate raises ValueError."""
    match = _JSON_BLOCK.search(text)
    if not match:
        raise ValueError("no JSON object in model output")
    try:
        return JudgeOutput.model_validate(json.loads(match.group(0)))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"model output failed validation: {exc}") from exc


@dataclass
class LLMJudge:
    client: LLMClient
    prompt_version: str = "v2"
    max_output_tokens: int = 800

    @property
    def model(self) -> str:
        return self.client.model

    @property
    def external(self) -> bool:
        # A client that doesn't say is assumed external: the policy fails closed.
        return bool(getattr(self.client, "external", True))

    def judge(self, ctx: CriterionContext, prompt: RenderedPrompt | None) -> Judgement:
        prompt = prompt or render(ctx, self.prompt_version)
        result = self.client.complete(prompt.system, prompt.user, max_tokens=self.max_output_tokens)
        in_tok, out_tok = result.input_tokens, result.output_tokens
        flags: list[str] = []
        replies = [result.text]
        request_ids = [result.request_id] if result.request_id else []
        reported = result.reported_cost_usd
        last = result
        try:
            output = parse_output(result.text)
        except ValueError as first_error:
            # One repair attempt: same prompt plus a reminder. Costs one more call, so
            # it is counted, and if it fails too the verdict is "unclear".
            retry = self.client.complete(
                prompt.system,
                prompt.user + "\n\nYour previous reply was not valid JSON for the required schema. "
                "Reply with the JSON object only.",
                max_tokens=self.max_output_tokens,
            )
            in_tok += retry.input_tokens
            out_tok += retry.output_tokens
            replies.append(retry.text)
            if retry.request_id:
                request_ids.append(retry.request_id)
            if reported is not None and retry.reported_cost_usd is not None:
                reported += retry.reported_cost_usd
            last = retry
            try:
                output = parse_output(retry.text)
                flags.append("output_repaired")
            except ValueError:
                log.warning("unparseable model output for %s: %s", ctx.key, first_error)
                output = JudgeOutput(
                    verdict="unclear",
                    confidence=0.0,
                    reasoning="The model did not return valid structured output.",
                )
                flags.append("invalid_model_output")
        return Judgement(
            output,
            self.model,
            self.prompt_version,
            in_tok,
            out_tok,
            flags,
            prompt_system=prompt.system,
            prompt_user=prompt.user,
            raw_responses=replies,
            model_version=last.model_version or self.model,
            request_ids=request_ids,
            stop_reason=last.stop_reason,
            reported_cost_usd=reported,
        )


# ---------------------------------------------------------------------------
# Offline baseline
# ---------------------------------------------------------------------------

RUNTIME_CLAIM = re.compile(
    r"(?i)\b(\d+\s*(ms|milliseconds?|seconds?|s)\b|under \d|within \d|faster|latency|"
    r"performance|throughput|works on (windows|macos|linux))"
)
DOC_CLAIM = re.compile(r"(?i)\b(docs?|documentation|user guide|guide|changelog|history|readme|advisory)\b")
TEST_CLAIM = re.compile(r"(?i)\b(automated test|tested|test that|tests? (cover|run))\b")
DOC_FILE = re.compile(r"(?im)^\+\+\+ b/(docs?/|.*\.(md|rst)$|CHANGES|CHANGELOG|HISTORY)")
TEST_FILE = re.compile(r"(?im)^\+\+\+ b/(.*tests?/|.*test_[^/]*\.py$)")
GENERIC = {
    "instead",
    "new",
    "user",
    "users",
    "the",
    "when",
    "same",
    "way",
    "every",
    "any",
    "one",
    "two",
    "example",
    "accepts",
    "raises",
    "error",
    "make",
    "makes",
    "uses",
    "use",
    "code",
    "change",
    "changes",
    "works",
    "work",
    "still",
    "keep",
    "keeps",
    "passed",
    "passing",
    "pass",
    "output",
}


@dataclass
class HeuristicJudge:
    """Keyword-coverage baseline. Deliberately simple and transparent."""

    model: str = "heuristic"
    prompt_version: str = "heuristic-v1"
    external: bool = False  # runs in-process; nothing leaves the machine

    def judge(self, ctx: CriterionContext, prompt: RenderedPrompt | None) -> Judgement:
        added = [
            line[1:].strip()
            for c in ctx.commits
            for line in c.diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        diffs = "\n".join(c.diff for c in ctx.commits)
        messages = [c.message.splitlines()[0] for c in ctx.commits if c.message]

        def out(verdict: Verdict, conf: float, why: str, quotes: list[str] | None = None) -> Judgement:
            ev = [EvidenceItem(quote=q, source="") for q in (quotes or [])]
            return Judgement(
                JudgeOutput(verdict=verdict, confidence=conf, reasoning=why, evidence=ev),
                self.model,
                self.prompt_version,
            )

        if RUNTIME_CLAIM.search(ctx.text):
            return out("unclear", 0.5, "Runtime, platform or performance claim: a diff can't show it.")
        if DOC_CLAIM.search(ctx.text) and not DOC_FILE.search(diffs):
            return out(
                "fail", 0.7, "The criterion needs documentation or a changelog entry; no such file changed."
            )
        if TEST_CLAIM.search(ctx.text) and not TEST_FILE.search(diffs):
            return out("fail", 0.6, "The criterion asks for tests; no test file changed.")

        terms = [t for t in dict.fromkeys(tokens(ctx.text)) if t not in GENERIC and len(t) > 2]
        if not terms:
            return out("unclear", 0.3, "No distinctive terms to look for.")
        haystack = "\n".join(added + messages).lower()
        found = [t for t in terms if t in haystack]
        coverage = len(found) / len(terms)
        scored = sorted(added + messages, key=lambda line: -sum(t in line.lower() for t in found))
        quotes = [q for q in scored[:2] if len(q) >= 8]
        if coverage >= 0.6:
            return out(
                "pass",
                round(min(0.9, coverage), 2),
                f"{len(found)} of {len(terms)} key terms appear in added lines or messages.",
                quotes,
            )
        if coverage >= 0.35:
            return out(
                "unclear", 0.4, f"Only {len(found)} of {len(terms)} key terms appear in the change.", quotes
            )
        return out("fail", round(1 - coverage, 2), f"Only {len(found)} of {len(terms)} key terms appear.")
