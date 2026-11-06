"""One place that defines every source_id format, so the fetchers, the seed loader and
the normalizer can't drift apart. IDs are namespaced and deterministic: fetching the
same object twice always yields the same key, which is what makes upserts idempotent."""

from __future__ import annotations

import hashlib


def commit(repo: str, sha: str) -> str:
    return f"{repo}@{sha}"


def pull_request(repo: str, number: int) -> str:
    return f"{repo}#pr-{number}"


def pr_commits(repo: str, number: int) -> str:
    return f"{repo}#pr-{number}/commits"


def review(repo: str, number: int, review_id: int | str) -> str:
    return f"{repo}#pr-{number}/review-{review_id}"


def issue(repo: str, number: int) -> str:
    return f"{repo}#issue-{number}"


def release(repo: str, tag: str) -> str:
    return f"{repo}@release-{tag}"


def workflow_run(repo: str, run_id: int | str) -> str:
    return f"{repo}#run-{run_id}"


def test_case(repo: str, path: str, name: str) -> str:
    return f"{repo}:{path}::{name}"


def email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def person_from_login(login: str) -> str:
    return f"login:{login.lower()}"


def person_from_email(email: str) -> str:
    return f"email:{email_hash(email)[:16]}"
