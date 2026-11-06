"""Settings from environment variables. Nothing secret has a default or lives in code."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw in (None, "") else float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw in (None, "") else int(raw)


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///devtrack.db"
    # Optional separate login for the question box. Unset: the main URL is opened
    # with a read-only session instead.
    readonly_database_url: str | None = field(default=None, repr=False)
    github_token: str | None = field(default=None, repr=False)
    github_repos: tuple[str, ...] = ("pallets/click", "psf/requests", "encode/httpx")
    github_max_commits_per_repo: int = 300
    # The Actions workflow file that runs the tests (e.g. "tests.yaml"). Unset: all workflows.
    github_ci_workflow: str | None = None

    # How the model judge is reached: "claude-cli" (Claude Code headless, uses the
    # logged-in subscription; how the published evals were run) or "api" (needs
    # ANTHROPIC_API_KEY).
    checker_backend: str = "claude-cli"
    claude_cli: str = "claude"
    anthropic_api_key: str | None = field(default=None, repr=False)
    checker_model: str = "claude-sonnet-5-5"
    checker_max_input_tokens: int = 12_000
    checker_max_output_tokens: int = 800
    checker_run_budget_usd: float = 2.00
    # USD per million tokens. Defaults are placeholders: set them to your actual price.
    price_input_per_mtok: float = 3.00
    price_output_per_mtok: float = 15.00

    # Approved internal model for controlled repositories (see policy.py). Unset: none.
    internal_model_url: str | None = None
    internal_model_name: str = "internal-approved"

    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        repos = os.environ.get("GITHUB_REPOS")
        return cls(
            database_url=os.environ.get("DATABASE_URL", cls.database_url),
            readonly_database_url=os.environ.get("DATABASE_URL_READONLY") or None,
            github_token=os.environ.get("GITHUB_TOKEN") or None,
            github_repos=tuple(r.strip() for r in repos.split(",") if r.strip())
            if repos
            else cls.github_repos,
            github_max_commits_per_repo=_env_int(
                "GITHUB_MAX_COMMITS_PER_REPO", cls.github_max_commits_per_repo
            ),
            github_ci_workflow=os.environ.get("GITHUB_CI_WORKFLOW") or None,
            checker_backend=os.environ.get("CHECKER_BACKEND", cls.checker_backend),
            claude_cli=os.environ.get("CLAUDE_CLI", cls.claude_cli),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
            checker_model=os.environ.get("CHECKER_MODEL", cls.checker_model),
            checker_max_input_tokens=_env_int("CHECKER_MAX_INPUT_TOKENS", cls.checker_max_input_tokens),
            checker_max_output_tokens=_env_int("CHECKER_MAX_OUTPUT_TOKENS", cls.checker_max_output_tokens),
            checker_run_budget_usd=_env_float("CHECKER_RUN_BUDGET_USD", cls.checker_run_budget_usd),
            price_input_per_mtok=_env_float("CHECKER_PRICE_INPUT_PER_MTOK", cls.price_input_per_mtok),
            price_output_per_mtok=_env_float("CHECKER_PRICE_OUTPUT_PER_MTOK", cls.price_output_per_mtok),
            internal_model_url=os.environ.get("INTERNAL_MODEL_URL") or None,
            internal_model_name=os.environ.get("INTERNAL_MODEL_NAME", cls.internal_model_name),
            log_level=os.environ.get("LOG_LEVEL", cls.log_level),
        )
