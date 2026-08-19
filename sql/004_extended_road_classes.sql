-- ============================================================================
-- CBI EXTENDED ROAD CLASSES — Arterials, Collectors, Locals, region-wide views
-- PostgreSQL 17.x | Schema: "Year_2025"
--
-- SAFE TO RE-RUN. Requires sql/003_multicorridor_migration.sql to have been
-- applied already.
--
-- What this adds on top of 003 (Interstates only):
--   1. Top 25 Arterial road/direction pairs (f_system 3/4, principal +
--      minor arterial) by peak AADT, registered as full corridor_definitions
--      rows (corridor_group = 'Arterial') — they flow through the existing
--      event detection / bottleneck detection / characterization / report
--      pipeline exactly like Interstates, no application code changes needed.
--   1b. All Expressway road/direction pairs (f_system 2 — limited-access
--      non-Interstate freeways: GA-400, US-19, US-78, GA-316, etc.),
--      corridor_group = 'Expressway', same full pipeline. Only ~22 eligible
--      pairs exist region-wide so all are included, no AADT cap.
--   2. corridor_segments rebuilt so the new Arterial/Expressway corridors
--      get segments.
--   3. watch_segments / watch_segment_metrics: top 25 Collector (f_system
--      5/6) and top 10 Local (f_system 7) individual TMC segments by AADT.
--      There are only ~10-25 multi-segment collector/local road/direction
--      pairs region-wide, so these are NOT run through the full spatial
--      bottleneck-detection pipeline — cbi_watch_segments.py computes a
--      lighter per-segment congestion summary directly from probe_readings
--      into watch_segment_metrics.
--   4. Region-wide dashboard views (month / weekday / hour / county),
--      built from congestion_events so they cover every corridor that goes
--      through full event detection (Interstates + Arterials).
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Register top 25 Arterial road/direction pairs by peak AADT
-- ---------------------------------------------------------------------------
WITH arterial_candidates AS (
    SELECT
        road,
        direction,
        COUNT(*) AS segment_count,
        MAX(aadt) AS peak_aadt
    FROM "Year_2025".tmc_metadata
    WHERE f_system IN (3, 4)
      AND direction IS NOT NULL
      AND BTRIM(direction) <> ''
    GROUP BY road, direction
    HAVING COUNT(*) >= 3
    ORDER BY MAX(aadt) DESC NULLS LAST
    LIMIT 25
)
INSERT INTO "Year_2025".corridor_definitions (
    road,
    direction,
    corridor_name,
    corridor_group,
    is_active
)
SELECT
    road,
    direction,
    road || ' ' || INITCAP(LOWER(direction)),
    'Arterial',
    true
FROM arterial_candidates
ON CONFLICT (road, direction) DO UPDATE SET
    corridor_name = EXCLUDED.corridor_name,
    corridor_group = EXCLUDED.corridor_group,
    is_active = true;

-- Deactivate any previously-registered Arterial corridor that fell out of
-- the current top-25-by-AADT cut (keeps corridor_definitions in sync with
-- the ranking instead of accumulating stale rows across re-runs).
UPDATE "Year_2025".corridor_definitions AS d
SET is_active = false
WHERE d.corridor_group = 'Arterial'
  AND NOT EXISTS (
      SELECT 1
      FROM "Year_2025".tmc_metadata AS m
      WHERE m.road = d.road
        AND m.direction = d.direction
        AND m.f_system IN (3, 4)
      GROUP BY m.road, m.direction
      HAVING COUNT(*) >= 3
      ORDER BY MAX(m.aadt) DESC NULLS LAST
      LIMIT 25
  );

-- ---------------------------------------------------------------------------
-- 1b. Register all Expressway road/direction pairs (f_system 2 — "other
-- freeway/expressway": limited-access, non-Interstate routes like GA-400,
-- US-19, US-78, GA-316). These were missed entirely by the original
-- discovery logic above, which only ever checked Interstate-named roads and
-- f_system 3/4 — despite some of these carrying more traffic than several
-- analyzed Interstates (US-19 peaks at 203,000 AADT). Only ~22 eligible
-- pairs exist region-wide, so no top-N cap is needed; all are registered.
-- ---------------------------------------------------------------------------
WITH expressway_candidates AS (
    SELECT road, direction, COUNT(*) AS segment_count, MAX(aadt) AS peak_aadt
    FROM "Year_2025".tmc_metadata
    WHERE f_system = 2
      AND direction IS NOT NULL
      AND BTRIM(direction) <> ''
    GROUP BY road, direction
    HAVING COUNT(*) >= 3
)
INSERT INTO "Year_2025".corridor_definitions (
    road, direction, corridor_name, corridor_group, is_active
)
SELECT
    road,
    direction,
    road || ' ' || INITCAP(LOWER(direction)),
    'Expressway',
    true
