-- 004_ai.sql (Postgres)
-- AI checker verdicts, human review of failures, and evaluation runs.

CREATE TABLE IF NOT EXISTS ai_verdicts (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    criterion_id   BIGINT NOT NULL REFERENCES acceptance_criteria(id),
    verdict        TEXT NOT NULL CHECK (verdict IN ('pass', 'fail', 'unclear')),
    confidence     DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    reasoning      TEXT NOT NULL,
    evidence       JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{"quote": ..., "source": ...}]
    flags          JSONB NOT NULL DEFAULT '[]'::jsonb,   -- guardrail flags, e.g. "injection_suspected"
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_hash     TEXT NOT NULL,                         -- cache key over prompt + context
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd       NUMERIC(10, 6) NOT NULL DEFAULT 0,
    -- A "fail" starts as pending_review and only counts once a person approves it.
    status         TEXT NOT NULL CHECK (status IN ('final', 'pending_review', 'approved', 'rejected')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (verdict = 'fail' OR status = 'final'),
    CHECK (verdict <> 'fail' OR status IN ('pending_review', 'approved', 'rejected'))
);
CREATE INDEX IF NOT EXISTS ix_verdicts_criterion_time ON ai_verdicts (criterion_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_verdicts_cache ON ai_verdicts (input_hash, model, prompt_version);
CREATE INDEX IF NOT EXISTS ix_verdicts_pending ON ai_verdicts (created_at) WHERE status = 'pending_review';

CREATE TABLE IF NOT EXISTS verdict_reviews (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    verdict_id  BIGINT NOT NULL UNIQUE REFERENCES ai_verdicts(id),
    decision    TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    reviewer    TEXT NOT NULL CHECK (length(reviewer) > 0),
    note        TEXT,
    decided_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Ground truth for evals: one human label per criterion.
CREATE TABLE IF NOT EXISTS eval_labels (
    criterion_id BIGINT PRIMARY KEY REFERENCES acceptance_criteria(id),
    label        TEXT NOT NULL CHECK (label IN ('pass', 'fail', 'unclear')),
    rationale    TEXT NOT NULL,
    labeled_by   TEXT NOT NULL,
    labeled_at   TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    prompt_version TEXT NOT NULL,
    model          TEXT NOT NULL,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    n              INTEGER NOT NULL,
    accuracy       DOUBLE PRECISION NOT NULL,
    fail_precision DOUBLE PRECISION,                      -- NULL when the run predicted no fails
    fail_recall    DOUBLE PRECISION,
    unclear_rate   DOUBLE PRECISION NOT NULL,
    cost_usd       NUMERIC(10, 6) NOT NULL DEFAULT 0,
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS eval_results (
    run_id       BIGINT NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
    criterion_id BIGINT NOT NULL REFERENCES acceptance_criteria(id),
    expected     TEXT NOT NULL CHECK (expected IN ('pass', 'fail', 'unclear')),
    predicted    TEXT NOT NULL CHECK (predicted IN ('pass', 'fail', 'unclear')),
    verdict_id   BIGINT REFERENCES ai_verdicts(id),
    PRIMARY KEY (run_id, criterion_id)
);

-- What counts: the newest verdict per criterion, where a fail counts only if approved.
-- A rejected fail is overturned and the criterion shows as needing a re-check.
CREATE OR REPLACE VIEW counted_verdicts AS
SELECT DISTINCT ON (v.criterion_id)
       v.criterion_id, v.id AS verdict_id, v.verdict, v.confidence, v.model,
       v.prompt_version, v.created_at
FROM ai_verdicts v
WHERE v.status IN ('final', 'approved')
ORDER BY v.criterion_id, v.created_at DESC, v.id DESC;
