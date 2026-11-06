#!/usr/bin/env python3
"""Build a seed snapshot of real public repository data from a local git clone.

Why this exists: the demo must work offline and without a GitHub token, but it should
run on real code, not invented commits. Git history already contains the commits, their
diffs and (through merge commits and squash subjects) the pull requests they belong to.
This script turns that history into JSON shaped exactly like the GitHub REST API
responses, so the seed goes through the very same staging -> normalize path as a live
`devtrack sync`.

What git can and can't tell us (recorded in each record's ``_provenance``):
  * commits, authors, dates, messages, file stats, patches: real.
  * PR number, title, merge time, merge commit, member commits: real (from the merge
    commit or the "(#123)" squash subject).
  * PR created_at: derived as the earliest commit date in the PR for merge-commit PRs;
    left empty for squash merges, where git keeps no trace of when the PR was opened.
  * Releases: real, from the repository's tags (name, date, target commit).
  * PR author login, reviews, issues, CI results: not in git. Reviews in the demo are synthetic and
    flagged as such (backlog/synthetic.py); a live sync pulls the real ones.
  * Author emails are replaced by a sha256 hash, and emails inside commit messages
    (Co-authored-by / Signed-off-by trailers) are masked, so no personal email
    address enters the seed files.

Usage:
    python scripts/build_seed_from_git.py <clone_dir> <owner/repo> [--prs 45] [--out seed/github]

The clone must have enough history (e.g. ``git clone --depth 400``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

PR_SQUASH = re.compile(r"^(?P<title>.+?)\s+\(#(?P<num>\d+)\)$")
PR_MERGE = re.compile(r"^Merge pull request #(?P<num>\d+) from (?P<branch>\S+)")
SKIP_TITLE = re.compile(r"^(Bump |\[pre-commit\.ci\]|Merge (branch|stable|remote)|Update .*lock)", re.I)
MAX_PATCH_BYTES = 12_000
MAX_PR_COMMITS = 25
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def git(clone: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(clone), *args], check=True, capture_output=True, text=True, errors="replace"
    ).stdout


def utc(iso: str) -> str:
    return dt.datetime.fromisoformat(iso).astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def exists(clone: Path, rev: str) -> bool:
    return (
        subprocess.run(["git", "-C", str(clone), "cat-file", "-e", rev], capture_output=True).returncode == 0
    )


def commit_detail(clone: Path, sha: str, *, with_patch: bool = True) -> dict[str, Any]:
    sep = "\x1f"
    fmt = sep.join(["%H", "%P", "%an", "%ae", "%aI", "%cn", "%ce", "%cI", "%B"])
    head = git(clone, "show", "-s", f"--format={fmt}", sha).split(sep)
    full, parents, an, ae, ad, cn, ce, cd, body = head
    files: list[dict[str, Any]] = []
    additions = deletions = 0
    is_merge = len(parents.split()) > 1
    if not is_merge:
        numstat = git(clone, "show", "--format=", "--numstat", sha)
        patches = (
            split_patch(git(clone, "show", "--format=", "--patch", "--no-color", sha)) if with_patch else {}
        )
        for line in numstat.strip().splitlines():
            a, d, name = line.split("\t", 2)
            add = 0 if a == "-" else int(a)
            dele = 0 if d == "-" else int(d)
            additions += add
            deletions += dele
            entry: dict[str, Any] = {
                "filename": name,
                "additions": add,
                "deletions": dele,
                "changes": add + dele,
            }
            patch = patches.get(name)
            if patch is not None:
                if len(patch) > MAX_PATCH_BYTES:
                    patch = patch[:MAX_PATCH_BYTES] + "\n... [patch truncated by seed builder]"
                entry["patch"] = patch
            files.append(entry)
    return {
        "sha": full,
        "commit": {
            "author": {"name": an, "email_sha256": email_hash(ae), "date": utc(ad)},
            "committer": {"name": cn, "email_sha256": email_hash(ce), "date": utc(cd)},
            "message": EMAIL.sub("[email]", body.strip()),  # trailers carry emails
        },
        "author": None,  # GitHub login: not knowable from git
        "parents": [{"sha": p} for p in parents.split()],
        "stats": {"additions": additions, "deletions": deletions, "total": additions + deletions},
        "files": files,
        "_provenance": "git-history",
    }


def split_patch(text: str) -> dict[str, str]:
    """Split `git show --patch` output into {filename: hunk text}, like the API's files[].patch."""
    out: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("diff --git "):
            if current is not None:
                out[current] = "\n".join(buf)
            match = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            current = match.group(2) if match else None
            buf = []
        elif current is not None and (buf or line.startswith("@@")):
            buf.append(line)
    if current is not None:
        out[current] = "\n".join(buf)
    return out


