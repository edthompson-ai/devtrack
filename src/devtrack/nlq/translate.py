"""Turn a question into safe, read-only SQL.

Templates only: common questions ("what's untested in feature X", "commits without a
task", "trace REQ-HTTPX-01", ...) map to vetted, parameterised SQL. Feature and repo
names are matched against the database, never pasted in. Every statement still goes
through the validator and runs on a read-only connection, as defence in depth.

An earlier version let a model write SQL for questions no template covered. That was
cut before production (DESIGN-DECISIONS.md, section 16): model-written SQL against a live
database needs its own security review, and the templates cover the questions people
actually asked. A semantic layer (named metrics the model picks from, instead of raw
SQL) is the planned way back.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from devtrack.db import Database
from devtrack.nlq.safe_sql import CheckedSQL, UnsafeSQL, check_sql
from devtrack.queries.saved import get as saved_query


@dataclass
class Translation:
    question: str
    sql: str
    params: tuple[Any, ...]
    method: str  # "template:<name>"
    explanation: str
    checked: CheckedSQL | None = None


@dataclass
class Answer:
    translation: Translation | None
    rows: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    suggestions: list[str] = field(default_factory=list)


EXAMPLES = [
    "What's untested in feature Pager and terminal output reliability?",
    "Which criteria have no code?",
    "Which commits aren't tied to a task in encode/httpx?",
    "What is the cycle time per story?",
    "Which requirements changed after the code merged?",
    "Who is the slowest reviewer?",
    "Trace REQ-HTTPX-01",
    "Which criteria did the checker fail?",
]


def _match_feature(db: Database, question: str) -> dict[str, Any] | None:
    features = db.query("SELECT id, key, title FROM features")
    q = question.lower()
    for f in features:
        if f["key"].lower() in q:
            return f
    words = set(re.findall(r"[a-z]{4,}", q)) - {"feature", "what", "which", "untested", "tests", "code"}
    best, best_score = None, 0.0
    for f in features:
        title_words = set(re.findall(r"[a-z]{4,}", f["title"].lower()))
        overlap = len(words & title_words)
        ratio = difflib.SequenceMatcher(None, q, f["title"].lower()).ratio()
        score = overlap + ratio
        if overlap and score > best_score:
            best, best_score = f, score
    return best


def _match_repo(db: Database, question: str) -> str | None:
    q = question.lower()
    for r in db.query("SELECT DISTINCT repo FROM commits"):
        repo = r["repo"]
        if repo.lower() in q or repo.split("/")[-1].lower() in re.findall(r"[a-z0-9_.-]+", q):
            return str(repo)
    return None


def _template(db: Database, question: str) -> Translation | None:
    q = question.lower().strip()
    if m := re.search(r"\b(req-[a-z]+-\d+)\b", q):
        key = m.group(1).upper()
        return Translation(
            question,
            saved_query("full_trace").sql(db.dialect),
            (key,),
            "template:full_trace",
            f"Full trace for {key}: criteria, tasks, commits, PRs and reviews.",
        )
    if re.search(
        r"\b(untested|no tests?|not tested|without tests?|no code|unimplemented|not implemented|gaps?)\b", q
    ):
        feature = _match_feature(db, question)
        want_tests = bool(re.search(r"test", q))
        want_code = bool(re.search(r"code|implement", q))
        condition = (
            "linked_tests = 0"
            if want_tests and not want_code
            else "linked_commits = 0"
            if want_code and not want_tests
            else "linked_tests = 0 OR linked_commits = 0"
        )
        base = saved_query("criteria_without_code_or_tests").sql(db.dialect).rstrip().rstrip(";")
        base = base[: base.rfind("ORDER BY")]
        sql = f"SELECT * FROM (\n{base}\n) AS gaps WHERE ({condition})"
        params: tuple[Any, ...] = ()
        if feature:
            sql += " AND feature = ?"
            params = (feature["key"],)
        sql += " ORDER BY criterion"
        scope = f"in {feature['key']} ({feature['title']})" if feature else "across all features"
        return Translation(
            question,
            sql,
            params,
            "template:criteria_gaps",
            f"Acceptance criteria {scope} with {condition.replace('linked_', 'no linked ').replace(' = 0', '')}.",
        )
    if re.search(
        r"\bcommits?\b.*\b(without|no|not|aren't|isn't|missing|orphan|unlinked)\b.*\btasks?\b", q
    ) or ("orphan" in q and "commit" in q):
        repo = _match_repo(db, question)
        base = saved_query("commits_without_task").sql(db.dialect).rstrip().rstrip(";")
        base = base[: base.rfind("ORDER BY")]
        sql = f"SELECT * FROM (\n{base}\n) AS orphans"
        params = ()
        if repo:
            sql += " WHERE repo = ?"
            params = (repo,)
        sql += " ORDER BY committed_at DESC"
        return Translation(
            question,
            sql,
            params,
            "template:commits_without_task",
            "Non-merge commits not linked to any task" + (f" in {repo}." if repo else "."),
        )
    if "cycle time" in q or "lead time" in q:
        return Translation(
            question,
            saved_query("cycle_time").sql(db.dialect),
            (),
            "template:cycle_time",
            "Hours from story start to the merge of its last PR.",
        )
    if re.search(r"requirements?\b.*\bchanged\b", q):
        return Translation(
            question,
            saved_query("requirements_changed_after_merge").sql(db.dialect),
            (),
            "template:requirements_changed_after_merge",
            "Criteria whose requirement changed after their code merged.",
        )
    if "turnaround" in q or ("review" in q and re.search(r"\b(slow|fast|time|quick|long)", q)):
        return Translation(
            question,
            saved_query("review_turnaround").sql(db.dialect),
            (),
            "template:review_turnaround",
            "Hours from PR opened to each reviewer's first review.",
        )
    if re.search(r"\b(fail(ed|ing|s)?|did not pass|didn't pass)\b", q) and re.search(r"criteri|check", q):
        sql = (
            "SELECT a.key AS criterion, a.text, v.verdict, v.confidence, v.status, v.model, v.prompt_version"
            " FROM ai_verdicts v JOIN acceptance_criteria a ON a.id = v.criterion_id"
            " WHERE v.verdict = 'fail' AND v.id IN (SELECT MAX(id) FROM ai_verdicts GROUP BY criterion_id)"
            " ORDER BY a.key"
        )
        return Translation(
            question,
            sql,
            (),
            "template:failed_criteria",
            "Latest checker verdict per criterion, where it was a fail (including ones awaiting review).",
        )
    return None


def translate(db: Database, question: str) -> Translation:
    question = question.strip()[:500]
    if not question:
        raise UnsafeSQL("ask a question")
    translation = _template(db, question)
    if translation is None:
        raise LookupError("no template matches")
    translation.checked = check_sql(translation.sql)
    return translation


def ask(db: Database, read_only_db: Database, question: str) -> Answer:
    """Translate, validate and run a question on the read-only connection."""
    try:
        t = translate(db, question)
    except LookupError:
        return Answer(None, error="I don't have a template for that question.", suggestions=EXAMPLES)
    except (UnsafeSQL, ValueError) as exc:
        return Answer(None, error=f"Refused: {exc}", suggestions=EXAMPLES)
    assert t.checked is not None
    try:
        rows = read_only_db.query(t.checked.sql, t.params)
    except Exception as exc:
        return Answer(t, error=f"The query failed: {type(exc).__name__}: {str(exc)[:200]}")
    return Answer(t, rows=rows)
