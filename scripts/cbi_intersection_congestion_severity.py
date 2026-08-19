"""
Intersection Congestion Severity — standalone methodology page.

An alternative to the Intersection Severity Index tab in the main regional
report: instead of summing severity_index across bottlenecks detected by
the full event-detection pipeline, this ranks candidate intersections
directly from probe_readings using a speed-drop ratio anchored to
region-wide hour-of-day baselines (rather than each TMC's own possibly
signal-cycle-noisy reference_speed field — see the "Occurrence caution"
note in the main report), combined with occurrence and AADT via K-means.

Methodology (as specified by the user):
  1. System-wide average speed by hour of day (every TMC region-wide).
  2. Arterial-only average speed by hour of day (f_system 3/4 only).
  3. Free-flow hour(s) = the hour(s) with the HIGHEST average speed in the
     Arterial-only curve; peak hour(s) = the LOWEST. Both are single,
     region-wide constants applied to every candidate, not computed per
     segment.
  4. Per-candidate speed-drop ratio = its own average speed at the peak
     hour(s) / its own average speed at the free-flow hour(s).
  5. Per-candidate occurrence = % of analyzed weekdays where its peak-hour
     speed drops below cbi_detector.CUTOFF_RATIO (0.70) of its free-flow-
     hour speed THAT SAME DAY.
  6. AADT with path/name integration: NULL AADT is backfilled from the max
     AADT among TMCs sharing the same tmclinear value (NPMRDS's own
     linear-route grouping key) — see NAMED_ARTERIALS below for why this
     matters and how each named arterial's real road/county identity was
     resolved (several of these roads are fragmented across multiple
     tmc_metadata `road` strings, or collide in name with unrelated roads
     elsewhere in the state). The backfill donor pool excludes Interstate/
     Expressway (f_system 1/2) siblings, and every candidate is separately
     required to be f_system-classified Arterial (3/4) itself — found via a
     real disparity: at complex interchanges, a tmclinear group (and even a
     `road` name like "WILLIAMS ST NW") can span both a surface street's own
     segments and the adjacent Interstate's ramp geometry, so backfilling
     across that boundary let a ~250,000 AADT Interstate ramp reading get
     inherited by an unrelated arterial TMC with no AADT of its own,
     producing a nonsensical severity_index outlier (see git history for
     the diagnosis). Also note: Floyd Rd, Atlanta Rd, and Piedmont Rd (3 of
     the 8 originally-named arterials) currently have NO usable AADT
     anywhere in their tmclinear groups — a genuine NPMRDS/HPMS coverage
     gap, not a bug — so they contribute zero candidate intersections
     until/unless an external AADT source is added for them.
  7. Intersections = consecutive TMCs sharing the same tmc_metadata
     `intersection` (cross-street) value, per named arterial/direction.
  8. Queue mile-hours: for every hour of every analyzed weekday, that
     TMC's own average speed is compared against ITS OWN average speed at
     the free-flow hour(s) (same 0.70 threshold) — every hour that fails
     counts as one "congested hour" that day. annual_queue_mile_hours =
     total congested hours across the year x segment length (miles);
     max_queue_mile_hours is the same figure for that intersection's
     single worst day. This is what lets the ranking answer "how LONG does
     congestion last here," not just "how much does speed drop at the one
     peak hour."
  9. Top N via K-means on [speed_drop_ratio, occurrence_pct, aadt,
     annual_queue_mile_hours] (standardized, see
     cbi_map_geometry.kmeans_multi_feature_classes).
  10. severity_index = annual_queue_mile_hours * (occurrence_pct / 100) *
      (1 - speed_drop_ratio) * (aadt / 100000) — the same shape as the
      main report's severity_index x AADT-weighting (see
      sql/009_aadt_weighted_severity.sql), now using this page's own
      directly-computed queue mile-hours instead of a proxy. Displayed
      alongside the K-means selection and used to order candidates within
      a cluster (more interpretable than the standardized cluster-fit
      score).

The "Average Speed by Hour of Day" chart also breaks the region-wide curve
down by FHWA functional road class (Interstate/Expressway/Arterial/
Collector/Local, using each TMC's own DIRECT functional class only, not
tmclinear-backfilled — see _load_hourly_baselines for why backfilling is
wrong for this specific purpose), each as its own line, alongside the
overall system-wide curve — purely for visual/methodology context; only
the Arterial-only curve is
used operationally to set the peak/free-flow hours.

The map and table show only the top N intersections (no gray "every other
analyzed location" context layer) — hovering a map segment shows the same
popup style (name, severity, worst date/time) as the main regional
report's Region Map tab, via the same _CANVAS_MAP_JS engine.

Standalone for now — reads directly from probe_readings/tmc_metadata, not
through corridor_definitions or the event-detection/bottleneck-detection
pipeline, so it carries no risk to the existing, working pipeline. Reuses
the main regional report's page chrome (CSS, canvas map JS, Plotly figure
helpers) via import so it looks like part of the same report family for
whenever it's combined into the main dashboard.
"""

from __future__ import annotations

import html
import time
from pathlib import Path

import plotly.graph_objects as go
import polars as pl
import psycopg

import cbi_database
import cbi_detector
import cbi_map_geometry
from cbi_generate_regional_report import (
    GRID,
    INK_MUTED,
    INK_PRIMARY,
    INK_SECONDARY,
    SURFACE,
    _CANVAS_MAP_JS,
    _PAGE_CSS,
    _agency_logo_data_uri,
    _canvas_map_html,
    _fig_html,
    _fmt2,
    _load_county_boundary_rings,
)

OUTPUT_FILENAME = "intersection_congestion_severity.html"

