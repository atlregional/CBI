-- ============================================================================
-- CBI INTERSECTION SEVERITY INDEX — combine bottlenecks that share a
-- physical intersection across directions/corridors into one per-location
-- score, Arterial roads only
-- PostgreSQL 17.x | Schema: "Year_2025"
--
-- SAFE TO RE-RUN. Requires sql/007_bottleneck_ranking_eligibility.sql.
--
-- Why: a single physical intersection (e.g. GA-20 x US-23/GA-42) shows up
-- as up to two separate bottleneck rows in segment_recurring_bottlenecks —
-- one per direction of the through road, sometimes more if a cross corridor
-- was also analyzed. Region-wide dashboards rank those directional
-- bottlenecks independently, which understates a genuinely bad
-- intersection that congests in both directions and overstates one that's
-- only bad in a single direction. This view groups by
-- representative_intersection (restricted to Arterial-group corridors,
-- since that's where signal-controlled, cross-direction intersections
-- exist — Interstates/Expressways don't have "intersections" in this
-- sense) and sums severity across every contributing, ranking-eligible
-- bottleneck at that location. ranking_eligible = false rows (confirmed
-- false-flagged by the overnight-congestion diagnostic in
-- cbi_bottleneck_ranking_eligibility.py) are excluded, so a location that
-- only "looks" bad because of the flat-reference-speed artifact doesn't
-- appear here.
-- ============================================================================

BEGIN;

DROP VIEW IF EXISTS "Year_2025".vw_intersection_severity_index;

CREATE VIEW "Year_2025".vw_intersection_severity_index AS
SELECT
    v.representative_intersection,
    COUNT(*) AS contributing_bottlenecks,
    STRING_AGG(DISTINCT v.corridor || ' ' || INITCAP(LOWER(v.direction)), ', ' ORDER BY v.corridor || ' ' || INITCAP(LOWER(v.direction))) AS corridors,
    ROUND(SUM(v.severity_index)::numeric, 2) AS intersection_severity_index,
    ROUND(AVG(v.occurrence_pct)::numeric, 1) AS avg_occurrence_pct,
    ROUND(SUM(v.annual_queue_mile_hours)::numeric, 2) AS total_annual_queue_mile_hours,
    ROUND(AVG(v.avg_congested_speed_ratio)::numeric, 3) AS avg_congested_speed_ratio,
    RANK() OVER (ORDER BY SUM(v.severity_index) DESC) AS intersection_severity_rank
FROM "Year_2025".vw_bottleneck_dashboard_ranked AS v
JOIN "Year_2025".corridor_definitions AS cd
  ON cd.road = v.corridor AND cd.direction = v.direction AND cd.is_active
WHERE cd.corridor_group = 'Arterial'
  AND v.ranking_eligible
  AND v.representative_intersection IS NOT NULL
GROUP BY v.representative_intersection
ORDER BY intersection_severity_index DESC;

COMMIT;
