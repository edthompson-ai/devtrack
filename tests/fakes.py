"""Test doubles for the GitHub API: a scripted session and an in-memory fake repo."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

API = "https://api.github.com"


@dataclass
class FakeResponse:
    status_code: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        if isinstance(self.body, (str, bytes)):
            return json.loads(self.body)
        return self.body

    @property
    def text(self) -> str:
        return self.body if isinstance(self.body, str) else json.dumps(self.body)


class ScriptedSession:
    """Returns pre-scripted responses in order; records every call."""

    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, *, params: dict[str, Any] | None = None, timeout: float = 0) -> FakeResponse:
        self.calls.append((url, params))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeGitHub:
    """A tiny in-memory GitHub REST API for one or more repos.

    Supports the endpoints the fetchers use, ``per_page``/``page`` pagination with
    Link headers, ``since`` on commits and issues, sort=updated on pulls, Actions runs
    (wrapped in ``{"workflow_runs": [...]}`` with a ``created=>=`` filter), releases,
    and ``commits/{tag}`` resolving a tag to its commit.
    """

    def __init__(self, page_size: int = 2) -> None:
        self.page_size = page_size
        self.repos: dict[str, dict[str, Any]] = {}
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def add_repo(self, repo: str) -> dict[str, Any]:
        self.repos[repo] = {
            "commits": [],
            "pulls": [],
            "pr_commits": {},
            "reviews": {},
            "issues": [],
            "runs": [],
            "releases": [],
            "tags": {},
        }
        return self.repos[repo]

    def get(self, url: str, *, params: dict[str, Any] | None = None, timeout: float = 0) -> FakeResponse:
        parsed = urlparse(url)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        query.update({k: str(v) for k, v in (params or {}).items()})
        self.calls.append((parsed.path, query))
        parts = parsed.path.strip("/").split("/")  # repos/{o}/{r}/...
        repo = f"{parts[1]}/{parts[2]}"
        data = self.repos[repo]
        rest = parts[3:]
        if rest == ["commits"]:
            items = sorted(data["commits"], key=lambda c: c["commit"]["committer"]["date"], reverse=True)
            if "since" in query:
                items = [c for c in items if c["commit"]["committer"]["date"] >= query["since"]]
            return self._page(url, query, [{"sha": c["sha"]} for c in items])
        if rest[:1] == ["commits"] and len(rest) == 2 and rest[1] in data["tags"]:
            return FakeResponse(200, {"sha": data["tags"][rest[1]]})
        if rest[:1] == ["commits"] and len(rest) == 2:
            commit = next(c for c in data["commits"] if c["sha"] == rest[1])
            return FakeResponse(200, json.loads(json.dumps(commit)))
        if rest == ["pulls"]:
            items = sorted(data["pulls"], key=lambda p: p["updated_at"], reverse=True)
            return self._page(url, query, items)
        if rest[:1] == ["pulls"] and rest[2:] == ["commits"]:
            shas = data["pr_commits"].get(int(rest[1]), [])
            return self._page(url, query, [{"sha": s} for s in shas])
        if rest[:1] == ["pulls"] and rest[2:] == ["reviews"]:
            return self._page(url, query, data["reviews"].get(int(rest[1]), []))
        if rest == ["issues"]:
            items = data["issues"]
            if "since" in query:
                items = [i for i in items if i["updated_at"] >= query["since"]]
            return self._page(url, query, items)
        if rest == ["actions", "runs"] or (rest[:2] == ["actions", "workflows"] and rest[3:] == ["runs"]):
            runs = sorted(data["runs"], key=lambda r: r["created_at"], reverse=True)
            if rest[:2] == ["actions", "workflows"]:
                runs = [r for r in runs if r.get("path", "").endswith(rest[2])]
            if "created" in query:
                runs = [r for r in runs if r["created_at"] >= query["created"].removeprefix(">=")]
            page = self._page(url, query, runs)
            page.body = {"total_count": len(runs), "workflow_runs": page.body}
            return page
        if rest == ["releases"]:
            return self._page(
                url, query, sorted(data["releases"], key=lambda r: r["published_at"], reverse=True)
            )
        return FakeResponse(404, {"message": "Not Found"})

    def _page(self, url: str, query: dict[str, Any], items: list[Any]) -> FakeResponse:
        page = int(query.get("page", 1))
        start = (page - 1) * self.page_size
        chunk = json.loads(json.dumps(items[start : start + self.page_size]))
        headers = {"X-RateLimit-Remaining": "4999", "X-RateLimit-Reset": "0"}
        if start + self.page_size < len(items):
            base = url.split("?")[0]
            nxt = {**query, "page": str(page + 1)}
            headers["Link"] = f'<{base}?{"&".join(f"{k}={v}" for k, v in nxt.items())}>; rel="next"'
        return FakeResponse(200, chunk, headers)


def api_commit(
    sha: str,
    date: str,
    message: str,
    *,
    login: str | None = "octocat",
    email: str = "octo@example.com",
    files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "sha": sha,
        "commit": {
            "author": {"name": "Octo Cat", "email": email, "date": date},
            "committer": {"name": "Octo Cat", "email": email, "date": date},
            "message": message,
        },
        "author": {"login": login} if login else None,
        "parents": [{"sha": "f" * 40}],
        "stats": {"additions": 1, "deletions": 0, "total": 1},
        "files": files
        if files is not None
        else [{"filename": "src/app.py", "additions": 1, "deletions": 0, "patch": "@@ -1 +1,2 @@\n+x = 1"}],
    }


def connection_error() -> Exception:
    return requests.ConnectionError("connection reset")


def api_run(
    run_id: int,
    run_number: int,
    sha: str,
    created: str,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    name: str = "Tests",
    path: str = ".github/workflows/tests.yaml",
) -> dict[str, Any]:
    """A GitHub Actions workflow run, trimmed to the fields the normalizer reads."""
    return {
        "id": run_id,
        "name": name,
        "path": path,
        "run_number": run_number,
        "head_sha": sha,
        "head_branch": "main",
        "event": "push",
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "created_at": created,
        "run_started_at": created,
        "updated_at": _plus_minutes(created, 9) if status == "completed" else created,
        "html_url": f"https://github.com/acme/app/actions/runs/{run_id}",
    }


def _plus_minutes(stamp: str, minutes: int) -> str:
    import datetime as dt

    moment = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")) + dt.timedelta(minutes=minutes)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
