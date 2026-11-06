-- 4. Requirements changed after the code for one of their criteria merged.
-- Reported per (requirement, criterion): those criteria were built against an older
-- version of the requirement and need re-checking.
WITH criterion_code AS (
    SELECT tc.criterion_id, MAX(pr.merged_at) AS last_merge
    FROM task_criteria tc
    JOIN commit_tasks ct ON ct.task_id = tc.task_id
    JOIN pull_request_commits prc ON prc.commit_id = ct.commit_id
    JOIN pull_requests pr ON pr.id = prc.pull_request_id
    WHERE pr.merged_at IS NOT NULL
    GROUP BY tc.criterion_id
)
SELECT r.key AS requirement,
       r.title,
       r.version,
       a.key AS criterion,
       cc.last_merge AS code_merged_at,
       r.updated_at AS requirement_changed_at,
       rev.change_note
FROM requirements r
JOIN requirement_criteria rc ON rc.requirement_id = r.id
JOIN acceptance_criteria a ON a.id = rc.criterion_id
JOIN criterion_code cc ON cc.criterion_id = rc.criterion_id
LEFT JOIN requirement_revisions rev ON rev.requirement_id = r.id AND rev.version = r.version
WHERE r.updated_at > cc.last_merge
ORDER BY r.updated_at DESC, r.key, a.key;
