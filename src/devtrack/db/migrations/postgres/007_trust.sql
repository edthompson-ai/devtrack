-- 007_trust.sql (Postgres)
-- Three things an auditor asks for:
--   * a requirement hierarchy, so verification rolls up from component to system
--   * an append-only, hash-chained audit log: who did what, when, on which build
--   * the full exchange behind every AI verdict: prompt, raw response, model version

-- Hierarchy: one parent per requirement (a tree). Cycles are refused by the
-- normalizer and caught by a data-quality check.
ALTER TABLE requirements ADD COLUMN IF NOT EXISTS parent_id BIGINT REFERENCES requirements(id);
ALTER TABLE requirements DROP CONSTRAINT IF EXISTS requirements_not_own_parent;
ALTER TABLE requirements ADD CONSTRAINT requirements_not_own_parent CHECK (parent_id IS NULL OR parent_id <> id);
CREATE INDEX IF NOT EXISTS ix_requirements_parent ON requirements (parent_id);

-- Audit log. Each row carries the hash of the previous row, so editing or removing a
-- row breaks the chain (`devtrack audit --verify`). UPDATE, DELETE and TRUNCATE are
-- refused by triggers, and the reader role can only read it.
CREATE TABLE IF NOT EXISTS audit_log (
    id           BIGSERIAL PRIMARY KEY,
    recorded_at  TEXT NOT NULL,          -- when the system recorded it (ISO-8601 UTC)
    effective_at TEXT,                   -- when it happened in the world, if different
    actor        TEXT NOT NULL CHECK (length(actor) > 0),
    action       TEXT NOT NULL,
    entity       TEXT NOT NULL,
    entity_key   TEXT NOT NULL,
    build_id     BIGINT REFERENCES builds(id),
    detail       TEXT NOT NULL DEFAULT '{}',   -- canonical JSON text, hashed as stored
    prev_hash    TEXT NOT NULL UNIQUE,         -- UNIQUE: two writers can't fork the chain
    hash         TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_audit_key ON audit_log (entity_key, id);
CREATE INDEX IF NOT EXISTS ix_audit_build ON audit_log (build_id);

CREATE OR REPLACE FUNCTION audit_log_is_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only (% refused)', TG_OP;
END
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS audit_log_no_change ON audit_log;
CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_is_append_only();
DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_is_append_only();

-- The full exchange behind each verdict. Prompts are stored as sent (already redacted),
-- responses as received, so any verdict can be re-read and re-run later.
ALTER TABLE ai_verdicts ADD COLUMN IF NOT EXISTS prompt_system TEXT;
ALTER TABLE ai_verdicts ADD COLUMN IF NOT EXISTS prompt_user   TEXT;
ALTER TABLE ai_verdicts ADD COLUMN IF NOT EXISTS raw_responses TEXT;  -- JSON array: one reply, two if repaired
ALTER TABLE ai_verdicts ADD COLUMN IF NOT EXISTS model_version TEXT;  -- what the API says actually answered
ALTER TABLE ai_verdicts ADD COLUMN IF NOT EXISTS request_ids   TEXT;  -- JSON array of provider request ids
ALTER TABLE ai_verdicts ADD COLUMN IF NOT EXISTS stop_reason   TEXT;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'devtrack_reader') THEN
        GRANT SELECT ON audit_log TO devtrack_reader;
    END IF;
END
$$;
