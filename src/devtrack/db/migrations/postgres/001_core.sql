-- 001_core.sql (Postgres)
-- Core entities, link tables, staging and sync bookkeeping.
--
-- Conventions
--   * Every entity row carries source_system, source_id, raw (original record as JSONB),
--     raw_hash (sha256 of the canonical raw JSON) and synced_at (last time the row was
--     written from its source). (source_system, source_id) is the idempotency key.
--   * source_id is namespaced so it is globally unique on its own too, e.g.
--     'pallets/click@2247b35...' for a commit, 'pallets/click#pr-3877' for a PR.
--   * Only constraint-backed indexes (PK / UNIQUE) live here. Query indexes are in
--     002_indexes.sql so the benchmark can measure before/after.

CREATE TABLE people (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    login         TEXT UNIQUE,                     -- GitHub login when known
    display_name  TEXT NOT NULL,
    email_hash    TEXT UNIQUE,                     -- sha256 of lower-cased email; raw email is never stored
    is_bot        BOOLEAN NOT NULL DEFAULT FALSE,
    source_system TEXT NOT NULL CHECK (source_system IN ('github', 'backlog', 'synthetic')),
    source_id     TEXT NOT NULL,
    raw           JSONB NOT NULL,
    raw_hash      TEXT NOT NULL,
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_system, source_id)
);

CREATE TABLE features (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,            -- e.g. FEAT-CLICK-01
    repo          TEXT NOT NULL,                   -- owner/name the feature ships in
    title         TEXT NOT NULL,
    description   TEXT,
    status        TEXT NOT NULL CHECK (status IN ('planned', 'in_progress', 'done')),
    owner_id      BIGINT REFERENCES people(id),
    created_at    TIMESTAMPTZ NOT NULL,
    source_system TEXT NOT NULL CHECK (source_system IN ('github', 'backlog', 'synthetic')),
    source_id     TEXT NOT NULL,
    raw           JSONB NOT NULL,
    raw_hash      TEXT NOT NULL,
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_system, source_id)
);

CREATE TABLE stories (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    feature_id    BIGINT NOT NULL REFERENCES features(id),
    key           TEXT NOT NULL UNIQUE,            -- e.g. ST-CLICK-104
    title         TEXT NOT NULL,
    description   TEXT,
    status        TEXT NOT NULL CHECK (status IN ('backlog', 'in_progress', 'in_review', 'done')),
    points        INTEGER CHECK (points IS NULL OR points > 0),
    started_at    TIMESTAMPTZ,                     -- cycle time starts here
    completed_at  TIMESTAMPTZ,
    source_system TEXT NOT NULL CHECK (source_system IN ('github', 'backlog', 'synthetic')),
    source_id     TEXT NOT NULL,
    raw           JSONB NOT NULL,
    raw_hash      TEXT NOT NULL,
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_system, source_id),
    CHECK (completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at)
);

CREATE TABLE tasks (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    story_id      BIGINT REFERENCES stories(id),   -- NULL only for GitHub issues not yet triaged
    key           TEXT NOT NULL UNIQUE,            -- TASK-CLICK-0007, or GH-pallets/click-123
    repo          TEXT NOT NULL,
    title         TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('todo', 'in_progress', 'done', 'closed')),
    issue_number  INTEGER,                         -- GitHub issue number when the task is an issue
    assignee_id   BIGINT REFERENCES people(id),
    created_at    TIMESTAMPTZ NOT NULL,
    closed_at     TIMESTAMPTZ,
    source_system TEXT NOT NULL CHECK (source_system IN ('github', 'backlog', 'synthetic')),
    source_id     TEXT NOT NULL,
    raw           JSONB NOT NULL,
    raw_hash      TEXT NOT NULL,
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_system, source_id),
    UNIQUE (repo, issue_number),
    -- planned work must sit under a story; only raw GitHub issues may float
    CHECK (source_system = 'github' OR story_id IS NOT NULL)
);