# Each named arterial the user asked to capture, resolved to its real
# tmc_metadata `road` value(s) and, where the name is ambiguous or the road
# spans counties well beyond the urban stretch meant here, a county filter
# — see the plan's "Confirmed road identity mapping" table for how each of
# these was resolved (direct DB investigation, not a guess): e.g. "MARTIN
# LUTHER KING JR DR" also matches an unrelated road in Coweta County 40
# miles south, and "GA-13"/"GA-9" are Buford Hwy/Roswell Rd's actual
# tmc_metadata names (NPMRDS attributes them by route number, not the
# common local name) but only their Fulton/DeKalb urban stretch — the rest
# of each route is a separate, rural highway far outside metro Atlanta.
NAMED_ARTERIALS: dict[str, dict] = {
    "Buford Hwy": {"roads": ["GA-13"], "counties": ["FULTON", "DEKALB"]},
    "Roswell Rd": {"roads": ["GA-9"], "counties": ["FULTON"]},
    "Floyd Rd": {"roads": ["FLOYD RD SW"], "counties": None},
    "Martin Luther King Jr Dr": {"roads": ["MARTIN LUTHER KING JR DR"], "counties": ["FULTON"]},
    "Howell Mill Rd": {"roads": ["HOWELL MILL RD NW"], "counties": None},
    "Piedmont Rd": {"roads": ["PIEDMONT AVE"], "counties": ["FULTON"]},
    "East-West Connector": {"roads": ["EAST-WEST CONN"], "counties": None},
    "Atlanta Rd": {"roads": ["ATLANTA RD"], "counties": None},
    # Scan-discovered candidates: other major named arterials with the same
    # fragmentation pattern (high combined AADT, not in corridor_definitions).
    "Peachtree Industrial Blvd": {
        "roads": ["PEACHTREE INDUSTRIAL BLVD", "PEACHTREE INDUSTRIAL BLVD FRONTAGE"],
        "counties": None,
    },
    "Ashford Dunwoody Rd": {"roads": ["ASHFORD DUNWOODY RD NE"], "counties": None},
    "Pleasant Hill Rd": {"roads": ["PLEASANT HILL RD"], "counties": None},
    "Johnson Ferry Rd": {"roads": ["JOHNSON FERRY RD"], "counties": None},
    "Mt Vernon Hwy": {"roads": ["MT VERNON HWY"], "counties": None},
    "Panola Rd": {"roads": ["PANOLA RD"], "counties": None},
    "Jonesboro Rd": {"roads": ["JONESBORO RD"], "counties": None},
    "N Druid Hills Rd": {"roads": ["N DRUID HILLS RD NE"], "counties": None},
    "Woodstock Rd": {"roads": ["WOODSTOCK RD"], "counties": None},
    "Wesley Chapel Rd": {"roads": ["WESLEY CHAPEL RD"], "counties": None},
    "Williams St": {"roads": ["WILLIAMS ST NW"], "counties": None},
}

TOP_N = 50

# Excludes candidate TMCs longer than this from analysis entirely — see the
# comment in _build_intersection_candidates for why: at this length, a
# straight-line map fallback stops looking like the real road, and the
# queue-mile-hours formula's linear length dependency starts rewarding
# segment length over actual congestion severity. Roughly the 95th
# percentile of named-arterial TMC lengths (median 0.28 mi).
MAX_SEGMENT_MILES = 1.5


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------
# FHWA functional system code -> the road-class buckets used throughout
# this report (matches ROAD_CLASS_WIDTH in cbi_map_geometry.py and the
# f_system groupings already established in sql/003 and sql/004:
# Interstate=1, Expressway=2, Arterial=3/4, Collector=5/6, Local=7).
ROAD_CLASS_BY_F_SYSTEM = """
    CASE
        WHEN f_system = 1 THEN 'Interstate'
        WHEN f_system = 2 THEN 'Expressway'
        WHEN f_system IN (3, 4) THEN 'Arterial'
        WHEN f_system IN (5, 6) THEN 'Collector'
        WHEN f_system = 7 THEN 'Local'
    END
"""

ROAD_CLASS_ORDER = ["Interstate", "Expressway", "Arterial", "Collector", "Local"]
ROAD_CLASS_LINE_COLORS = {
    "Interstate": "#1F3864",
    "Expressway": "#2a78d6",
    "Arterial": "#5aa9e6",
    "Collector": "#e08a1e",
    "Local": "#7b4fa6",
}


def _load_hourly_baselines(connection: psycopg.Connection) -> dict:
    """Steps 1-3: system-wide, per-road-class, and (within that) Arterial-
    only average speed by hour of day, region-wide, plus the free-flow/peak
    hour(s) derived from the Arterial-only curve per the user's explicit
    definition (highest avg speed hour = free-flow, lowest = peak). Two
    queries: the system-wide curve is a plain scan (~15s); the per-class
    curve joins every probe reading to its TMC's DIRECT (not tmclinear-
    backfilled) functional class in one pass (~2 minutes) rather than
    running five separate filtered scans, and the Arterial-only figures
    used operationally are pulled out of that same result.

    Deliberately NOT backfilled here, unlike AADT elsewhere in this file:
    a real disparity report showed the Local curve reading suspiciously
    close to Arterial — backfilling functional class via MAX(f_system)
    OVER tmclinear picks the highest-numbered class present ANYWHERE in a
    tmclinear group, so a group spanning both an Arterial primary segment
    and an unrelated Local-classified sibling pushes every NULL-f_system
    member in that group to "Local" (confirmed directly: this inflated the
    Local bucket from 54 directly-classified TMCs to 234 backfilled ones).
    For classifying WHAT a road IS, only genuinely, directly classified
    TMCs should count — backfilling makes sense only when the segment's
    identity is already established some other way (e.g. AADT for a named
    arterial whose road/direction is already known from the road name
    join), not here."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT EXTRACT(HOUR FROM measurement_tstamp)::int AS hour, AVG(speed)::numeric(10,2)
            FROM "Year_2025".probe_readings
            WHERE speed IS NOT NULL AND speed > 0
            GROUP BY 1 ORDER BY 1
            """
        )
        system_wide = [(int(h), float(s)) for h, s in cursor.fetchall()]

        cursor.execute(
            f"""
            WITH classified AS (
                SELECT tmc, {ROAD_CLASS_BY_F_SYSTEM} AS road_class
                FROM "Year_2025".tmc_metadata
            )
            SELECT c.road_class, EXTRACT(HOUR FROM p.measurement_tstamp)::int AS hour, AVG(p.speed)::numeric(10,2)
            FROM "Year_2025".probe_readings p
            JOIN classified c ON c.tmc = p.tmc_code
            WHERE p.speed IS NOT NULL AND p.speed > 0 AND c.road_class IS NOT NULL
            GROUP BY 1, 2 ORDER BY 1, 2
            """
        )
        by_class: dict[str, list[tuple[int, float]]] = {cls: [] for cls in ROAD_CLASS_ORDER}
        for road_class, hour, speed in cursor.fetchall():
            by_class[road_class].append((int(hour), float(speed)))

    arterial = by_class["Arterial"]
    max_speed = max(s for _, s in arterial)
    min_speed = min(s for _, s in arterial)
    free_flow_hours = [h for h, s in arterial if s == max_speed]
    peak_hours = [h for h, s in arterial if s == min_speed]

    return {
        "system_wide": system_wide,
        "by_class": by_class,
        "arterial": arterial,
        "free_flow_hours": free_flow_hours,
        "peak_hours": peak_hours,
    }


