-- ============================================================================
-- CBI RANKING ELIGIBILITY — exclude rural minor-arterial corridors from the
-- region-wide "Top Bottlenecks" ranking
-- PostgreSQL 17.x | Schema: "Year_2025"
--
-- SAFE TO RE-RUN. Requires sql/004_extended_road_classes.sql to have been
-- applied already.
--
-- Why: the CBI congestion test (speed < 70% of a flat, non-time-varying
-- reference_speed) misclassifies ordinary signal-cycle deceleration as
-- congestion on signalized roads — confirmed via the Griffin St/Macon St
-- (GA-20) and Fayetteville Rd/North Ave (GA-54) investigations: even at
-- 3-5 AM with no traffic, 34-42% of readings were flagged "congested" on
-- these segments. That inflates occurrence_pct and severity_index enough
-- to outrank genuine freeway interchange bottlenecks, which is misleading
-- in a side-by-side region-wide ranking. Per-corridor rankings and reports
-- are unaffected (those still show a corridor's own bottlenecks against
-- each other, which is a fair comparison) — this only removes the affected
-- corridors from competing against Interstates/Expressways/fully-urban
-- Arterials in the network-wide "Top Bottlenecks" list.
--
-- Scope: every Arterial-group corridor with at least one rural segment
-- (tmc_metadata.urban_code IS NULL) is marked ranking_eligible = false.
-- Interstate and Expressway corridors are left eligible regardless of
-- rurality — the user's request was specifically about arterials, which
-- carry stop-sign/signal control that Interstates/Expressways don't.
-- ============================================================================

BEGIN;

ALTER TABLE "Year_2025".corridor_definitions
    ADD COLUMN IF NOT EXISTS ranking_eligible boolean NOT NULL DEFAULT true;

-- Reset before recomputing, so re-running this file after new corridors are
-- added or road classifications change reflects the current rural/urban mix.
UPDATE "Year_2025".corridor_definitions SET ranking_eligible = true;

UPDATE "Year_2025".corridor_definitions AS cd
SET ranking_eligible = false
WHERE cd.corridor_group = 'Arterial'
  AND EXISTS (
      SELECT 1
      FROM "Year_2025".corridor_segments AS cs
      JOIN "Year_2025".tmc_metadata AS tm ON tm.tmc = cs.tmc
      WHERE cs.corridor_id = cd.corridor_id
        AND tm.urban_code IS NULL
  );

DROP VIEW IF EXISTS "Year_2025".vw_bottleneck_dashboard_ranked;

CREATE VIEW "Year_2025".vw_bottleneck_dashboard_ranked AS
SELECT
    a.*,
    cd.ranking_eligible,
    ROUND(
        (
            a.annual_queue_mile_hours
            * a.occurrence_pct / 100.0
            * (1.0 - a.avg_congested_speed_ratio)
        )::numeric,
        2
    ) AS severity_index,
    RANK() OVER (
        PARTITION BY a.corridor, a.direction
        ORDER BY
            a.annual_queue_mile_hours
            * a.occurrence_pct / 100.0
            * (1.0 - a.avg_congested_speed_ratio) DESC
    ) AS corridor_severity_rank,
    CASE WHEN cd.ranking_eligible THEN
        RANK() OVER (
            PARTITION BY cd.ranking_eligible
            ORDER BY
                a.annual_queue_mile_hours
                * a.occurrence_pct / 100.0
                * (1.0 - a.avg_congested_speed_ratio) DESC
        )
    END AS network_severity_rank
FROM "Year_2025".vw_bottleneck_annual_dashboard AS a
JOIN "Year_2025".corridor_definitions AS cd
  ON cd.road = a.corridor AND cd.direction = a.direction AND cd.is_active;

COMMIT;

-- ---------------------------------------------------------------------------
-- Validation
-- ---------------------------------------------------------------------------
-- SELECT road, direction, corridor_group, ranking_eligible
-- FROM "Year_2025".corridor_definitions
-- WHERE corridor_group = 'Arterial'
-- ORDER BY ranking_eligible, road, direction;
--
-- SELECT COUNT(*) FILTER (WHERE network_severity_rank IS NOT NULL) AS ranked,
--        COUNT(*) FILTER (WHERE network_severity_rank IS NULL) AS excluded
-- FROM "Year_2025".vw_bottleneck_dashboard_ranked;
