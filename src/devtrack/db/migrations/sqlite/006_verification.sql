-- 006_verification.sql (SQLite)
-- Turns the tracker into a verification system:
--   * every requirement names HOW it is verified (test, analysis, inspection, demonstration)
--   * builds and releases join the chain: commit -> build -> release
--   * test runs and manual sign-offs become verification results tied to one build
--   * a requirement change invalidates earlier results and opens re-verification items
--   * each repository carries a data classification that decides which models may see it

ALTER TABLE requirements
    ADD COLUMN verification_method TEXT NOT NULL DEFAULT 'test'
    CHECK (verification_method IN ('test', 'analysis', 'inspection', 'demonstration'));

-- A CI run of one commit. Real ones come from the GitHub Actions API; the offline seed
-- generates synthetic ones (flagged).
CREATE TABLE IF NOT EXISTS builds (
    id            INTEGER PRIMARY KEY,
    repo          TEXT NOT NULL,
    workflow      TEXT NOT NULL,                    -- run numbers count per workflow
    run_number    INTEGER NOT NULL CHECK (run_number > 0),
    head_sha      TEXT NOT NULL CHECK (length(head_sha) = 40),
    commit_id     INTEGER REFERENCES commits(id),   -- NULL when the commit isn't loaded (e.g. a release branch)
    branch        TEXT,
    event         TEXT NOT NULL,                    -- push, pull_request, release, ...
    status        TEXT NOT NULL CHECK (status IN ('success', 'failure', 'cancelled', 'in_progress')),
    started_at    TEXT,
    finished_at   TEXT,
    url           TEXT,
    source_system TEXT NOT NULL CHECK (source_system IN ('github', 'backlog', 'synthetic')),
    source_id     TEXT NOT NULL,
    raw           TEXT NOT NULL CHECK (json_valid(raw)),
    raw_hash      TEXT NOT NULL,
    synced_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (source_system, source_id),
    UNIQUE (repo, workflow, run_number, source_system),
    CHECK (finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at)
);

-- A shipped version: a tag pointing at a commit, built by a specific build.
CREATE TABLE IF NOT EXISTS releases (
    id            INTEGER PRIMARY KEY,
    repo          TEXT NOT NULL,
    tag           TEXT NOT NULL,
    target_sha    TEXT NOT NULL CHECK (length(target_sha) = 40),
    build_id      INTEGER REFERENCES builds(id),
    released_at   TEXT NOT NULL,
    url           TEXT,
    source_system TEXT NOT NULL CHECK (source_system IN ('github', 'backlog', 'synthetic')),
    source_id     TEXT NOT NULL,
    raw           TEXT NOT NULL CHECK (json_valid(raw)),
    raw_hash      TEXT NOT NULL,
    synced_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (source_system, source_id),
    UNIQUE (repo, tag)
);

-- One test's outcome in one build (from JUnit reports or the synthetic seed).
CREATE TABLE IF NOT EXISTS test_runs (
    id            INTEGER PRIMARY KEY,
    build_id      INTEGER NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    test_case_id  INTEGER REFERENCES test_cases(id),
    path          TEXT NOT NULL,
    name          TEXT NOT NULL,
    outcome       TEXT NOT NULL CHECK (outcome IN ('passed', 'failed', 'skipped', 'error')),
    duration_ms   INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    message       TEXT,
    UNIQUE (build_id, path, name)
);

-- Evidence that a criterion was (or wasn't) met, on one specific build.
-- source 'test_runs' rows are derived automatically; 'manual' rows are sign-offs for
-- analysis, inspection and demonstration.
CREATE TABLE IF NOT EXISTS verification_results (
    id                 INTEGER PRIMARY KEY,
    criterion_id       INTEGER NOT NULL REFERENCES acceptance_criteria(id),
    build_id           INTEGER NOT NULL REFERENCES builds(id),
    method             TEXT NOT NULL CHECK (method IN ('test', 'analysis', 'inspection', 'demonstration')),
    result             TEXT NOT NULL CHECK (result IN ('passed', 'failed')),
    source             TEXT NOT NULL CHECK (source IN ('test_runs', 'manual')),
    evidence           TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(evidence)),
    verified_by        TEXT NOT NULL,
    verified_at        TEXT NOT NULL,
    note               TEXT,
    invalidated_at     TEXT,                  -- set by change control
    invalidated_reason TEXT,
    UNIQUE (criterion_id, build_id, method, source),
    CHECK ((invalidated_at IS NULL) = (invalidated_reason IS NULL))
);

