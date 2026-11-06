-- 1. Acceptance criteria with no linked commits or no linked tests.
-- A criterion is "covered by code" when a task linked to it has at least one commit.
-- Ordered so criteria with neither code nor tests come first.
WITH crit_commits AS (
    SELECT tc.criterion_id, COUNT(DISTINCT ct.commit_id) AS commits
    FROM task_criteria tc
    JOIN commit_tasks ct ON ct.task_id = tc.task_id
    GROUP BY tc.criterion_id
),
crit_tests AS (
    SELECT criterion_id, COUNT(*) AS tests
    FROM criterion_tests
    GROUP BY criterion_id
)
SELECT f.key AS feature,
       s.key AS story,
       a.key AS criterion,
       a.text AS criterion_text,
       COALESCE(cc.commits, 0) AS linked_commits,
       COALESCE(ct.tests, 0) AS linked_tests,
       CASE
           WHEN COALESCE(cc.commits, 0) = 0 AND COALESCE(ct.tests, 0) = 0 THEN 'no code, no tests'
           WHEN COALESCE(cc.commits, 0) = 0 THEN 'no code'
           ELSE 'no tests'
       END AS gap
FROM acceptance_criteria a
JOIN stories s ON s.id = a.story_id
JOIN features f ON f.id = s.feature_id
LEFT JOIN crit_commits cc ON cc.criterion_id = a.id
LEFT JOIN crit_tests ct ON ct.criterion_id = a.id
WHERE COALESCE(cc.commits, 0) = 0 OR COALESCE(ct.tests, 0) = 0
ORDER BY CASE WHEN COALESCE(cc.commits, 0) = 0 THEN 0 ELSE 1 END, a.key;
