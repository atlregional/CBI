"""
Arterial Intersection Congestion Severity — steps 1-8.

Methodology (as specified, anchored to FHWA's PHED — Annual Hours of Peak
Hour Excessive Delay — construction, which is itself built from NPMRDS
travel times plus a traffic-volume estimate):
  1. System-wide average speed by hour of day.
  2. Arterial-only average speed by hour of day (f_system IN ARTERIAL_F_SYSTEM)
     — this is the free-flow BASE for every arterial segment: the same
     functional-class (Arterial) hourly curve is what step 3 reads its
     free-flow hour from, rather than any one segment's own possibly
     signal-cycle-noisy speed.
  3. Free-flow hour(s) = the hour(s) with the HIGHEST average speed in the
     arterial-wide curve from step 2 — identified once, system-wide, then
     applied per-segment as that segment's own free-flow baseline (V_FF,i:
     one value per segment, not varying by hour or day — kept unchanged
     from the prior version deliberately, so the EAVHD change below can be
     evaluated on its own rather than alongside a second methodology
     change at the same time). This is deliberately NOT computed per-
     segment-independently: a chronically congested segment could
     otherwise define its own artificially-low "free flow" hour and never
     register as abnormal — the exact failure mode documented in this
     project's Known Limitations section (McDonough US-23/GA-42 case).
  4. AADT per segment, with named-arterial path integration so fragmented
     corridors (Buford Hwy, Roswell Rd, etc. — see sql/001) roll up
     correctly instead of scattering across dozens of disconnected segments.
     Roads NOT in the curated registry fall back to their own raw
     tmc_metadata.road text as their group, with one normalization: a
     trailing compass-direction suffix (NE/NW/SE/SW/N/S/E/W) is stripped
     first — see _normalize_road_base for why (confirmed directly: "N
     Druid Hills Rd NE" and "N Druid Hills Rd", and "Camp Creek Pky" and
     "Camp Creek Pky SW", were fragmenting the same physical road into two
     ranked rows purely from that suffix).
  5. Excess travel time per vehicle, per segment/hour/day:
     ETT_i,t,d = L_i * max(0, 1/V_i,t,d - 1/V_FF,i) hours — the per-vehicle
     time lost to congestion traversing segment i during hour t on day d,
     relative to that segment's own free-flow baseline. Unlike the fixed
     AM/PM peak windows used for speed_drop_ratio/occurrence below, this
     is computed for EVERY hour of every analyzed weekday, since delay can
     occur outside the nominal peak.
  6. Hourly directional volume estimate (there is no real per-hour volume
     data in this database — see HOURLY_DISTRIBUTION_FACTOR and
     DIRECTIONAL_FACTOR below for what this approximates and how):
     V_hat_i,h = AADT_i * HDF_h * D.
  7. Estimated vehicle-hours of delay: EVD_i,t,d = ETT_i,t,d * V_hat_i,t;
     annual per-segment total: EAVHD_i = sum over (t, d) of EVD_i,t,d.
     Since V_hat only varies by hour (not day), this factors as
     EAVHD_i = AADT_i * D * sum_t [ HDF_t * sum_d ETT_i,t,d ] — computed
     that way (compute_excess_travel_time_by_hour + compute_segment_eavhd)
     rather than looping every (hour, day) pair explicitly.
  8. Aggregation by path/road name: segments are rolled up to one row per
     named arterial/path (arterial_group) — e.g. Buford Hwy, Roswell Rd,
     Piedmont Ave — not per intersection. speed_drop_ratio (Intensity) is
     averaged across the road's segments, AADT (Exposure) is the median,
     and EAVHD is SUMMED across every segment — EAVHD_total, the road's
     total estimated annual vehicle-hours of delay. Recurrence is a
     LENGTH-AND-DAY-WEIGHTED ratio, NOT a plain average of per-segment
     occurrence_pct — see aggregate_to_road_level for why a plain average
     (or, worse, an "any segment congested that day" OR-style rollup)
     lets a long multi-segment road accumulate recurrence unfairly
     relative to a short one. Ranking uses EAVHD_density = EAVHD_total /
     total_miles, NOT the raw total: the raw total scales directly with
     how many miles of road matched a named arterial, so a 30+ mile
     rural/exurban route (many segments, most only mildly congested)
     consistently out-ranked short, severely-congested in-town corridors
     purely from accumulating delay over more total miles (confirmed
     directly on the prior queue-mile-hours version of this metric, which
     had the identical problem — GA-120/GA-20/GA-92/US-41/GA-138 filled
     the top 5). Roads under MIN_ROAD_MILES total length are excluded from
     ranking entirely, since dividing by a near-zero denominator produces
     the opposite artifact — an outsized density with almost no real
     congestion behind it. The old queue-mile-hours severity_index is kept
     as an experimental comparison column, not used for ranking.

Output: a new table, arterial_intersection_severity, plus a DataFrame
returned for use by the standalone HTML page generator (Section 6).

On "estimated": every EAVHD figure here is built from a MODELED hourly
volume (AADT x a generic distribution curve x an assumed directional
split), not an observed vehicle count — see HOURLY_DISTRIBUTION_FACTOR and
DIRECTIONAL_FACTOR below. Calling it plain "vehicle-hours of delay" would
overstate the precision; every user-facing label in this file and the page
generator says "Estimated Annual Vehicle-Hours of Delay (EAVHD)" for that
reason, deliberately, every time. EAVHD_density is the headline ranking
variable and answers "how concentrated is delay on this road" — it is a
DIFFERENT question from EAVHD_total ("how much total regional delay does
this road produce"), and the two can disagree (a 46-mile road can have
12x the total delay of a 1.5-mile road while having a fraction of its
density) — see the page's title and methodology text, which are explicit
that this is a ranking by DENSITY, not an unqualified "most congested"
claim.

On Congestion Operating Patterns (COPs) vs. HDF (two DELIBERATELY separate
concepts, and deliberately NOT called "TPG" — that acronym is already used
in traffic engineering for volume/count pattern groups, and these are
something different): compute_hourly_profiles/build_cop_features/
cluster_congestion_patterns classify every segment into a congestion-SHAPE
cluster (e.g. "COP-3 — AM/PM Commuter", "COP-1 — All-Day Urban") derived
purely from this database's own INRIX-observed speeds, normalized against
each segment's own free-flow speed. This is real, directly-measured
information about WHEN speed deteriorates on each Atlanta road — but it is
NOT a volume distribution, and cannot be turned into one: there is no
reliable one-to-one relationship between speed and volume, especially near
capacity, where speed can fall while actual throughput also falls. So the
COP label is used here only as a descriptive/diagnostic column and as the
input to compute_rank_stability's sensitivity test (does EAVHD's ranking
hold up under different plausible HDF shapes) — it does NOT replace
HOURLY_DISTRIBUTION_FACTOR or select a different HDF curve per group. A
future refinement could match each COP's own shape to whichever candidate
HDF curve resembles it most closely; this version does not attempt that
and tests all HDF candidates against every road instead.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

import polars as pl
import psycopg

import cbi_database
import cbi_map_geometry

# ---- Config — review before running -----------------------------------
ARTERIAL_F_SYSTEM = (3, 4)      # FHWA: 3=Principal Arterial, 4=Minor Arterial
CONGESTION_RATIO = 0.70          # consistent with the rest of the pipeline
FREE_FLOW_TOP_N_HOURS = 1        # widen to e.g. 3 for a more stable baseline
AM_PEAK_HOURS = (7, 8, 9)
PM_PEAK_HOURS = (16, 17, 18)
TOP_N = 20
MIN_ANALYZED_DAYS = 30           # segments with less data than this are excluded
WEEKDAYS_PER_YEAR = 261          # matches the weekday-count convention used
                                  # elsewhere in this project's reports

# Corridor eligibility floor (aggregate_to_road_level's eligible_for_ranking,
# enforced by rank_roads): a road's EAVHD_density divides EAVHD_total by its
# own analyzed length, so a named arterial resolved to just one short/
# partial TMC fragment blows the density metric up to an outsized value
# with no real congestion behind it. eligible_for_ranking requires
# road_miles >= MIN_ROAD_MILES AND (road_miles >= 1.0 OR tmc_count >= 4) —
# a road under this absolute floor is excluded outright regardless of TMC
# count; between MIN_ROAD_MILES and 1.0 mile, a road still needs at least 4
# TMCs supporting it to count as well-segmented rather than a thin, weakly
# supported fragment (confirmed directly: GA-205, 0.54 mi on just 2 TMCs
# with 82.6% of its own delay concentrated in one of them, fails this test;
# GA-11, 8.06 mi on 15 TMCs, passes comfortably).
MIN_ROAD_MILES = 0.5

# Directional split of two-way AADT applied to each (one-directional) TMC's
# hourly volume estimate. There is no directional-count data in this
# database, so this is a TRANSPARENT, NEUTRAL APPROXIMATION for a typical
# urban arterial (FHWA notes urban-center facilities can be near a 50/50
# directional split), not a measured value. Replace with a real per-road
# directional factor the moment one is available; until then this is
# explicitly the same assumption for every road, so it does not bias one
# road relative to another.
DIRECTIONAL_FACTOR = 0.5

# Hourly Distribution Factor: the estimated percentage of a road's AADT
# occurring in each hour of an average weekday, i.e. V_hat_i,h = AADT_i *
# (HDF_h / 100) * D. FHWA's Traffic Monitoring Guide describes building
# exactly this kind of hourly adjustment/distribution factor from the
# percentage of daily traffic in each hour, generally recommending it be
# stratified by facility type and geography (GDOT continuous-count-station
# profiles, ideally grouped by functional class/urban context, would be
# the right source for this project). No such GDOT/TMAS profile is loaded
# in this database, so this falls to the documented fallback: a REAL,
# PUBLISHED, count-station-derived curve rather than a fabricated one —
# Delaware DOT Traffic Monitoring Program, "Hourly Distribution of AADT
# for Traffic Groups (%)," Average Weekday Traffic (2002), averaged across
# the source document's 7 populated traffic-pattern groups (an 8th group
# present in that document is entirely zero and excluded). That source
# does not include the group-to-facility-type legend needed to confirm
# which specific column is "urban arterial," so this is used as a general
# urban/mixed-roadway weekday shape, not a facility-type-specific one —
# explicitly the weakest, least-defensible input to EAVHD. See
# compute_rank_stability for the 4-scenario sensitivity test this is
# checked against.
HOURLY_DISTRIBUTION_FACTOR: dict[int, float] = {
    0: 1.59, 1: 1.10, 2: 0.78, 3: 0.50, 4: 0.52, 5: 0.82,
    6: 1.62, 7: 2.54, 8: 3.75, 9: 5.18, 10: 6.35, 11: 7.13,
    12: 7.61, 13: 7.47, 14: 7.43, 15: 7.41, 16: 7.30, 17: 6.94,
    18: 6.21, 19: 5.29, 20: 4.42, 21: 3.55, 22: 2.64, 23: 1.86,
}  # sums to ~100.0

# HDF sensitivity-test candidates (see compute_rank_stability) — 4 plausible
# scenarios total including the primary curve above, per the reviewed
# guidance: "run the entire ranking with perhaps four HDF scenarios... if
# rankings remain stable, you've largely neutralized the criticism that the
# absolute EAVHD numbers depend on Delaware's specific curve."
#   - HDF_ALTERNATE_FLATTER: the SAME cited Delaware source's TPGROUP1
#     column (not the 7-group average) — a real, individually-cited flatter
#     midday shape (~6.6-6.9% through 11am-5pm), used as-is.
#   - HDF_UNIFORM: 1/24 every hour — a deliberately naive, non-peaked
#     baseline scenario (not a real curve; a synthetic control case).
#   - HDF_SYNTHETIC_COMMUTER: a deliberately synthetic, textbook AM+PM
#     double-peak commuter shape (NOT a real cited curve) — built here only
#     as a plausible bracketing alternative for sensitivity testing, never
#     used as the primary/production curve. Labeled "synthetic" everywhere
#     it appears so it is never mistaken for measured data.
HDF_ALTERNATE_FLATTER: dict[int, float] = {
    0: 2.11, 1: 1.49, 2: 1.24, 3: 0.83, 4: 0.80, 5: 1.13,
    6: 1.87, 7: 2.65, 8: 3.59, 9: 4.59, 10: 5.62, 11: 6.32,
    12: 6.62, 13: 6.75, 14: 6.86, 15: 6.88, 16: 6.90, 17: 6.69,
    18: 6.15, 19: 5.47, 20: 4.89, 21: 4.28, 22: 3.60, 23: 2.69,
}
HDF_UNIFORM: dict[int, float] = {h: 100.0 / 24 for h in range(24)}
_SYNTHETIC_COMMUTER_RAW: dict[int, float] = {
    0: 0.3, 1: 0.2, 2: 0.2, 3: 0.2, 4: 0.3, 5: 1.0,
    6: 3.5, 7: 8.5, 8: 9.0, 9: 6.0, 10: 4.0, 11: 4.0,
    12: 4.5, 13: 4.5, 14: 4.5, 15: 5.5, 16: 8.5, 17: 9.5,
    18: 8.0, 19: 5.0, 20: 3.5, 21: 2.5, 22: 1.5, 23: 0.8,
}
_synthetic_commuter_sum = sum(_SYNTHETIC_COMMUTER_RAW.values())
HDF_SYNTHETIC_COMMUTER: dict[int, float] = {
    h: v / _synthetic_commuter_sum * 100.0 for h, v in _SYNTHETIC_COMMUTER_RAW.items()
}

# --- Atlanta-specific Congestion Operating Pattern (COP) clustering (kept
# SEPARATE from HDF/volume -- speed shape cannot be reliably converted to a
# volume distribution, especially near capacity where speed can fall while
# throughput also falls; see the module docstring). Derived entirely from
# this database's own INRIX-observed speeds -- a genuinely measured,
# Atlanta-specific descriptive dimension, not an imported national curve.
# Deliberately NOT called "TPG" (Traffic Pattern Group), which already
# means a volume/count pattern in traffic engineering. ---
COP_AM_HOURS = (6, 7, 8, 9, 10)
COP_PM_HOURS = (15, 16, 17, 18, 19)
COP_DAYTIME_HOURS = tuple(range(6, 21))   # 6am-8pm inclusive -- overnight
                                            # hours carry little
                                            # discrimination for arterial
                                            # congestion shape
COP_K_CANDIDATES = (4, 5, 6)

CONGESTION_PATTERN_LABELS: dict[str, str] = {
    "all_day": "COP-1 - All-Day Urban",
    "pm_dominant": "COP-2 - PM-Dominant",
    "am_pm_commuter": "COP-3 - AM/PM Commuter",
    "midday_commercial": "COP-4 - Midday/Commercial",
    "irregular": "COP-5 - Irregular/Mixed",
    "uncongested": "COP-6 - Mostly Uncongested",
}

# Trailing compass-direction suffix (NE/NW/SE/SW/N/S/E/W) on an otherwise
# uncurated road name -- see _normalize_road_base.
_DIRECTION_SUFFIX_RE = re.compile(r"\s+(NE|NW|SE|SW|N|S|E|W)$")
# -------------------------------------------------------------------------


def _normalize_road_base(road: str) -> str:
    """Strips a single trailing compass-direction suffix from an uncurated
    road name before using it as its own fallback group (see
    attach_named_arterial_groups) — fixes physical-corridor fragmentation
    like "N Druid Hills Rd NE" vs "N Druid Hills Rd", or "Camp Creek Pky"
    vs "Camp Creek Pky SW", where tmc_metadata records the same real road
    under slightly different direction-suffixed text (confirmed directly:
    both pairs already share the same county, so this is genuinely the
    same physical road, not two different ones that happen to share a base
    name). Does NOT touch curated NAMED_ARTERIALS matches, and does not
    attempt broader normalization (abbreviation variants like "Blvd" vs
    "Boulevard" are not addressed here — only the specific suffix pattern
    that was confirmed to be fragmenting real roads)."""
    return _DIRECTION_SUFFIX_RE.sub("", road.strip())


@dataclass(frozen=True)
class FreeFlowResult:
    free_flow_hours: list[int]
    arterial_hourly_curve: pl.DataFrame


def compute_system_wide_hourly_speed(connection: psycopg.Connection) -> pl.DataFrame:
    """Step 1: average speed by hour of day, all roads."""
    query = """
        SELECT
            EXTRACT(HOUR FROM measurement_tstamp)::integer AS hr,
            AVG(speed) AS avg_speed_system
        FROM "Year_2025".probe_readings
        WHERE speed IS NOT NULL
        GROUP BY hr
        ORDER BY hr
    """
    return pl.read_database_uri(
        query=query, uri=cbi_database.database_uri(), engine="connectorx"
    )


def compute_arterial_hourly_speed(connection: psycopg.Connection) -> pl.DataFrame:
    """Step 2: average speed by hour of day, Arterials only — the
    functional-class curve every arterial segment's free-flow hour is
    drawn from (step 3)."""
    f_system_list = ",".join(str(v) for v in ARTERIAL_F_SYSTEM)
    query = f"""
        SELECT
            EXTRACT(HOUR FROM r.measurement_tstamp)::integer AS hr,
            AVG(r.speed) AS avg_speed_arterial
        FROM "Year_2025".probe_readings AS r
        JOIN "Year_2025".tmc_metadata AS m ON m.tmc = r.tmc_code
        WHERE r.speed IS NOT NULL
          AND m.f_system IN ({f_system_list})
        GROUP BY hr
        ORDER BY hr
    """
    return pl.read_database_uri(
        query=query, uri=cbi_database.database_uri(), engine="connectorx"
    )


def identify_free_flow_hours(
    arterial_hourly: pl.DataFrame, top_n: int = FREE_FLOW_TOP_N_HOURS
) -> FreeFlowResult:
    """
    Step 3 (part 1): the hour(s) with the highest arterial-wide average
    speed, identified once from the system/arterial aggregate — not
    per-segment. This single reference point gets applied to every segment
    individually in compute_segment_drop_ratios().
    """
    ranked = arterial_hourly.sort("avg_speed_arterial", descending=True)
    free_flow_hours = ranked.head(top_n)["hr"].to_list()
    return FreeFlowResult(free_flow_hours=free_flow_hours, arterial_hourly_curve=arterial_hourly)


def compute_segment_hourly_speed(connection: psycopg.Connection) -> pl.DataFrame:
    """Per-segment average speed by hour, Arterials only. Feeds step 3 (part 2)
    and carries `miles` through for the EAVHD computation (steps 5-7)."""
    f_system_list = ",".join(str(v) for v in ARTERIAL_F_SYSTEM)
    query = f"""
        SELECT
            r.tmc_code,
            m.road,
            m.intersection,
            m.county,
            m.aadt,
            m.miles,
            EXTRACT(HOUR FROM r.measurement_tstamp)::integer AS hr,
            AVG(r.speed) AS avg_speed,
            COUNT(*) AS n_obs
        FROM "Year_2025".probe_readings AS r
        JOIN "Year_2025".tmc_metadata AS m ON m.tmc = r.tmc_code
        WHERE r.speed IS NOT NULL
          AND m.f_system IN ({f_system_list})
        GROUP BY r.tmc_code, m.road, m.intersection, m.county, m.aadt, m.miles, hr
    """
    return pl.read_database_uri(
        query=query, uri=cbi_database.database_uri(), engine="connectorx"
    )


def compute_segment_drop_ratios(segment_hourly: pl.DataFrame, free_flow: FreeFlowResult) -> pl.DataFrame:
    """
    Step 3 (part 2): apply the system-identified free-flow hour(s) to each
    segment's own speed at those hours to get a per-segment free-flow
    baseline (V_FF,i), then compute that segment's speed-drop ratio
    (Intensity's complement: speed_drop_ratio = V/V_FF, so Intensity =
    1 - speed_drop_ratio) during AM/PM peak hours against its own
    free-flow baseline.
    """
    free_flow_speed_per_segment = (
        segment_hourly
        .filter(pl.col("hr").is_in(free_flow.free_flow_hours))
        .group_by("tmc_code")
        .agg(pl.col("avg_speed").mean().alias("free_flow_speed"))
    )

    peak_hours = list(AM_PEAK_HOURS) + list(PM_PEAK_HOURS)

    peak_speed_per_segment = (
        segment_hourly
        .filter(pl.col("hr").is_in(peak_hours))
        .group_by(["tmc_code", "road", "intersection", "county", "aadt", "miles"])
        .agg(pl.col("avg_speed").min().alias("peak_worst_speed"))
    )

    joined = peak_speed_per_segment.join(
        free_flow_speed_per_segment, on="tmc_code", how="inner"
    )

    return joined.with_columns(
        (pl.col("peak_worst_speed") / pl.col("free_flow_speed")).alias("peak_drop_ratio")
    ).filter(
        pl.col("free_flow_speed") > 0
    )


def attach_occurrence(connection: psycopg.Connection, segments: pl.DataFrame) -> pl.DataFrame:
    """
    Recurrence inputs: for each segment, the count of analyzed weekdays
    (analyzed_days) and how many of those had peak-hour average speed
    below CONGESTION_RATIO of the segment's own free-flow baseline
    (congested_days) — both carried forward so aggregate_to_road_level can
    compute a proper length-weighted road-level recurrence instead of a
    plain unweighted average of per-segment percentages. occurrence_pct
    itself is still computed here as a per-SEGMENT diagnostic value,
    consistent in spirit with the occurrence definition used throughout
    the rest of this project (Appendix A.4).

    "That day's peak speed" means the worst HOURLY AVERAGE among the peak
    hours that day, NOT the worst single 5-minute reading — a real bug,
    caught by auditing GA-205's TMC 101N14404, which showed a peak speed
    ratio (peak_drop_ratio, itself correctly computed from hourly averages
    in compute_segment_drop_ratios) around 0.80 but 100% recurrence: the
    prior version of this query ran MIN(r.speed) directly over raw
    probe_readings rows within the peak-hour window, so a single noisy
    5-minute dip anywhere in a 6-hour peak window (much easier to trigger
    than an hourly-average dip) was enough to flag the whole day
    "congested," inflating recurrence toward saturation across nearly
    every segment. Fixed by averaging to the hour first (matching
    compute_segment_drop_ratios's own peak-hour statistic exactly), then
    taking the worst of those hourly averages per day.
    """
    tmc_list = segments["tmc_code"].to_list()
    if not tmc_list:
        return segments.with_columns(pl.lit(0.0).alias("occurrence_pct"))

    tmc_in = ",".join(f"'{t}'" for t in tmc_list)
    peak_hours = list(AM_PEAK_HOURS) + list(PM_PEAK_HOURS)
    peak_hours_in = ",".join(str(h) for h in peak_hours)

    query = f"""
        WITH hourly AS (
            SELECT
                r.tmc_code,
                r.measurement_tstamp::date AS analysis_date,
                EXTRACT(HOUR FROM r.measurement_tstamp)::integer AS hr,
                AVG(r.speed) AS avg_speed
            FROM "Year_2025".probe_readings AS r
            WHERE r.tmc_code IN ({tmc_in})
              AND EXTRACT(HOUR FROM r.measurement_tstamp)::integer IN ({peak_hours_in})
              AND r.speed IS NOT NULL
            GROUP BY r.tmc_code, r.measurement_tstamp::date, hr
        )
        SELECT
            tmc_code,
            analysis_date,
            MIN(avg_speed) AS min_peak_speed
        FROM hourly
        GROUP BY tmc_code, analysis_date
    """
    daily = pl.read_database_uri(
        query=query, uri=cbi_database.database_uri(), engine="connectorx"
    )

    free_flow_lookup = segments.select(["tmc_code", "free_flow_speed"])
    daily = daily.join(free_flow_lookup, on="tmc_code", how="inner").with_columns(
        (pl.col("min_peak_speed") < CONGESTION_RATIO * pl.col("free_flow_speed")).alias("is_congested_day")
    )

    occurrence = (
        daily.group_by("tmc_code")
        .agg(
            pl.col("analysis_date").n_unique().alias("analyzed_days"),
            pl.col("is_congested_day").sum().alias("congested_days"),
        )
        .filter(pl.col("analyzed_days") >= MIN_ANALYZED_DAYS)
        .with_columns(
            (100.0 * pl.col("congested_days") / pl.col("analyzed_days")).alias("occurrence_pct")
        )
    )

    return segments.join(
        occurrence.select(["tmc_code", "analyzed_days", "congested_days", "occurrence_pct"]),
        on="tmc_code",
        how="inner",
    )


def compute_hourly_profiles(segment_hourly: pl.DataFrame, segments: pl.DataFrame) -> pl.DataFrame:
    """
    R_i,h = V_i,h / V_FF,i for every hour of the day, per segment —
    normalizes away each road's own absolute free-flow speed so a 35 mph
    arterial and a 55 mph arterial with the same congestion SHAPE cluster
    together. segment_hourly already carries V_i,h (average speed by hour,
    across all analyzed days — the same query used for the free-flow/peak
    speed lookups); free_flow_speed comes from `segments` (already computed
    in compute_segment_drop_ratios). Returns one row per tmc_code with
    columns r_0..r_23 (null in a given hour's column if that TMC had no
    data for that hour).
    """
    free_flow_lookup = segments.select(["tmc_code", "free_flow_speed"]).unique(subset=["tmc_code"])
    ratios = segment_hourly.join(free_flow_lookup, on="tmc_code", how="inner").with_columns(
        (pl.col("avg_speed") / pl.col("free_flow_speed")).alias("ratio")
    )
    wide = ratios.pivot(on="hr", index="tmc_code", values="ratio")
    return wide.rename({c: f"r_{c}" for c in wide.columns if c != "tmc_code"})


def build_cop_features(profiles_wide: pl.DataFrame, segments: pl.DataFrame) -> pl.DataFrame:
    """
    X_i = [R_6..R_20, AM_min, PM_min, N_congested_hours, Recurrence] — the
    daytime hourly ratios plus four interpretable summary features, so a
    handful of noisy individual hours don't dominate cluster membership on
    their own.
    """
    daytime_cols = [f"r_{h}" for h in COP_DAYTIME_HOURS if f"r_{h}" in profiles_wide.columns]
    am_cols = [f"r_{h}" for h in COP_AM_HOURS if f"r_{h}" in profiles_wide.columns]
    pm_cols = [f"r_{h}" for h in COP_PM_HOURS if f"r_{h}" in profiles_wide.columns]

    occurrence_lookup = segments.select(["tmc_code", "occurrence_pct"]).unique(subset=["tmc_code"])

    features = profiles_wide.with_columns(
        pl.min_horizontal(am_cols).alias("am_min"),
        pl.min_horizontal(pm_cols).alias("pm_min"),
        pl.sum_horizontal(
            [(pl.col(c) < CONGESTION_RATIO).cast(pl.Int32) for c in daytime_cols]
        ).alias("n_congested_hours"),
    ).join(occurrence_lookup, on="tmc_code", how="inner").with_columns(
        (pl.col("occurrence_pct") / 100.0).alias("recurrence")
    )

    keep_cols = ["tmc_code"] + daytime_cols + ["am_min", "pm_min", "n_congested_hours", "recurrence"]
    return features.select(keep_cols).drop_nulls()


def _label_congestion_pattern(am_min: float, pm_min: float, n_congested: float, n_daytime_hours: int) -> str:
    """Heuristic label from a cluster's own centroid (AM_min/PM_min depth,
    how many of the ~15 daytime hours run below CONGESTION_RATIO), mapped
    onto the fixed COP archetype taxonomy (CONGESTION_PATTERN_LABELS) so a
    given archetype always carries the same COP number across runs, rather
    than a per-run severity-ordered numbering that could shift."""
    congested_share = n_congested / n_daytime_hours
    am_congested = am_min < CONGESTION_RATIO
    pm_congested = pm_min < CONGESTION_RATIO

    if congested_share >= 0.65:
        return CONGESTION_PATTERN_LABELS["all_day"]
    if am_congested and pm_congested:
        return CONGESTION_PATTERN_LABELS["am_pm_commuter"]
    if pm_congested:
        return CONGESTION_PATTERN_LABELS["pm_dominant"]
    if am_congested:
        return CONGESTION_PATTERN_LABELS["am_pm_commuter"]
    if congested_share >= 0.25:
        return CONGESTION_PATTERN_LABELS["midday_commercial"]
    if congested_share <= 0.05 and am_min >= 0.85 and pm_min >= 0.85:
        return CONGESTION_PATTERN_LABELS["uncongested"]
    return CONGESTION_PATTERN_LABELS["irregular"]


def cluster_congestion_patterns(
    features: pl.DataFrame, k_candidates: tuple[int, ...] = COP_K_CANDIDATES
) -> tuple[pl.DataFrame, dict]:
    """
    K-means AND hierarchical (Agglomerative) clustering, tested across
    k_candidates, selected by silhouette score — computed on standardized
    features so daytime ratio columns don't get swamped by the different
    scale of n_congested_hours. Returns (tmc_code + congestion_pattern +
    cluster_id, diagnostics) — diagnostics carries every (method, k,
    silhouette) tried and the chosen one for the printed summary in
    run_analysis, so the choice is inspectable, not a silent black box.
    Silhouette picks the CANDIDATE POOL; it is not asserted to be the only
    valid choice — inspect diagnostics/cluster_names against the printed
    per-cluster summary before trusting a given run's labels blindly, per
    the "don't worship the mathematical optimum" guidance this followed.
    """
    from sklearn.cluster import AgglomerativeClustering, KMeans
    from sklearn.metrics import silhouette_score
    from sklearn.preprocessing import StandardScaler

    feature_cols = [c for c in features.columns if c != "tmc_code"]
    x = features.select(feature_cols).to_numpy()
    x_scaled = StandardScaler().fit_transform(x)

    candidates = []
    for k in k_candidates:
        km = KMeans(n_clusters=k, random_state=42, n_init=10).fit(x_scaled)
        candidates.append(("kmeans", k, silhouette_score(x_scaled, km.labels_), km.labels_))

        agg = AgglomerativeClustering(n_clusters=k).fit(x_scaled)
        candidates.append(("hierarchical", k, silhouette_score(x_scaled, agg.labels_), agg.labels_))

    best_method, best_k, best_score, best_labels = max(candidates, key=lambda c: c[2])

    labeled = features.select(["tmc_code"] + feature_cols).with_columns(
        pl.Series("cluster_id", best_labels)
    )

    cluster_names: dict[int, str] = {}
    for cluster_id in sorted(set(best_labels)):
        members = labeled.filter(pl.col("cluster_id") == cluster_id)
        cluster_names[cluster_id] = _label_congestion_pattern(
            members["am_min"].mean(), members["pm_min"].mean(),
            members["n_congested_hours"].mean(), len(COP_DAYTIME_HOURS),
        )

    labeled = labeled.with_columns(
        pl.col("cluster_id").replace_strict(cluster_names, return_dtype=pl.Utf8).alias("congestion_pattern")
    )

    diagnostics = {
        "chosen_method": best_method,
        "chosen_k": best_k,
        "chosen_silhouette": round(best_score, 3),
        "all_candidates": [(m, k, round(s, 3)) for m, k, s, _ in candidates],
        "cluster_names": cluster_names,
        "cluster_sizes": {
            cid: int((labeled["cluster_id"] == cid).sum()) for cid in sorted(set(best_labels))
        },
    }
    return labeled.select(["tmc_code", "cluster_id", "congestion_pattern"]), diagnostics


def compute_excess_travel_time_by_hour(connection: psycopg.Connection, segments: pl.DataFrame) -> pl.DataFrame:
    """
    Step 5: per-vehicle excess travel time (hours), summed across every
    analyzed weekday, for each (segment, hour-of-day) — the sum_d ETT_i,t,d
    term in step 7's factored EAVHD formula. Every hour of every analyzed
    weekday is checked (not just the fixed AM/PM peak windows used for
    speed_drop_ratio/occurrence above), since delay can occur outside the
    nominal peak: ETT_i,t,d = L_i * max(0, 1/V_i,t,d - 1/V_FF,i).
    """
    tmc_list = segments["tmc_code"].to_list()
    if not tmc_list:
        return pl.DataFrame(schema={"tmc_code": pl.Utf8, "hr": pl.Int64, "sum_excess_tt_hours": pl.Float64})

    tmc_in = ",".join(f"'{t}'" for t in tmc_list)

    query = f"""
        SELECT
            r.tmc_code,
            r.measurement_tstamp::date AS analysis_date,
            EXTRACT(HOUR FROM r.measurement_tstamp)::integer AS hr,
            AVG(r.speed) AS avg_speed
        FROM "Year_2025".probe_readings AS r
        WHERE r.tmc_code IN ({tmc_in})
          AND r.speed IS NOT NULL
          AND EXTRACT(ISODOW FROM r.measurement_tstamp) BETWEEN 1 AND 5
        GROUP BY r.tmc_code, r.measurement_tstamp::date, hr
    """
    hourly = pl.read_database_uri(
        query=query, uri=cbi_database.database_uri(), engine="connectorx"
    )

    free_flow_lookup = segments.select(["tmc_code", "free_flow_speed", "miles"]).unique(subset=["tmc_code"])
    hourly = hourly.join(free_flow_lookup, on="tmc_code", how="inner").with_columns(
        (
            pl.col("miles")
            * (pl.lit(1.0) / pl.col("avg_speed") - pl.lit(1.0) / pl.col("free_flow_speed")).clip(lower_bound=0.0)
        ).alias("excess_tt_hours")
    )

    return (
        hourly.group_by(["tmc_code", "hr"])
        .agg(pl.col("excess_tt_hours").sum().alias("sum_excess_tt_hours"))
    )


def compute_segment_eavhd(
    segments: pl.DataFrame,
    hourly_excess: pl.DataFrame,
    hdf: dict[int, float] = HOURLY_DISTRIBUTION_FACTOR,
    output_col: str = "eavhd_hours",
) -> pl.DataFrame:
    """
    Steps 6-7: apply the hourly distribution factor (estimated directional
    volume by hour) to each segment's per-hour excess travel time, sum
    across all 24 hours, and scale by AADT and the directional factor to
    get EAVHD_i — that segment's estimated annual vehicle-hours of delay,
    written to `output_col`. Parameterized by `hdf`/`output_col` so
    compute_rank_stability can call this again with the alternate HDF
    scenarios without duplicating this logic.
    """
    hdf_df = pl.DataFrame({"hr": list(hdf.keys()), "hdf_pct": list(hdf.values())})
    weighted = hourly_excess.join(hdf_df, on="hr", how="inner").with_columns(
        (pl.col("sum_excess_tt_hours") * pl.col("hdf_pct") / 100.0).alias("weighted_excess_tt_hours")
    )
    per_segment = (
        weighted.group_by("tmc_code")
        .agg(pl.col("weighted_excess_tt_hours").sum().alias("annual_excess_tt_per_veh_hours"))
    )

    return segments.join(per_segment, on="tmc_code", how="left").with_columns(
        pl.col("annual_excess_tt_per_veh_hours").fill_null(0.0)
    ).with_columns(
        (pl.col("aadt") * DIRECTIONAL_FACTOR * pl.col("annual_excess_tt_per_veh_hours")).alias(output_col)
    ).drop("annual_excess_tt_per_veh_hours")


def compute_queue_mile_hours(connection: psycopg.Connection, segments: pl.DataFrame) -> pl.DataFrame:
    """
    Legacy metric, kept purely as an experimental comparison column (NOT
    used for ranking — see aggregate_to_road_level / rank_roads): total
    annual queue mile-hours per segment, using a binary congested/not-
    congested hourly threshold (CONGESTION_RATIO) rather than EAVHD's
    continuous excess-travel-time formulation. Every hour of every
    analyzed weekday where a segment's average speed falls below
    CONGESTION_RATIO of its own free-flow-hour speed counts as one
    congested hour; summed across the year and multiplied by segment
    length (miles).
    """
    tmc_list = segments["tmc_code"].to_list()
    if not tmc_list:
        return segments.with_columns(pl.lit(0.0).alias("annual_queue_mile_hours"))

    tmc_in = ",".join(f"'{t}'" for t in tmc_list)

    query = f"""
        SELECT
            r.tmc_code,
            r.measurement_tstamp::date AS analysis_date,
            EXTRACT(HOUR FROM r.measurement_tstamp)::integer AS hr,
            AVG(r.speed) AS avg_speed
        FROM "Year_2025".probe_readings AS r
        WHERE r.tmc_code IN ({tmc_in})
          AND r.speed IS NOT NULL
          AND EXTRACT(ISODOW FROM r.measurement_tstamp) BETWEEN 1 AND 5
        GROUP BY r.tmc_code, r.measurement_tstamp::date, hr
    """
    hourly = pl.read_database_uri(
        query=query, uri=cbi_database.database_uri(), engine="connectorx"
    )

    free_flow_lookup = segments.select(["tmc_code", "free_flow_speed", "miles"]).unique(subset=["tmc_code"])
    hourly = hourly.join(free_flow_lookup, on="tmc_code", how="inner").with_columns(
        (pl.col("avg_speed") < CONGESTION_RATIO * pl.col("free_flow_speed")).alias("is_congested")
    )

    congested_hours = (
        hourly.group_by("tmc_code")
        .agg(pl.col("is_congested").sum().alias("congested_hours"))
    )

    queue = congested_hours.join(
        segments.select(["tmc_code", "miles"]).unique(subset=["tmc_code"]),
        on="tmc_code", how="inner",
    ).with_columns(
        (pl.col("congested_hours") * pl.col("miles")).alias("annual_queue_mile_hours")
    )

    return segments.join(
        queue.select(["tmc_code", "annual_queue_mile_hours"]), on="tmc_code", how="left"
    ).with_columns(pl.col("annual_queue_mile_hours").fill_null(0.0))


def attach_named_arterial_groups(connection: psycopg.Connection, segments: pl.DataFrame) -> pl.DataFrame:
    """
    Step 4: tag each segment with its named-arterial group where one
    exists (Section sql/001_named_arterial_registry.sql). Segments that
    don't match any curated pattern fall back to their own road name,
    normalized to strip a trailing compass-direction suffix
    (_normalize_road_base) so direction-suffix variants of the same
    physical road (see module docstring) collapse into one group instead
    of fragmenting.
    """
    aliases_query = """
        SELECT a.arterial_name, al.road_pattern
        FROM "Year_2025".named_arterial_road_aliases al
        JOIN "Year_2025".named_arterials a ON a.arterial_id = al.arterial_id
    """
    aliases = pl.read_database_uri(
        query=aliases_query, uri=cbi_database.database_uri(), engine="connectorx"
    )

    def find_group(road: str) -> str:
        if road is None:
            return "UNKNOWN"
        road_upper = road.upper()
        for pattern, name in zip(aliases["road_pattern"].to_list(), aliases["arterial_name"].to_list()):
            if pattern.upper() in road_upper:
                return name
        return _normalize_road_base(road)

    groups = [find_group(r) for r in segments["road"].to_list()]
    return segments.with_columns(pl.Series("arterial_group", groups))


def _dominant_congestion_pattern(segments_with_pattern: pl.DataFrame) -> pl.DataFrame:
    """One row per arterial_group: its most common congestion_pattern among
    member segments (plain Python Counter, not polars .mode(), since mode-
    inside-group_by ties/behavior vary across polars versions and this is
    a small, one-time lookup)."""
    counts: dict[str, Counter] = {}
    for arterial_group, pattern in zip(
        segments_with_pattern["arterial_group"].to_list(),
        segments_with_pattern["congestion_pattern"].to_list(),
    ):
        if pattern is None:
            continue
        counts.setdefault(arterial_group, Counter())[pattern] += 1
    dominant = {
        group: counter.most_common(1)[0][0] if counter else "Unclassified"
        for group, counter in counts.items()
    }
    return pl.DataFrame({
        "arterial_group": list(dominant.keys()),
        "congestion_pattern": list(dominant.values()),
    })


def aggregate_to_road_level(segments: pl.DataFrame) -> pl.DataFrame:
    """
    Step 8: collapse to one row per named arterial/path (arterial_group) —
    the unit of analysis is the whole road (e.g. "Buford Hwy", "Roswell
    Rd", "Piedmont Ave"), not a single intersection. speed_drop_ratio
    (Intensity's complement) is averaged across the road's segments, AADT
    (Exposure) is the median, and eavhd_hours is SUMMED across every
    segment — eavhd_total, the road's total estimated annual vehicle-hours
    of delay (kept for display as "total regional impact"). worst_
    intersection (the segment with the lowest speed_drop_ratio, i.e. the
    biggest drop) is kept only for narrative/display context, not as part
    of the ranking.

    Recurrence and speed ratio are LENGTH-WEIGHTED averages of each
    segment's own already-computed value, not a plain unweighted mean —
    so a handful of very short TMC fragments don't carry the same
    influence as long ones:
        recurrence_weighted  = sum_i(miles_i * occurrence_pct_i) / sum_i(miles_i)
        speed_ratio_weighted = sum_i(miles_i * peak_drop_ratio_i) / sum_i(miles_i)
    occurrence_pct/speed_drop_ratio are set equal to these weighted values
    (recurrence_weighted/speed_ratio_weighted are also kept under their
    own names as explicit QC columns).

    eavhd_density = eavhd_total / road_miles is what actually drives
    ranking, NOT the raw total — the raw total scales directly with how
    many miles of road matched a named arterial, so a 30+ mile rural route
    would out-rank a short, severely-congested in-town corridor purely
    from accumulating delay over more total miles.

    Eligibility and QC (computed for every road, not just the ones that
    end up ranked — see rank_roads for where eligible_for_ranking is
    actually applied as a filter):
      - tmc_count: how many distinct TMCs support this road's numbers —
        a road built from 2 short fragments is a much weaker basis for a
        ranking claim than one built from 15.
      - max_tmc_eavhd_share = max_i(eavhd_hours_i) / eavhd_total — what
        fraction of the road's total delay comes from its single worst
        TMC. High values mean the "road-level" number is really describing
        one localized bottleneck, not a corridor-wide problem.
      - eligible_for_ranking = road_miles >= 0.5 AND (road_miles >= 1.0 OR
        tmc_count >= 4) — a road that is both very short AND thinly
        supported (few TMCs) is excluded from ranking entirely (see
        rank_roads), rather than just flagged, since its density figure is
        not a meaningfully comparable "corridor" measurement. Confirmed
        directly: GA-205 (0.54 mi, 2 TMCs, one of which supplies 82.6% of
        its own EAVHD) fails this test and should NOT rank; GA-11 (8.06
        mi, 15 TMCs, worst single TMC only 16.7% of its own EAVHD) passes
        comfortably.
      - qc_high_concentration = max_tmc_eavhd_share > 0.60 — a softer
        warning flag (not a filter): a road CAN be eligible for ranking
        and still have most of its delay concentrated in one segment,
        worth a second look before citing it as a corridor-wide problem.
      - qc_flag = tmc_count < 4 OR road_miles < 1.0 OR
        max_tmc_eavhd_share > 0.60 — a general "look closer before citing
        this row" flag, broader than eligibility alone.

    corridor_excess_travel_time_minutes estimates the average excess
    minutes a vehicle loses crossing the WHOLE analyzed road (not "at the
    worst intersection" — a reader could otherwise misread it that way):
    eavhd_total (vehicle-hours) divided by the estimated total annual
    (weekday) vehicle-trips through it — aadt * DIRECTIONAL_FACTOR *
    WEEKDAYS_PER_YEAR — converted to minutes. A road-level summary stat,
    not part of the ranking, and intentionally kept out of the headline
    table (see the page generator) since it scales with road length in a
    way that invites exactly that misreading for long multi-segment roads.

    The old (binary-threshold) queue-mile-hours severity_index is kept as
    an experimental comparison column — annual_queue_mile_hours is SUMMED
    the same way eavhd_total is, and severity_index uses the same
    per-mile-density treatment EAVHD does, for a fair side-by-side — but
    neither drives ranking here (see rank_roads).
    """
    weighted_segments = segments.with_columns(
        (pl.col("miles") * pl.col("occurrence_pct")).alias("weighted_occurrence"),
        (pl.col("miles") * pl.col("peak_drop_ratio")).alias("weighted_drop_ratio"),
    ).sort("peak_drop_ratio")

    grouped = (
        weighted_segments.group_by("arterial_group", maintain_order=True)
        .agg(
            pl.col("weighted_drop_ratio").sum().alias("weighted_drop_ratio_sum"),
            pl.col("weighted_occurrence").sum().alias("weighted_occurrence_sum"),
            pl.col("aadt").median().alias("aadt"),
            pl.col("eavhd_hours").sum().alias("eavhd_total"),
            pl.col("eavhd_hours").max().alias("max_tmc_eavhd"),
            pl.col("eavhd_hours_alt_flat").sum().alias("eavhd_total_alt_flat"),
            pl.col("eavhd_hours_alt_uniform").sum().alias("eavhd_total_alt_uniform"),
            pl.col("eavhd_hours_alt_commuter").sum().alias("eavhd_total_alt_commuter"),
            pl.col("annual_queue_mile_hours").sum().alias("annual_queue_mile_hours"),
            pl.col("miles").sum().alias("road_miles"),
            pl.col("tmc_code").n_unique().alias("tmc_count"),
            pl.col("county").first().alias("county"),
            pl.col("intersection").first().alias("worst_intersection"),
        )
        .filter(
            pl.col("aadt").is_not_null() & (pl.col("aadt") > 0)
            & pl.col("road_miles").is_not_null() & (pl.col("road_miles") > 0)
        )
    )
    grouped = grouped.with_columns(
        (pl.col("weighted_occurrence_sum") / pl.col("road_miles")).alias("recurrence_weighted"),
        (pl.col("weighted_drop_ratio_sum") / pl.col("road_miles")).alias("speed_ratio_weighted"),
        (pl.col("eavhd_total") / pl.col("road_miles")).alias("eavhd_density"),
        (pl.col("eavhd_total_alt_flat") / pl.col("road_miles")).alias("eavhd_density_alt_flat"),
        (pl.col("eavhd_total_alt_uniform") / pl.col("road_miles")).alias("eavhd_density_alt_uniform"),
        (pl.col("eavhd_total_alt_commuter") / pl.col("road_miles")).alias("eavhd_density_alt_commuter"),
        (pl.col("max_tmc_eavhd") / pl.col("eavhd_total")).alias("max_tmc_eavhd_share"),
        (
            60.0 * pl.col("eavhd_total")
            / (pl.col("aadt") * DIRECTIONAL_FACTOR * WEEKDAYS_PER_YEAR)
        ).alias("corridor_excess_travel_time_minutes"),
        (pl.col("annual_queue_mile_hours") / pl.col("road_miles")).alias("queue_mile_hours_per_mile"),
    )
    grouped = grouped.with_columns(
        pl.col("recurrence_weighted").alias("occurrence_pct"),
        pl.col("speed_ratio_weighted").alias("speed_drop_ratio"),
        (pl.col("max_tmc_eavhd_share") > 0.60).alias("qc_high_concentration"),
        (
            (pl.col("road_miles") >= MIN_ROAD_MILES)
            & ((pl.col("road_miles") >= 1.0) | (pl.col("tmc_count") >= 4))
        ).alias("eligible_for_ranking"),
    )
    grouped = grouped.with_columns(
        (
            (pl.col("tmc_count") < 4)
            | (pl.col("road_miles") < 1.0)
            | pl.col("qc_high_concentration")
        ).alias("qc_flag"),
        (
            pl.col("queue_mile_hours_per_mile")
            * (pl.col("occurrence_pct") / 100.0)
            * (1.0 - pl.col("speed_drop_ratio"))
            * (pl.col("aadt") / 100_000.0)
        ).alias("severity_index"),
    )

    if "congestion_pattern" in segments.columns:
        pattern_lookup = _dominant_congestion_pattern(segments.select(["arterial_group", "congestion_pattern"]))
        grouped = grouped.join(pattern_lookup, on="arterial_group", how="left").with_columns(
            pl.col("congestion_pattern").fill_null("Unclassified")
        )
    else:
        grouped = grouped.with_columns(pl.lit("Unclassified").alias("congestion_pattern"))

    return grouped


def compute_rank_stability(roads: pl.DataFrame) -> dict:
    """
    HDF sensitivity check: re-ranks all roads under 3 alternate HDF
    scenarios (flatter/uniform/synthetic-commuter — already summed into
    eavhd_density_alt_* by aggregate_to_road_level) and compares each to
    the primary EAVHD_density ranking via Spearman rank correlation (all
    roads) and top-N overlap. Per the reviewed guidance: if rho is in the
    0.95-0.99 range and most of the same roads stay in the top 20
    regardless of which plausible HDF scenario is used, that demonstrates
    "arterial rankings were stable under alternative reasonable hourly
    distributions" — a materially stronger argument than defending any one
    imported curve as correct. This is a GLOBAL check across every road,
    not yet matched per-COP to its most-resembling candidate curve (see
    module docstring).
    """
    from scipy.stats import spearmanr

    roads = roads.filter(pl.col("eligible_for_ranking"))
    ordered = roads.sort("eavhd_density", descending=True)
    primary_rank = {g: i + 1 for i, g in enumerate(ordered["arterial_group"].to_list())}
    primary_top_n = set(ordered.head(TOP_N)["arterial_group"].to_list())

    results = {}
    for label, density_col in [
        ("flatter", "eavhd_density_alt_flat"),
        ("uniform", "eavhd_density_alt_uniform"),
        ("synthetic commuter-peaked", "eavhd_density_alt_commuter"),
    ]:
        alt_ordered = roads.sort(density_col, descending=True)
        alt_rank = {g: i + 1 for i, g in enumerate(alt_ordered["arterial_group"].to_list())}
        groups = list(primary_rank.keys())
        corr, _ = spearmanr([primary_rank[g] for g in groups], [alt_rank[g] for g in groups])
        alt_top_n = set(alt_ordered.head(TOP_N)["arterial_group"].to_list())
        results[label] = {
            "spearman": round(float(corr), 3),
            "top_n_overlap": len(primary_top_n & alt_top_n),
        }
    return results


def rank_roads(roads: pl.DataFrame, top_n: int = TOP_N) -> pl.DataFrame:
    """
    Headline rank = EAVHD_density, worst (highest) first, among roads
    where eligible_for_ranking is True (see aggregate_to_road_level) — a
    plain numeric sort, not a K-means tier: EAVHD already has direct
    physical meaning (estimated vehicle-hours of delay per mile of road),
    so there is no need for a clustering step to make it interpretable the
    way the old abstract severity_index needed one. This ranks by DELAY
    DENSITY (congestion concentration), not by total regional burden — see
    the module docstring and the page's title/methodology for why those
    are different questions with potentially different answers. Ineligible
    roads (too short and too thinly supported by TMCs, e.g. GA-205) are
    excluded here, not merely flagged — their density figure is not a
    meaningfully comparable "corridor" measurement.
    """
    eligible = roads.filter(pl.col("eligible_for_ranking"))
    return eligible.sort("eavhd_density", descending=True).head(top_n).with_row_index("rank", offset=1)


def save_results(connection: psycopg.Connection, ranked: pl.DataFrame) -> None:
    with connection.cursor() as cursor:
        cursor.execute('DROP TABLE IF EXISTS "Year_2025".arterial_intersection_severity')
        cursor.execute(
            """
            CREATE TABLE "Year_2025".arterial_intersection_severity (
                rank integer,
                arterial_group text,
                worst_intersection text,
                county text,
                congestion_pattern text,
                speed_drop_ratio double precision,
                occurrence_pct double precision,
                recurrence_weighted double precision,
                speed_ratio_weighted double precision,
                aadt double precision,
                tmc_count integer,
                road_miles double precision,
                corridor_excess_travel_time_minutes double precision,
                eavhd_total double precision,
                eavhd_density double precision,
                max_tmc_eavhd_share double precision,
                qc_high_concentration boolean,
                qc_flag boolean,
                eligible_for_ranking boolean,
                annual_queue_mile_hours double precision,
                severity_index double precision,
                computed_at timestamptz DEFAULT now()
            )
            """
        )

        rows = [
            (
                int(row["rank"]), row["arterial_group"], row["worst_intersection"],
                row["county"], row["congestion_pattern"], float(row["speed_drop_ratio"]),
                float(row["occurrence_pct"]), float(row["recurrence_weighted"]),
                float(row["speed_ratio_weighted"]), float(row["aadt"]),
                int(row["tmc_count"]), float(row["road_miles"]),
                float(row["corridor_excess_travel_time_minutes"]),
                float(row["eavhd_total"]), float(row["eavhd_density"]),
                float(row["max_tmc_eavhd_share"]), bool(row["qc_high_concentration"]),
                bool(row["qc_flag"]), bool(row["eligible_for_ranking"]),
                float(row["annual_queue_mile_hours"]), float(row["severity_index"]),
            )
            for row in ranked.iter_rows(named=True)
        ]

        cursor.executemany(
            """
            INSERT INTO "Year_2025".arterial_intersection_severity (
                rank, arterial_group, worst_intersection, county, congestion_pattern,
                speed_drop_ratio, occurrence_pct, recurrence_weighted, speed_ratio_weighted,
                aadt, tmc_count, road_miles, corridor_excess_travel_time_minutes,
                eavhd_total, eavhd_density, max_tmc_eavhd_share, qc_high_concentration,
                qc_flag, eligible_for_ranking, annual_queue_mile_hours, severity_index
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )
    connection.commit()


def save_stability(connection: psycopg.Connection, stability: dict) -> None:
    """Persists compute_rank_stability's summary so the page generator can
    render real numbers rather than requiring a look at console output."""
    with connection.cursor() as cursor:
        cursor.execute('DROP TABLE IF EXISTS "Year_2025".arterial_severity_rank_stability')
        cursor.execute(
            """
            CREATE TABLE "Year_2025".arterial_severity_rank_stability (
                hdf_variant text,
                spearman_correlation double precision,
                top_n_overlap integer,
                top_n integer,
                computed_at timestamptz DEFAULT now()
            )
            """
        )
        cursor.executemany(
            """
            INSERT INTO "Year_2025".arterial_severity_rank_stability
                (hdf_variant, spearman_correlation, top_n_overlap, top_n)
            VALUES (%s, %s, %s, %s)
            """,
            [
                (label, result["spearman"], result["top_n_overlap"], TOP_N)
                for label, result in stability.items()
            ],
        )
    connection.commit()


def attach_tmc_geometry(connection: psycopg.Connection, segments: pl.DataFrame) -> pl.DataFrame:
    """Real per-TMC endpoint coordinates + direction, for the map (see
    save_map_segments) — reuses the same real-road-curve technique as the
    main multi-corridor report: cbi_map_geometry.load_tmc_polylines()
    (decoded from the HERE-derived shapefile topology) traces the actual
    curve when available, falling back to a straight line between these
    endpoints otherwise (cbi_map_geometry.resolve_points)."""
    tmc_list = segments["tmc_code"].to_list()
    if not tmc_list:
        return segments
    tmc_in = ",".join(f"'{t}'" for t in tmc_list)
    query = f"""
        SELECT tmc, direction, start_latitude, start_longitude, end_latitude, end_longitude
        FROM "Year_2025".tmc_metadata
        WHERE tmc IN ({tmc_in})
    """
    geometry = pl.read_database_uri(
        query=query, uri=cbi_database.database_uri(), engine="connectorx"
    ).rename({"tmc": "tmc_code"})
    return segments.join(geometry, on="tmc_code", how="left")


def save_map_segments(connection: psycopg.Connection, segments: pl.DataFrame, ranked: pl.DataFrame) -> None:
    """
    Persists one row per (ranked road, member TMC) with real map geometry,
    for the page generator's map — restricted to the TMCs of the TOP_N
    roads actually shown (not every eligible road), same as the main
    report's bottleneck map only drawing what's displayed. Severity color
    is a SEPARATE, display-only K-means fit (cbi_map_geometry.
    kmeans_severity_classes) on just these ranked roads' eavhd_density —
    fitting on the full ~100-road population instead would put nearly all
    of the displayed top 20 in a single "most severe" cluster, the same
    selection-vs-symbology issue documented elsewhere in this project — so
    every member TMC of a road is colored by that road's OWN tier among
    just the displayed roads, not the region-wide distribution.
    """
    ranks, legend = cbi_map_geometry.kmeans_severity_classes(ranked["eavhd_density"].to_list())
    tier_by_group = dict(zip(ranked["arterial_group"].to_list(), ranks))
    rank_by_group = dict(zip(ranked["arterial_group"].to_list(), ranked["rank"].to_list()))

    displayed_groups = set(ranked["arterial_group"].to_list())
    map_segments = segments.filter(pl.col("arterial_group").is_in(displayed_groups))

    with connection.cursor() as cursor:
        cursor.execute('DROP TABLE IF EXISTS "Year_2025".arterial_intersection_severity_segments')
        cursor.execute(
            """
            CREATE TABLE "Year_2025".arterial_intersection_severity_segments (
                arterial_group text,
                road_rank integer,
                severity_tier integer,
                tmc_code text,
                direction text,
                start_latitude double precision,
                start_longitude double precision,
                end_latitude double precision,
                end_longitude double precision,
                miles double precision,
                eavhd_hours double precision,
                computed_at timestamptz DEFAULT now()
            )
            """
        )

        rows = [
            (
                row["arterial_group"], rank_by_group[row["arterial_group"]],
                tier_by_group[row["arterial_group"]], row["tmc_code"], row["direction"],
                row["start_latitude"], row["start_longitude"],
                row["end_latitude"], row["end_longitude"],
                float(row["miles"]), float(row["eavhd_hours"]),
            )
            for row in map_segments.iter_rows(named=True)
            if row["start_latitude"] is not None and row["end_latitude"] is not None
        ]

        cursor.executemany(
            """
            INSERT INTO "Year_2025".arterial_intersection_severity_segments (
                arterial_group, road_rank, severity_tier, tmc_code, direction,
                start_latitude, start_longitude, end_latitude, end_longitude,
                miles, eavhd_hours
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )
    connection.commit()
    print(f"  {len(rows)} map segments saved across {len(displayed_groups)} roads, {len(legend)} severity tiers")


def run_analysis(connection: psycopg.Connection) -> pl.DataFrame:
    print("Step 1: system-wide hourly speed...")
    system_hourly = compute_system_wide_hourly_speed(connection)

    print("Step 2: arterial-wide hourly speed (free-flow base)...")
    arterial_hourly = compute_arterial_hourly_speed(connection)

    print("Step 3a: identifying free-flow hour(s)...")
    free_flow = identify_free_flow_hours(arterial_hourly)
    print(f"  Free-flow hour(s): {free_flow.free_flow_hours}")

    print("Step 3b: per-segment hourly speed...")
    segment_hourly = compute_segment_hourly_speed(connection)

    print("Step 3c: per-segment peak-hour drop ratios...")
    segments = compute_segment_drop_ratios(segment_hourly, free_flow)

    print("Attaching occurrence (Recurrence)...")
    segments = attach_occurrence(connection, segments)

    print("Building normalized 24-hour speed profiles (Congestion Operating Patterns)...")
    profiles = compute_hourly_profiles(segment_hourly, segments)
    cop_features = build_cop_features(profiles, segments)
    pattern_labels, cop_diagnostics = cluster_congestion_patterns(cop_features)
    print(
        f"  chosen: {cop_diagnostics['chosen_method']} k={cop_diagnostics['chosen_k']} "
        f"(silhouette={cop_diagnostics['chosen_silhouette']})"
    )
    for candidate in cop_diagnostics["all_candidates"]:
        print(f"    tried: {candidate[0]:<12} k={candidate[1]}  silhouette={candidate[2]}")
    for cluster_id, name in cop_diagnostics["cluster_names"].items():
        print(f"    cluster {cluster_id}: {name} ({cop_diagnostics['cluster_sizes'][cluster_id]} segments)")
    segments = segments.join(
        pattern_labels.select(["tmc_code", "congestion_pattern"]), on="tmc_code", how="left"
    )

    print("Step 5: computing per-hour excess travel time per segment...")
    hourly_excess = compute_excess_travel_time_by_hour(connection, segments)

    print("Steps 6-7: estimating EAVHD per segment (AADT x HDF x D)...")
    segments = compute_segment_eavhd(segments, hourly_excess, HOURLY_DISTRIBUTION_FACTOR, "eavhd_hours")
    print("  ...and under 3 alternate HDF scenarios, for the sensitivity check below...")
    segments = compute_segment_eavhd(segments, hourly_excess, HDF_ALTERNATE_FLATTER, "eavhd_hours_alt_flat")
    segments = compute_segment_eavhd(segments, hourly_excess, HDF_UNIFORM, "eavhd_hours_alt_uniform")
    segments = compute_segment_eavhd(segments, hourly_excess, HDF_SYNTHETIC_COMMUTER, "eavhd_hours_alt_commuter")

    print("Computing legacy queue mile-hours (experimental comparison column)...")
    segments = compute_queue_mile_hours(connection, segments)

    print("Step 4: attaching named-arterial groups...")
    segments = attach_named_arterial_groups(connection, segments)

    print("Attaching TMC map geometry...")
    segments = attach_tmc_geometry(connection, segments)

    print("Step 8: aggregating to road/path level...")
    roads = aggregate_to_road_level(segments)
    n_eligible = int(roads["eligible_for_ranking"].sum())
    n_flagged = int(roads["qc_flag"].sum())
    print(
        f"  {roads.height} named arterials/roads -- {n_eligible} eligible for ranking, "
        f"{roads.height - n_eligible} excluded (too short and too thinly supported), "
        f"{n_flagged} carry a qc_flag"
    )
    for road in ("GA-11", "GA-205"):
        match = roads.filter(pl.col("arterial_group") == road)
        if match.height:
            r = match.row(0, named=True)
            print(
                f"    {road}: miles={r['road_miles']:.2f} tmc_count={r['tmc_count']} "
                f"max_tmc_eavhd_share={r['max_tmc_eavhd_share']:.1%} "
                f"eligible_for_ranking={r['eligible_for_ranking']} qc_flag={r['qc_flag']}"
            )

    print("Checking EAVHD rank stability under alternate HDF scenarios (eligible roads only)...")
    stability = compute_rank_stability(roads)
    for label, result in stability.items():
        print(
            f"  vs. {label}: Spearman rank correlation={result['spearman']}, "
            f"top-{TOP_N} overlap={result['top_n_overlap']}/{TOP_N}"
        )

    print("Ranking by EAVHD density (eligible roads only)...")
    ranked = rank_roads(roads)

    print("Saving results...")
    save_results(connection, ranked)
    save_stability(connection, stability)
    print("Saving map segments...")
    save_map_segments(connection, segments, ranked)

    print(f"\nTop {TOP_N} arterial roads by Estimated Annual Vehicle-Hours of Delay per mile (EAVHD density):")
    for row in ranked.select([
        "rank", "arterial_group", "county", "congestion_pattern", "speed_drop_ratio",
        "occurrence_pct", "aadt", "road_miles", "tmc_count", "max_tmc_eavhd_share",
        "qc_high_concentration", "eavhd_total", "eavhd_density", "severity_index",
    ]).iter_rows(named=True):
        # Plain row-by-row print (not the DataFrame repr, which uses Unicode
        # box-drawing characters that crash on a cp1252 Windows console).
        qc_note = " [QC: high concentration]" if row["qc_high_concentration"] else ""
        print(
            f"  #{row['rank']:>2}  {row['arterial_group']:<28} {row['county'] or '':<10} "
            f"[{row['congestion_pattern']}] "
            f"drop={row['speed_drop_ratio']:.2f} occ={row['occurrence_pct']:.1f}% "
            f"aadt={row['aadt']:,.0f} miles={row['road_miles']:.1f} tmc_count={row['tmc_count']} "
            f"max_tmc_share={row['max_tmc_eavhd_share']:.1%} "
            f"EAVHD_total={row['eavhd_total']:,.0f} veh-hrs "
            f"EAVHD_density={row['eavhd_density']:,.0f} veh-hrs/mi "
            f"(old severity={row['severity_index']:,.1f}){qc_note}"
        )

    return ranked


if __name__ == "__main__":
    with psycopg.connect(**cbi_database.connection_kwargs()) as conn:
        run_analysis(conn)
