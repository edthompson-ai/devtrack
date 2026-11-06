-- 003_search.sql (SQLite)
-- Full-text search with FTS5 external-content tables kept in sync by triggers,
-- and an embeddings table (vectors stored as JSON; cosine similarity in Python).

CREATE VIRTUAL TABLE IF NOT EXISTS criteria_fts USING fts5(
    text, content = 'acceptance_criteria', content_rowid = 'id', tokenize = 'porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS criteria_fts_ai AFTER INSERT ON acceptance_criteria BEGIN
    INSERT INTO criteria_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS criteria_fts_ad AFTER DELETE ON acceptance_criteria BEGIN
    INSERT INTO criteria_fts (criteria_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS criteria_fts_au AFTER UPDATE OF text ON acceptance_criteria BEGIN
    INSERT INTO criteria_fts (criteria_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO criteria_fts (rowid, text) VALUES (new.id, new.text);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS commits_fts USING fts5(
    message, content = 'commits', content_rowid = 'id', tokenize = 'porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS commits_fts_ai AFTER INSERT ON commits BEGIN
    INSERT INTO commits_fts (rowid, message) VALUES (new.id, new.message);
END;
CREATE TRIGGER IF NOT EXISTS commits_fts_ad AFTER DELETE ON commits BEGIN
    INSERT INTO commits_fts (commits_fts, rowid, message) VALUES ('delete', old.id, old.message);
END;
CREATE TRIGGER IF NOT EXISTS commits_fts_au AFTER UPDATE OF message ON commits BEGIN
    INSERT INTO commits_fts (commits_fts, rowid, message) VALUES ('delete', old.id, old.message);
    INSERT INTO commits_fts (rowid, message) VALUES (new.id, new.message);
END;

CREATE TABLE IF NOT EXISTS embeddings (
    entity      TEXT NOT NULL CHECK (entity IN ('criterion', 'commit_diff')),
    entity_id   INTEGER NOT NULL,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL CHECK (dim > 0),
    vector_json TEXT NOT NULL CHECK (json_valid(vector_json)),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (entity, entity_id, model)
);