FROM expressway_candidates
ON CONFLICT (road, direction) DO UPDATE SET
    corridor_name = EXCLUDED.corridor_name,
    corridor_group = EXCLUDED.corridor_group,
    is_active = true;

UPDATE "Year_2025".corridor_definitions AS d
SET is_active = false
WHERE d.corridor_group = 'Expressway'
  AND NOT EXISTS (
      SELECT 1
      FROM "Year_2025".tmc_metadata AS m
      WHERE m.road = d.road AND m.direction = d.direction AND m.f_system = 2
      GROUP BY m.road, m.direction
      HAVING COUNT(*) >= 3
  );

-- ---------------------------------------------------------------------------
-- 2. Rebuild corridor_segments so new Arterial/Expressway corridors get
-- segments too
-- (identical logic to 003_multicorridor_migration.sql section 2 — generic
-- over every active row in corridor_definitions regardless of corridor_group)
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS "Year_2025".corridor_segments;

CREATE TABLE "Year_2025".corridor_segments AS
SELECT
    d.corridor_id,
    d.corridor_name,
    ROW_NUMBER() OVER (
        PARTITION BY d.corridor_id
        ORDER BY m.road_order, m.tmc
    )::integer AS segment_order,
    m.tmc,
    m.road,
    m.direction,
    m.intersection,
    m.miles::double precision AS miles,
    m.road_order,
    m.start_latitude,
    m.start_longitude,
    m.end_latitude,
    m.end_longitude
FROM "Year_2025".corridor_definitions AS d
JOIN "Year_2025".tmc_metadata AS m
  ON m.road = d.road
 AND m.direction = d.direction
WHERE d.is_active;

ALTER TABLE "Year_2025".corridor_segments
ADD CONSTRAINT corridor_segments_pk
PRIMARY KEY (corridor_id, segment_order);

CREATE UNIQUE INDEX idx_corridor_segments_corridor_tmc
ON "Year_2025".corridor_segments (corridor_id, tmc);

CREATE INDEX idx_corridor_segments_tmc
ON "Year_2025".corridor_segments (tmc);

CREATE INDEX idx_corridor_segments_road_direction
ON "Year_2025".corridor_segments (road, direction);

ANALYZE "Year_2025".corridor_segments;

-- ---------------------------------------------------------------------------
-- 3. Watch segments — top 25 Collector + top 10 Local TMCs by AADT
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS "Year_2025".watch_segments (
    tmc text PRIMARY KEY,
    road text NOT NULL,
    direction text,
    intersection text,
    county text,
    f_system integer,
    road_class text NOT NULL CHECK (road_class IN ('Collector', 'Local')),
    aadt integer,
    miles double precision,
    rank_in_class integer NOT NULL
);

CREATE TABLE IF NOT EXISTS "Year_2025".watch_segment_metrics (
    tmc text PRIMARY KEY
        REFERENCES "Year_2025".watch_segments(tmc) ON DELETE CASCADE,
    analyzed_days integer,
    occurrence_days integer,
    occurrence_pct double precision,
    avg_congested_minutes_occurrence_day double precision,
    annual_segment_mile_hours double precision,
    average_congested_speed_ratio double precision,
    peak_hour integer,
    peak_hour_label text,
    peak_weekday_name text,
    computed_at timestamptz NOT NULL DEFAULT now()
);

-- Re-derive the watch list every run (ranking can shift as AADT data
-- changes); CASCADE clears any stale watch_segment_metrics rows along with
-- it — cbi_watch_segments.py repopulates them on the next pipeline run.
TRUNCATE "Year_2025".watch_segments CASCADE;

INSERT INTO "Year_2025".watch_segments (
    tmc, road, direction, intersection, county, f_system,
    road_class, aadt, miles, rank_in_class
)
SELECT
    tmc, road, direction, intersection, county, f_system,
    'Collector', aadt, miles::double precision,
    ROW_NUMBER() OVER (ORDER BY aadt DESC NULLS LAST)::integer
FROM "Year_2025".tmc_metadata
WHERE f_system IN (5, 6)
  AND aadt IS NOT NULL
ORDER BY aadt DESC NULLS LAST
LIMIT 25;

INSERT INTO "Year_2025".watch_segments (
    tmc, road, direction, intersection, county, f_system,
    road_class, aadt, miles, rank_in_class
)
SELECT
    tmc, road, direction, intersection, county, f_system,
    'Local', aadt, miles::double precision,
    ROW_NUMBER() OVER (ORDER BY aadt DESC NULLS LAST)::integer
FROM "Year_2025".tmc_metadata
WHERE f_system = 7
  AND aadt IS NOT NULL
ORDER BY aadt DESC NULLS LAST
LIMIT 10;

CREATE INDEX IF NOT EXISTS idx_watch_segments_class
ON "Year_2025".watch_segments (road_class, rank_in_class);