CREATE TABLE acceptance_criteria (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    story_id      BIGINT NOT NULL REFERENCES stories(id),
    key           TEXT NOT NULL UNIQUE,            -- AC-CLICK-104-2
    text          TEXT NOT NULL CHECK (length(text) > 0),
    created_at    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL,
    source_system TEXT NOT NULL CHECK (source_system IN ('github', 'backlog', 'synthetic')),
    source_id     TEXT NOT NULL,
    raw           JSONB NOT NULL,
    raw_hash      TEXT NOT NULL,
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_system, source_id)
);

CREATE TABLE requirements (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,            -- REQ-CLICK-03
    title         TEXT NOT NULL,
    body          TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    author_id     BIGINT REFERENCES people(id),
    created_at    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL,
    source_system TEXT NOT NULL CHECK (source_system IN ('github', 'backlog', 'synthetic')),
    source_id     TEXT NOT NULL,
    raw           JSONB NOT NULL,
    raw_hash      TEXT NOT NULL,
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_system, source_id),
    CHECK (updated_at >= created_at)
);

-- Every version of a requirement, so "changed after code merged" can say what changed.
CREATE TABLE requirement_revisions (
    requirement_id BIGINT NOT NULL REFERENCES requirements(id),
    version        INTEGER NOT NULL CHECK (version >= 1),
    body           TEXT NOT NULL,
    change_note    TEXT,
    changed_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (requirement_id, version)
);

CREATE TABLE commits (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repo          TEXT NOT NULL,
    sha           TEXT NOT NULL CHECK (length(sha) = 40),
    author_id     BIGINT REFERENCES people(id),
    authored_at   TIMESTAMPTZ,
    committed_at  TIMESTAMPTZ NOT NULL,
    message       TEXT NOT NULL,
    is_merge      BOOLEAN NOT NULL DEFAULT FALSE,
    additions     INTEGER CHECK (additions IS NULL OR additions >= 0),
    deletions     INTEGER CHECK (deletions IS NULL OR deletions >= 0),
    files_changed INTEGER CHECK (files_changed IS NULL OR files_changed >= 0),
    diff          TEXT,                            -- unified diff, truncated per file (see ingest/normalize.py)
    source_system TEXT NOT NULL CHECK (source_system IN ('github', 'backlog', 'synthetic')),
    source_id     TEXT NOT NULL,
    raw           JSONB NOT NULL,
    raw_hash      TEXT NOT NULL,
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_system, source_id),
    UNIQUE (repo, sha)
);

CREATE TABLE pull_requests (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repo             TEXT NOT NULL,
    number           INTEGER NOT NULL CHECK (number > 0),
    title            TEXT NOT NULL,
    body             TEXT,
    state            TEXT NOT NULL CHECK (state IN ('open', 'closed', 'merged')),
    author_id        BIGINT REFERENCES people(id),
    created_at       TIMESTAMPTZ,
    merged_at        TIMESTAMPTZ,
    closed_at        TIMESTAMPTZ,
    merge_commit_sha TEXT,
    head_ref         TEXT,
    base_ref         TEXT,
    source_system    TEXT NOT NULL CHECK (source_system IN ('github', 'backlog', 'synthetic')),
    source_id        TEXT NOT NULL,
    raw              JSONB NOT NULL,
    raw_hash         TEXT NOT NULL,
    synced_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_system, source_id),
    UNIQUE (repo, number),
    CHECK (state <> 'merged' OR merged_at IS NOT NULL)
);

CREATE TABLE reviews (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pull_request_id BIGINT NOT NULL REFERENCES pull_requests(id),
    reviewer_id     BIGINT NOT NULL REFERENCES people(id),
    state           TEXT NOT NULL CHECK (state IN ('APPROVED', 'CHANGES_REQUESTED', 'COMMENTED', 'DISMISSED')),
    body            TEXT,
    submitted_at    TIMESTAMPTZ NOT NULL,
    source_system   TEXT NOT NULL CHECK (source_system IN ('github', 'backlog', 'synthetic')),
    source_id       TEXT NOT NULL,
    raw             JSONB NOT NULL,
    raw_hash        TEXT NOT NULL,
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_system, source_id)
);

