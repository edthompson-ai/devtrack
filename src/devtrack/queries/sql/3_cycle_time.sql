-- 3. Cycle time: from story start to the merge of the last PR carrying its work.
-- HOURS_BETWEEN(a, b) is expanded per dialect by queries/saved.py.
WITH story_merges AS (
    SELECT t.story_id,
           MAX(pr.merged_at) AS last_merge,
           COUNT(DISTINCT pr.id) AS prs
    FROM tasks t
    JOIN commit_tasks ct ON ct.task_id = t.id
    JOIN pull_request_commits prc ON prc.commit_id = ct.commit_id
    JOIN pull_requests pr ON pr.id = prc.pull_request_id
    WHERE pr.merged_at IS NOT NULL
    GROUP BY t.story_id
)
SELECT f.key AS feature,
       s.key AS story,
       s.title,
       s.status,
       s.started_at,
       sm.last_merge,
       sm.prs,
       ROUND(CAST(HOURS_BETWEEN(s.started_at, sm.last_merge) AS NUMERIC), 1) AS cycle_hours,
       ROUND(CAST(HOURS_BETWEEN(s.started_at, sm.last_merge) / 24.0 AS NUMERIC), 1) AS cycle_days
FROM stories s
JOIN features f ON f.id = s.feature_id
JOIN story_merges sm ON sm.story_id = s.id
WHERE s.started_at IS NOT NULL
ORDER BY cycle_hours DESC;
