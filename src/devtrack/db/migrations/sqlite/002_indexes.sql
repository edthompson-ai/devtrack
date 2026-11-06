-- 002_indexes.sql (SQLite)
-- Query indexes. Kept apart from 001 so scripts/benchmark_queries.sh can run the saved
-- queries with and without them. PK/UNIQUE indexes from 001 exist in both runs.

-- Foreign keys. SQLite does not index FK columns automatically either; without these,
-- joins from child to parent and ON DELETE checks scan the child table.
CREATE INDEX IF NOT EXISTS ix_features_owner         ON features (owner_id);
CREATE INDEX IF NOT EXISTS ix_stories_feature        ON stories (feature_id);
CREATE INDEX IF NOT EXISTS ix_tasks_story            ON tasks (story_id);
CREATE INDEX IF NOT EXISTS ix_tasks_assignee         ON tasks (assignee_id);
CREATE INDEX IF NOT EXISTS ix_criteria_story         ON acceptance_criteria (story_id);
CREATE INDEX IF NOT EXISTS ix_requirements_author    ON requirements (author_id);
CREATE INDEX IF NOT EXISTS ix_commits_author         ON commits (author_id);
CREATE INDEX IF NOT EXISTS ix_prs_author             ON pull_requests (author_id);
CREATE INDEX IF NOT EXISTS ix_reviews_pr             ON reviews (pull_request_id);
CREATE INDEX IF NOT EXISTS ix_test_cases_commit      ON test_cases (commit_id);
CREATE INDEX IF NOT EXISTS ix_staging_batch          ON staging_records (batch_id);

-- Link tables: the composite PK covers (left, right); add (right, left) so the
-- join works fast from either side.
CREATE INDEX IF NOT EXISTS ix_task_criteria_rev      ON task_criteria (criterion_id, task_id);
CREATE INDEX IF NOT EXISTS ix_commit_tasks_rev       ON commit_tasks (task_id, commit_id);
CREATE INDEX IF NOT EXISTS ix_req_criteria_rev       ON requirement_criteria (criterion_id, requirement_id);
CREATE INDEX IF NOT EXISTS ix_pr_commits_rev         ON pull_request_commits (commit_id, pull_request_id);
CREATE INDEX IF NOT EXISTS ix_criterion_tests_rev    ON criterion_tests (test_case_id, criterion_id);

-- Status and date filters used by the dashboard and saved queries.
CREATE INDEX IF NOT EXISTS ix_stories_status_started ON stories (status, started_at);
CREATE INDEX IF NOT EXISTS ix_tasks_status           ON tasks (status);
CREATE INDEX IF NOT EXISTS ix_commits_repo_time      ON commits (repo, committed_at);
CREATE INDEX IF NOT EXISTS ix_prs_merged             ON pull_requests (merged_at) WHERE merged_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_prs_merge_sha          ON pull_requests (merge_commit_sha);
CREATE INDEX IF NOT EXISTS ix_reviews_reviewer_time  ON reviews (reviewer_id, submitted_at);
CREATE INDEX IF NOT EXISTS ix_requirements_updated   ON requirements (updated_at);
CREATE INDEX IF NOT EXISTS ix_staging_unprocessed    ON staging_records (entity, id) WHERE processed_at IS NULL;
