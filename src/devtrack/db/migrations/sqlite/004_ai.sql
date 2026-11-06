-- 004_ai.sql (SQLite)
-- AI checker verdicts, human review of failures, and evaluation runs.

CREATE TABLE IF NOT EXISTS ai_verdicts (
    id             INTEGER PRIMARY KEY,
    criterion_id   INTEGER NOT NULL REFERENCES acceptance_criteria(id),
    verdict        TEXT NOT NULL CHECK (verdict IN ('pass', 'fail', 'unclear')),
    confidence     REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    reasoning      TEXT NOT NULL,
    evidence       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(evidence)),   -- [{"quote": ..., "source": ...}]
    flags          TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(flags)),   -- guardrail flags, e.g. "injection_suspected"
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_hash     TEXT NOT NULL,                         -- cache key over prompt + context
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL NOT NULL DEFAULT 0,
    -- A "fail" starts as pending_review and only counts once a person approves it.
    status         TEXT NOT NULL CHECK (status IN ('final', 'pending_review', 'approved', 'rejected')),
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (verdict = 'fail' OR status = 'final'),
    CHECK (verdict <> 'fail' OR status IN ('pending_review', 'approved', 'rejected'))
);
CREATE INDEX IF NOT EXISTS ix_verdicts_criterion_time ON ai_verdicts (criterion_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_verdicts_cache ON ai_verdicts (input_hash, model, prompt_version);
CREATE INDEX IF NOT EXISTS ix_verdicts_pending ON ai_verdicts (created_at) WHERE status = 'pending_review';

CREATE TABLE IF NOT EXISTS verdict_reviews (
    id          INTEGER PRIMARY KEY,
    verdict_id  INTEGER NOT NULL UNIQUE REFERENCES ai_verdicts(id),
    decision    TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    reviewer    TEXT NOT NULL CHECK (length(reviewer) > 0),
    note        TEXT,
    decided_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Ground truth for evals: one human label per criterion.
CREATE TABLE IF NOT EXISTS eval_labels (
    criterion_id INTEGER PRIMARY KEY REFERENCES acceptance_criteria(id),
    label        TEXT NOT NULL CHECK (label IN ('pass', 'fail', 'unclear')),
    rationale    TEXT NOT NULL,
    labeled_by   TEXT NOT NULL,
    labeled_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id             INTEGER PRIMARY KEY,
    prompt_version TEXT NOT NULL,
    model          TEXT NOT NULL,
    started_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    n              INTEGER NOT NULL,
    accuracy       REAL NOT NULL,
    fail_precision REAL,                      -- NULL when the run predicted no fails
    fail_recall    REAL,
    unclear_rate   REAL NOT NULL,
    cost_usd       REAL NOT NULL DEFAULT 0,
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS eval_results (
    run_id       INTEGER NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
    criterion_id INTEGER NOT NULL REFERENCES acceptance_criteria(id),
    expected     TEXT NOT NULL CHECK (expected IN ('pass', 'fail', 'unclear')),
    predicted    TEXT NOT NULL CHECK (predicted IN ('pass', 'fail', 'unclear')),
    verdict_id   INTEGER REFERENCES ai_verdicts(id),
    PRIMARY KEY (run_id, criterion_id)
);

-- What counts: the newest verdict per criterion, where a fail counts only if approved.
-- A rejected fail is overturned and the criterion shows as needing a re-check.
CREATE VIEW IF NOT EXISTS counted_verdicts AS
SELECT criterion_id, verdict_id, verdict, confidence, model, prompt_version, created_at
FROM (
    SELECT v.criterion_id, v.id AS verdict_id, v.verdict, v.confidence, v.model,
           v.prompt_version, v.created_at,
           ROW_NUMBER() OVER (PARTITION BY v.criterion_id ORDER BY v.created_at DESC, v.id DESC) AS rn
    FROM ai_verdicts v
    WHERE v.status IN ('final', 'approved')
)
WHERE rn = 1;
