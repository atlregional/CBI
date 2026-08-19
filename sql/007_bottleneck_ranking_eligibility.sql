-- ============================================================================
-- CBI BOTTLENECK RANKING ELIGIBILITY — exclude individual false-flagged
-- bottlenecks on short Arterial corridors from the region-wide ranking
-- PostgreSQL 17.x | Schema: "Year_2025"
--
-- SAFE TO RE-RUN. Requires sql/006_corridor_split_schema.sql to have been
-- applied already.
--
-- Why a bottleneck-level flag, not just the corridor-level one from
-- 005_ranking_eligibility.sql: that migration excluded whole corridors by a
-- proxy (any rural segment), which both missed real cases (fully-urban
-- short Arterial corridors like US-19-4 Northbound / "Smith St to I-75 Exit
-- 235" showed the identical false-flag signature despite 0 rural segments
-- and 5.6 average lanes) and risked excluding genuine bottlenecks that
-- happen to sit on a corridor with any rural mileage at all.
--
-- The reliable diagnostic, confirmed across every case investigated this
-- session: a genuine bottleneck shows near-zero congestion overnight
-- (2-4 AM, when there is essentially no traffic) and a sharp contrast at
-- the afternoon peak. A false-flagged one — where a flat, non-time-varying
-- reference_speed sets too aggressive a bar for a signal-controlled road —
-- shows substantial congestion even at 2-4 AM, because ordinary signal-
-- cycle deceleration alone clears the 70%-of-reference-speed line.
-- Confirmed genuine (Camp Creek Pkwy, GA-5-Conn, Abernathy Rd, GA-13 short
-- splits): 0.4-2.4% of readings flagged congested at 2-4 AM. Confirmed
-- false flags (Griffin St/Macon St on GA-20, Fayetteville Rd/North Ave on
-- GA-54): 34-42% at 2-4 AM. The gap between those two groups is wide enough
-- that OVERNIGHT_CONGESTION_THRESHOLD_PCT below does not need to be
-- precisely tuned.
--
-- Scoped to short Arterial-group corridors (<= SHORT_CORRIDOR_MAX_SEGMENTS
-- segments) because that is where this shows up: a short corridor's whole
-- span can sit above the 2-4 AM congestion rate on a bad reference_speed
-- alone, where a long corridor has room for genuine local peaks and clean
-- overnight troughs elsewhere along its length.
-- ============================================================================

BEGIN;

ALTER TABLE "Year_2025".segment_recurring_bottlenecks
    ADD COLUMN IF NOT EXISTS overnight_congested_pct double precision,
    ADD COLUMN IF NOT EXISTS ranking_eligible boolean NOT NULL DEFAULT true;

DROP VIEW IF EXISTS "Year_2025".vw_bottleneck_dashboard_ranked;

CREATE VIEW "Year_2025".vw_bottleneck_dashboard_ranked AS
SELECT
    a.*,
    cd.ranking_eligible AS corridor_ranking_eligible,
    b.ranking_eligible AS bottleneck_ranking_eligible,
    (cd.ranking_eligible AND b.ranking_eligible) AS ranking_eligible,
    b.overnight_congested_pct,
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
    CASE WHEN cd.ranking_eligible AND b.ranking_eligible THEN
        RANK() OVER (
            PARTITION BY (cd.ranking_eligible AND b.ranking_eligible)
            ORDER BY
                a.annual_queue_mile_hours
                * a.occurrence_pct / 100.0
                * (1.0 - a.avg_congested_speed_ratio) DESC
        )
    END AS network_severity_rank
FROM "Year_2025".vw_bottleneck_annual_dashboard AS a
JOIN "Year_2025".corridor_definitions AS cd
  ON cd.road = a.corridor AND cd.direction = a.direction AND cd.is_active
JOIN "Year_2025".segment_recurring_bottlenecks AS b
  ON b.bottleneck_id = a.bottleneck_id;

COMMIT;
