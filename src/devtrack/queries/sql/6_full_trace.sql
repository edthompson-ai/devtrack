-- 6. Full trace for one requirement: requirement -> criteria -> tasks -> commits -> PRs -> reviews.
-- LEFT JOINs keep the gaps visible: a criterion with no task shows NULLs to the right.
SELECT r.key AS requirement,
       a.key AS criterion,
       a.text AS criterion_text,
       t.key AS task,
       t.status AS task_status,
       c.repo,
       c.sha,
       c.committed_at,
       pr.number AS pr_number,
       pr.title AS pr_title,
       pr.merged_at,
       rv.state AS review_state,
       rp.display_name AS reviewer,
       rv.submitted_at AS reviewed_at
FROM requirements r
JOIN requirement_criteria rc ON rc.requirement_id = r.id
JOIN acceptance_criteria a ON a.id = rc.criterion_id
LEFT JOIN task_criteria tc ON tc.criterion_id = a.id
LEFT JOIN tasks t ON t.id = tc.task_id
LEFT JOIN commit_tasks ct ON ct.task_id = t.id
LEFT JOIN commits c ON c.id = ct.commit_id
LEFT JOIN pull_request_commits prc ON prc.commit_id = c.id
LEFT JOIN pull_requests pr ON pr.id = prc.pull_request_id
LEFT JOIN reviews rv ON rv.pull_request_id = pr.id
LEFT JOIN people rp ON rp.id = rv.reviewer_id
WHERE r.key = ?
ORDER BY a.key, t.key, c.committed_at, rv.submitted_at;
