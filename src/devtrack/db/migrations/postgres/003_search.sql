-- 003_search.sql (Postgres)
-- Full-text search on acceptance criteria and commit messages, and embeddings for
-- criteria and diffs.

ALTER TABLE acceptance_criteria
    ADD COLUMN IF NOT EXISTS search tsvector
    GENERATED ALWAYS AS (to_tsvector('english', coalesce(text, ''))) STORED;
CREATE INDEX IF NOT EXISTS ix_criteria_search ON acceptance_criteria USING gin (search);

ALTER TABLE commits
    ADD COLUMN IF NOT EXISTS search tsvector
    GENERATED ALWAYS AS (to_tsvector('english', coalesce(message, ''))) STORED;
CREATE INDEX IF NOT EXISTS ix_commits_search ON commits USING gin (search);

-- Embeddings live in their own table keyed by (entity, entity_id, model) so a new
-- embedding model can be added without a schema change or a full rewrite. Vectors are
-- stored as JSON and compared in Python. (A pgvector column and HNSW index were planned
-- here; they were removed before any deployment because that path was never exercised.
-- See DESIGN-DECISIONS.md, section 16.)
CREATE TABLE IF NOT EXISTS embeddings (
    entity      TEXT NOT NULL CHECK (entity IN ('criterion', 'commit_diff')),
    entity_id   BIGINT NOT NULL,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL CHECK (dim > 0),
    vector_json JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (entity, entity_id, model)
);