def _load_backfilled_tmc_metadata() -> pl.DataFrame:
    """Every TMC region-wide, with AADT and functional class backfilled
    from same-tmclinear siblings — step 6's "path/name integration" for
    AADT. 75% of TMCs are secondary/frontage geometry variants (NPMRDS TMC
    codes like 101+nnnnn, 101-nnnnn, 101Nnnnnn) that NPMRDS never
    attributes AADT or a functional class to directly, unlike the primary
    101Pnnnnn variant at the same physical location — tmclinear (NPMRDS's
    own linear-route id, confirmed 100% populated) groups these safely
    without merging genuinely different roads.

    The backfill donor pool is restricted to siblings that are NOT
    Interstate/Expressway (f_system 1/2) — found directly by inspecting a
    real disparity report: at complex interchanges, one tmclinear group can
    span both a named surface street's own segments AND the adjacent
    Interstate's ramp/collector-distributor geometry (e.g. "WILLIAMS ST NW"
    includes TMCs literally f_system-classified as Interstate, immediately
    next to the Downtown Connector). Backfilling AADT across that boundary
    let a ~250,000 AADT Interstate ramp reading get inherited by an
    unrelated Williams St arterial TMC with no AADT of its own, producing a
    nonsensical severity_index outlier. Restricting the donor pool to
    likely-surface-street siblings (not Interstate/Expressway) avoids this
    while still fixing the same-road "+"/"-"/"N" vs "P" gap this backfill
    exists for."""
    return pl.read_database_uri(
        query="""
            WITH donors AS (
                SELECT tmclinear, MAX(aadt) AS donor_max_aadt, MAX(f_system) AS donor_max_f_system
                FROM "Year_2025".tmc_metadata
                WHERE f_system NOT IN (1, 2)
                GROUP BY tmclinear
            )
            SELECT
                t.tmc, t.road, t.direction, t.county, t.intersection, t.road_order, t.miles,
                t.start_latitude, t.start_longitude, t.end_latitude, t.end_longitude,
                COALESCE(t.aadt, d.donor_max_aadt) AS aadt_filled,
                COALESCE(t.f_system, d.donor_max_f_system) AS f_system_filled
            FROM "Year_2025".tmc_metadata t
            LEFT JOIN donors d ON d.tmclinear = t.tmclinear
        """,
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )


def _select_named_arterial_tmcs(all_tmcs: pl.DataFrame) -> pl.DataFrame:
    """Filters the region-wide backfilled TMC table down to NAMED_ARTERIALS,
    tagging each row with its display name. Kept as a Python-side filter
    over one region-wide query rather than 19 separate SQL queries — the
    backfill window function only needs to run once."""
    frames = []
    for name, spec in NAMED_ARTERIALS.items():
        subset = all_tmcs.filter(pl.col("road").is_in(spec["roads"]))
        if spec.get("counties"):
            subset = subset.filter(pl.col("county").is_in(spec["counties"]))
        if subset.is_empty():
            continue
        frames.append(subset.with_columns(pl.lit(name).alias("arterial_name")))
    return pl.concat(frames) if frames else all_tmcs.clear()


def _select_existing_arterial_tmcs(connection: psycopg.Connection, all_tmcs: pl.DataFrame) -> pl.DataFrame:
    """TMCs belonging to every Arterial corridor already defined in
    corridor_definitions (GA-20, GA-141, US-19, US-78, and 46 others) —
    the roads behind the main regional report's own Intersection Severity
    Index tab. Reuses their already-validated corridor_segments membership
    directly (no road-name/county heuristics needed, unlike
    NAMED_ARTERIALS, since these corridors were already carefully defined
    by the existing pipeline) and joins onto the same backfilled AADT/
    functional-class columns as the named-arterial TMCs, so the two sets
    combine into one consistent pool for characterization/ranking."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT cs.tmc, d.corridor_name
            FROM "Year_2025".corridor_definitions AS d
            JOIN "Year_2025".corridor_segments AS cs ON cs.corridor_id = d.corridor_id
            WHERE d.corridor_group = 'Arterial' AND d.is_active
            """
        )
        rows = cursor.fetchall()
    if not rows:
        return all_tmcs.clear().with_columns(pl.lit(None, dtype=pl.Utf8).alias("arterial_name"))
    mapping = pl.DataFrame(rows, schema=["tmc", "arterial_name"], orient="row")
    return all_tmcs.join(mapping, on="tmc", how="inner")


def _combine_arterial_tmcs(named: pl.DataFrame, existing: pl.DataFrame) -> pl.DataFrame:
    """Concatenates the two TMC sources, preferring the NAMED_ARTERIALS
    label on overlap (e.g. Buford Hwy's Fulton/DeKalb segments are also
    part of the already-defined "GA-13" corridors — keeping the more
    recognizable common name rather than showing the same physical
    location twice under two different labels)."""
    combined = pl.concat([named, existing], how="vertical_relaxed")
    return combined.unique(subset=["tmc"], keep="first", maintain_order=True)


