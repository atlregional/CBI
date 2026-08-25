-- ============================================================================
-- NAMED ARTERIAL REGISTRY
-- PostgreSQL 17.x | Schema: "Year_2025"
--
-- Solves the fragmentation problem: major arterials (Buford Hwy, Roswell Rd,
-- etc.) are split into many short TMC segments, sometimes with inconsistent
-- road-name text and even different functional classification, as they cross
-- jurisdictions. This registry lets many tmc_metadata.road values be grouped
-- under one canonical arterial name for analysis and AADT rollup.
--
-- IMPORTANT: the road_pattern values seeded below are best-guess starting
-- points, not verified against your actual tmc_metadata.road text. Run the
-- "candidate road name discovery" query at the bottom for each arterial and
-- add/correct patterns before trusting the grouping.
-- ============================================================================

CREATE TABLE IF NOT EXISTS "Year_2025".named_arterials (
    arterial_id     integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    arterial_name   text NOT NULL UNIQUE,
    notes           text
);

CREATE TABLE IF NOT EXISTS "Year_2025".named_arterial_road_aliases (
    arterial_id     integer NOT NULL
        REFERENCES "Year_2025".named_arterials(arterial_id) ON DELETE CASCADE,
    road_pattern    text NOT NULL,   -- matched via ILIKE against tmc_metadata.road
    PRIMARY KEY (arterial_id, road_pattern)
);

CREATE INDEX IF NOT EXISTS idx_arterial_aliases_arterial
ON "Year_2025".named_arterial_road_aliases (arterial_id);


-- ---------------------------------------------------------------------------
-- Seed data: the arterials named in review, plus their most likely road-name
-- variants. GEORGIA-SPECIFIC NOTE: several of these are co-signed with state
-- route or US route numbers (e.g. Buford Hwy = US-23/SR-13), which is exactly
-- the kind of naming inconsistency this registry exists to solve — patterns
-- below try to catch both the local name and the likely route number.
-- ---------------------------------------------------------------------------

INSERT INTO "Year_2025".named_arterials (arterial_name, notes) VALUES
    ('Buford Hwy', 'Also signed as US-23 / SR-13 through parts of DeKalb/Gwinnett'),
    ('Roswell Rd', 'Also signed as SR-9 through parts of Fulton/Cobb'),
    ('Floyd Rd', NULL),
    ('MLK', 'Martin Luther King Jr Dr/Blvd — verify exact text, several MLK-named streets exist in different cities'),
    ('Howell Mill Rd', NULL),
    ('Piedmont Rd', 'Also signed as SR-237 in places'),
    ('East-West Connector', NULL),
    ('Atlanta Rd', 'Common name in multiple cities — verify county/city context before trusting matches')
ON CONFLICT (arterial_name) DO NOTHING;

INSERT INTO "Year_2025".named_arterial_road_aliases (arterial_id, road_pattern)
SELECT a.arterial_id, v.pattern
FROM "Year_2025".named_arterials a
JOIN (VALUES
    ('Buford Hwy',           'BUFORD HWY'),
    ('Buford Hwy',           'BUFORD HIGHWAY'),
    ('Buford Hwy',           'US-23'),
    ('Buford Hwy',           'SR-13'),
    ('Roswell Rd',           'ROSWELL RD'),
    ('Roswell Rd',           'ROSWELL ROAD'),
    ('Roswell Rd',           'SR-9'),
    ('Floyd Rd',             'FLOYD RD'),
    ('Floyd Rd',             'FLOYD ROAD'),
    ('MLK',                  'MLK'),
    ('MLK',                  'MARTIN LUTHER KING'),
    ('Howell Mill Rd',       'HOWELL MILL'),
    ('Piedmont Rd',          'PIEDMONT RD'),
    ('Piedmont Rd',          'PIEDMONT ROAD'),
    ('Piedmont Rd',          'SR-237'),
    ('East-West Connector',  'EAST-WEST CONNECTOR'),
    ('East-West Connector',  'EAST WEST CONNECTOR'),
    ('Atlanta Rd',           'ATLANTA RD'),
    ('Atlanta Rd',           'ATLANTA ROAD')
) AS v(arterial_name, pattern)
  ON v.arterial_name = a.arterial_name
ON CONFLICT DO NOTHING;


-- ---------------------------------------------------------------------------
-- Candidate road-name discovery — run this per arterial BEFORE trusting the
-- seed patterns above, to see what tmc_metadata.road actually contains.
-- Example for Buford Hwy:
-- ---------------------------------------------------------------------------

-- SELECT DISTINCT road, COUNT(*) AS segment_count
-- FROM tmc_metadata
-- WHERE road ILIKE '%BUFORD%' OR road ILIKE '%US-23%' OR road ILIKE '%SR-13%'
-- GROUP BY road
-- ORDER BY segment_count DESC;

-- Repeat for each named arterial, then INSERT any additional road_pattern
-- values found into named_arterial_road_aliases.


-- ---------------------------------------------------------------------------
-- Coverage check — confirms how many tmc_metadata rows actually resolve to
-- a named arterial once patterns are finalized. Segments matching zero
-- patterns are analyzed individually by the main script, not dropped.
-- ---------------------------------------------------------------------------

-- SELECT a.arterial_name, COUNT(DISTINCT m.tmc) AS matched_segments
-- FROM "Year_2025".named_arterial_road_aliases al
-- JOIN "Year_2025".named_arterials a ON a.arterial_id = al.arterial_id
-- JOIN tmc_metadata m ON m.road ILIKE '%' || al.road_pattern || '%'
-- GROUP BY a.arterial_name
-- ORDER BY matched_segments DESC;
