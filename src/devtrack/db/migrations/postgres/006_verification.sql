-- 006_verification.sql (Postgres)
-- Turns the tracker into a verification system:
--   * every requirement names HOW it is verified (test, analysis, inspection, demonstration)
--   * builds and releases join the chain: commit -> build -> release
--   * test runs and manual sign-offs become verification results tied to one build
--   * a requirement change invalidates earlier results and opens re-verification items
--   * each repository carries a data classification that decides which models may see it

ALTER TABLE requirements
    ADD COLUMN IF NOT EXISTS verification_method TEXT NOT NULL DEFAULT 'test'
    CHECK (verification_method IN ('test', 'analysis', 'inspection', 'demonstration'));

-- A CI run of one commit. Real ones come from the GitHub Actions API; the offline seed
-- generates synthetic ones (flagged).
CREATE TABLE IF NOT EXISTS builds (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repo          TEXT NOT NULL,
    workflow      TEXT NOT NULL,                    -- run numbers count per workflow
    run_number    INTEGER NOT NULL CHECK (run_number > 0),
    head_sha      TEXT NOT NULL CHECK (length(head_sha) = 40),
    commit_id     BIGINT REFERENCES commits(id),   -- NULL when the commit isn't loaded (e.g. a release branch)
    branch        TEXT,
    event         TEXT NOT NULL,                    -- push, pull_request, release, ...
    status        TEXT NOT NULL CHECK (status IN ('success', 'failure', 'cancelled', 'in_progress')),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    url           TEXT,
    source_system TEXT NOT NULL CHECK (source_system IN ('github', 'backlog', 'synthetic')),
    source_id     TEXT NOT NULL,
    raw           JSONB NOT NULL,
    raw_hash      TEXT NOT NULL,
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_system, source_id),
    UNIQUE (repo, workflow, run_number, source_system),
    CHECK (finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at)
);

-- A shipped version: a tag pointing at a commit, built by a specific build.
CREATE TABLE IF NOT EXISTS releases (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repo          TEXT NOT NULL,
    tag           TEXT NOT NULL,
    target_sha    TEXT NOT NULL CHECK (length(target_sha) = 40),
    build_id      BIGINT REFERENCES builds(id),
    released_at   TIMESTAMPTZ NOT NULL,
    url           TEXT,
    source_system TEXT NOT NULL CHECK (source_system IN ('github', 'backlog', 'synthetic')),
    source_id     TEXT NOT NULL,
    raw           JSONB NOT NULL,
    raw_hash      TEXT NOT NULL,
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_system, source_id),
    UNIQUE (repo, tag)
);

-- One test's outcome in one build (from JUnit reports or the synthetic seed).
CREATE TABLE IF NOT EXISTS test_runs (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    build_id      BIGINT NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    test_case_id  BIGINT REFERENCES test_cases(id),
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
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    criterion_id       BIGINT NOT NULL REFERENCES acceptance_criteria(id),
    build_id           BIGINT NOT NULL REFERENCES builds(id),
    method             TEXT NOT NULL CHECK (method IN ('test', 'analysis', 'inspection', 'demonstration')),
    result             TEXT NOT NULL CHECK (result IN ('passed', 'failed')),
    source             TEXT NOT NULL CHECK (source IN ('test_runs', 'manual')),
    evidence           JSONB NOT NULL DEFAULT '[]'::jsonb,
    verified_by        TEXT NOT NULL,
    verified_at        TIMESTAMPTZ NOT NULL,
    note               TEXT,
    invalidated_at     TIMESTAMPTZ,                  -- set by change control
    invalidated_reason TEXT,
    UNIQUE (criterion_id, build_id, method, source),
    CHECK ((invalidated_at IS NULL) = (invalidated_reason IS NULL))
);

-- Opened automatically when a requirement changes: every criterion under it must be
-- verified again against the new version. Closed by the first valid passing result
-- recorded after the change; superseded (not closed) when a newer version of the same
-- requirement opens its own item for the criterion.
CREATE TABLE IF NOT EXISTS reverification_items (
    id                        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    requirement_id            BIGINT NOT NULL REFERENCES requirements(id),
    requirement_version       INTEGER NOT NULL CHECK (requirement_version >= 2),
    criterion_id              BIGINT NOT NULL REFERENCES acceptance_criteria(id),
    opened_at                 TIMESTAMPTZ NOT NULL,
    status                    TEXT NOT NULL CHECK (status IN ('open', 'closed', 'superseded')),
    closed_at                 TIMESTAMPTZ,
    closed_by_verification_id BIGINT REFERENCES verification_results(id),
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
    set_at         TIMESTAMPTZ NOT NULL DEFAULT now()
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

-- Staging accepts the new entity types.
ALTER TABLE staging_records DROP CONSTRAINT IF EXISTS staging_records_entity_check;
ALTER TABLE staging_records ADD CONSTRAINT staging_records_entity_check CHECK (entity IN (
    'person', 'feature', 'story', 'task', 'criterion', 'requirement',
    'commit', 'pull_request', 'pr_commits', 'review', 'issue', 'link',
    'build', 'release', 'test_report', 'verification'));

-- The question box may read the new tables (no-op without the role).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'devtrack_reader') THEN
        GRANT SELECT ON builds, releases, test_runs, verification_results, reverification_items, repo_policies
        TO devtrack_reader;
    END IF;
END
$$;