def find_prs(clone: Path, limit: int) -> list[dict[str, Any]]:
    """Walk first-parent history newest first and pick out merged PRs."""
    sep = "\x1f"
    log = git(clone, "log", "--first-parent", f"--format=%H{sep}%P{sep}%s{sep}%b{sep}%cI\x1e")
    prs: list[dict[str, Any]] = []
    for record in log.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        sha, parents, subject, body, cdate = record.split(sep)
        parent_list = parents.split()
        number = title = branch = None
        member_shas: list[str] = []
        if len(parent_list) == 2:
            m = PR_MERGE.match(subject)
            s = PR_SQUASH.match(subject)
            if m:
                number, branch = int(m.group("num")), m.group("branch")
                title = next((ln.strip() for ln in body.splitlines() if ln.strip()), subject)
            elif s:
                number, title = int(s.group("num")), s.group("title")
            if number is None or not exists(clone, parent_list[1]) or not exists(clone, parent_list[0]):
                continue
            member_shas = git(
                clone, "rev-list", "--no-merges", "--reverse", f"{parent_list[0]}..{parent_list[1]}"
            ).split()
        elif len(parent_list) == 1:
            s = PR_SQUASH.match(subject)
            if not s:
                continue
            number, title = int(s.group("num")), s.group("title")
            member_shas = [sha]
        else:
            continue
        if not member_shas or len(member_shas) > MAX_PR_COMMITS or SKIP_TITLE.search(title or ""):
            continue
        if branch and branch.split("/", 1)[-1].startswith("dependabot"):
            continue
        prs.append(
            {
                "number": number,
                "title": title,
                "merge_sha": sha,
                "merged_at": utc(cdate),
                "branch": branch,
                "members": member_shas,
                "is_squash": len(parent_list) == 1,
            }
        )
        if len(prs) >= limit:
            break
    return prs


def find_releases(clone: Path, since: str) -> list[dict[str, Any]]:
    """Release tags created on or after ``since``. For annotated tags the peeled
    object is the commit; for lightweight tags the tag points at the commit itself."""
    sep = "\x1f"
    out = git(
        clone,
        "for-each-ref",
        "refs/tags",
        "--sort=creatordate",
        f"--format=%(refname:short){sep}%(creatordate:iso-strict){sep}%(objectname){sep}%(*objectname)",
    )
    releases = []
    for line in out.split("\n"):
        if not line:
            continue
        # str.strip() would treat the \x1f separator as whitespace, so pad instead.
        tag, created, obj, peeled = [*line.split(sep), "", "", "", ""][:4]
        when = utc(created)
        if when < since:
            continue
        releases.append(
            {"tag_name": tag, "target_sha": peeled or obj, "published_at": when, "_provenance": "git-tag"}
        )
    return releases


def build(clone: Path, repo: str, limit: int) -> dict[str, Any]:
    prs = find_prs(clone, limit)
    commits: dict[str, dict[str, Any]] = {}
    pulls: list[dict[str, Any]] = []
    pr_commits: list[dict[str, Any]] = []
    for pr in prs:
        for sha in pr["members"]:
            if sha not in commits:
                commits[sha] = commit_detail(clone, sha)
        if not pr["is_squash"] and pr["merge_sha"] not in commits:
            commits[pr["merge_sha"]] = commit_detail(clone, pr["merge_sha"], with_patch=False)
        # A merge-commit PR's branch commits show roughly when work started. A squash
        # merge rewrites history into one commit stamped at merge time, so the open
        # time is unknown: record None rather than a made-up value.
        first = (
            None if pr["is_squash"] else min(commits[s]["commit"]["author"]["date"] for s in pr["members"])
        )
        tip = commits[pr["members"][-1]]["commit"]["author"]
        pulls.append(
            {
                "number": pr["number"],
                "title": pr["title"],
                "body": None,
                "state": "closed",
                "created_at": first,
                "updated_at": pr["merged_at"],
                "merged_at": pr["merged_at"],
                "closed_at": pr["merged_at"],
                "merge_commit_sha": pr["merge_sha"],
                "user": None,
                "head": {"ref": (pr["branch"] or "").split("/", 1)[-1] or None},
                "base": {"ref": "main"},
                "_git_author": {"name": tip["name"], "email_sha256": tip["email_sha256"]},
                "_provenance": "git-history",
                "_derived": {
                    "created_at": "unknown (squash merge)"
                    if pr["is_squash"]
                    else "earliest commit author date in the PR"
                },
            }
        )
        pr_commits.append({"number": pr["number"], "shas": pr["members"]})
    earliest = min(c["commit"]["committer"]["date"] for c in commits.values())
    return {
        "repo": repo,
        "releases": find_releases(clone, earliest),
        "built_from": git(clone, "rev-parse", "HEAD").strip(),
        "built_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commits": sorted(commits.values(), key=lambda c: c["commit"]["committer"]["date"]),
        "pulls": pulls,
        "pr_commits": pr_commits,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clone", type=Path)
    ap.add_argument("repo")
    ap.add_argument("--prs", type=int, default=45)
    ap.add_argument("--out", type=Path, default=Path("seed/github"))
    args = ap.parse_args()
    data = build(args.clone, args.repo, args.prs)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / (args.repo.replace("/", "__") + ".json.gz")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f, sort_keys=True)
    print(f"{args.repo}: {len(data['pulls'])} PRs, {len(data['commits'])} commits -> {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
