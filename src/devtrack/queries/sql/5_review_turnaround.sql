-- 5. Review turnaround by reviewer: hours from PR opened to that reviewer's first review.
WITH first_reviews AS (
    SELECT reviewer_id, pull_request_id, MIN(submitted_at) AS first_at
    FROM reviews
    GROUP BY reviewer_id, pull_request_id
)
SELECT p.display_name AS reviewer,
       p.source_system,
       COUNT(*) AS prs_reviewed,
       ROUND(CAST(AVG(HOURS_BETWEEN(pr.created_at, fr.first_at)) AS NUMERIC), 1) AS avg_hours,
       ROUND(CAST(MIN(HOURS_BETWEEN(pr.created_at, fr.first_at)) AS NUMERIC), 1) AS fastest_hours,
       ROUND(CAST(MAX(HOURS_BETWEEN(pr.created_at, fr.first_at)) AS NUMERIC), 1) AS slowest_hours
FROM first_reviews fr
JOIN pull_requests pr ON pr.id = fr.pull_request_id
JOIN people p ON p.id = fr.reviewer_id
WHERE pr.created_at IS NOT NULL
GROUP BY p.id, p.display_name, p.source_system
ORDER BY avg_hours DESC;
