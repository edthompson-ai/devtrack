-- 005_reader_role.sql (Postgres only)
-- The question box connects as devtrack_reader. Docker compose creates that role at
-- first start (scripts/docker/initdb); this grants it SELECT on the allow-listed
-- tables only. Raw staging payloads, sync state and embeddings stay out of reach.
-- Without the role (tests, other setups) this is a no-op.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'devtrack_reader') THEN
        GRANT USAGE ON SCHEMA public TO devtrack_reader;
        GRANT SELECT ON
            features, stories, tasks, acceptance_criteria, requirements, requirement_revisions,
            commits, pull_requests, reviews, people, test_cases,
            task_criteria, commit_tasks, requirement_criteria, pull_request_commits, criterion_tests,
            ai_verdicts, verdict_reviews, counted_verdicts, eval_labels, eval_runs, eval_results
        TO devtrack_reader;
    END IF;
END
$$;
