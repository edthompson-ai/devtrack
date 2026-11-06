"""Phase 6: every dashboard page renders on seeded data, and the review form is safe."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from devtrack.checker.guardrails import Budget
from devtrack.checker.judges import HeuristicJudge
from devtrack.checker.run import check_many
from devtrack.config import Settings
from devtrack.db import connect, migrate
from devtrack.evals.run import load_labels, run_eval
from devtrack.ingest.seed import DEFAULT_SEED_DIR, load_seed
from devtrack.search import build_embeddings
from devtrack.web.app import create_app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[FlaskClient]:
    url = f"sqlite:///{tmp_path / 'web.db'}"
    db = connect(url)
    migrate(db)
    load_seed(db)
    load_labels(db, DEFAULT_SEED_DIR / "eval_labels.json")
    build_embeddings(db)
    keys = [r["key"] for r in db.query("SELECT key FROM acceptance_criteria")]
    check_many(db, keys, HeuristicJudge(), Budget(1, 3, 15))
    run_eval(db, HeuristicJudge(), Budget(1, 3, 15))
    db.close()
    app = create_app(Settings(database_url=url))
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.mark.parametrize(
    ("path", "expect"),
    [
        ("/", "criteria with no linked code"),
        ("/", "requirements fully verified on the latest build"),
        ("/verification", "Needs re-verification"),
        ("/verification", "SUB-HTTP-01"),
        ("/audit", "Chain intact"),
        ("/audit?key=AC-REQ-202-1", "verification.invalidated"),
        ("/verification", "build #1049"),
        ("/verification?target=pallets/click@8.5.0", "(8.5.0)"),
        ("/trace?req=REQ-CLICK-01", "Failed · build #1049"),
        ("/trace?req=REQ-HTTPX-01", "AC-HTTPX-101-3"),
        ("/gaps", "AC-CLICK-201-4"),
        ("/verdicts", "pending review"),
        ("/verdicts?status=pending_review", "Confirm fail"),
        ("/evals", "Confusion matrix"),
        ("/performance", "Query performance"),
        ("/queries?q=cycle_time", "ST-CLICK-101"),
        ("/queries?q=full_trace&arg=REQ-CLICK-01", "AC-CLICK-101-1"),
        ("/ask?q=Which+criteria+have+no+code%3F", "AC-HTTPX-203-3"),
        ("/ask?q=drop+table+features", "template"),
    ],
)
def test_pages_render(client: FlaskClient, path: str, expect: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 200
    assert expect in resp.get_data(as_text=True)


def test_repo_text_is_escaped(client: FlaskClient) -> None:
    html = client.get("/verdicts").get_data(as_text=True)
    assert "<script" not in html.lower()


def _pending_id_and_token(client: FlaskClient) -> tuple[str, str]:
    html = client.get("/verdicts?status=pending_review").get_data(as_text=True)
    action = re.search(r'action="/verdicts/(\d+)/review', html)
    token = re.search(r'name="csrf" value="([^"]+)"', html)
    assert action and token
    return action.group(1), token.group(1)


def test_review_requires_csrf_token(client: FlaskClient) -> None:
    vid, _ = _pending_id_and_token(client)
    resp = client.post(f"/verdicts/{vid}/review", data={"decision": "approved", "reviewer": "maya"})
    assert resp.status_code == 400


def test_review_flow(client: FlaskClient) -> None:
    vid, token = _pending_id_and_token(client)
    resp = client.post(
        f"/verdicts/{vid}/review",
        data={"csrf": token, "decision": "approved", "reviewer": ""},
        follow_redirects=True,
    )
    assert "reviewer name is required" in resp.get_data(as_text=True)
    resp = client.post(
        f"/verdicts/{vid}/review",
        data={"csrf": token, "decision": "approved", "reviewer": "maya"},
        follow_redirects=True,
    )
    assert f"Verdict {vid} approved" in resp.get_data(as_text=True)


def test_verification_page_shows_the_build_and_the_evidence(client: FlaskClient) -> None:
    html = client.get("/verification").get_data(as_text=True)
    # The scripted regression: failing test, named, on the latest click build.
    assert "test_tempfile_pager_closes_file_before_unlink" in html
    assert "synthetic CI" in html
    # A manual inspection that failed, with who signed it off.
    assert "jamal-wright" in html
    # Change control: REQ-REQ-02's analyses predate v3 and were invalidated.
    assert "REQ-REQ-02 changed to v3" in html


def test_unknown_release_target_is_404(client: FlaskClient) -> None:
    assert client.get("/verification?target=pallets/click@9.9.9").status_code == 404


def test_verdict_page_shows_the_full_exchange(tmp_path: Path) -> None:
    from devtrack.checker.judges import LLMJudge, LLMResult
    from devtrack.checker.run import check_criterion

    class Fake:
        model = "claude-test"
        external = True

        def complete(self, system: str, user: str, *, max_tokens: int) -> LLMResult:
            body = '{"verdict": "unclear", "evidence": [], "confidence": 0.3, "reasoning": "cannot tell"}'
            return LLMResult(body, 100, 20, model_version="claude-test-20261001", request_id="req_9")

    url = f"sqlite:///{tmp_path / 'v.db'}"
    db = connect(url)
    migrate(db)
    load_seed(db)
    rec = check_criterion(db, "AC-REQ-101-1", LLMJudge(Fake()), Budget(1, 3, 15))
    db.close()
    app = create_app(Settings(database_url=url))
    with app.test_client() as c:
        html = c.get(f"/verdicts/{rec.id}").get_data(as_text=True)
        assert "claude-test-20261001" in html and "req_9" in html and "cannot tell" in html
        assert "AC-REQ-101-1" in html and "full exchange" in c.get("/verdicts").get_data(as_text=True)
        assert c.get("/verdicts/999999").status_code == 404
