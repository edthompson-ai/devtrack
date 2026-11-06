"""Guardrails around the AI checker.

Repository content (commit messages, diffs, PR text) is written by people we don't
control, so it is treated as untrusted data, never as instructions:

1. Secrets are redacted before anything leaves the process, and files that commonly
   hold secrets (.env, keys, certificates) are dropped from the context entirely.
2. Untrusted text is fenced in tags carrying a random nonce, so content can't fake a
   closing tag and "escape" into the instructions.
3. Instruction-like text inside repo content is detected. Detection doesn't block the
   check, but a suspicious "pass" is downgraded to "unclear" for a human: we don't
   rely on the model to resist the injection.
4. Every quote the model offers as evidence must appear verbatim in the context we
   sent. A "pass" with no verifiable evidence becomes "unclear".
5. Cost is capped per run: the estimated cost of each call is checked against the
   remaining budget before the call is made.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 1. Secrets
# ---------------------------------------------------------------------------

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{10,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("url_password", re.compile(r"(?<=://)([^:/\s@]+):([^@\s/]{3,})(?=@)")),
    (
        "assigned_secret",
        re.compile(
            r"""(?ix)
            \b(api[_-]?key|secret|token|passw(?:or)?d|pwd|access[_-]?key|client[_-]?secret|auth)
            (\s*[:=]\s*)
            (["'])([^"'\s]{8,})\3
            """
        ),
    ),
]
SECRET_FILES = re.compile(
    r"(?i)(^|/)(\.env(\..*)?|.*\.(pem|key|p12|pfx|jks)|id_(rsa|ed25519|ecdsa)|secrets?\.\w+)$"
)


def redact_secrets(text: str) -> tuple[str, list[str]]:
    """Mask anything that looks like a credential. Returns (clean_text, kinds_found)."""
    found: list[str] = []

    def mask(kind: str) -> Callable[[re.Match[str]], str]:
        def repl(m: re.Match[str]) -> str:
            found.append(kind)
            if kind == "assigned_secret":
                return f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED]{m.group(3)}"
            if kind == "url_password":
                return f"{m.group(1)}:[REDACTED]"
            return f"[REDACTED:{kind}]"

        return repl

    for kind, pattern in SECRET_PATTERNS:
        text = pattern.sub(mask(kind), text)
    return text, found


def is_secret_file(path: str) -> bool:
    return bool(SECRET_FILES.search(path))


# ---------------------------------------------------------------------------
# 2. Fencing untrusted content
# ---------------------------------------------------------------------------


def new_nonce() -> str:
    return secrets.token_hex(6)


def fence(content: str, label: str, nonce: str) -> str:
    """Wrap untrusted content. The nonce makes the closing tag unguessable; any
    attempt to write a closing tag inside the content is also neutralised."""
    content = re.sub(r"</?\s*untrusted", "<_untrusted", content, flags=re.I)
    return f'<untrusted_repo_content kind="{label}" nonce="{nonce}">\n{content}\n</untrusted_repo_content nonce="{nonce}">'


# ---------------------------------------------------------------------------
# 3. Injection detection
# ---------------------------------------------------------------------------

INJECTION_PATTERNS = [
    re.compile(
        r"(?i)\b(ignore|disregard|forget|override)\b.{0,30}\b(previous|prior|above|earlier|all)\b.{0,20}"
        r"\b(instructions?|rules|prompts?|directions)"
    ),
    re.compile(
        r"(?i)\b(note|message|instructions?)\s+(to|for)\s+(the\s+)?(ai|llm|model|assistant|reviewers?|bots?)\b"
    ),
    re.compile(r"(?i)\byou are now\b|\bnew instructions\b|\bsystem prompt\b"),
    re.compile(r"(?i)\bmark\b[^.\n]{0,60}\bas\s+(pass(ed)?|approved|complete|done|met)\b"),
    re.compile(r"(?i)\b(confidence|verdict)\s*(=|:|of)?\s*(1(\.0)?|100%|\"?pass\"?)\b.{0,10}$", re.M),
    re.compile(r"(?i)</?\s*(system|assistant|instructions?)\s*>"),
]


def detect_injection(text: str) -> list[str]:
    hits: list[str] = []
    for pattern in INJECTION_PATTERNS:
        for m in pattern.finditer(text):
            snippet = m.group(0).strip()
            if snippet and snippet not in hits:
                hits.append(snippet[:120])
    return hits


# ---------------------------------------------------------------------------
# 4. Evidence verification
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    text = re.sub(r"(?m)^[+\- ]", "", text)  # diff markers are optional in a quote
    return re.sub(r"\s+", " ", text).strip().lower()


def verify_quotes(quotes: list[str], context: str) -> tuple[list[str], list[str]]:
    """Split quotes into (verified, rejected): verified ones occur verbatim (ignoring
    whitespace, case and leading diff markers) in the context that was sent."""
    haystack = _norm(context)
    verified, rejected = [], []
    for q in quotes:
        nq = _norm(q)
        if len(nq) >= 8 and nq in haystack:
            verified.append(q)
        else:
            rejected.append(q)
    return verified, rejected


# ---------------------------------------------------------------------------
# 5. Cost
# ---------------------------------------------------------------------------


class BudgetExceeded(RuntimeError):
    pass


def estimate_tokens(text: str) -> int:
    """Rough token count (about 4 characters per token for English and code). Used
    only for budgeting before a call; billing uses the API's reported usage."""
    return max(1, len(text) // 4)


@dataclass
class Budget:
    limit_usd: float
    price_input_per_mtok: float
    price_output_per_mtok: float
    spent_usd: float = 0.0
    calls: int = 0
    log: list[float] = field(default_factory=list)

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.price_input_per_mtok + output_tokens * self.price_output_per_mtok
        ) / 1_000_000

    def reserve(self, input_tokens: int, max_output_tokens: int) -> None:
        worst = self.cost(input_tokens, max_output_tokens)
        if self.spent_usd + worst > self.limit_usd:
            raise BudgetExceeded(
                f"next call could cost ${worst:.4f}; ${self.limit_usd - self.spent_usd:.4f} left of ${self.limit_usd:.2f}"
            )

    def record(self, input_tokens: int, output_tokens: int, reported_cost: float | None = None) -> float:
        """Count a finished call. A backend that reports its own cost (the Claude Code
        CLI) is trusted over our price table, which may be out of date."""
        spent = reported_cost if reported_cost is not None else self.cost(input_tokens, output_tokens)
        self.spent_usd += spent
        self.calls += 1
        self.log.append(spent)
        return spent
