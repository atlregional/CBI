-- ============================================================================
-- CBI CORRIDOR SPLIT SCHEMA — support multiple corridor_definitions rows
-- per physical (road, direction), each bound to a slice of tmc_metadata
-- PostgreSQL 17.x | Schema: "Year_2025"
--
-- SAFE TO RE-RUN. Requires sql/005_ranking_eligibility.sql to have been
-- applied already.
--
-- Why: several "Expressway" corridors (US-78, US-19, Sugarloaf Pkwy,
-- GA-141, GA-166, ...) were registered because part of their named route
-- has f_system=2 (limited-access) segments, but corridor_segments was then
-- built by joining ALL tmc_metadata rows for that (road, direction) — every
-- signalized f_system 3/4 segment sharing the route name came along too.
-- For several of these routes, the signalized mileage is the majority of
-- the corridor and of its detected bottlenecks, inheriting the same
-- reference-speed-threshold artifact already documented for the Arterial
-- group, but hidden inside a corridor labeled "Expressway" competing in
-- the freeway-oriented ranking.
--
-- source_road / segment_order_start / segment_order_end let a corridor's
-- display road/name diverge from the tmc_metadata.road it actually pulls
-- segments from, and bound that pull to a road_order range — so a single
-- named route can be split into several corridor_definitions rows, one per
-- contiguous classification run, without changing how any other pipeline
-- stage reads corridor_segments (still just corridor_id, unchanged).
-- ============================================================================

BEGIN;

ALTER TABLE "Year_2025".corridor_definitions
    ADD COLUMN IF NOT EXISTS source_road text,
    ADD COLUMN IF NOT EXISTS segment_order_start double precision,
    ADD COLUMN IF NOT EXISTS segment_order_end double precision;

-- Existing (unsplit) corridors: source_road = road, no bounds — the rebuilt
-- corridor_segments query below treats NULL bounds as "whole route", so
-- this is a no-op for every corridor that isn't being split.
UPDATE "Year_2025".corridor_definitions
SET source_road = road
WHERE source_road IS NULL;

ALTER TABLE "Year_2025".corridor_definitions
    ALTER COLUMN source_road SET NOT NULL;

-- ---------------------------------------------------------------------------
-- Rebuild corridor_segments with the generalized join (source_road +
-- optional road_order bounds instead of a bare road/direction match).
-- Identical output to before for every corridor with segment_order_start/end
-- NULL — this only changes behavior for split corridors.
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
  ON m.road = d.source_road
 AND m.direction = d.direction
 AND (d.segment_order_start IS NULL OR m.road_order >= d.segment_order_start)
 AND (d.segment_order_end IS NULL OR m.road_order <= d.segment_order_end)
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

COMMIT;