-- ---------------------------------------------------------------------------
-- 4. Region-wide dashboard views (General tab source data)
-- Built from congestion_events, which every fully-analyzed corridor
-- (Interstate + Arterial) writes to — covers the whole analyzed network,
-- not just segments that turned into a detected recurring bottleneck.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW "Year_2025".vw_region_monthly_dashboard AS
SELECT
    DATE_TRUNC('month', e.analysis_date)::date AS month,
    COUNT(*) AS event_count,
    ROUND(SUM(e.duration_minutes)::numeric, 0) AS total_duration_minutes,
    ROUND(SUM(e.event_area_mile_hours)::numeric, 2) AS total_mile_hours
FROM "Year_2025".congestion_events AS e
JOIN "Year_2025".corridor_definitions AS d
  ON d.road = e.corridor AND d.direction = e.direction
WHERE d.is_active
GROUP BY DATE_TRUNC('month', e.analysis_date);

CREATE OR REPLACE VIEW "Year_2025".vw_region_weekday_dashboard AS
SELECT
    EXTRACT(ISODOW FROM e.analysis_date)::integer AS weekday_number,
    TO_CHAR(e.analysis_date, 'Dy') AS weekday_name,
    COUNT(*) AS event_count,
    ROUND(SUM(e.duration_minutes)::numeric, 0) AS total_duration_minutes,
    ROUND(SUM(e.event_area_mile_hours)::numeric, 2) AS total_mile_hours
FROM "Year_2025".congestion_events AS e
JOIN "Year_2025".corridor_definitions AS d
  ON d.road = e.corridor AND d.direction = e.direction
WHERE d.is_active
GROUP BY
    EXTRACT(ISODOW FROM e.analysis_date),
    TO_CHAR(e.analysis_date, 'Dy');

-- Hourly attribution must NOT be a naive EXTRACT(HOUR FROM start_time) —
-- some events span many hours (one logged case ran 4:50 AM-11:40 PM), and
-- crediting the whole event's duration/mile-hours to its start hour alone
-- artificially inflated early-morning hours (this regressed once already:
-- 004 was patched live in the database directly, then silently reverted
-- back to this naive form the next time this file was re-run to add the
-- Expressway road class — the fix belongs in the file, not just a live
-- ALTER, so it survives re-runs). Each event is sliced into the calendar
-- hours it actually overlaps and its duration/mile-hours are split
-- proportionally to how many minutes fall in each hour.
CREATE OR REPLACE VIEW "Year_2025".vw_region_hourly_dashboard AS
WITH hour_slices AS (
    SELECT
        e.event_id, e.analysis_date, e.corridor, e.direction,
        gs.hour_start,
        EXTRACT(EPOCH FROM (
            LEAST(e.end_time, gs.hour_start + interval '1 hour') - GREATEST(e.start_time, gs.hour_start)
        )) / 60.0 AS overlap_minutes,
        e.duration_minutes, e.event_area_mile_hours
    FROM "Year_2025".congestion_events AS e
    JOIN "Year_2025".corridor_definitions AS d
      ON d.road = e.corridor AND d.direction = e.direction
    CROSS JOIN LATERAL generate_series(
        date_trunc('hour', e.start_time),
        date_trunc('hour', e.end_time),
        interval '1 hour'
    ) AS gs(hour_start)
    WHERE d.is_active
)
SELECT
    EXTRACT(HOUR FROM hour_start)::integer AS hour_of_day,
    COUNT(DISTINCT event_id) AS event_count,
    ROUND(SUM(overlap_minutes)::numeric, 0) AS total_duration_minutes,
    ROUND(SUM(
        event_area_mile_hours * overlap_minutes / NULLIF(duration_minutes, 0)
    )::numeric, 2) AS total_mile_hours
FROM hour_slices
WHERE overlap_minutes > 0
GROUP BY EXTRACT(HOUR FROM hour_start);

CREATE OR REPLACE VIEW "Year_2025".vw_region_county_dashboard AS
SELECT
    COALESCE(m.county, 'UNKNOWN') AS county,
    COUNT(*) AS event_count,
    ROUND(SUM(e.duration_minutes)::numeric, 0) AS total_duration_minutes,
    ROUND(SUM(e.event_area_mile_hours)::numeric, 2) AS total_mile_hours
FROM "Year_2025".congestion_events AS e
JOIN "Year_2025".corridor_definitions AS d
  ON d.road = e.corridor AND d.direction = e.direction
LEFT JOIN "Year_2025".tmc_metadata AS m
  ON m.tmc = e.first_tmc
WHERE d.is_active
GROUP BY COALESCE(m.county, 'UNKNOWN');

COMMIT;

-- ---------------------------------------------------------------------------
-- Validation / inventory
-- ---------------------------------------------------------------------------
SELECT corridor_group, COUNT(*) AS corridors
FROM "Year_2025".corridor_definitions
WHERE is_active
GROUP BY corridor_group
ORDER BY corridor_group;

SELECT road_class, COUNT(*) AS segments
FROM "Year_2025".watch_segments
GROUP BY road_class
ORDER BY road_class;