_CHARACTERIZATION_SCHEMA = {
    "tmc_code": pl.Utf8, "analyzed_days": pl.Int64, "occurrence_days": pl.Int64,
    "avg_speed_peak": pl.Float64, "avg_speed_free_flow": pl.Float64,
    "total_congested_hours": pl.Int64, "max_daily_congested_hours": pl.Int64,
    "peak_hour": pl.Int64, "onset_hour": pl.Int64, "busiest_dow": pl.Int64,
    "busiest_date": pl.Date, "busiest_hour": pl.Int64, "min_speed": pl.Float64,
}


def _load_tmc_characterization(
    connection: psycopg.Connection,
    tmcs: list[str],
    peak_hours: list[int],
    free_flow_hours: list[int],
) -> pl.DataFrame:
    """Steps 4-5 and 8, per TMC, from one pass over every weekday-hour of
    probe data for these TMCs:
      - speed_drop_ratio / occurrence_pct: day_peak_speed is THIS TMC'S OWN
        worst hour on that specific day (not the fixed, region-wide peak
        hour(s)) — validating this methodology directly against one of the
        main report's own already-ranked intersections (Sharon Rd, GA-141
        Northbound) showed a real gap: that location's true worst hour is
        2 PM, not the region-wide 5 PM average, so scoring it against a
        rigid global peak hour understated both its speed-drop ratio and
        its occurrence (16-84% instead of the main report's 100%).
        day_free_flow_speed still uses the fixed, region-wide free-flow
        hour(s) (per the specified methodology — that's the stable
        reference every candidate is compared against). occurrence uses
        cbi_detector.CUTOFF_RATIO (0.70), anchored to this TMC's own
        free-flow-hour speed that day rather than the probe feed's
        reference_speed field, which can itself reflect normal signal-
        cycle slowdowns on arterials (see the Occurrence caution note in
        the main report). peak_hours is still accepted/passed through for
        the hourly-baseline chart's reference markers, just no longer used
        to compute day_peak_speed here.
      - queue mile-hours inputs: every one of the 24 hours (not just the
        2 fixed hours above) is flagged "congested" if its average speed
        falls below CUTOFF_RATIO of this TMC's own average speed at the
        free-flow hour(s) — total_congested_hours (summed across the
        whole year) and max_daily_congested_hours (that TMC's single
        worst day) get multiplied by segment length in
        _build_intersection_candidates to produce annual/max queue
        mile-hours.
      - peak_hour: the hour of day with this TMC's own lowest average
        speed across the year (which need not match the global,
        region-wide peak hour used for scoring above).
      - onset_hour: the earliest hour whose congested-hour count reaches
        at least 30% of the peak hour's own count — a proportional
        threshold (not an absolute day-count) so it scales with however
        much data a given TMC has, and doesn't trigger on a single noisy
        day at an otherwise free-flowing hour.
      - busiest_dow / busiest_date+busiest_hour/min_speed: the weekday
        with the most congested hours, and the single lowest-speed
        hour-day observation of the year, respectively."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            WITH hourly AS (
                SELECT
                    p.tmc_code,
                    p.measurement_tstamp::date AS day,
                    EXTRACT(HOUR FROM p.measurement_tstamp)::int AS hour,
                    EXTRACT(ISODOW FROM p.measurement_tstamp)::int AS dow,
                    AVG(p.speed) AS avg_speed
                FROM "Year_2025".probe_readings p
                WHERE p.tmc_code = ANY(%(tmcs)s)
                  AND p.speed IS NOT NULL AND p.speed > 0
                  AND EXTRACT(ISODOW FROM p.measurement_tstamp) BETWEEN 1 AND 5
                GROUP BY p.tmc_code, p.measurement_tstamp::date,
                         EXTRACT(HOUR FROM p.measurement_tstamp), EXTRACT(ISODOW FROM p.measurement_tstamp)
            ),
            free_flow AS (
                SELECT tmc_code, AVG(avg_speed) AS free_flow_speed
                FROM hourly WHERE hour = ANY(%(free_flow_hours)s)
                GROUP BY tmc_code
            ),
            daily_peak AS (
                -- day_peak_speed is this TMC's own worst hour THAT DAY
                -- (MIN across all 24), not the fixed region-wide peak
                -- hour(s) — a real intersection's true worst hour can
                -- differ from the region-wide 5 PM average (confirmed
                -- directly: one validated-against-the-main-report
                -- intersection peaks at 2 PM, not 5 PM), and scoring it
                -- against the wrong hour understated both its speed-drop
                -- ratio and its occurrence. free_flow_speed still uses the
                -- fixed, region-wide free-flow hour(s) per the specified
                -- methodology, since that's the stable reference every
                -- candidate is compared against.
                SELECT tmc_code, day,
                    MIN(avg_speed) AS day_peak_speed,
                    AVG(avg_speed) FILTER (WHERE hour = ANY(%(free_flow_hours)s)) AS day_free_flow_speed
                FROM hourly GROUP BY tmc_code, day
            ),
            flagged AS (
                SELECT h.*, (h.avg_speed < %(threshold)s * f.free_flow_speed) AS is_congested
                FROM hourly h JOIN free_flow f ON f.tmc_code = h.tmc_code
            ),
            daily_totals AS (
                SELECT tmc_code, day, COUNT(*) FILTER (WHERE is_congested) AS congested_hours
                FROM flagged GROUP BY tmc_code, day
            ),
            by_hour AS (
                SELECT tmc_code, hour,
                    COUNT(*) FILTER (WHERE is_congested) AS congested_count,
                    AVG(avg_speed) AS avg_speed_this_hour
                FROM flagged GROUP BY tmc_code, hour
            ),
            by_dow AS (
                SELECT tmc_code, dow, COUNT(*) FILTER (WHERE is_congested) AS congested_count
                FROM flagged GROUP BY tmc_code, dow
            ),
            worst_reading AS (
                SELECT DISTINCT ON (tmc_code) tmc_code, day, hour, avg_speed
                FROM flagged ORDER BY tmc_code, avg_speed ASC
            ),
            peak_hour_pick AS (
                SELECT DISTINCT ON (tmc_code) tmc_code, hour AS peak_hour, congested_count AS peak_congested_count
                FROM by_hour ORDER BY tmc_code, avg_speed_this_hour ASC
            ),
            onset_hour_pick AS (
                SELECT DISTINCT ON (bh.tmc_code) bh.tmc_code, bh.hour AS onset_hour
                FROM by_hour bh
                JOIN peak_hour_pick pp ON pp.tmc_code = bh.tmc_code
                WHERE pp.peak_congested_count > 0 AND bh.congested_count >= 0.3 * pp.peak_congested_count
                ORDER BY bh.tmc_code, bh.hour ASC
            ),
            busiest_dow_pick AS (
                SELECT DISTINCT ON (tmc_code) tmc_code, dow AS busiest_dow
                FROM by_dow ORDER BY tmc_code, congested_count DESC
            ),
            occurrence AS (
                SELECT tmc_code,
                    COUNT(*) FILTER (
                        WHERE day_peak_speed IS NOT NULL AND day_free_flow_speed IS NOT NULL AND day_free_flow_speed > 0
                    ) AS analyzed_days,
                    COUNT(*) FILTER (WHERE day_peak_speed < %(threshold)s * day_free_flow_speed) AS occurrence_days,
                    AVG(day_peak_speed) AS avg_speed_peak,
                    AVG(day_free_flow_speed) AS avg_speed_free_flow
                FROM daily_peak
                WHERE day_peak_speed IS NOT NULL AND day_free_flow_speed IS NOT NULL AND day_free_flow_speed > 0
                GROUP BY tmc_code
            ),
            queue AS (
                SELECT tmc_code,
                    SUM(congested_hours) AS total_congested_hours,
                    MAX(congested_hours) AS max_daily_congested_hours
                FROM daily_totals GROUP BY tmc_code
            )
            SELECT
                o.tmc_code, o.analyzed_days, o.occurrence_days, o.avg_speed_peak, o.avg_speed_free_flow,
                q.total_congested_hours, q.max_daily_congested_hours,
                ph.peak_hour, oh.onset_hour, bd.busiest_dow,
                wr.day AS busiest_date, wr.hour AS busiest_hour, wr.avg_speed AS min_speed
            FROM occurrence o
            JOIN queue q ON q.tmc_code = o.tmc_code
            JOIN peak_hour_pick ph ON ph.tmc_code = o.tmc_code
            LEFT JOIN onset_hour_pick oh ON oh.tmc_code = o.tmc_code
            JOIN busiest_dow_pick bd ON bd.tmc_code = o.tmc_code
            JOIN worst_reading wr ON wr.tmc_code = o.tmc_code
            """,
            {
                "peak_hours": peak_hours,
                "free_flow_hours": free_flow_hours,
                "tmcs": tmcs,
                "threshold": cbi_detector.CUTOFF_RATIO,
            },
        )
        columns = [d.name for d in cursor.description]
        rows = cursor.fetchall()
    if not rows:
        return pl.DataFrame(schema=_CHARACTERIZATION_SCHEMA)
    return pl.DataFrame(rows, schema=columns, orient="row")


