"""Full-text search and lexical similarity over criteria and commits.

Full text: SQLite FTS5 (bm25 ranking) or Postgres tsvector/GIN (ts_rank). User input
is never spliced into the query language: FTS5 terms are quoted, Postgres uses
websearch_to_tsquery, which accepts arbitrary text safely.

Similarity: vectors from the lexical embedder (hashed bag of words, see
embeddings/hashing.py) are read back and compared in Python. That is fine at this
scale (a few hundred diffs per repo). It finds shared identifiers and words, not
meaning; the UI calls it "lexical similarity" for that reason.
"""

from __future__ import annotations

import json
import re
from typing import Any

from devtrack.db import Database
from devtrack.embeddings.hashing import Embedder, HashingEmbedder, cosine, diff_text

_WORD = re.compile(r"\w+")


def _fts5_query(text: str) -> str:
    terms = _WORD.findall(text)
    return " ".join(f'"{t}"' for t in terms) or '""'


def search_criteria(db: Database, text: str, limit: int = 10) -> list[dict[str, Any]]:
    if db.dialect == "sqlite":
        sql = (
            "SELECT a.key, a.text, bm25(criteria_fts) AS score FROM criteria_fts"
            " JOIN acceptance_criteria a ON a.id = criteria_fts.rowid"
            " WHERE criteria_fts MATCH ? ORDER BY score LIMIT ?"
        )
        return db.query(sql, (_fts5_query(text), limit))
    sql = (
        "SELECT a.key, a.text, ts_rank(a.search, q) AS score"
        " FROM acceptance_criteria a, websearch_to_tsquery('english', ?) q"
        " WHERE a.search @@ q ORDER BY score DESC LIMIT ?"
    )
    return db.query(sql, (text, limit))


def search_commits(db: Database, text: str, limit: int = 10) -> list[dict[str, Any]]:
    if db.dialect == "sqlite":
        sql = (
            "SELECT c.repo, c.sha, c.message, bm25(commits_fts) AS score FROM commits_fts"
            " JOIN commits c ON c.id = commits_fts.rowid"
            " WHERE commits_fts MATCH ? ORDER BY score LIMIT ?"
        )
        return db.query(sql, (_fts5_query(text), limit))
    sql = (
        "SELECT c.repo, c.sha, c.message, ts_rank(c.search, q) AS score"
        " FROM commits c, websearch_to_tsquery('english', ?) q"
        " WHERE c.search @@ q ORDER BY score DESC LIMIT ?"
    )
    return db.query(sql, (text, limit))


def build_embeddings(db: Database, embedder: Embedder | None = None) -> int:
    """Embed every criterion and commit diff that doesn't have a vector for this model."""
    emb = embedder or HashingEmbedder()
    todo: list[tuple[str, int, str]] = []
    for r in db.query(
        "SELECT a.id, a.text FROM acceptance_criteria a WHERE NOT EXISTS (SELECT 1 FROM embeddings e"
        " WHERE e.entity = 'criterion' AND e.entity_id = a.id AND e.model = ?)",
        (emb.model,),
    ):
        todo.append(("criterion", r["id"], r["text"]))
    for r in db.query(
        "SELECT c.id, c.message, c.diff FROM commits c WHERE NOT c.is_merge AND NOT EXISTS (SELECT 1 FROM"
        " embeddings e WHERE e.entity = 'commit_diff' AND e.entity_id = c.id AND e.model = ?)",
        (emb.model,),
    ):
        todo.append(("commit_diff", r["id"], diff_text(r["message"], r["diff"])))
    with db.transaction():
        for entity, entity_id, text in todo:
            vec = emb.embed(text)
            db.execute(
                "INSERT INTO embeddings (entity, entity_id, model, dim, vector_json) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT DO NOTHING",
                (entity, entity_id, emb.model, emb.dim, db.json(vec)),
            )
    return len(todo)


def _vec(value: Any) -> list[float]:
    return [float(v) for v in (json.loads(value) if isinstance(value, str) else value)]


def similar_commits(
    db: Database,
    criterion_key: str,
    *,
    repo: str | None = None,
    limit: int = 5,
    embedder: Embedder | None = None,
) -> list[dict[str, Any]]:
    """Commits whose diff looks most like the criterion: candidate links for a human."""
    emb = embedder or HashingEmbedder()
    crit = db.query_one(
        "SELECT e.vector_json FROM embeddings e JOIN acceptance_criteria a ON a.id = e.entity_id"
        " WHERE e.entity = 'criterion' AND e.model = ? AND a.key = ?",
        (emb.model, criterion_key),
    )
    if crit is None:
        return []
    target = _vec(crit["vector_json"])
    repo_filter = " AND c.repo = ?" if repo else ""
    params: list[Any] = [emb.model] + ([repo] if repo else [])
    rows = db.query(
        "SELECT c.repo, c.sha, c.message, e.vector_json FROM embeddings e JOIN commits c ON c.id = e.entity_id"
        f" WHERE e.entity = 'commit_diff' AND e.model = ?{repo_filter}",
        params,
    )
    scored = [
        {
            "repo": r["repo"],
            "sha": r["sha"],
            "message": r["message"],
            "similarity": round(cosine(target, _vec(r["vector_json"])), 4),
        }
        for r in rows
    ]
    scored.sort(key=lambda r: r["similarity"], reverse=True)
    return scored[:limit]
