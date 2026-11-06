"""Phase 3: saved queries return the gaps planted in the seed; indexes are used;
search and similarity work."""

from __future__ import annotations

import pytest

from devtrack.db import Database
from devtrack.embeddings.hashing import HashingEmbedder, cosine, tokens
from devtrack.ingest.seed import load_seed
from devtrack.queries.saved import all_queries, expand, get
from devtrack.search import build_embeddings, search_commits, search_criteria, similar_commits


@pytest.fixture
def loaded(db: Database) -> Database:
    load_seed(db)
    return db


def test_six_saved_queries_are_registered() -> None:
    assert [q.number for q in all_queries().values()] == [1, 2, 3, 4, 5, 6]


def test_hours_macro_expands_per_dialect() -> None:
    sql = "SELECT HOURS_BETWEEN(s.started_at, pr.merged_at)"
    assert "julianday(pr.merged_at) - julianday(s.started_at)" in expand(sql, "sqlite")
    assert "EXTRACT(EPOCH FROM (pr.merged_at - s.started_at))" in expand(sql, "postgres")


def test_q1_finds_criteria_with_no_code(loaded: Database) -> None:
    rows = get("criteria_without_code_or_tests").run(loaded)
    no_code = {r["criterion"] for r in rows if r["gap"] == "no code, no tests"}
    assert no_code == {"AC-CLICK-201-4", "AC-REQ-203-4", "AC-HTTPX-203-3"}
    # criteria that have both code and a linked test are not gaps
    keys = {r["criterion"] for r in rows}
    assert "AC-CLICK-101-3" not in keys and "AC-HTTPX-203-1" not in keys
    assert rows[0]["gap"] == "no code, no tests"  # worst gaps first


def test_q2_finds_commits_with_no_task(loaded: Database) -> None:
    rows = get("commits_without_task").run(loaded)
    messages = {r["message"].splitlines()[0] for r in rows}
    assert "Start 8.6.0." in messages  # real commit in PR #3866 that no task claims
    assert "Tweak retry timing in test helper" in messages  # synthetic stray commit
    shas = {r["sha"] for r in rows}
    linked = {
        r["sha"]
        for r in loaded.query(
            "SELECT DISTINCT c.sha FROM commits c JOIN commit_tasks ct ON ct.commit_id = c.id"
        )
    }
    assert not shas & linked
    marks = ",".join("?" * len(shas))
    assert not loaded.query(f"SELECT 1 FROM commits WHERE is_merge AND sha IN ({marks})", tuple(shas))


def test_q3_cycle_time_matches_hand_calculation(loaded: Database) -> None:
    rows = {r["story"]: r for r in get("cycle_time").run(loaded)}
    st = rows["ST-CLICK-101"]
    # started 2026-08-10T14:00Z, last linked PR (#3777) merged 2026-08-19T15:20:27Z
    assert st["last_merge"] == "2026-08-19T15:20:27Z"
    assert float(st["cycle_hours"]) == pytest.approx(217.3, abs=0.05)
    assert st["prs"] == 4


def test_q4_requirements_changed_after_merge(loaded: Database) -> None:
    rows = get("requirements_changed_after_merge").run(loaded)
    assert {r["requirement"] for r in rows} == {"REQ-CLICK-01", "REQ-REQ-02", "REQ-HTTPX-01"}
    httpx = {r["criterion"] for r in rows if r["requirement"] == "REQ-HTTPX-01"}
    # 101-6 (docs, merged 2025-09) came after the requirement changed (2025-01): not stale
    assert "AC-HTTPX-101-6" not in httpx and "AC-HTTPX-101-3" in httpx
    assert all(r["requirement_changed_at"] > r["code_merged_at"] for r in rows)


def test_q5_review_turnaround_per_reviewer(loaded: Database) -> None:
    rows = get("review_turnaround").run(loaded)
    assert {r["reviewer"] for r in rows} == {"Jamal Wright", "Nina Kowalski", "Sofia Reyes"}
    for r in rows:
        assert 0 <= float(r["fastest_hours"]) <= float(r["avg_hours"]) <= float(r["slowest_hours"])


def test_q6_full_trace_walks_every_hop(loaded: Database) -> None:
    rows = get("full_trace").run(loaded, "REQ-HTTPX-01")
    assert {r["criterion"] for r in rows} == {f"AC-HTTPX-101-{i}" for i in range(1, 7)}
    row = next(r for r in rows if r["criterion"] == "AC-HTTPX-101-2")
    assert row["task"] == "TASK-HTTPX-1012" and row["pr_number"] == 3418
    assert row["reviewer"] is not None and row["review_state"] in {
        "APPROVED",
        "COMMENTED",
        "CHANGES_REQUESTED",
    }


def test_trace_keeps_gaps_visible(loaded: Database) -> None:
    rows = get("full_trace").run(loaded, "REQ-CLICK-03")
    codemod = [r for r in rows if r["criterion"] == "AC-CLICK-201-4"]
    assert codemod and codemod[0]["task"] == "TASK-CLICK-2013" and codemod[0]["sha"] is None


def test_sqlite_plans_use_the_link_indexes(sqlite_db: Database) -> None:
    load_seed(sqlite_db)
    plan = " ".join(
        r["detail"]
        for r in sqlite_db.query("EXPLAIN QUERY PLAN SELECT commit_id FROM commit_tasks WHERE task_id = 5")
    )
    assert "ix_commit_tasks_rev" in plan
    plan = " ".join(
        r["detail"]
        for r in sqlite_db.query(
            "EXPLAIN QUERY PLAN SELECT task_id FROM task_criteria WHERE criterion_id = 5"
        )
    )
    assert "ix_task_criteria_rev" in plan


def test_full_text_search(loaded: Database) -> None:
    hits = search_criteria(loaded, "PowerShell completion")
    assert hits and hits[0]["key"].startswith("AC-CLICK-203")
    commits = search_commits(loaded, "zstd")
    assert any("zstd" in c["message"].lower() for c in commits)
    # hostile input is treated as plain words, never as query syntax
    assert isinstance(search_criteria(loaded, 'quote" OR 1=1 -- ); DROP TABLE x'), list)
    assert isinstance(search_commits(loaded, "NEAR(a b) * ^ :"), list)


def test_hashing_embedder_is_deterministic_and_lexical() -> None:
    emb = HashingEmbedder()
    a = emb.embed("Strip ANSI codes from prompt()")
    assert a == emb.embed("Strip ANSI codes from prompt()")
    assert tokens("strip_ansi TextWrapper") == ["strip", "ansi", "text", "wrapper"]
    near = cosine(a, emb.embed("def strip_ansi(prompt): remove codes"))
    far = cosine(a, emb.embed("Handle empty zstd responses"))
    assert near > far


def test_similar_commits_suggests_candidates(loaded: Database) -> None:
    assert build_embeddings(loaded) > 0
    assert build_embeddings(loaded) == 0  # incremental: nothing left to embed
    hits = similar_commits(loaded, "AC-HTTPX-203-1", repo="encode/httpx")
    assert hits and any("zstd" in h["message"].lower() for h in hits[:3])