def _build_intersection_candidates(tmc_frame: pl.DataFrame, characterization: pl.DataFrame) -> pl.DataFrame:
    """Steps 4-10: join per-TMC characterization onto the named-arterial
    TMC set, derive speed_drop_ratio/occurrence_pct and per-TMC queue
    mile-hours (congested-hour count x this TMC's own segment length),
    then group consecutive same-intersection TMCs (the primary/secondary
    NPMRDS variant pairs at one physical cross-street — see
    _load_backfilled_tmc_metadata) into one candidate location per
    (arterial, direction, intersection). The location is represented by
    whichever member TMC has the WORST (lowest) speed_drop_ratio — sorting
    before grouping so every .first() aggregation below refers to that
    same row, keeping the reported coordinates/queue-hours/peak-onset-
    busiest fields tied to the segment that actually produced the reported
    ratio, not an arbitrary group member. occurrence_pct (max) and aadt
    (median) are aggregated across the whole group independently, since
    those represent "how bad does this intersection get" and "how much
    traffic does it carry" respectively, not one single segment's
    reading."""
    joined = tmc_frame.join(characterization, left_on="tmc", right_on="tmc_code", how="inner")
    joined = joined.filter(
        pl.col("avg_speed_free_flow").is_not_null()
        & (pl.col("avg_speed_free_flow") > 0)
        & pl.col("avg_speed_peak").is_not_null()
        & pl.col("aadt_filled").is_not_null()
        & pl.col("intersection").is_not_null()
        & pl.col("miles").is_not_null()
        & (pl.col("analyzed_days") >= 30)
        # Belt-and-suspenders on top of the backfill donor-pool fix above:
        # a road-name match can still catch a directly Interstate/Expressway-
        # classified TMC (own f_system, not backfilled) at an interchange —
        # e.g. a genuine Interstate ramp coded under a nearby arterial's
        # street name. "Named arterial" analysis should only ever include
        # actually Arterial-classified segments.
        & pl.col("f_system_filled").is_in([3, 4])
        # "Intersection" congestion should be localized (cross-street to
        # cross-street), not a multi-mile highway stretch — found directly
        # investigating a real disparity report: some rural Arterial TMCs
        # (e.g. a single GA-54 segment covering 6.07 miles with no shapefile
        # geometry at all) are (a) too long to render as a plausible straight
        # line between two intersections, since real roads bend over that
        # distance, and (b) since annual_queue_mile_hours scales directly
        # with segment length, a long rural segment congested for the same
        # DURATION as a short urban one gets credited many times the queue
        # mile-hours purely from being physically longer, not from being
        # more severely congested — this was inflating rural highway
        # segments' severity_index past genuinely worse urban arterials
        # (e.g. Buford Hwy) for a reason unrelated to actual congestion
        # severity. MAX_SEGMENT_MILES excludes the minority (~5% of named-
        # arterial TMCs, ~17% of already-analyzed-corridor TMCs) that are
        # long enough for both problems to matter.
        & (pl.col("miles") <= MAX_SEGMENT_MILES)
    ).with_columns(
        (pl.col("avg_speed_peak") / pl.col("avg_speed_free_flow")).alias("speed_drop_ratio"),
        (100.0 * pl.col("occurrence_days") / pl.col("analyzed_days")).alias("occurrence_pct"),
        (pl.col("total_congested_hours") * pl.col("miles")).alias("annual_queue_mile_hours"),
        (pl.col("max_daily_congested_hours") * pl.col("miles")).alias("max_queue_mile_hours"),
    )

    if joined.is_empty():
        return joined

    joined_sorted = joined.sort("speed_drop_ratio")
    grouped = joined_sorted.group_by(
        ["arterial_name", "direction", "intersection"], maintain_order=True
    ).agg(
        [
            pl.col("speed_drop_ratio").first().alias("speed_drop_ratio"),
            pl.col("occurrence_pct").max().alias("occurrence_pct"),
            pl.col("aadt_filled").median().alias("aadt"),
            pl.col("county").first().alias("county"),
            pl.col("tmc").first().alias("tmc"),
            pl.col("start_latitude").first().alias("start_latitude"),
            pl.col("start_longitude").first().alias("start_longitude"),
            pl.col("end_latitude").first().alias("end_latitude"),
            pl.col("end_longitude").first().alias("end_longitude"),
            pl.col("analyzed_days").first().alias("analyzed_days"),
            pl.col("annual_queue_mile_hours").first().alias("annual_queue_mile_hours"),
            pl.col("max_queue_mile_hours").first().alias("max_queue_mile_hours"),
            pl.col("peak_hour").first().alias("peak_hour"),
            pl.col("onset_hour").first().alias("onset_hour"),
            pl.col("busiest_dow").first().alias("busiest_dow"),
            pl.col("busiest_date").first().alias("busiest_date"),
            pl.col("busiest_hour").first().alias("busiest_hour"),
            pl.col("min_speed").first().alias("min_speed"),
        ]
    )
    grouped = grouped.with_columns(
        (
            pl.col("annual_queue_mile_hours")
            * (pl.col("occurrence_pct") / 100.0)
            * (1.0 - pl.col("speed_drop_ratio"))
            * (pl.col("aadt") / 100_000.0)
        ).alias("severity_index")
    )
    return grouped


