-- 2. Commits not tied to any task (merge commits excluded: they carry no work of their own).
-- NOT EXISTS is an anti-join: the planner stops at the first matching link.
SELECT c.repo,
       c.sha,
       c.message,
       c.committed_at,
       c.source_system,
       p.display_name AS author,
       (SELECT pr.number
          FROM pull_request_commits prc
          JOIN pull_requests pr ON pr.id = prc.pull_request_id
         WHERE prc.commit_id = c.id
         ORDER BY pr.number
         LIMIT 1) AS pr_number
FROM commits c
LEFT JOIN people p ON p.id = c.author_id
WHERE NOT c.is_merge
  AND NOT EXISTS (SELECT 1 FROM commit_tasks ct WHERE ct.commit_id = c.id)
ORDER BY c.committed_at DESC;
