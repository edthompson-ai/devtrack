"""Command line: one subcommand per pipeline step, plus `demo` that runs them all.

devtrack init                 create or upgrade the schema
devtrack seed                 load the offline seed (real git data + synthetic backlog)
devtrack sync                 incremental sync from the GitHub API (needs GITHUB_TOKEN)
devtrack dq                   run data-quality checks
devtrack query <name> [arg]   run a saved query
devtrack embed                lexical-similarity vectors for criteria and diffs
devtrack check [--all|KEY..]  run the AI checker (--judge heuristic|llm|internal, --prompt v1|v2)
devtrack review ID --approve|--reject --by NAME
devtrack eval                 evaluate a judge against the labels
devtrack eval-gate [--live]   fail if fail detection drops below the keyword baseline
devtrack ingest-junit FILE --repo R --build B   load a JUnit test report for one build
devtrack verify KEY --build B --method M --result R --by NAME   manual sign-off
devtrack status [--release TAG] [--criteria]    verification status per requirement
devtrack policy [set REPO CLASS --by NAME]      data classification per repository
devtrack audit [--key K] [--verify]             audit trail; --verify checks the hash chain
devtrack ask "question"       plain-English question -> vetted template SQL -> rows
devtrack bench                before/after index benchmark (empty database only)
devtrack serve                dashboard on http://127.0.0.1:8000
devtrack demo                 init + seed + embed + check + eval, then serve
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from devtrack.checker.guardrails import Budget
from devtrack.checker.judges import (
    AnthropicClient,
    ApprovedInternalModelClient,
    ClaudeCodeCLIClient,
    HeuristicJudge,
    Judge,
    LLMClient,
    LLMJudge,
)
from devtrack.config import Settings
from devtrack.db import Database, connect, migrate
from devtrack.logging_setup import configure

log = logging.getLogger("devtrack")
DOCS = Path(__file__).resolve().parents[2] / "docs"


def _db(settings: Settings) -> Database:
    db = connect(settings.database_url)
    migrate(db)
    return db


def _budget(settings: Settings) -> Budget:
    return Budget(
        settings.checker_run_budget_usd, settings.price_input_per_mtok, settings.price_output_per_mtok
    )


def make_judge(settings: Settings, kind: str, prompt: str) -> Judge:
    if kind == "heuristic":
        return HeuristicJudge()
    if kind == "internal":
        internal = ApprovedInternalModelClient(
            settings.internal_model_url, model=settings.internal_model_name
        )
        return LLMJudge(internal, prompt_version=prompt, max_output_tokens=settings.checker_max_output_tokens)
    client: LLMClient
    if settings.checker_backend == "api":
        if not settings.anthropic_api_key:
            raise SystemExit(
                "CHECKER_BACKEND=api needs ANTHROPIC_API_KEY (or use the default claude-cli backend)"
            )
        client = AnthropicClient(api_key=settings.anthropic_api_key, model=settings.checker_model)
    elif settings.checker_backend == "claude-cli":
        client = ClaudeCodeCLIClient(model=settings.checker_model, executable=settings.claude_cli)
    else:
        raise SystemExit(f"unknown CHECKER_BACKEND {settings.checker_backend!r}: use claude-cli or api")
    return LLMJudge(client, prompt_version=prompt, max_output_tokens=settings.checker_max_output_tokens)


def _print_rows(rows: list[dict[str, Any]], limit: int = 50) -> None:
    if not rows:
        print("(no rows)")
        return
    cols = list(rows[0])
    widths = {c: min(max(len(c), *(len(str(r[c])) for r in rows[:limit])), 60) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows[:limit]:
        print("  ".join(str(r[c]).replace("\n", " ")[: widths[c]].ljust(widths[c]) for c in cols))
    if len(rows) > limit:
        print(f"... {len(rows) - limit} more rows")


def cmd_init(args: argparse.Namespace, s: Settings) -> None:
    db = connect(s.database_url)
    applied = migrate(db)
    print("applied:", ", ".join(applied) or "nothing (up to date)")


def cmd_seed(args: argparse.Namespace, s: Settings) -> None:
    from devtrack.evals.run import load_labels
    from devtrack.ingest.seed import DEFAULT_SEED_DIR, load_seed

    db = _db(s)
    stats = load_seed(db, Path(args.seed_dir) if args.seed_dir else DEFAULT_SEED_DIR)
    labels = load_labels(
        db, (Path(args.seed_dir) if args.seed_dir else DEFAULT_SEED_DIR) / "eval_labels.json"
    )
    print(json.dumps({**dict(stats), "eval_labels": labels}, indent=2, sort_keys=True))


def cmd_sync(args: argparse.Namespace, s: Settings) -> None:
    from devtrack.github.client import GitHubClient
    from devtrack.ingest.sync import sync_all

    db = _db(s)
    repos = args.repo or list(s.github_repos)
    stats = sync_all(
        db, GitHubClient(token=s.github_token), repos, max_items=args.max_items, workflow=s.github_ci_workflow
    )
    print(json.dumps(dict(stats), indent=2, sort_keys=True))


def cmd_dq(args: argparse.Namespace, s: Settings) -> None:
    from devtrack.quality import run_checks

    results = run_checks(_db(s))
    bad = [r for r in results if not r.ok]
    for r in results:
        print(
            f"{'ok  ' if r.ok else 'FAIL'} {r.check.name}"
            + ("" if r.ok else f"  ({r.failures}) {r.sample[:2]}")
        )
    print(f"\n{len(results) - len(bad)}/{len(results)} checks passed")
    if bad:
        sys.exit(1)


def cmd_query(args: argparse.Namespace, s: Settings) -> None:
    from devtrack.queries.saved import all_queries, get

    if not args.name:
        for slug, q in all_queries().items():
            print(f"{q.number}. {slug:36} {q.title}")
        return
    q = get(args.name)
    if args.sql:
        print(q.sql(connect(s.database_url).dialect))
        return
    _print_rows(q.run(_db(s), *args.args), limit=args.limit)


def cmd_embed(args: argparse.Namespace, s: Settings) -> None:
    from devtrack.search import build_embeddings

    print(f"embedded {build_embeddings(_db(s))} new items")


def cmd_check(args: argparse.Namespace, s: Settings) -> None:
    from devtrack.checker.run import check_many

    db = _db(s)
    keys = args.keys or [r["key"] for r in db.query("SELECT key FROM acceptance_criteria ORDER BY key")]
    if not args.keys and not args.all:
        raise SystemExit("name criteria (AC-...) or pass --all")
    judge = make_judge(s, args.judge, args.prompt)
    summary = check_many(db, keys, judge, _budget(s), force=args.force)
    for r in summary.records:
        print(f"{r.criterion:16} {r.verdict:8} {r.confidence:.2f} {r.status:15} {','.join(r.flags)}")
    print(
        f"\nchecked {summary.checked} ({summary.cached} cached), cost ${summary.cost_usd:.4f},"
        f" verdicts {summary.verdicts}"
        + (f", {summary.skipped_budget} skipped: budget" if summary.skipped_budget else "")
        + (f", {summary.blocked_policy} blocked by data policy" if summary.blocked_policy else "")
    )


def cmd_review(args: argparse.Namespace, s: Settings) -> None:
    from devtrack.checker.run import review_fail

    review_fail(
        _db(s),
        args.verdict_id,
        decision="approved" if args.approve else "rejected",
        reviewer=args.by,
        note=args.note or "",
    )
    print("recorded")


def cmd_eval(args: argparse.Namespace, s: Settings) -> None:
    from devtrack.evals.run import load_labels, run_eval, write_report
    from devtrack.ingest.seed import DEFAULT_SEED_DIR

    db = _db(s)
    if args.load_labels or not db.scalar("SELECT COUNT(*) FROM eval_labels"):
        load_labels(db, Path(args.labels) if args.labels else DEFAULT_SEED_DIR / "eval_labels.json")
    result = run_eval(db, make_judge(s, args.judge, args.prompt), _budget(s), notes=args.notes or "")
    m = result.metrics
    print(
        f"{result.prompt_version} / {result.model}: n={m.n} accuracy={m.accuracy:.0%} "
        f"fail precision={m.fail_precision} fail recall={m.fail_recall} unclear={m.unclear_rate:.0%} "
        f"cost=${result.cost_usd:.4f}"
    )
    if args.report:
        print("report:", write_report(db, result, Path(args.report)))


def cmd_eval_report(args: argparse.Namespace, s: Settings) -> None:
    from devtrack.evals.evidence import write_evidence
    from devtrack.evals.run import history, result_from_db, write_report

    db = _db(s)
    runs = history(db)
    if not runs:
        raise SystemExit("no eval runs stored")
    detail = args.run or int(runs[-1]["id"])
    print("report:", write_report(db, result_from_db(db, detail), Path(args.report)))
    if args.evidence:
        for path in write_evidence(db):
            print("evidence:", path)


def cmd_eval_gate(args: argparse.Namespace, s: Settings) -> None:
    from devtrack.evals.gate import run_gate
    from devtrack.evals.run import load_labels
    from devtrack.ingest.seed import DEFAULT_SEED_DIR

    db = _db(s)
    if not db.scalar("SELECT COUNT(*) FROM eval_labels"):
        load_labels(db, DEFAULT_SEED_DIR / "eval_labels.json")
    live = make_judge(s, "llm", args.prompt) if args.live else None
    results = run_gate(db, live_judge=live, budget=_budget(s))
    for g in results:
        print(f"{'PASS' if g.ok else 'FAIL'}  {g.name}: {g.detail}")
    if not all(g.ok for g in results):
        print("\neval gate failed: fail detection is below the keyword baseline (docs/eval-baseline.json)")
        sys.exit(1)


def cmd_ask(args: argparse.Namespace, s: Settings) -> None:
    from devtrack.nlq.translate import ask

    db = _db(s)
    answer = ask(db, connect(s.readonly_database_url or s.database_url, read_only=True), args.question)
    if answer.translation:
        print(
            f"-- {answer.translation.method}: {answer.translation.explanation}\n{answer.translation.sql}\n"
            f"-- params: {answer.translation.params}\n"
        )
    if answer.error:
        print(answer.error)
        for ex in answer.suggestions:
            print("  try:", ex)
        sys.exit(1)
    _print_rows(answer.rows)


def cmd_bench(args: argparse.Namespace, s: Settings) -> None:
    from devtrack.bench import benchmark, write_report

    result = benchmark(connect(s.database_url), factor=args.scale, runs=args.runs)
    print("report:", write_report(result, Path(args.out)))


def cmd_ingest_junit(args: argparse.Namespace, s: Settings) -> None:
    from devtrack.ingest.junit import junit_report_payload
    from devtrack.ingest.normalize import process_staging
    from devtrack.ingest.staging import finish_batch, stage, start_batch
    from devtrack.verification import find_build, refresh

    db = _db(s)
    build = find_build(db, args.repo, args.build)
    source_id = str(db.scalar("SELECT source_id FROM builds WHERE id = ?", (build.id,)))
    payload = junit_report_payload(db, Path(args.file), repo=args.repo, build_source_id=source_id)
    batch = start_batch(db, "github", f"{args.repo} junit")
    new, same = stage(db, batch, "github", [("test_report", f"{source_id}/tests", payload)])
    stats = process_staging(db)
    finish_batch(db, batch, "succeeded", {"staged_new": new, "staged_unchanged": same, **stats})
    outcomes: dict[str, int] = {}
    for t in payload["tests"]:
        outcomes[t["outcome"]] = outcomes.get(t["outcome"], 0) + 1
    print(f"build {build.label} ({build.head_sha[:10]}): {len(payload['tests'])} tests {outcomes}")
    print("verification:", dict(refresh(db)))


def cmd_verify(args: argparse.Namespace, s: Settings) -> None:
    from devtrack.verification import find_build, record_verification, refresh

    db = _db(s)
    repo = db.scalar(
        "SELECT f.repo FROM acceptance_criteria a JOIN stories st ON st.id = a.story_id"
        " JOIN features f ON f.id = st.feature_id WHERE a.key = ?",
        (args.criterion,),
    )
    if repo is None:
        raise SystemExit(f"unknown criterion {args.criterion}")
    build = find_build(db, str(repo), args.build)
    vid = record_verification(
        db,
        criterion_key=args.criterion,
        build_id=build.id,
        method=args.method,
        result=args.result,
        verified_by=args.by,
        note=args.note,
    )
    refresh(db)
    print(f"recorded {args.method} {args.result} for {args.criterion} on build {build.label} (id {vid})")


def cmd_status(args: argparse.Namespace, s: Settings) -> None:
    from devtrack.verification import requirement_status

    db = _db(s)
    for r in requirement_status(db, release=args.release, repo=args.repo):
        if r.is_rollup:
            target = f"roll-up of {', '.join(r.children)}"
            method = "roll-up"
        else:
            target = (
                f"{r.repo} {r.target.label} {r.target.head_sha[:10]}" if r.target else f"{r.repo} (no build)"
            )
            method = r.method
        name = "  " * r.depth + r.key
        print(f"{name:18} v{r.version} {method:13} {r.label:26} {target}")
        if args.criteria:
            for c in r.criteria:
                on = f"build {c.result_build.label}" if c.result_build else ""
                extra = f"  re-verify since v{c.open_items[-1]['version']}" if c.open_items else ""
                print(f"    {c.key:18} {c.label:26} {on}{extra}")


def cmd_policy(args: argparse.Namespace, s: Settings) -> None:
    from devtrack.policy import all_policies, set_classification

    db = _db(s)
    if args.action == "set":
        if not (args.repo and args.classification and args.by):
            raise SystemExit(
                "usage: devtrack policy set REPO public|internal|controlled --by NAME [--note ...]"
            )
        set_classification(db, args.repo, args.classification, set_by=args.by, note=args.note)
    for p in all_policies(db):
        source = f"set by {p.set_by}" if p.explicit else "default (fail closed)"
        print(f"{p.repo:24} {p.classification:11} {source}  {p.note or ''}")


def cmd_audit(args: argparse.Namespace, s: Settings) -> None:
    from devtrack.audit import history, verify_chain

    db = _db(s)
    if args.verify:
        check = verify_chain(db)
        if check.ok:
            print(f"audit chain intact: {check.rows} rows")
            return
        print(f"AUDIT CHAIN BROKEN at row {check.first_bad_id}: {check.reason}")
        sys.exit(1)
    for r in reversed(history(db, args.key, limit=args.limit)):
        build = f" build {r['build_repo']}#{r['build_run']}" if r["build_run"] else ""
        when = r["effective_at"] or r["recorded_at"]
        print(f"{r['id']:>6} {when} {r['actor']:16} {r['action']:26} {r['entity_key']}{build}")


def cmd_serve(args: argparse.Namespace, s: Settings) -> None:
    from devtrack.web.app import create_app

    app = create_app(s)
    print(f"dashboard: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)


def cmd_demo(args: argparse.Namespace, s: Settings) -> None:
    from devtrack.checker.run import check_many
    from devtrack.evals.run import load_labels, run_eval, write_report
    from devtrack.ingest.seed import DEFAULT_SEED_DIR, load_seed
    from devtrack.search import build_embeddings

    db = _db(s)
    stats = load_seed(db)
    load_labels(db, DEFAULT_SEED_DIR / "eval_labels.json")
    log.info("seed: %s", {k: v for k, v in stats.items() if k.startswith("staged")})
    build_embeddings(db)
    keys = [r["key"] for r in db.query("SELECT key FROM acceptance_criteria ORDER BY key")]
    judges: list[Judge] = [HeuristicJudge()]
    # The container can't use a Claude subscription, so the demo only adds the model
    # judge on the API path. Model evals are run with `devtrack eval --judge llm`.
    if s.checker_backend == "api" and s.anthropic_api_key:
        judges.append(make_judge(s, "llm", "v2"))
    else:
        log.info("demo runs the offline heuristic checker; see README for the model eval")
    for judge in judges:
        summary = check_many(db, keys, judge, _budget(s))
        log.info("%s: %s (cost $%.4f)", judge.model, summary.verdicts, summary.cost_usd)
        # The demo runs on every container start; don't append an identical eval run each time.
        already = db.scalar(
            "SELECT COUNT(*) FROM eval_runs WHERE prompt_version = ? AND model = ?",
            (judge.prompt_version, judge.model),
        )
        if not already:
            result = run_eval(db, judge, _budget(s), notes="demo")
            write_report(db, result, DOCS / "eval-report.md")
    if not args.no_serve:
        cmd_serve(args, s)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="devtrack", description="Dev Tracking Data Compiler")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create or upgrade the schema").set_defaults(fn=cmd_init)
    sp = sub.add_parser("seed", help="load the offline seed")
    sp.add_argument("--seed-dir")
    sp.set_defaults(fn=cmd_seed)
    sp = sub.add_parser("sync", help="incremental sync from the GitHub API")
    sp.add_argument("--repo", action="append", help="owner/name; repeatable (default: GITHUB_REPOS)")
    sp.add_argument("--max-items", type=int, default=300)
    sp.set_defaults(fn=cmd_sync)
    sub.add_parser("dq", help="data-quality checks").set_defaults(fn=cmd_dq)
    sp = sub.add_parser("query", help="run a saved query (no name: list them)")
    sp.add_argument("name", nargs="?")
    sp.add_argument("args", nargs="*")
    sp.add_argument(
        "--sql", action="store_true", help="print the SQL for this database instead of running it"
    )
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(fn=cmd_query)
    sub.add_parser("embed", help="build embeddings").set_defaults(fn=cmd_embed)
    sp = sub.add_parser("check", help="run the AI checker")
    sp.add_argument("keys", nargs="*")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--judge", choices=["heuristic", "llm", "internal"], default="heuristic")
    sp.add_argument("--prompt", default="v2")
    sp.add_argument("--force", action="store_true", help="ignore the cache")
    sp.set_defaults(fn=cmd_check)
    sp = sub.add_parser("review", help="approve or reject a pending fail")
    sp.add_argument("verdict_id", type=int)
    group = sp.add_mutually_exclusive_group(required=True)
    group.add_argument("--approve", action="store_true")
    group.add_argument("--reject", action="store_true")
    sp.add_argument("--by", required=True)
    sp.add_argument("--note")
    sp.set_defaults(fn=cmd_review)
    sp = sub.add_parser("eval", help="evaluate a judge against the labels")
    sp.add_argument("--judge", choices=["heuristic", "llm", "internal"], default="heuristic")
    sp.add_argument("--prompt", default="v2")
    sp.add_argument("--labels")
    sp.add_argument("--load-labels", action="store_true")
    sp.add_argument("--notes")
    sp.add_argument("--report", default=str(DOCS / "eval-report.md"))
    sp.set_defaults(fn=cmd_eval)
    sp = sub.add_parser("ingest-junit", help="load a JUnit XML test report for one build")
    sp.add_argument("file")
    sp.add_argument("--repo", required=True)
    sp.add_argument("--build", required=True, help="build id, run number, release tag or commit sha")
    sp.set_defaults(fn=cmd_ingest_junit)
    sp = sub.add_parser("verify", help="record a manual analysis/inspection/demonstration result")
    sp.add_argument("criterion")
    sp.add_argument("--build", required=True, help="build id, run number, release tag or commit sha")
    sp.add_argument("--method", required=True, choices=["analysis", "inspection", "demonstration"])
    sp.add_argument("--result", required=True, choices=["passed", "failed"])
    sp.add_argument("--by", required=True)
    sp.add_argument("--note")
    sp.set_defaults(fn=cmd_verify)
    sp = sub.add_parser("status", help="verification status per requirement")
    sp.add_argument("--release", help="evaluate against this release tag instead of the latest build")
    sp.add_argument("--repo")
    sp.add_argument("--criteria", action="store_true", help="show each criterion")
    sp.set_defaults(fn=cmd_status)
    sp = sub.add_parser("policy", help="data classification per repository")
    sp.add_argument("action", choices=["list", "set"], nargs="?", default="list")
    sp.add_argument("repo", nargs="?")
    sp.add_argument("classification", nargs="?", choices=["public", "internal", "controlled"])
    sp.add_argument("--by")
    sp.add_argument("--note")
    sp.set_defaults(fn=cmd_policy)
    sp = sub.add_parser("audit", help="show the audit trail, or verify its hash chain")
    sp.add_argument("--key", help="only events for this requirement, criterion or repository")
    sp.add_argument("--verify", action="store_true", help="recompute the hash chain and report tampering")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(fn=cmd_audit)
    sp = sub.add_parser("eval-report", help="rewrite the eval report (and evidence files) from stored runs")
    sp.add_argument("--run", type=int, help="run to show in detail (default: the latest)")
    sp.add_argument("--report", default=str(DOCS / "eval-report.md"))
    sp.add_argument("--evidence", action="store_true", help="also write docs/evidence/eval-*.json[l.gz]")
    sp.set_defaults(fn=cmd_eval_report)
    sp = sub.add_parser("eval-gate", help="fail if fail detection drops below the keyword baseline")
    sp.add_argument("--live", action="store_true", help="also run the model judge now (needs a backend)")
    sp.add_argument("--prompt", default="v2")
    sp.set_defaults(fn=cmd_eval_gate)
    sp = sub.add_parser("ask", help="ask a question in plain English")
    sp.add_argument("question")
    sp.set_defaults(fn=cmd_ask)
    sp = sub.add_parser("bench", help="before/after index benchmark on an EMPTY database")
    sp.add_argument("--scale", type=float, default=1.0)
    sp.add_argument("--runs", type=int, default=5)
    sp.add_argument("--out", default=str(DOCS))
    sp.set_defaults(fn=cmd_bench)
    for name, fn in (("serve", cmd_serve), ("demo", cmd_demo)):
        sp = sub.add_parser(name)
        sp.add_argument("--host", default="127.0.0.1")
        sp.add_argument("--port", type=int, default=8000)
        if name == "demo":
            sp.add_argument("--no-serve", action="store_true")
        sp.set_defaults(fn=fn)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    configure(settings.log_level)
    args.fn(args, settings)


if __name__ == "__main__":
    main()