# Descriptive tier labels for the K-means legend, worst-last (matches
# BOTTLENECK_SEVERITY_COLORS' light-orange -> dark-maroon ramp) — used
# instead of cbi_map_geometry.kmeans_multi_feature_classes' generic
# "Cluster N of K" default, which doesn't tell a reader which end is worse.
SEVERITY_TIER_LABELS = ["Least Severe", "Low", "Moderate", "High", "Most Severe"]


_SEVERITY_FEATURES = ["speed_drop_ratio", "occurrence_pct", "aadt", "annual_queue_mile_hours"]
_SEVERITY_HIGHER_IS_WORSE = [False, True, True, True]


def _kmeans_severity_tiers(df: pl.DataFrame) -> tuple[list[int], list[tuple[str, str]]]:
    """kmeans_multi_feature_classes on [speed_drop_ratio, occurrence_pct,
    aadt, annual_queue_mile_hours], relabeled with SEVERITY_TIER_LABELS
    (Least Severe...Most Severe) instead of the generic "Cluster N of K" —
    shared by both the region-wide selection fit and the display-only
    re-fit below, so the two stay in sync if the feature set ever changes."""
    features = df.select(_SEVERITY_FEATURES).rows()
    ranks, raw_legend, _ = cbi_map_geometry.kmeans_multi_feature_classes(
        [list(row) for row in features], higher_is_worse=_SEVERITY_HIGHER_IS_WORSE
    )
    legend = [
        (color, SEVERITY_TIER_LABELS[i] if i < len(SEVERITY_TIER_LABELS) else label)
        for i, (color, label) in enumerate(raw_legend)
    ]
    return ranks, legend


def _select_top_n(candidates: pl.DataFrame, n: int = TOP_N) -> pl.DataFrame:
    """Step 9: multi-feature K-means fit region-wide (over every candidate,
    not just the top N), ranked worst-cluster-first; within a cluster,
    ordered by severity_index (step 10) rather than the abstract
    standardized cluster-fit score, so a top-N cut that lands mid-cluster
    picks the worst members of that cluster first by a number a reader can
    actually interpret. This fit exists purely to choose WHICH N win —
    see _reclassify_for_symbology for why the map/legend colors come from
    a separate, second fit rather than this one."""
    ranks, _ = _kmeans_severity_tiers(candidates)
    candidates = candidates.with_columns(pl.Series("severity_rank", ranks))
    ranked = candidates.sort(["severity_rank", "severity_index"], descending=[True, True])
    return ranked.head(n)


def _reclassify_for_symbology(top_n: pl.DataFrame) -> tuple[pl.DataFrame, list[tuple[str, str]]]:
    """Re-fits K-means on just the SELECTED top-N candidates, overwriting
    their severity_rank, so the map/legend's 5-tier color ramp actually
    differentiates among them. Fitting on the full region-wide pool (as
    _select_top_n does, deliberately, for selection) and then displaying
    only the worst-ranked cluster's members — which is what the top N
    almost always are, once N is much smaller than that cluster's total
    size — collapsed every displayed segment into the same "Most Severe"
    color with no way to tell them apart (confirmed directly: expanding to
    67 arterials/1200 candidates made every one of the top 50 land in one
    oversized worst cluster). Selection stays based on the region-wide
    fit; only the DISPLAYED color/legend comes from this local one."""
    ranks, legend = _kmeans_severity_tiers(top_n)
    return top_n.with_columns(pl.Series("severity_rank", ranks)), legend


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------
def _hourly_comparison_figure(baselines: dict) -> go.Figure:
    system_hours = [h for h, _ in baselines["system_wide"]]
    system_speeds = [s for _, s in baselines["system_wide"]]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=system_hours, y=system_speeds, name="System-wide (all roads)",
        mode="lines+markers", line=dict(color=INK_MUTED, width=2, dash="dash"),
    ))
    for road_class in ROAD_CLASS_ORDER:
        curve = baselines["by_class"].get(road_class) or []
        if not curve:
            continue
        fig.add_trace(go.Scatter(
            x=[h for h, _ in curve], y=[s for _, s in curve], name=road_class,
            mode="lines+markers", line=dict(color=ROAD_CLASS_LINE_COLORS[road_class], width=2.5),
        ))
    fig.update_layout(
        title=dict(text="Average Speed by Hour of Day", font=dict(color=INK_PRIMARY, size=15)),
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        font=dict(color=INK_SECONDARY, family="system-ui, -apple-system, 'Segoe UI', sans-serif"),
        height=440, width=820, margin=dict(l=50, r=140, t=50, b=40),
        legend=dict(orientation="v", x=1.02, y=1, xanchor="left"),
        xaxis=dict(title="Hour of day", dtick=2, gridcolor=GRID, linecolor=GRID),
        yaxis=dict(title="Average speed (mph)", gridcolor=GRID, linecolor=GRID),
    )
    return fig