-- Test functions detected in diffs (added "def test_..." lines), so criteria can link to tests.
CREATE TABLE test_cases (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repo          TEXT NOT NULL,
    path          TEXT NOT NULL,
    name          TEXT NOT NULL,
    commit_id     BIGINT REFERENCES commits(id),   -- commit that introduced it
    source_system TEXT NOT NULL CHECK (source_system IN ('github', 'backlog', 'synthetic')),
    source_id     TEXT NOT NULL,
    raw           JSONB NOT NULL,
    raw_hash      TEXT NOT NULL,
    synced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_system, source_id),
    UNIQUE (repo, path, name)
);

-- ---------------------------------------------------------------------------
-- Link tables (many-to-many). Composite PK = no duplicate links, and doubles as
-- the index for the left-to-right direction. The reverse index is in 002.
-- ---------------------------------------------------------------------------
CREATE TABLE task_criteria (
    task_id      BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    criterion_id BIGINT NOT NULL REFERENCES acceptance_criteria(id) ON DELETE CASCADE,
    link_source  TEXT NOT NULL CHECK (link_source IN ('backlog_map', 'manual')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, criterion_id)
);

CREATE TABLE commit_tasks (
    commit_id   BIGINT NOT NULL REFERENCES commits(id) ON DELETE CASCADE,
    task_id     BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    link_source TEXT NOT NULL CHECK (link_source IN ('message_ref', 'pr_ref', 'backlog_map', 'manual')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (commit_id, task_id)
);

CREATE TABLE requirement_criteria (
    requirement_id BIGINT NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    criterion_id   BIGINT NOT NULL REFERENCES acceptance_criteria(id) ON DELETE CASCADE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (requirement_id, criterion_id)
);

CREATE TABLE pull_request_commits (
    pull_request_id BIGINT NOT NULL REFERENCES pull_requests(id) ON DELETE CASCADE,
    commit_id       BIGINT NOT NULL REFERENCES commits(id) ON DELETE CASCADE,
    PRIMARY KEY (pull_request_id, commit_id)
);

CREATE TABLE criterion_tests (
    criterion_id BIGINT NOT NULL REFERENCES acceptance_criteria(id) ON DELETE CASCADE,
    test_case_id BIGINT NOT NULL REFERENCES test_cases(id) ON DELETE CASCADE,
    link_source  TEXT NOT NULL CHECK (link_source IN ('backlog_map', 'manual')),
    PRIMARY KEY (criterion_id, test_case_id)
);

-- ---------------------------------------------------------------------------
-- Staging and sync bookkeeping
-- ---------------------------------------------------------------------------
CREATE TABLE ingest_batches (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_system TEXT NOT NULL,
    scope         TEXT NOT NULL,                   -- repo name, or 'backlog'
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    status        TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    stats         JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Raw records land here first, exactly as fetched. Identical re-fetches are no-ops
-- (unique on payload_hash); a changed record gets a new staging row.
CREATE TABLE staging_records (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id      BIGINT NOT NULL REFERENCES ingest_batches(id),
    source_system TEXT NOT NULL,
    entity        TEXT NOT NULL CHECK (entity IN (
                      'person', 'feature', 'story', 'task', 'criterion', 'requirement',
                      'commit', 'pull_request', 'pr_commits', 'review', 'issue', 'link')),
    source_id     TEXT NOT NULL,
    payload       JSONB NOT NULL,
    payload_hash  TEXT NOT NULL,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at  TIMESTAMPTZ,
    error         TEXT,
    UNIQUE (source_system, entity, source_id, payload_hash)
);

-- Incremental sync watermark per source, scope and entity. The watermark is the
-- newest source-side updated_at we have fully processed (not our wall clock).
CREATE TABLE sync_state (
    source_system TEXT NOT NULL,
    scope         TEXT NOT NULL,
    entity        TEXT NOT NULL,
    watermark     TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_system, scope, entity)
);