-- Opened automatically when a requirement changes: every criterion under it must be
-- verified again against the new version. Closed by the first valid passing result
-- recorded after the change; superseded (not closed) when a newer version of the same
-- requirement opens its own item for the criterion.
CREATE TABLE IF NOT EXISTS reverification_items (
    id                        INTEGER PRIMARY KEY,
    requirement_id            INTEGER NOT NULL REFERENCES requirements(id),
    requirement_version       INTEGER NOT NULL CHECK (requirement_version >= 2),
    criterion_id              INTEGER NOT NULL REFERENCES acceptance_criteria(id),
    opened_at                 TEXT NOT NULL,
    status                    TEXT NOT NULL CHECK (status IN ('open', 'closed', 'superseded')),
    closed_at                 TEXT,
    closed_by_verification_id INTEGER REFERENCES verification_results(id),
    UNIQUE (requirement_id, requirement_version, criterion_id),
    CHECK ((status = 'open') = (closed_at IS NULL)),
    CHECK (closed_by_verification_id IS NULL OR status = 'closed')
);

-- Data classification per repository. 'controlled' (export-controlled, e.g. ITAR/EAR)
-- may never be sent to an external model. Repositories not listed are treated as
-- controlled: the policy fails closed.
CREATE TABLE IF NOT EXISTS repo_policies (
    repo           TEXT PRIMARY KEY,
    classification TEXT NOT NULL CHECK (classification IN ('public', 'internal', 'controlled')),
    note           TEXT,
    set_by         TEXT NOT NULL,
    set_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS ix_builds_repo_time       ON builds (repo, finished_at);
CREATE INDEX IF NOT EXISTS ix_builds_sha             ON builds (repo, head_sha);
CREATE INDEX IF NOT EXISTS ix_builds_commit          ON builds (commit_id);
CREATE INDEX IF NOT EXISTS ix_releases_build         ON releases (build_id);
CREATE INDEX IF NOT EXISTS ix_test_runs_case         ON test_runs (test_case_id);
CREATE INDEX IF NOT EXISTS ix_verif_criterion_build  ON verification_results (criterion_id, build_id);
CREATE INDEX IF NOT EXISTS ix_verif_build            ON verification_results (build_id);
CREATE INDEX IF NOT EXISTS ix_reverif_open           ON reverification_items (criterion_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS ix_reverif_requirement    ON reverification_items (requirement_id);

-- Staging accepts the new entity types. SQLite can't alter a CHECK constraint, so the
-- table is rebuilt (the documented SQLite procedure); nothing references it by FK.
CREATE TABLE staging_records_new (
    id            INTEGER PRIMARY KEY,
    batch_id      INTEGER NOT NULL REFERENCES ingest_batches(id),
    source_system TEXT NOT NULL,
    entity        TEXT NOT NULL CHECK (entity IN (
                      'person', 'feature', 'story', 'task', 'criterion', 'requirement',
                      'commit', 'pull_request', 'pr_commits', 'review', 'issue', 'link',
                      'build', 'release', 'test_report', 'verification')),
    source_id     TEXT NOT NULL,
    payload       TEXT NOT NULL CHECK (json_valid(payload)),
    payload_hash  TEXT NOT NULL,
    fetched_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    processed_at  TEXT,
    error         TEXT,
    UNIQUE (source_system, entity, source_id, payload_hash)
);
INSERT INTO staging_records_new SELECT id, batch_id, source_system, entity, source_id, payload, payload_hash,
    fetched_at, processed_at, error FROM staging_records;
DROP TABLE staging_records;
ALTER TABLE staging_records_new RENAME TO staging_records;
CREATE INDEX IF NOT EXISTS ix_staging_batch ON staging_records (batch_id);
CREATE INDEX IF NOT EXISTS ix_staging_unprocessed ON staging_records (entity, id) WHERE processed_at IS NULL;