def _format_hour_ampm(hour: int | None) -> str:
    if hour is None:
        return "N/A"
    period = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:00 {period}"


def _build_map(top_n: pl.DataFrame) -> str:
    """Same interactive Canvas map engine as the main regional report's
    Region Map tab — real TMC road geometry, hover popups showing name/
    severity/worst date&time (_CANVAS_MAP_JS, imported rather than
    reimplemented, so this behaves identically). Only the top N
    intersections are drawn — no gray "every other analyzed location"
    context layer. corridorId (the 4th record element) is left null since
    this standalone page has no Corridors tab to click-navigate to;
    hovering still works without it, and the click-to-navigate branch in
    the shared JS simply no-ops when it's absent."""
    polylines = cbi_map_geometry.load_tmc_polylines()
    segments: list[list] = []
    for row in top_n.iter_rows(named=True):
        points = cbi_map_geometry.offset_points(
            cbi_map_geometry.resolve_points(
                row.get("tmc"), row["start_latitude"], row["start_longitude"],
                row["end_latitude"], row["end_longitude"], polylines,
            )
        )
        color = cbi_map_geometry.BOTTLENECK_SEVERITY_COLORS[int(row["severity_rank"])]
        direction_abbrev = cbi_map_geometry.abbreviate_direction(row["direction"])
        segments.append(
            [
                points,
                color,
                3.2,
                None,
                {
                    "name": f"{row['arterial_name']} {direction_abbrev} — {row['intersection']}",
                    "severity": float(row["severity_index"]),
                    "worstDate": cbi_map_geometry.format_popup_date(row.get("busiest_date")),
                    "worstTime": _format_hour_ampm(row.get("busiest_hour")),
                },
            ]
        )

    return _canvas_map_html(
        segments, height=620, width=1180, interactive=True,
        county_rings=_load_county_boundary_rings(), responsive=True,
    )


