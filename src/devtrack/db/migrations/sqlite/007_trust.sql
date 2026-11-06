-- 007_trust.sql (SQLite)
-- Requirement hierarchy, append-only audit log, full verdict record. See the Postgres
-- version for the reasoning.

ALTER TABLE requirements ADD COLUMN parent_id INTEGER REFERENCES requirements(id);
CREATE INDEX IF NOT EXISTS ix_requirements_parent ON requirements (parent_id);
-- SQLite can't add a table CHECK after the fact; a trigger does the same job.
CREATE TRIGGER IF NOT EXISTS requirements_not_own_parent_ins BEFORE INSERT ON requirements
WHEN NEW.parent_id IS NOT NULL AND NEW.parent_id = NEW.id
BEGIN SELECT RAISE(ABORT, 'a requirement cannot be its own parent'); END;
CREATE TRIGGER IF NOT EXISTS requirements_not_own_parent_upd BEFORE UPDATE OF parent_id ON requirements
WHEN NEW.parent_id IS NOT NULL AND NEW.parent_id = NEW.id
BEGIN SELECT RAISE(ABORT, 'a requirement cannot be its own parent'); END;

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY,
    recorded_at  TEXT NOT NULL,
    effective_at TEXT,
    actor        TEXT NOT NULL CHECK (length(actor) > 0),
    action       TEXT NOT NULL,
    entity       TEXT NOT NULL,
    entity_key   TEXT NOT NULL,
    build_id     INTEGER REFERENCES builds(id),
    detail       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail)),
    prev_hash    TEXT NOT NULL UNIQUE,
    hash         TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_audit_key ON audit_log (entity_key, id);
CREATE INDEX IF NOT EXISTS ix_audit_build ON audit_log (build_id);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only (UPDATE refused)'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only (DELETE refused)'); END;

ALTER TABLE ai_verdicts ADD COLUMN prompt_system TEXT;
ALTER TABLE ai_verdicts ADD COLUMN prompt_user   TEXT;
ALTER TABLE ai_verdicts ADD COLUMN raw_responses TEXT;
ALTER TABLE ai_verdicts ADD COLUMN model_version TEXT;
ALTER TABLE ai_verdicts ADD COLUMN request_ids   TEXT;
ALTER TABLE ai_verdicts ADD COLUMN stop_reason   TEXT;
