-- ============================================================================
-- CBI AADT-WEIGHTED SEVERITY — factor traffic volume into the region-wide
-- bottleneck ranking, so a low-volume signalized road can't outrank a
-- much busier freeway interchange on a purely geometric queue-extent score
-- PostgreSQL 17.x | Schema: "Year_2025"
--
-- SAFE TO RE-RUN. Requires sql/008_intersection_severity_index.sql.
--
-- Why: severity_index (occurrence x annual_queue_mile_hours x speed drop)
-- has no traffic-volume term at all — it's purely "how many roadway-miles
-- read below threshold speed, how often, how far below." Investigated
-- directly: rank 5 (Jot Em Down Rd, US-19, 37,111 AADT) and rank 10
-- (Sharon Rd, GA-141, 41,800 AADT) sat in a top-10 otherwise entirely
-- Interstate interchanges carrying 163,000-371,000 AADT — a 5-9x volume
-- gap — yet scored comparably to or above several of them. Checked
-- whether this was the already-documented reference-speed-threshold
-- artifact (like Griffin St/Macon St) via three further diagnostics —
-- occurrence-profile flatness across the bottleneck's own extent chief
-- among them — and none discriminated: confirmed-genuine cases (Camp
-- Creek Pkwy, GA-5-Conn, Abernathy Rd, even the #1-2 Interstate entries)
-- show equally flat 95-100% occurrence throughout, so flatness isn't a
-- reliable signal either. The one thing that directly, defensibly
-- explains the disproportion is what the geometric formula never
-- accounts for: how many actual vehicles are affected.
--
-- aadt_weighted_severity_index multiplies the original severity_index by
-- (peak segment AADT / 100,000) — 100,000 is close to the median AADT
-- (104,000) across all currently ranking-eligible bottlenecks, so a
-- bottleneck at typical volume is left roughly unchanged, below-median
-- volume is discounted, above-median is boosted, proportional to how far
-- it sits from what's typical for a ranked bottleneck in this dataset.
-- This does NOT replace severity_index — that stays exactly as originally
-- computed and FHWA-attributed (see the Metadata tab), still shown
-- alongside for methodological transparency. Only the region-wide
-- ranking, the intersection severity index, and the region map's color
-- classification switch to the AADT-weighted figure, since those are
-- specifically the places where bottlenecks on roads of very different
-- volume compete against each other head to head.
-- ============================================================================

BEGIN;

DROP VIEW IF EXISTS "Year_2025".vw_intersection_severity_index;
DROP VIEW IF EXISTS "Year_2025".vw_bottleneck_dashboard_ranked;

CREATE VIEW "Year_2025".vw_bottleneck_dashboard_ranked AS
SELECT
    a.*,
    cd.ranking_eligible AS corridor_ranking_eligible,
    b.ranking_eligible AS bottleneck_ranking_eligible,
    (cd.ranking_eligible AND b.ranking_eligible) AS ranking_eligible,
    b.overnight_congested_pct,
    peak_aadt.aadt AS peak_segment_aadt,
    ROUND(
        (
            a.annual_queue_mile_hours
            * a.occurrence_pct / 100.0
            * (1.0 - a.avg_congested_speed_ratio)
        )::numeric,
        2
    ) AS severity_index,
    ROUND(
        (
            a.annual_queue_mile_hours
            * a.occurrence_pct / 100.0
            * (1.0 - a.avg_congested_speed_ratio)
            * (COALESCE(peak_aadt.aadt, 100000) / 100000.0)
        )::numeric,
        2
    ) AS aadt_weighted_severity_index,
    RANK() OVER (
        PARTITION BY a.corridor, a.direction
        ORDER BY
            a.annual_queue_mile_hours
            * a.occurrence_pct / 100.0
            * (1.0 - a.avg_congested_speed_ratio) DESC
    ) AS corridor_severity_rank,
    CASE WHEN cd.ranking_eligible AND b.ranking_eligible THEN
        RANK() OVER (
            PARTITION BY (cd.ranking_eligible AND b.ranking_eligible)
            ORDER BY
                a.annual_queue_mile_hours
                * a.occurrence_pct / 100.0
                * (1.0 - a.avg_congested_speed_ratio)
                * (COALESCE(peak_aadt.aadt, 100000) / 100000.0) DESC
        )
    END AS network_severity_rank
FROM "Year_2025".vw_bottleneck_annual_dashboard AS a
JOIN "Year_2025".corridor_definitions AS cd
  ON cd.road = a.corridor AND cd.direction = a.direction AND cd.is_active
JOIN "Year_2025".segment_recurring_bottlenecks AS b
  ON b.bottleneck_id = a.bottleneck_id
LEFT JOIN LATERAL (
    SELECT t.aadt
    FROM "Year_2025".corridor_segments AS cs
    JOIN "Year_2025".tmc_metadata AS t ON t.tmc = cs.tmc
    WHERE cs.corridor_id = cd.corridor_id AND cs.segment_order = a.peak_segment_order
    LIMIT 1
) AS peak_aadt ON true;

CREATE VIEW "Year_2025".vw_intersection_severity_index AS
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
    RANK() OVER (ORDER BY SUM(v.aadt_weighted_severity_index) DESC) AS intersection_severity_rank
FROM "Year_2025".vw_bottleneck_dashboard_ranked AS v
JOIN "Year_2025".corridor_definitions AS cd
  ON cd.road = v.corridor AND cd.direction = v.direction AND cd.is_active
WHERE cd.corridor_group = 'Arterial'
  AND v.ranking_eligible
  AND v.representative_intersection IS NOT NULL
GROUP BY v.representative_intersection
ORDER BY aadt_weighted_intersection_severity_index DESC;

COMMIT;
