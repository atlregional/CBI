-- ============================================================================
-- CBI INTERSECTION SEVERITY LOCATION — add representative coordinates,
-- county, and CID to the intersection severity index, for its own map tab
-- PostgreSQL 17.x | Schema: "Year_2025"
--
-- SAFE TO RE-RUN. Requires sql/009_aadt_weighted_severity.sql and
-- scripts/cbi_build_cid_lookup.py (creates "Year_2025".tmc_cid) to have
-- been applied already.
--
-- An intersection's row already combines multiple bottlenecks that share a
-- representative_intersection name (different directions, sometimes
-- different corridors) — for a single map marker, this picks the peak
-- segment of whichever contributing bottleneck has the single highest
-- aadt_weighted_severity_index as "the" representative location. All
-- contributing bottlenecks are for the same physical intersection, so
-- their peak segments should sit at essentially the same coordinates
-- regardless of which one is picked.
-- ============================================================================

BEGIN;

DROP VIEW IF EXISTS "Year_2025".vw_intersection_severity_index;

CREATE VIEW "Year_2025".vw_intersection_severity_index AS
WITH ranked_bottlenecks AS (
    SELECT
        v.*,
        cd.corridor_id,
        ROW_NUMBER() OVER (
            PARTITION BY v.representative_intersection
            ORDER BY v.aadt_weighted_severity_index DESC
        ) AS pick_rank
    FROM "Year_2025".vw_bottleneck_dashboard_ranked AS v
    JOIN "Year_2025".corridor_definitions AS cd
      ON cd.road = v.corridor AND cd.direction = v.direction AND cd.is_active
    WHERE cd.corridor_group = 'Arterial'
      AND v.ranking_eligible
      AND v.representative_intersection IS NOT NULL
),
representative_location AS (
    SELECT
        rb.representative_intersection,
        cs.tmc,
        (cs.start_latitude + cs.end_latitude) / 2.0 AS latitude,
        (cs.start_longitude + cs.end_longitude) / 2.0 AS longitude,
        t.county,
        tc.cid_name
    FROM ranked_bottlenecks AS rb
    JOIN "Year_2025".corridor_segments AS cs
      ON cs.corridor_id = rb.corridor_id AND cs.segment_order = rb.peak_segment_order
    JOIN "Year_2025".tmc_metadata AS t ON t.tmc = cs.tmc
    LEFT JOIN "Year_2025".tmc_cid AS tc ON tc.tmc = cs.tmc
    WHERE rb.pick_rank = 1
)
SELECT
    v.representative_intersection,
    COUNT(*) AS contributing_bottlenecks,
    STRING_AGG(DISTINCT v.corridor || ' ' || INITCAP(LOWER(v.direction)), ', ' ORDER BY v.corridor || ' ' || INITCAP(LOWER(v.direction))) AS corridors,
    ROUND(SUM(v.severity_index)::numeric, 2) AS intersection_severity_index,
    ROUND(SUM(v.aadt_weighted_severity_index)::numeric, 2) AS aadt_weighted_intersection_severity_index,
    ROUND(AVG(v.occurrence_pct)::numeric, 1) AS avg_occurrence_pct,
    ROUND(SUM(v.annual_queue_mile_hours)::numeric, 2) AS total_annual_queue_mile_hours,
    ROUND(AVG(v.avg_congested_speed_ratio)::numeric, 3) AS avg_congested_speed_ratio,
    MAX(v.peak_segment_aadt) AS max_contributing_aadt,
    rl.county,
    rl.cid_name,
    rl.latitude,
    rl.longitude,
    RANK() OVER (ORDER BY SUM(v.aadt_weighted_severity_index) DESC) AS intersection_severity_rank
FROM "Year_2025".vw_bottleneck_dashboard_ranked AS v
JOIN "Year_2025".corridor_definitions AS cd
  ON cd.road = v.corridor AND cd.direction = v.direction AND cd.is_active
JOIN representative_location AS rl ON rl.representative_intersection = v.representative_intersection
WHERE cd.corridor_group = 'Arterial'
  AND v.ranking_eligible
  AND v.representative_intersection IS NOT NULL
GROUP BY v.representative_intersection, rl.county, rl.cid_name, rl.latitude, rl.longitude
ORDER BY aadt_weighted_intersection_severity_index DESC;

COMMIT;