WEEKDAY_NAMES_BY_ISODOW = {1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday", 5: "Friday"}


def _build_top_n_table(top_n: pl.DataFrame) -> str:
    header = (
        "<tr><th>Rank</th><th>Arterial</th><th>Direction</th><th>Intersection</th>"
        "<th>County</th><th>AADT</th><th>Occurrence %</th><th>Speed-Drop Ratio</th>"
        "<th>Severity Index</th><th>Peak Hour</th><th>Onset</th><th>Max Queue Mi-Hrs</th>"
        "<th>Min Speed</th><th>Busiest Day</th><th>Busiest Time</th></tr>"
    )
    rows = "".join(
        "<tr>"
        f"<td>{i + 1}</td>"
        f"<td>{html.escape(str(row['arterial_name']))}</td>"
        f"<td>{html.escape(cbi_map_geometry.abbreviate_direction(row['direction']))}</td>"
        f"<td>{html.escape(str(row['intersection'] or ''))}</td>"
        f"<td>{html.escape(str(row['county'] or '-'))}</td>"
        f"<td>{int(row['aadt']):,}</td>"
        f"<td>{_fmt2(row['occurrence_pct'])}%</td>"
        f"<td>{_fmt2(row['speed_drop_ratio'])}</td>"
        f"<td>{_fmt2(row['severity_index'])}</td>"
        f"<td>{_format_hour_ampm(row['peak_hour'])}</td>"
        f"<td>{_format_hour_ampm(row['onset_hour'])}</td>"
        f"<td>{_fmt2(row['max_queue_mile_hours'])}</td>"
        f"<td>{_fmt2(row['min_speed'])} mph</td>"
        f"<td>{html.escape(WEEKDAY_NAMES_BY_ISODOW.get(row['busiest_dow'], '-'))}</td>"
        f"<td>{cbi_map_geometry.format_popup_date(row['busiest_date'])} {_format_hour_ampm(row['busiest_hour'])}</td>"
        "</tr>"
        for i, row in enumerate(top_n.iter_rows(named=True))
    )
    return f"<div class='card' style='overflow-x:auto;'><table class='data'>{header}{rows}</table></div>"


def generate_intersection_congestion_severity(connection: psycopg.Connection, output_root: Path) -> Path:
    t_start = time.time()
    print("Loading region-wide hourly baselines (system-wide + Arterial)...")
    baselines = _load_hourly_baselines(connection)
    print(
        f"  free-flow hour(s): {baselines['free_flow_hours']}, "
        f"peak hour(s): {baselines['peak_hours']} ({time.time() - t_start:.1f}s)"
    )

    print("Loading and backfilling region-wide TMC metadata...")
    all_tmcs = _load_backfilled_tmc_metadata()
    named_tmcs = _select_named_arterial_tmcs(all_tmcs)
    existing_tmcs = _select_existing_arterial_tmcs(connection, all_tmcs)
    arterial_tmcs = _combine_arterial_tmcs(named_tmcs, existing_tmcs)
    arterial_count = arterial_tmcs["arterial_name"].n_unique()
    print(f"  {arterial_tmcs.height} TMCs across {arterial_count} arterials "
          f"({len(NAMED_ARTERIALS)} newly-named + {existing_tmcs['arterial_name'].n_unique()} already-analyzed)")

    print("Computing per-TMC characterization (speed-drop, occurrence, queue mile-hours, timing)...")
    characterization = _load_tmc_characterization(
        connection, arterial_tmcs["tmc"].to_list(), baselines["peak_hours"], baselines["free_flow_hours"]
    )

    candidates = _build_intersection_candidates(arterial_tmcs, characterization)
    print(f"  {candidates.height} candidate intersections")

    top_n = _select_top_n(candidates)
    top_n, legend = _reclassify_for_symbology(top_n)
    map_html = _build_map(top_n)

    hourly_fig = _hourly_comparison_figure(baselines)
    legend_items = "".join(
        f"<div class='map-legend-item'><span class='swatch dot' style='background:{color}'></span>{label}</div>"
        for color, label in legend
    )

    explain_html = f"""
    <div class='map-explain'>
      <h4>Reading this page</h4>
      <p>Free-flow hour(s): <strong>{', '.join(f'{h}:00' for h in baselines['free_flow_hours'])}</strong>
      &mdash; peak hour(s): <strong>{', '.join(f'{h}:00' for h in baselines['peak_hours'])}</strong>.
      Both are fixed, region-wide constants derived from the Arterial-only average-speed-by-hour curve
      (highest average speed = free-flow, lowest = peak) &mdash; the same two hours are used to score
      every candidate intersection below, so every location is measured against the same reference.</p>
      <p>Speed-drop ratio = a candidate's own average speed at the peak hour(s) divided by its own
      average speed at the free-flow hour(s); lower means a bigger drop. Occurrence is the % of
      analyzed weekdays that ratio fell below {cbi_detector.CUTOFF_RATIO:.0%} for that specific day
      &mdash; anchored to this page's own free-flow-hour baseline rather than each TMC's raw
      reference_speed field, to avoid crediting normal signal-cycle slowdowns as congestion (see the
      Occurrence caution note in the main regional report).</p>
      <p>Queue mile-hours: every hour of every analyzed weekday is checked against this location's own
      free-flow-hour speed (same {cbi_detector.CUTOFF_RATIO:.0%} threshold) — every hour that fails
      counts as one congested hour that day. Max Queue Mi-Hrs in the table below is that count on the
      single worst day, times segment length; this is what lets the ranking capture how LONG
      congestion lasts here, not just how much speed drops at one hour.</p>
      <p>AADT is backfilled from same-route (tmclinear) siblings where NPMRDS left it null, and
      intersections combine the primary/secondary TMC pair NPMRDS records for the same physical
      cross-street. The top {TOP_N} are selected by K-means clustering on speed-drop ratio,
      occurrence, AADT, and annual queue mile-hours together (standardized so no one factor
      dominates), fit region-wide across every candidate &mdash; only these {TOP_N} are drawn on
      the map. The map/legend colors come from a SECOND K-means fit scoped to just these {TOP_N}
      (same 4 features, same 5-tier labels) so the color ramp differentiates among them; fitting
      that on the full region-wide pool instead would put nearly all of them in the single worst
      cluster, since the region-wide "most severe" cluster is far larger than {TOP_N}.</p>
      <p><strong>Severity Index</strong> = annual queue mile-hours &times; (occurrence % / 100)
      &times; (1 &minus; speed-drop ratio) &times; (AADT / 100,000) &mdash; the same shape as the
      main regional report's AADT-weighted severity index, now using this page's own directly-
      computed queue mile-hours. Shown for interpretability alongside the K-means selection; it does
      not change which {TOP_N} intersections are selected, only how they're ordered within a
      cluster.</p>
      <p>Peak Hour/Onset are this location's OWN busiest/starting hour (not necessarily the
      region-wide reference hours above) &mdash; Onset is the earliest hour whose congested-hour
      count reaches at least 30% of the peak hour's own count. Busiest Day/Time are the single
      worst-speed observation of the year at this location.</p>
    </div>
    """

    generated_on = time.strftime("%Y-%m-%d")
    logo_uri = _agency_logo_data_uri()
    logo_html = (
        f'<img class="agency-logo" src="{logo_uri}" alt="Atlanta Regional Commission logo" />'
        if logo_uri else ""
    )

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Intersection Congestion Severity — CBI</title>
<style>{_PAGE_CSS}</style>
</head>
<body>
<script>{_CANVAS_MAP_JS}</script>
<header class="masthead">
  {logo_html}
  <h1>Intersection Congestion Severity</h1>
  <p>Alternative methodology &mdash; standalone page. Generated {generated_on}.</p>
</header>
<main>
  <p class="muted">Ranks the top {TOP_N} arterial intersections region-wide by a speed-drop ratio
  anchored to region-wide peak/free-flow hour baselines, combined with occurrence, AADT, and queue
  mile-hours via K-means. Covers {arterial_count} arterials: {len(NAMED_ARTERIALS)} newly named ones
  not currently covered by the main CBI pipeline's corridor analysis (Buford Hwy, Roswell Rd,
  Piedmont Rd, and others — see the page source for the full list and how each road's identity was
  resolved) plus every already-analyzed Arterial corridor (GA-20, GA-141, US-19, US-78, and the
  rest), all scored here with this page's own methodology so they're directly comparable on one
  ranking. This is a parallel, standalone methodology for now, not yet combined into the main
  regional report.</p>
  <div class="card map-card">
    <div class='map-with-legend'>
      {explain_html}
      {map_html}
      <div class='map-legend'>
        <h4>Severity Ranking<br/>(K-means: speed-drop, occurrence, AADT, queue mi-hrs)</h4>
        {legend_items}
      </div>
    </div>
  </div>
  <div class="chart-grid">
    <div class="card">{_fig_html(hourly_fig, fixed_size=True)}</div>
  </div>
  <h3 class="section-title">Top {TOP_N} Intersections</h3>
  {_build_top_n_table(top_n)}
</main>
</body>
</html>
"""

    output_dir = output_root
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / OUTPUT_FILENAME
    output_path.write_text(doc, encoding="utf-8")
    print(f"Done in {time.time() - t_start:.1f}s -> {output_path}")
    return output_path


def main() -> None:
    output_root = Path(r"C:\Users\Soheil\Desktop\CBI\outputs\multi_corridor")
    with psycopg.connect(**cbi_database.connection_kwargs()) as connection:
        generate_intersection_congestion_severity(connection, output_root)


if __name__ == "__main__":
    main()
