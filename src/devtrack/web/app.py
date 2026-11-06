"""Dashboard: verification status per requirement and build, trace view, gaps, AI
verdicts with evidence and human review, eval scorecard, query performance, and the
question box.

Server-rendered (Flask + Jinja, autoescaped). One database connection per request;
the question box uses a separate read-only connection. The only write is the
approve/reject form, which is CSRF-protected. There is no login: this is a local
demo; put it behind SSO before exposing it (see README, production section).
"""

from __future__ import annotations

import json
import os
import secrets
from collections import OrderedDict
from pathlib import Path
from typing import Any

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for

from devtrack.checker.run import ReviewError, review_fail
from devtrack.config import Settings
from devtrack.db import Database, connect, migrate
from devtrack.policy import all_policies
from devtrack.quality import run_checks
from devtrack.queries.saved import all_queries, get
from devtrack.verification import (
    CRITERION_LABEL,
    recent_builds,
    requirement_status,
)

DOCS = Path(__file__).resolve().parents[3] / "docs"
MIN_SIMILARITY = 0.2


def _loads(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or Settings.from_env()
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("DASHBOARD_SECRET_KEY") or secrets.token_hex(32)
    app.config["SETTINGS"] = settings
    migrate_db = connect(settings.database_url)
    migrate(migrate_db)
    migrate_db.close()

    def db() -> Database:
        if "db" not in g:
            g.db = connect(settings.database_url)
        return g.db  # type: ignore[no-any-return]

    def ro_db() -> Database:
        if "ro_db" not in g:
            g.ro_db = connect(settings.readonly_database_url or settings.database_url, read_only=True)
        return g.ro_db  # type: ignore[no-any-return]

    @app.teardown_appcontext
    def close(_: BaseException | None) -> None:
        for key in ("db", "ro_db"):
            conn = g.pop(key, None)
            if conn is not None:
                conn.close()

    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return str(session["csrf"])

    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.filters["short"] = lambda sha: (sha or "")[:8]
    app.jinja_env.filters["day"] = lambda ts: (str(ts) or "")[:10] if ts else ""

    @app.context_processor
    def nav() -> dict[str, Any]:
        pending = db().scalar("SELECT COUNT(*) FROM ai_verdicts WHERE status = 'pending_review'")
        return {"pending_reviews": int(pending or 0), "dialect": db().dialect}

    # -- overview -----------------------------------------------------------------

    @app.get("/")
    def overview() -> str:
        d = db()
        gaps = get("criteria_without_code_or_tests").run(d)
        counts = {
            t: int(d.scalar(f"SELECT COUNT(*) FROM {t}"))
            for t in (
                "features",
                "stories",
                "tasks",
                "acceptance_criteria",
                "requirements",
                "commits",
                "pull_requests",
                "reviews",
            )
        }
        latest_eval = d.query_one("SELECT * FROM eval_runs ORDER BY id DESC LIMIT 1")
        verdicts = d.query("SELECT verdict, COUNT(*) AS n FROM counted_verdicts GROUP BY verdict")
        dq = run_checks(d)
        status = [r for r in requirement_status(d) if not r.is_rollup]
        return render_template(
            "overview.html",
            req_total=len(status),
            req_verified=sum(1 for r in status if r.status == "verified"),
            req_failed=sum(1 for r in status if r.status == "failed"),
            open_items=sum(r.open_items for r in status),
            counts=counts,
            no_code=sum(1 for r in gaps if r["linked_commits"] == 0),
            no_tests=sum(1 for r in gaps if r["linked_tests"] == 0),
            orphan_commits=len(get("commits_without_task").run(d)),
            stale=len({r["criterion"] for r in get("requirements_changed_after_merge").run(d)}),
            latest_eval=latest_eval,
            verdicts={r["verdict"]: r["n"] for r in verdicts},
            dq_ok=sum(r.ok for r in dq),
            dq_total=len(dq),
            dq_failed=[r for r in dq if not r.ok],
            sources=d.query(
                "SELECT source_system, COUNT(*) AS n FROM commits GROUP BY source_system ORDER BY 1"
            ),
        )

    # -- trace ------------------------------------------------------------------------

    @app.get("/trace")
    def trace() -> str:
        d = db()
        reqs = d.query("SELECT key, title, version, updated_at FROM requirements ORDER BY key")
        key = request.args.get("req") or (reqs[0]["key"] if reqs else None)
        req = d.query_one("SELECT * FROM requirements WHERE key = ?", (key,)) if key else None
        revisions = (
            d.query(
                "SELECT version, body, change_note, changed_at FROM requirement_revisions WHERE requirement_id = ?"
                " ORDER BY version",
                (req["id"],),
            )
            if req
            else []
        )
        rows = get("full_trace").run(d, key) if key else []
        # Latest verdict per criterion in any state, so a fail awaiting review shows as such.
        verdicts = {
            r["key"]: r
            for r in d.query(
                "SELECT a.key, v.verdict, v.confidence, v.status FROM ai_verdicts v"
                " JOIN acceptance_criteria a ON a.id = v.criterion_id"
                " WHERE v.id IN (SELECT MAX(id) FROM ai_verdicts GROUP BY criterion_id)"
            )
        }
        stale = {
            r["criterion"] for r in get("requirements_changed_after_merge").run(d) if r["requirement"] == key
        }
        criteria: OrderedDict[str, dict[str, Any]] = OrderedDict()
        for r in rows:
            c = criteria.setdefault(r["criterion"], {"text": r["criterion_text"], "tasks": OrderedDict()})
            if r["task"] is None:
                continue
            t = c["tasks"].setdefault(r["task"], {"status": r["task_status"], "commits": OrderedDict()})
            if r["sha"] is None:
                continue
            cm = t["commits"].setdefault(
                r["sha"],
                {
                    "repo": r["repo"],
                    "pr": r["pr_number"],
                    "pr_title": r["pr_title"],
                    "merged_at": r["merged_at"],
                    "reviews": [],
                },
            )
            if r["reviewer"] and (r["reviewer"], r["review_state"]) not in [
                (x["reviewer"], x["state"]) for x in cm["reviews"]
            ]:
                cm["reviews"].append({"reviewer": r["reviewer"], "state": r["review_state"]})
        verification = dict(_criteria_status(d, key))
        return render_template(
            "trace.html",
            verification=verification,
            criterion_label=CRITERION_LABEL,
            reqs=reqs,
            req=req,
            revisions=revisions,
            criteria=criteria,
            verdicts=verdicts,
            stale=stale,
        )

    def _criteria_status(d: Database, key: str | None) -> list[tuple[str, Any]]:
        if not key:
            return []
        return [(c.key, c) for r in requirement_status(d) if r.key == key for c in r.criteria]

    # -- verification ----------------------------------------------------------------

    @app.get("/verification")
    def verification() -> str:
        d = db()
        releases = d.query("SELECT repo, tag, released_at FROM releases ORDER BY repo, released_at DESC")
        target = request.args.get("target", "")
        release = repo = None
        if "@" in target:
            repo, release = target.rsplit("@", 1)
            if not any(r["repo"] == repo and r["tag"] == release for r in releases):
                abort(404)
        status = requirement_status(d, release=release, repo=repo)
        return render_template(
            "verification.html",
            status=status,
            target=target,
            release=release,
            releases=releases,
            builds=recent_builds(d, limit=25),
            policies=all_policies(d),
            order=["failed", "reverify", "verified", "verified_older_build", "not_verified"],
            criterion_label=CRITERION_LABEL,
        )

    @app.get("/audit")
    def audit_page() -> str:
        from devtrack.audit import history, verify_chain

        d = db()
        key = (request.args.get("key") or "").strip() or None
        return render_template(
            "audit.html", events=history(d, key, limit=300), key=key, chain=verify_chain(d)
        )

    # -- gaps ---------------------------------------------------------------------------

    @app.get("/gaps")
    def gaps() -> str:
        from devtrack.search import similar_commits

        d = db()
        crit_gaps = get("criteria_without_code_or_tests").run(d)
        suggestions = {}
        for r in crit_gaps:
            if r["linked_commits"] == 0:
                repo = d.scalar("SELECT f.repo FROM features f WHERE f.key = ?", (r["feature"],))
                hits = similar_commits(d, r["criterion"], repo=repo, limit=3)
                # Below ~0.2 the lexical embedder is matching noise; show nothing rather than guesses.
                suggestions[r["criterion"]] = [h for h in hits if float(h["similarity"]) >= MIN_SIMILARITY]
        return render_template(
            "gaps.html",
            no_code=[r for r in crit_gaps if r["linked_commits"] == 0],
            no_tests=[r for r in crit_gaps if r["linked_commits"] > 0],
            suggestions=suggestions,
            orphans=get("commits_without_task").run(d),
            stale=get("requirements_changed_after_merge").run(d),
        )

    # -- verdicts and human review ----------------------------------------------------------

    @app.get("/verdicts")
    def verdicts() -> str:
        status = request.args.get("status", "")
        where = "WHERE v.status = ?" if status else ""
        rows = db().query(
            "SELECT v.id, v.verdict, v.confidence, v.reasoning, v.evidence, v.flags, v.model, v.model_version,"
            " v.prompt_version, v.cost_usd, v.status, (v.prompt_user IS NOT NULL) AS has_exchange, a.key, a.text"
            " FROM ai_verdicts v JOIN acceptance_criteria a ON a.id = v.criterion_id"
            f" {where} ORDER BY CASE v.status WHEN 'pending_review' THEN 0 ELSE 1 END, v.id DESC LIMIT 300",
            (status,) if status else (),
        )
        for r in rows:
            r["evidence"] = _loads(r["evidence"])
            r["flags"] = _loads(r["flags"])
        return render_template("verdicts.html", rows=rows, status=status)

    @app.get("/verdicts/<int:verdict_id>")
    def verdict_detail(verdict_id: int) -> str:
        from devtrack.audit import history

        d = db()
        v = d.query_one(
            "SELECT v.*, a.key, a.text FROM ai_verdicts v JOIN acceptance_criteria a ON a.id = v.criterion_id"
            " WHERE v.id = ?",
            (verdict_id,),
        )
        if v is None:
            abort(404)
        for k in ("evidence", "flags", "raw_responses", "request_ids"):
            v[k] = _loads(v[k]) if v[k] is not None else None
        events = [e for e in history(d, v["key"], limit=200) if e["action"].startswith("verdict.")]
        return render_template("verdict.html", v=v, events=events)

    @app.post("/verdicts/<int:verdict_id>/review")
    def review(verdict_id: int) -> Any:
        if request.form.get("csrf") != session.get("csrf"):
            abort(400, "invalid form token")
        decision = request.form.get("decision", "")
        try:
            review_fail(
                db(),
                verdict_id,
                decision=decision,
                reviewer=request.form.get("reviewer", ""),
                note=request.form.get("note", ""),
            )
            flash(f"Verdict {verdict_id} {decision}.", "ok")
        except ReviewError as exc:
            flash(str(exc), "error")
        return redirect(url_for("verdicts", status=request.args.get("status", "")))

    # -- evals --------------------------------------------------------------------------------

    @app.get("/evals")
    def evals() -> str:
        d = db()
        runs = d.query("SELECT * FROM eval_runs ORDER BY id DESC")
        latest = runs[0] if runs else None
        confusion: dict[str, dict[str, int]] = {}
        misses: list[dict[str, Any]] = []
        if latest:
            for r in d.query(
                "SELECT expected, predicted, COUNT(*) AS n FROM eval_results WHERE run_id = ?"
                " GROUP BY expected, predicted",
                (latest["id"],),
            ):
                confusion.setdefault(r["expected"], {})[r["predicted"]] = r["n"]
            misses = d.query(
                "SELECT a.key, a.text, er.expected, er.predicted, v.reasoning, l.rationale"
                " FROM eval_results er JOIN acceptance_criteria a ON a.id = er.criterion_id"
                " LEFT JOIN ai_verdicts v ON v.id = er.verdict_id"
                " LEFT JOIN eval_labels l ON l.criterion_id = er.criterion_id"
                " WHERE er.run_id = ? AND er.expected <> er.predicted ORDER BY a.key",
                (latest["id"],),
            )
        return render_template(
            "evals.html",
            runs=runs,
            latest=latest,
            confusion=confusion,
            misses=misses,
            labels=("pass", "fail", "unclear"),
        )

    # -- performance ----------------------------------------------------------------------------

    @app.get("/performance")
    def performance() -> str:
        reports = {}
        for dialect in ("postgres", "sqlite"):
            path = DOCS / f"query-performance.{dialect}.json"
            if path.exists():
                data = json.loads(path.read_text())
                before = {r["query"]: r for r in data["results"] if r["phase"] == "before"}
                after = {r["query"]: r for r in data["results"] if r["phase"] == "after"}
                data["pairs"] = [(before[k], after[k]) for k in before]
                reports[dialect] = data
        return render_template("performance.html", reports=reports)

    # -- ask -------------------------------------------------------------------------------------

    @app.route("/ask", methods=["GET", "POST"])
    def ask_page() -> str:
        from devtrack.nlq.translate import EXAMPLES, ask

        question = request.values.get("q", "").strip()
        answer = None
        if question:
            answer = ask(db(), ro_db(), question)
        return render_template(
            "ask.html",
            question=question,
            answer=answer,
            examples=EXAMPLES,
        )

    # -- saved queries ------------------------------------------------------------------------------

    @app.get("/queries")
    def queries() -> str:
        d = db()
        slug = request.args.get("q")
        selected = get(slug) if slug in all_queries() else None
        arg = request.args.get("arg") or "REQ-HTTPX-01"
        rows = selected.run(d, *((arg,) if selected.params else ())) if selected else []
        return render_template(
            "queries.html",
            queries=all_queries(),
            selected=selected,
            rows=rows,
            arg=arg,
            sql=selected.sql(d.dialect) if selected else "",
        )

    return app
