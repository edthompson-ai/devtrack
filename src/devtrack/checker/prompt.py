"""Versioned prompt templates. A prompt version is immutable once results exist for it:
to change wording, add v3 rather than editing v2, so eval runs stay comparable."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

from devtrack.checker.context import CriterionContext
from devtrack.checker.guardrails import fence, new_nonce

PROMPT_DIR = Path(__file__).parent / "prompts"
DEFAULT_VERSION = "v2"


@dataclass(frozen=True)
class RenderedPrompt:
    version: str
    system: str
    user: str
    nonce: str = ""

    def cache_text(self) -> str:
        """The prompt with the per-call nonce removed: identical inputs share a cache key."""
        return self.system + "\n" + self.user.replace(self.nonce, "")


@cache
def versions() -> list[str]:
    return sorted({p.name.split(".")[0] for p in PROMPT_DIR.glob("v*.system.txt")})


def render(
    ctx: CriterionContext, version: str = DEFAULT_VERSION, *, nonce: str | None = None
) -> RenderedPrompt:
    if version not in versions():
        raise ValueError(f"unknown prompt version {version!r}; have {versions()}")
    nonce = nonce or new_nonce()
    system = (PROMPT_DIR / f"{version}.system.txt").read_text().strip()
    template = (PROMPT_DIR / f"{version}.user.txt").read_text()
    blocks = []
    for c in ctx.commits:
        prs = ", ".join(f"#{n}" for n in c.pr_numbers) or "none"
        body = f"commit {c.sha[:10]} ({c.repo}, {c.committed_at}, PR {prs})\nmessage:\n{c.message}\n\ndiff:\n{c.diff}"
        blocks.append(fence(body, "commit", nonce))
    user = template.format(
        feature=ctx.feature,
        story=ctx.story,
        requirements="\n".join(f"- {r}" for r in ctx.requirements) or "- (none linked)",
        criterion_key=ctx.key,
        criterion_text=ctx.text,
        tasks=", ".join(f"{t['key']} ({t['status']}): {t['title']}" for t in ctx.tasks) or "none",
        tests=", ".join(ctx.tests) or "none linked",
        n_commits=len(ctx.commits),
        truncation_note="; some diffs truncated" if ctx.truncated else "",
        commits="\n\n".join(blocks) or "(no commits linked)",
    )
    return RenderedPrompt(version, system, user, nonce)
