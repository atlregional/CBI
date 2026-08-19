"""
Builds one self-contained regional HTML report covering every corridor the
multi-corridor pipeline has analyzed (Interstates + Expressways + Arterials),
plus the Collector/Local watch list from cbi_watch_segments.py.

Tabs:
  - General:   region-wide KPIs (highest month / worst weekday / worst hour)
               and month / weekday / hour / county breakdowns, sourced from
               the vw_region_* views in sql/004_extended_road_classes.sql.
  - Region Map: every classified road segment in the network, line width by
               functional road class (Interstate thickest -> Local
               thinnest), color by congestion severity (occurrence_pct,
               sequential blue ramp) where analyzed, gray otherwise.
  - Corridors: one ranked-bottleneck table + severity chart + mini-map per
               active corridor, reusing the same queries as the per-corridor
               Word report (cbi_generate_corridor_report.py).
  - Watch Segments: the top Collector/Local TMCs and their congestion
               summary (cbi_watch_segments.compute_watch_segment_metrics).

Colors follow the CBI dataviz reference palette: sequential blue ramp for
magnitude/severity, slot-1 blue for single-series bars, status red only to
flag the single worst bucket in a KPI chart.
"""

from __future__ import annotations

import base64
import html
import json
import math
from datetime import date
from pathlib import Path
from urllib.parse import quote

import plotly.graph_objects as go
import polars as pl
import psycopg

import cbi_database
import cbi_map_geometry
from cbi_corridor_registry import CorridorContext, get_active_corridors
from cbi_generate_corridor_report import _load_corridor_summary

COUNTY_BOUNDARIES_PATH = Path(__file__).resolve().parent.parent / "data" / "ga_region_counties.geojson"
AGENCY_LOGO_PATH = Path(__file__).resolve().parent.parent / "Logo" / "ARC_logo_PMS7549.png"

# --- Palette (dataviz skill reference/palette.md) --------------------------
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
SURFACE = "#fcfcfb"
PAGE = "#f9f9f7"
SERIES_BLUE = "#2a78d6"
STATUS_CRITICAL = "#d03b3b"
NAVY = "#1F3864"
NO_DATA_GRAY = "#c3c2b7"

# Sequential blue ramp, 5 bins low -> high severity (occurrence_pct 0-100%)
# — still used for the Collector/Local watch-segment layer and per-corridor
# mini-maps, which weren't part of this ask.
SEVERITY_BINS = [0, 20, 40, 60, 80, 100.01]
SEVERITY_COLORS = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#0d366b"]
SEVERITY_LABELS = ["0-20%", "20-40%", "40-60%", "60-80%", "80-100%"]

# Google Maps traffic-layer ramp (light orange -> amber -> orange -> red ->
# dark maroon), K-means clustered on severity, low -> high — see
# cbi_map_geometry.py (shared with the corridor Word reports' static maps,
# so a color means the same severity everywhere in this report, not a
# locally-rescaled one).
BOTTLENECK_SEVERITY_COLORS = cbi_map_geometry.BOTTLENECK_SEVERITY_COLORS

ROAD_CLASS_WIDTH = cbi_map_geometry.ROAD_CLASS_WIDTH
WEEKDAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Paused, not removed: the underlying data/table (_load_watch_segments,
# _watch_table) still builds normally, just isn't linked into the nav or
# rendered into the page — flip back to True to bring the tab back.
SHOW_WATCH_SEGMENTS_TAB = False

# Re-enabled now that the link points at the agency's internal SharePoint
# (see SHAREPOINT_CORRIDOR_REPORTS_BASE_URL below) instead of a public
# relative path — flip back to False to hide it again without deleting
# _find_corridor_docx() or the button's HTML/CSS.
SHOW_DOWNLOAD_REPORT_BUTTON = True

# Corridor Word reports are kept on the agency's internal OneDrive/
# SharePoint (not the public GitHub Pages deployment), so only ARC staff
# with SharePoint access can open them. This is the predictable, path-
# based portion of a SharePoint "personal" (OneDrive for Business) file
# URL — confirmed against two real, working links the user copied
# directly from SharePoint (for i-285-co and i-75-no): both shared this
# exact prefix and only differed after it by the {slug}/{filename} path,
# which matches the same relative layout used locally. Real SharePoint
# "Copy Link" URLs also carry a per-file ?d=...&csf=1&web=1&e=... suffix
# (a share-link ID SharePoint generates per file) that can't be
# reconstructed this way — see scripts/cbi_get_sharepoint_links.ps1 for a
# way to capture those real links via Microsoft Graph if this simpler
# path-based URL turns out not to open correctly.
SHAREPOINT_CORRIDOR_REPORTS_BASE_URL = (
    "https://atlantaregional-my.sharepoint.com/:w:/r/personal/"
    "ssameti_atlantaregional_org/Documents/Desktop/CBI/outputs/multi_corridor"
)


def _fmt2(value) -> str:
    """Every number displayed in this report is rounded to 2 decimals,
    regardless of the precision stored upstream."""
    if value is None:
        return "-"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def _agency_logo_data_uri() -> str | None:
    """Base64 data URI for the masthead logo, so the report stays a single
    portable HTML file with no external image reference."""
    if not AGENCY_LOGO_PATH.exists():
        return None
    encoded = base64.b64encode(AGENCY_LOGO_PATH.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


# ---------------------------------------------------------------------------
# Region-wide aggregate loaders
# ---------------------------------------------------------------------------
def _load_region_monthly() -> pl.DataFrame:
    return pl.read_database_uri(
        query='SELECT * FROM "Year_2025".vw_region_monthly_dashboard ORDER BY month',
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )


def _load_region_weekday() -> pl.DataFrame:
    return pl.read_database_uri(
        query='SELECT * FROM "Year_2025".vw_region_weekday_dashboard ORDER BY weekday_number',
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )


def _load_region_hourly() -> pl.DataFrame:
    return pl.read_database_uri(
        query='SELECT * FROM "Year_2025".vw_region_hourly_dashboard ORDER BY hour_of_day',
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )


def _load_region_county() -> pl.DataFrame:
    return pl.read_database_uri(
        query="""
            SELECT * FROM "Year_2025".vw_region_county_dashboard
            WHERE county <> 'UNKNOWN'
            ORDER BY total_mile_hours DESC
        """,
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )


# FHWA functional system code -> the road-class buckets shown in the
# Average Speed by Hour of Day chart (matches ROAD_CLASS_WIDTH above and
# the f_system groupings already established in sql/003 and sql/004:
# Interstate=1, Expressway=2, Arterial=3/4, Collector=5/6, Local=7). Moved
# here from cbi_intersection_congestion_severity.py, which still imports
# _load_hourly_baselines from this module for its own peak/free-flow-hour
# methodology, but no longer renders this chart itself.
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
    """System-wide, per-road-class, and (within that) Arterial-only average
    speed by hour of day, region-wide, plus the free-flow/peak hour(s)
    derived from the Arterial-only curve (highest avg speed hour = free-
    flow, lowest = peak) — cbi_intersection_congestion_severity.py's own
    methodology uses those two hours as its fixed region-wide reference.
    Two queries: the system-wide curve is a plain scan (~15s); the
    per-class curve joins every probe reading to its TMC's DIRECT (not
    tmclinear-backfilled) functional class in one pass (~15-30s) rather
    than running five separate filtered scans.

    Deliberately NOT backfilled here: a real disparity report showed the
    Local curve reading suspiciously close to Arterial — backfilling
    functional class via MAX(f_system) OVER tmclinear picks the highest-
    numbered class present ANYWHERE in a tmclinear group, so a group
    spanning both an Arterial primary segment and an unrelated Local-
    classified sibling pushes every NULL-f_system member in that group to
    "Local" (confirmed directly: this inflated the Local bucket from 54
    directly-classified TMCs to 234 backfilled ones). For classifying WHAT
    a road IS, only genuinely, directly classified TMCs should count."""
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


def _hourly_speed_by_class_figure(baselines: dict) -> go.Figure:
    """Average Speed by Hour of Day, full report width, one line per road
    class plus the overall system-wide curve — moved here from the
    Intersection Congestion Severity standalone page so it's part of the
    main Regional Overview tab instead."""
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
        height=480, width=1180, margin=dict(l=60, r=160, t=50, b=40),
        legend=dict(orientation="v", x=1.01, y=1, xanchor="left"),
        xaxis=dict(title="Hour of day", dtick=2, gridcolor=GRID, linecolor=GRID),
        yaxis=dict(title="Average speed (mph)", gridcolor=GRID, linecolor=GRID),
    )
    return fig


def _load_worst_day() -> dict:
    """The single calendar date with the highest region-wide congestion
    (same event_area_mile_hours measure as the month/weekday/hour/county
    charts above, just for one specific date instead of an aggregate), plus
    a snapshot of whichever bottleneck contributed the most that day: its
    peak-segment AADT and annual AADT-weighted severity index (both the
    same figures used everywhere else in this report, for consistency —
    not day-specific versions of those two), and how many hours of active
    congestion it produced on that specific date. Returns a dict with
    analysis_date=None if congestion_events has no rows at all."""
    df = pl.read_database_uri(
        query="""
            WITH daily_totals AS (
                SELECT e.analysis_date, SUM(e.event_area_mile_hours) AS total_mile_hours
                FROM "Year_2025".congestion_events AS e
                JOIN "Year_2025".corridor_definitions AS d
                  ON d.road = e.corridor AND d.direction = e.direction
                WHERE d.is_active
                GROUP BY e.analysis_date
            ),
            worst AS (
                SELECT analysis_date, total_mile_hours
                FROM daily_totals ORDER BY total_mile_hours DESC LIMIT 1
            ),
            worst_bottleneck AS (
                SELECT
                    bdm.bottleneck_id, bdm.queue_mile_hours AS day_queue_mile_hours,
                    bdm.active_congestion_minutes,
                    r.corridor, r.direction, r.representative_intersection,
                    r.peak_segment_aadt, r.aadt_weighted_severity_index, r.peak_segment_order
                FROM "Year_2025".bottleneck_daily_metrics AS bdm
                JOIN "Year_2025".vw_bottleneck_dashboard_ranked AS r ON r.bottleneck_id = bdm.bottleneck_id
                WHERE bdm.analysis_date = (SELECT analysis_date FROM worst)
                  AND bdm.occurrence AND r.ranking_eligible
                ORDER BY bdm.queue_mile_hours DESC
                LIMIT 1
            ),
            worst_bottleneck_ctx AS (
                SELECT wb.*, cl.corridor_id, cl.corridor_name, t.county, tc.cid_name
                FROM worst_bottleneck AS wb
                JOIN "Year_2025".corridor_definitions AS cl
                  ON cl.road = wb.corridor AND cl.direction = wb.direction AND cl.is_active
                JOIN "Year_2025".corridor_segments AS cs
                  ON cs.corridor_id = cl.corridor_id AND cs.segment_order = wb.peak_segment_order
                JOIN "Year_2025".tmc_metadata AS t ON t.tmc = cs.tmc
                LEFT JOIN "Year_2025".tmc_cid AS tc ON tc.tmc = cs.tmc
            )
            SELECT w.analysis_date, w.total_mile_hours, wc.*
            FROM worst AS w
            LEFT JOIN worst_bottleneck_ctx AS wc ON true
        """,
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )
    if df.is_empty():
        return {"analysis_date": None}
    return df.row(0, named=True)


def _load_worst_day_active_bottleneck_ids(worst_date) -> set[int]:
    """Every ranking-eligible bottleneck that actually occurred on the
    region's single worst day — used to spotlight just those segments on
    the Worst Day map, filtered from the SAME already-K-means-classified
    region-wide record set the Region Map tab uses (see
    cbi_map_geometry.bottleneck_records_and_legend), not a fresh fit on
    just this one day's handful of bottlenecks."""
    with psycopg.connect(**cbi_database.connection_kwargs()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT bdm.bottleneck_id
                FROM "Year_2025".bottleneck_daily_metrics AS bdm
                JOIN "Year_2025".vw_bottleneck_dashboard_ranked AS r ON r.bottleneck_id = bdm.bottleneck_id
                WHERE bdm.analysis_date = %s AND bdm.occurrence AND r.ranking_eligible
                """,
                (worst_date,),
            )
            return {row[0] for row in cursor.fetchall()}


def _load_worst_day_hourly(worst_date) -> pl.DataFrame:
    """Hour-of-day congestion profile for one specific date, using the same
    proportional-overlap slicing as vw_region_hourly_dashboard (see
    sql/004_extended_road_classes.sql) — that view aggregates across every
    analyzed date, so it can't answer "what did this one day look like,"
    which is what the Worst Day section needs."""
    return pl.read_database_uri(
        query=f"""
            WITH hour_slices AS (
                SELECT
                    e.event_id, gs.hour_start,
                    EXTRACT(EPOCH FROM (
                        LEAST(e.end_time, gs.hour_start + interval '1 hour') - GREATEST(e.start_time, gs.hour_start)
                    )) / 60.0 AS overlap_minutes,
                    e.duration_minutes, e.event_area_mile_hours
                FROM "Year_2025".congestion_events AS e
                JOIN "Year_2025".corridor_definitions AS d
                  ON d.road = e.corridor AND d.direction = e.direction
                CROSS JOIN LATERAL generate_series(
                    date_trunc('hour', e.start_time), date_trunc('hour', e.end_time), interval '1 hour'
                ) AS gs(hour_start)
                WHERE d.is_active AND e.analysis_date = '{worst_date.isoformat()}'::date
            )
            SELECT
                EXTRACT(HOUR FROM hour_start)::integer AS hour_of_day,
                ROUND(SUM(event_area_mile_hours * overlap_minutes / NULLIF(duration_minutes, 0))::numeric, 2)
                    AS total_mile_hours
            FROM hour_slices
            WHERE overlap_minutes > 0
            GROUP BY EXTRACT(HOUR FROM hour_start)
            ORDER BY hour_of_day
        """,
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )


def _load_metadata_counts(connection: psycopg.Connection) -> dict:
    """Record counts describing the scope of data behind this report —
    what was read from the source tables versus what fed into the analysis
    that produced the charts and tables in the other tabs."""
    with connection.cursor() as cursor:
        cursor.execute('SELECT COUNT(*) FROM "Year_2025".tmc_metadata')
        (tmc_total,) = cursor.fetchone()

        cursor.execute(
            'SELECT COUNT(*), MIN(measurement_tstamp), MAX(measurement_tstamp) '
            'FROM "Year_2025".probe_readings'
        )
        probe_total, probe_min, probe_max = cursor.fetchone()

        cursor.execute(
            'SELECT COUNT(*) FROM "Year_2025".corridor_segments c '
            'JOIN "Year_2025".probe_readings r ON r.tmc_code = c.tmc'
        )
        (probe_corridor,) = cursor.fetchone()

        cursor.execute(
            'SELECT COUNT(*) FROM "Year_2025".watch_segments w '
            'JOIN "Year_2025".probe_readings r ON r.tmc_code = w.tmc'
        )
        (probe_watch,) = cursor.fetchone()

        cursor.execute(
            'SELECT COUNT(*) FROM "Year_2025".congestion_events e '
            'JOIN "Year_2025".corridor_definitions d '
            '  ON d.road = e.corridor AND d.direction = e.direction '
            'WHERE d.is_active'
        )
        (events_total,) = cursor.fetchone()

        cursor.execute('SELECT COUNT(*) FROM "Year_2025".corridor_segments')
        (segments_total,) = cursor.fetchone()

        cursor.execute(
            'SELECT corridor_group, COUNT(*) FROM "Year_2025".corridor_definitions '
            'WHERE is_active GROUP BY corridor_group'
        )
        corridors_by_group = dict(cursor.fetchall())

        cursor.execute(
            'SELECT road_class, COUNT(*) FROM "Year_2025".watch_segments GROUP BY road_class'
        )
        watch_by_class = dict(cursor.fetchall())

        cursor.execute('SELECT COUNT(*) FROM "Year_2025".segment_recurring_bottlenecks')
        (bottlenecks_total,) = cursor.fetchone()

        cursor.execute('SELECT COUNT(*) FROM "Year_2025".bottleneck_daily_metrics')
        (daily_metrics_total,) = cursor.fetchone()

        cursor.execute(
            'SELECT MIN(analysis_date), MAX(analysis_date), COUNT(DISTINCT analysis_date) '
            'FROM "Year_2025".congestion_events'
        )
        date_min, date_max, date_count = cursor.fetchone()

    return {
        "tmc_total": tmc_total,
        "probe_total": probe_total,
        "probe_min": probe_min,
        "probe_max": probe_max,
        "probe_analyzed": probe_corridor + probe_watch,
        "events_total": events_total,
        "segments_total": segments_total,
        "corridors_by_group": corridors_by_group,
        "corridors_total": sum(corridors_by_group.values()),
        "watch_by_class": watch_by_class,
        "watch_total": sum(watch_by_class.values()),
        "bottlenecks_total": bottlenecks_total,
        "daily_metrics_total": daily_metrics_total,
        "date_min": date_min,
        "date_max": date_max,
        "date_count": date_count,
    }


def _load_watch_segments() -> pl.DataFrame:
    """Each watch segment is a single TMC probe segment, not a multi-segment
    corridor — 'from' is its own reference intersection; 'to' is looked up
    from the next TMC along the same road/direction (by road_order), the
    same convention corridor extents use elsewhere in this report."""
    return pl.read_database_uri(
        query="""
            SELECT w.road_class, w.rank_in_class, w.road, w.direction, w.tmc,
                   w.intersection AS from_intersection, nxt.intersection AS to_intersection,
                   w.miles, w.county, c.cid_name, w.aadt,
                   m.analyzed_days, m.occurrence_pct,
                   m.annual_segment_mile_hours, m.average_congested_speed_ratio,
                   m.peak_hour_label, m.peak_weekday_name
            FROM "Year_2025".watch_segments AS w
            JOIN "Year_2025".tmc_metadata AS wt ON wt.tmc = w.tmc
            LEFT JOIN "Year_2025".tmc_cid AS c ON c.tmc = w.tmc
            LEFT JOIN LATERAL (
                SELECT t2.intersection
                FROM "Year_2025".tmc_metadata AS t2
                WHERE t2.road = w.road AND t2.direction = w.direction
                  AND t2.road_order > wt.road_order
                ORDER BY t2.road_order
                LIMIT 1
            ) AS nxt ON true
            LEFT JOIN "Year_2025".watch_segment_metrics AS m ON m.tmc = w.tmc
            ORDER BY w.road_class, w.rank_in_class
        """,
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )


def _load_corridor_extent(corridor: CorridorContext) -> tuple[str | None, str | None]:
    """(from_intersection, to_intersection) — the first and last corridor
    segment's reference intersection, i.e. the corridor's real-world extent
    (e.g. Downtown Connector Northbound: I-85 Split/Exit 242 to Techwood
    Dr/Exit 103), same convention as the watch-segment from/to lookup."""
    df = pl.read_database_uri(
        query=f"""
            SELECT intersection
            FROM "Year_2025".corridor_segments
            WHERE corridor_id = {corridor.corridor_id}
              AND segment_order IN (
                  (SELECT MIN(segment_order) FROM "Year_2025".corridor_segments WHERE corridor_id = {corridor.corridor_id}),
                  (SELECT MAX(segment_order) FROM "Year_2025".corridor_segments WHERE corridor_id = {corridor.corridor_id})
              )
            ORDER BY segment_order
        """,
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )
    if df.height == 0:
        return None, None
    values = df["intersection"].to_list()
    return values[0], values[-1]


def _load_segment_occurrence_profile(corridor: CorridorContext) -> pl.DataFrame:
    """Raw per-segment occurrence profile — the signal the peak-detection
    algorithm in cbi_corridor_bottlenecks.py works from. Used as a fallback
    chart for corridors where that algorithm found no discrete bottleneck
    (e.g. a beltway that's congested fairly uniformly along its whole
    length has no single 'peak' standing out from its surroundings, even
    though the corridor clearly carries real, measurable congestion)."""
    return pl.read_database_uri(
        query=f"""
            SELECT segment_order, intersection, weekday_occurrence_pct
            FROM "Year_2025".segment_profile
            WHERE corridor_id = {corridor.corridor_id}
            ORDER BY segment_order
        """,
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )


def _load_regional_bottleneck_rankings() -> pl.DataFrame:
    """Every recurring bottleneck across every analyzed corridor, ranked
    region-wide by severity (vw_bottleneck_dashboard_ranked.network_severity_rank),
    for the Top Bottlenecks tab. Peak hour/day/county/AADT come from the
    bottleneck's single representative (peak) segment rather than its full
    span — computing it from every segment in every bottleneck's range
    region-wide took over a minute; the peak segment is representative and
    keeps this to a few seconds."""
    return pl.read_database_uri(
        query="""
            WITH ranked AS (
                SELECT bottleneck_id, network_severity_rank, corridor, direction,
                       representative_intersection, occurrence_pct, annual_queue_mile_hours,
                       avg_congested_speed_ratio, severity_index, aadt_weighted_severity_index,
                       peak_segment_order, analyzed_days
                FROM "Year_2025".vw_bottleneck_dashboard_ranked
                WHERE ranking_eligible
            ),
            corridor_lookup AS (
                SELECT corridor_id, road, direction, corridor_name, corridor_group
                FROM "Year_2025".corridor_definitions
                WHERE is_active
            ),
            peak_ctx AS (
                SELECT r.bottleneck_id, cs.tmc, t.county, t.aadt, tc.cid_name
                FROM ranked r
                JOIN corridor_lookup cl ON cl.road = r.corridor AND cl.direction = r.direction
                JOIN "Year_2025".corridor_segments cs
                  ON cs.corridor_id = cl.corridor_id AND cs.segment_order = r.peak_segment_order
                JOIN "Year_2025".tmc_metadata t ON t.tmc = cs.tmc
                LEFT JOIN "Year_2025".tmc_cid tc ON tc.tmc = cs.tmc
            ),
            congested AS (
                SELECT pc.bottleneck_id,
                       EXTRACT(HOUR FROM p.measurement_tstamp)::integer AS hour,
                       EXTRACT(ISODOW FROM p.measurement_tstamp)::integer AS dow
                FROM peak_ctx AS pc
                JOIN "Year_2025".probe_readings AS p ON p.tmc_code = pc.tmc
                WHERE p.speed IS NOT NULL AND p.reference_speed > 0
                  AND p.speed < 0.70 * p.reference_speed
            ),
            hour_ranked AS (
                SELECT bottleneck_id, hour,
                       ROW_NUMBER() OVER (PARTITION BY bottleneck_id ORDER BY COUNT(*) DESC) AS rn
                FROM congested GROUP BY bottleneck_id, hour
            ),
            dow_ranked AS (
                SELECT bottleneck_id, dow,
                       ROW_NUMBER() OVER (PARTITION BY bottleneck_id ORDER BY COUNT(*) DESC) AS rn
                FROM congested GROUP BY bottleneck_id, dow
            )
            SELECT
                r.network_severity_rank, cl.corridor_name, cl.corridor_group,
                r.corridor AS road, r.direction, r.representative_intersection,
                r.occurrence_pct, r.annual_queue_mile_hours, r.avg_congested_speed_ratio,
                r.severity_index, r.aadt_weighted_severity_index, r.analyzed_days,
                pc.county, pc.cid_name, pc.aadt, pc.tmc,
                TO_CHAR(make_time(h.hour, 0, 0), 'HH12:00 AM') AS peak_hour_label,
                (ARRAY['Monday', 'Tuesday', 'Wednesday', 'Thursday',
                       'Friday', 'Saturday', 'Sunday'])[d.dow] AS peak_weekday_name
            FROM ranked AS r
            JOIN corridor_lookup AS cl ON cl.road = r.corridor AND cl.direction = r.direction
            LEFT JOIN peak_ctx AS pc ON pc.bottleneck_id = r.bottleneck_id
            LEFT JOIN hour_ranked AS h ON h.bottleneck_id = r.bottleneck_id AND h.rn = 1
            LEFT JOIN dow_ranked AS d ON d.bottleneck_id = r.bottleneck_id AND d.rn = 1
            ORDER BY r.network_severity_rank
        """,
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )


def _load_ranked_bottlenecks_detailed(corridor: CorridorContext) -> pl.DataFrame:
    """Same ranked bottlenecks as the Word report's _load_ranked_bottlenecks,
    plus the extra context the watch-segments table already has: county and
    AADT (from the peak segment's TMC) and peak hour/day of week (computed
    from probe readings across the bottleneck's full segment span, same
    methodology as cbi_watch_segments.py)."""
    return pl.read_database_uri(
        query=f"""
            WITH ranked AS (
                SELECT bottleneck_id, corridor_severity_rank AS severity_rank,
                       representative_intersection, occurrence_pct,
                       annual_queue_mile_hours, avg_congested_speed_ratio,
                       severity_index, peak_segment_order,
                       start_segment_order, end_segment_order
                FROM "Year_2025".vw_bottleneck_dashboard_ranked
                WHERE corridor = '{corridor.road}' AND direction = '{corridor.direction}'
            ),
            peak_ctx AS (
                SELECT r.bottleneck_id, t.county, t.aadt
                FROM ranked r
                JOIN "Year_2025".corridor_segments AS cs
                  ON cs.corridor_id = {corridor.corridor_id}
                 AND cs.segment_order = r.peak_segment_order
                JOIN "Year_2025".tmc_metadata AS t ON t.tmc = cs.tmc
            ),
            bottleneck_segments AS (
                SELECT r.bottleneck_id, cs.tmc
                FROM ranked r
                JOIN "Year_2025".corridor_segments AS cs
                  ON cs.corridor_id = {corridor.corridor_id}
                 AND cs.segment_order BETWEEN r.start_segment_order AND r.end_segment_order
            ),
            congested AS (
                SELECT bs.bottleneck_id,
                       EXTRACT(HOUR FROM p.measurement_tstamp)::integer AS hour,
                       EXTRACT(ISODOW FROM p.measurement_tstamp)::integer AS dow
                FROM bottleneck_segments AS bs
                JOIN "Year_2025".probe_readings AS p ON p.tmc_code = bs.tmc
                WHERE p.speed IS NOT NULL AND p.reference_speed > 0
                  AND p.speed < 0.70 * p.reference_speed
            ),
            hour_ranked AS (
                SELECT bottleneck_id, hour,
                       ROW_NUMBER() OVER (PARTITION BY bottleneck_id ORDER BY COUNT(*) DESC) AS rn
                FROM congested GROUP BY bottleneck_id, hour
            ),
            dow_ranked AS (
                SELECT bottleneck_id, dow,
                       ROW_NUMBER() OVER (PARTITION BY bottleneck_id ORDER BY COUNT(*) DESC) AS rn
                FROM congested GROUP BY bottleneck_id, dow
            )
            SELECT
                r.severity_rank, r.representative_intersection, r.occurrence_pct,
                r.annual_queue_mile_hours, r.avg_congested_speed_ratio, r.severity_index,
                pc.county, pc.aadt,
                TO_CHAR(make_time(h.hour, 0, 0), 'HH12:00 AM') AS peak_hour_label,
                (ARRAY['Monday', 'Tuesday', 'Wednesday', 'Thursday',
                       'Friday', 'Saturday', 'Sunday'])[d.dow] AS peak_weekday_name
            FROM ranked AS r
            LEFT JOIN peak_ctx AS pc ON pc.bottleneck_id = r.bottleneck_id
            LEFT JOIN hour_ranked AS h ON h.bottleneck_id = r.bottleneck_id AND h.rn = 1
            LEFT JOIN dow_ranked AS d ON d.bottleneck_id = r.bottleneck_id AND d.rn = 1
            ORDER BY r.severity_rank
        """,
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )


def _load_corridor_map_segments() -> pl.DataFrame:
    """Interstate + Expressway + Arterial segments with their severity metric,
    for the region map's colored traces and each corridor's mini-map."""
    return pl.read_database_uri(
        query="""
            SELECT
                d.corridor_id, d.corridor_group, d.corridor_name, c.tmc,
                c.start_latitude, c.start_longitude,
                c.end_latitude, c.end_longitude,
                p.weekday_occurrence_pct AS severity_pct
            FROM "Year_2025".corridor_segments AS c
            JOIN "Year_2025".corridor_definitions AS d
              ON d.corridor_id = c.corridor_id
            LEFT JOIN "Year_2025".segment_profile AS p
              ON p.corridor_id = c.corridor_id AND p.segment_order = c.segment_order
            WHERE d.is_active
              AND c.start_latitude IS NOT NULL AND c.start_longitude IS NOT NULL
              AND c.end_latitude IS NOT NULL AND c.end_longitude IS NOT NULL
        """,
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )


# Region-wide bottleneck segment query and K-means-fit-once-then-filter
# record builder now live in cbi_map_geometry.py, since
# cbi_generate_corridor_report.py's per-corridor static map images need the
# exact same region-wide fit (see its _build_corridor_map_png) — aliased
# here under their original names so the rest of this file (region map,
# intersection tab, corridor mini-maps) is unchanged.
_load_bottleneck_map_segments = cbi_map_geometry.load_bottleneck_map_segments


def _load_watch_map_segments() -> pl.DataFrame:
    return pl.read_database_uri(
        query="""
            SELECT
                w.road_class, w.road, w.direction, w.tmc,
                t.start_latitude, t.start_longitude,
                t.end_latitude, t.end_longitude,
                m.occurrence_pct AS severity_pct
            FROM "Year_2025".watch_segments AS w
            JOIN "Year_2025".tmc_metadata AS t ON t.tmc = w.tmc
            LEFT JOIN "Year_2025".watch_segment_metrics AS m ON m.tmc = w.tmc
            WHERE t.start_latitude IS NOT NULL AND t.start_longitude IS NOT NULL
              AND t.end_latitude IS NOT NULL AND t.end_longitude IS NOT NULL
        """,
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )


# ---------------------------------------------------------------------------
# Figure builders
# ---------------------------------------------------------------------------
def _layout(fig: go.Figure, title: str, height: int = 340, width: int | None = None) -> go.Figure:
    fig.update_layout(
        title=dict(text=title, font=dict(color=INK_PRIMARY, size=15)),
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font=dict(color=INK_SECONDARY, family="system-ui, -apple-system, 'Segoe UI', sans-serif"),
        height=height,
        width=width,
        margin=dict(l=50, r=20, t=50, b=40),
        showlegend=False,
    )
    fig.update_xaxes(gridcolor=GRID, linecolor=GRID, tickfont=dict(color=INK_MUTED))
    fig.update_yaxes(gridcolor=GRID, linecolor=GRID, tickfont=dict(color=INK_MUTED))
    return fig


def _single_series_bar(
    x: list, y: list, title: str, highlight_max: bool = True,
    width: int | None = None, height: int = 340,
) -> go.Figure:
    colors = [SERIES_BLUE] * len(y)
    if highlight_max and y:
        finite = [v if v is not None else float("-inf") for v in y]
        colors[finite.index(max(finite))] = STATUS_CRITICAL

    fig = go.Figure(
        go.Bar(x=x, y=y, marker_color=colors, hovertemplate="%{x}<br>%{y:,.2f}<extra></extra>")
    )
    fig.update_yaxes(tickformat=",.2f")
    return _layout(fig, title, height=height, width=width)


def _severity_bin(pct: float | None) -> int:
    if pct is None:
        return -1
    for i in range(len(SEVERITY_BINS) - 1):
        if SEVERITY_BINS[i] <= pct < SEVERITY_BINS[i + 1]:
            return i
    return len(SEVERITY_COLORS) - 1


_bottleneck_records_and_legend = cbi_map_geometry.bottleneck_records_and_legend


def _segment_records_for_class(df: pl.DataFrame, width: float) -> list[list]:
    """[points, color, width] per segment, for the canvas tile-map JS (see
    _CANVAS_MAP_JS) — points is a [[lat, lon], ...] polyline, real road
    curve where the HERE shapefile covers this TMC, straight line
    otherwise (see cbi_map_geometry.resolve_points). Color follows the same
    severity bins as before; width is the road-class width, thinned
    slightly for no-data segments so analyzed roads still stand out. Offset
    per cbi_map_geometry.offset_points so opposite-direction segments on
    the same physical road render as two visually distinct parallel
    lines."""
    polylines = cbi_map_geometry.load_tmc_polylines()
    records = []
    for row in df.iter_rows(named=True):
        bin_index = _severity_bin(row.get("severity_pct"))
        color = NO_DATA_GRAY if bin_index == -1 else SEVERITY_COLORS[bin_index]
        seg_width = width if bin_index >= 0 else max(1.0, width - 0.6)
        points = cbi_map_geometry.offset_points(
            cbi_map_geometry.resolve_points(
                row.get("tmc"), row["start_latitude"], row["start_longitude"],
                row["end_latitude"], row["end_longitude"], polylines,
            )
        )
        records.append([points, color, seg_width])
    return records


_COUNTY_BOUNDARY_RINGS_CACHE: list[list[list[float]]] | None = None


def _load_county_boundary_rings() -> list[list[list[float]]]:
    """County outlines for the 21-county study region (US Census cb_2015
    500k cartographic boundaries, filtered to just this region and saved
    locally as data/ga_region_counties.geojson). Returns a list of rings,
    each a list of [lat, lon] points, for the canvas tile-map JS to draw."""
    global _COUNTY_BOUNDARY_RINGS_CACHE
    if _COUNTY_BOUNDARY_RINGS_CACHE is not None:
        return _COUNTY_BOUNDARY_RINGS_CACHE

    rings: list[list[list[float]]] = []
    if COUNTY_BOUNDARIES_PATH.exists():
        with COUNTY_BOUNDARIES_PATH.open(encoding="utf-8") as f:
            geojson = json.load(f)
        for feature in geojson["features"]:
            geometry = feature["geometry"]
            polygons = (
                [geometry["coordinates"]]
                if geometry["type"] == "Polygon"
                else geometry["coordinates"]
            )
            for polygon in polygons:
                exterior_ring = polygon[0]
                rings.append([[lat, lon] for lon, lat in exterior_ring])

    _COUNTY_BOUNDARY_RINGS_CACHE = rings
    return rings


_MAP_COUNTER = 0


def _canvas_map_html(
    segments: list[list],
    height: int,
    width: int,
    interactive: bool,
    county_rings: list[list[list[float]]] | None = None,
    responsive: bool = False,
    points: list[list] | None = None,
) -> str:
    """<canvas> pair + inline init call for one tile-basemap map instance.
    See _CANVAS_MAP_JS for the shared rendering engine (Web Mercator
    projection, CARTO->OSM tile loading via plain <img> tags rather than
    fetch/XHR — Plotly's MapLibre-based map trace never rendered a basemap
    in this environment across several attempts; this technique, adapted
    from a working reference file the user supplied, draws tiles as plain
    images onto a canvas, which browsers allow cross-origin without CORS
    headers for display purposes, unlike the fetch-based texture loading
    WebGL/MapLibre requires.

    Canvas pixel buffers are also unaffected by the container being
    display:none at draw time (unlike Plotly, which measures the container
    and gets 0x0), so these need no resize-on-tab-switch workaround."""
    global _MAP_COUNTER
    _MAP_COUNTER += 1
    map_id = f"tilemap-{_MAP_COUNTER}"
    payload = {
        "tileCanvasId": f"{map_id}-tiles",
        "dataCanvasId": f"{map_id}-data",
        "wrapId": f"{map_id}-wrap",
        "zoomInId": f"{map_id}-zoom-in",
        "zoomOutId": f"{map_id}-zoom-out",
        "width": width,
        "height": height,
        "interactive": interactive,
        "responsive": responsive,
        "segments": segments,
        "points": points or [],
        "countyRings": county_rings or [],
    }
    payload_json = json.dumps(payload, separators=(",", ":"))
    zoom_controls = (
        f"""<div class="tile-map-zoom">
          <button id="{map_id}-zoom-in" type="button" aria-label="Zoom in">+</button>
          <button id="{map_id}-zoom-out" type="button" aria-label="Zoom out">&minus;</button>
        </div>"""
        if interactive
        else ""
    )
    # Responsive maps fill whatever width their container actually gives
    # them (measured and kept in sync by createTileMap's ResizeObserver) —
    # no inline width, just 100%. Non-responsive ones (corridor mini-maps)
    # keep the fixed pixel width they were built for.
    wrap_style = "width:100%;" if responsive else f"width:{width}px;"
    return f"""
    <div class="tile-map" id="{map_id}-wrap" style="{wrap_style}">
      <canvas id="{map_id}-tiles" width="{width}" height="{height}"></canvas>
      <canvas id="{map_id}-data" width="{width}" height="{height}"></canvas>
      <div class="tile-map-attribution">&copy; <a href="https://carto.com/attributions" target="_blank" rel="noopener">CARTO</a>
        &copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors
        &mdash; drag to pan, scroll or use +/&minus; to zoom.</div>
      {zoom_controls}
    </div>
    <script>createTileMap({payload_json});</script>
    """


def _build_region_map_html(
    corridor_segments_df: pl.DataFrame,
) -> str:
    # No gray full-network context layer — it added visual clutter without
    # much information (every classified road in the region, most of it
    # irrelevant to the analyzed corridors) and made the actual bottleneck
    # lines harder to pick out. The Collector/Local watch-segment layer
    # was dropped from this map too (still available as data in the Watch
    # Segments tab) — it used a different, unrelated color scale
    # (occurrence %, blue ramp) from the bottleneck layer's AADT-weighted
    # severity traffic-color ramp, which read as two competing legends on
    # one map. Only the bottleneck layer is drawn now.
    segments: list[list] = []

    bottleneck_records, severity_legend = _bottleneck_records_and_legend(corridor_segments_df)
    segments.extend(bottleneck_records)

    road_class_items = "".join(
        f"<div class='map-legend-item'><span class='swatch' "
        f"style='height:{max(2, min(w * 2.1, 14)):.0f}px;background:{INK_SECONDARY}'></span>{cls}</div>"
        for cls, w in ROAD_CLASS_WIDTH.items()
        if cls not in ("Collector", "Local")
    )
    severity_items = "".join(
        f"<div class='map-legend-item'><span class='swatch dot' style='background:{color}'></span>{label}</div>"
        for color, label in severity_legend
    )
    legend_html = (
        "<div class='map-legend'>"
        "<h4>Road Class (line thickness)</h4>" + road_class_items +
        "<h4>Bottleneck Severity<br/>(Google Maps-style traffic colors)</h4>" + severity_items +
        "</div>"
    )

    explain_html = (
        "<div class='map-explain'>"
        "<h4>Reading this map</h4>"
        "<p>Colored, full-width sections are the exact extent of a detected recurring "
        "bottleneck — not the whole corridor — colored by AADT-weighted severity (see the "
        "Metadata tab for why traffic volume factors in). Two directions of the same physical "
        "road (e.g. I-285 Clockwise / Counterclockwise) are drawn with a small fixed sideways "
        "offset so they render as two distinct parallel lines instead of overlapping into "
        "one.</p>"
        "<p>Color follows the same orange-to-dark-red language most map apps use for heavy "
        "traffic, so it reads at a glance without needing the legend: light orange is the least "
        "severe of these bottlenecks (still a real, confirmed recurring one — not \"fine\"), dark "
        "maroon the most. The 5 color classes are K-means clustered on this run's actual severity "
        "distribution rather than a fixed scale, so the breaks reflect real gaps in how severe "
        "this region's bottlenecks are, not an arbitrary "
        "percentage.</p>"
        "<p>Bottlenecks confirmed as false-flagged by the overnight-congestion diagnostic (a "
        "flat reference speed misreading signal-cycle or ramp-geometry noise as congestion) "
        "are excluded from this layer entirely, not just deprioritized — see the Metadata "
        "tab.</p>"
        "<p>A corridor with no colored sections either has none of its congestion concentrated "
        "at a discrete, repeatable location, or falls back to a segment-by-segment profile "
        "instead (see that corridor's own section in the Corridors tab).</p>"
        "<p>Collector and Local roads don't go through bottleneck detection (too few "
        "multi-segment corridors exist), so they aren't shown on this map — see the Watch "
        "Segments tab for their lighter occurrence-based summary instead.</p>"
        "<p>The Corridors tab's severity index is a different, more complete score than plain "
        "occurrence: it multiplies occurrence rate by annual queue mile-hours and by how much "
        "speed drops, then scales by traffic volume (AADT) — that AADT-weighted figure is "
        "exactly what's clustered here for the bottleneck layer's color.</p>"
        "<p>Line geometry is drawn straight between each TMC segment's endpoint coordinates — "
        "the source data has no curved road shapes, so tight curves and interchange ramps are "
        "approximated as straight segments rather than traced exactly.</p>"
        "</div>"
    )

    map_html = _canvas_map_html(
        segments, height=760, width=1180, interactive=True,
        county_rings=_load_county_boundary_rings(), responsive=True,
    )
    return f"<div class='map-with-legend'>{explain_html}{map_html}{legend_html}</div>"


# ---------------------------------------------------------------------------
# HTML assembly helpers
# ---------------------------------------------------------------------------
_FIG_COUNTER = 0


def _fig_html(fig: go.Figure, fixed_size: bool = False) -> str:
    """fixed_size=True renders at the figure's explicit width/height with
    Plotly's responsive-to-container behavior turned off. Used for charts
    that live inside initially-hidden panels (the Corridors tab, shown only
    after a click) — Plotly measures a display:none container as 0x0 at
    draw time, and a responsive chart never recovers a sane size from that,
    even after the container becomes visible and JS asks it to resize."""
    global _FIG_COUNTER
    include_js = _FIG_COUNTER == 0
    _FIG_COUNTER += 1
    config = {"responsive": False, "displaylogo": False} if fixed_size else None
    return fig.to_html(
        full_html=False,
        include_plotlyjs=("inline" if include_js else False),
        config=config,
    )


def _chart_card(fig: go.Figure, note: str) -> str:
    """A chart plus a brief, plain-English note on what it measures and how
    it's computed — so a reader doesn't have to already know the CBI
    methodology to interpret the number."""
    return f"<div class='card'>{_fig_html(fig, fixed_size=True)}<p class='chart-note'>{note}</p></div>"


def _build_worst_day_section_html(
    worst_day: dict,
    hourly_fig: go.Figure | None,
    map_records: list[list],
) -> str:
    """The region's single worst calendar date (highest total event mile-
    hours, same measure as the four charts above it), plus a snapshot of
    whichever bottleneck contributed most that day and a map spotlighting
    every bottleneck that was actually active on that date. AADT and
    Severity Index are the same annual, AADT-weighted figures used
    everywhere else in this report (not day-specific versions of those
    two) — Hours of Delay is the one day-specific duration figure, that
    bottleneck's own active-congestion time on this date."""
    analysis_date = worst_day.get("analysis_date")
    if analysis_date is None:
        return "<p class='muted'>No congestion events are available yet to identify a worst day.</p>"

    date_label = cbi_map_geometry.format_popup_date(analysis_date)
    weekday_label = analysis_date.strftime("%A")
    hours_of_delay = (
        worst_day["active_congestion_minutes"] / 60.0
        if worst_day.get("active_congestion_minutes") is not None
        else None
    )
    location_label = " ".join(
        part for part in [
            html.escape(str(worst_day.get("corridor_name") or "")),
            f"— {html.escape(str(worst_day['representative_intersection']))}"
            if worst_day.get("representative_intersection")
            else "",
        ]
        if part
    ) or "N/A"

    stats_html = f"""
    <div class="kpi-row">
      <div class="kpi-card"><div class="label">Highest Regional Congestion Day</div>
        <div class="value">{html.escape(date_label)}</div>
        <div class="sub">{weekday_label}</div></div>
      <div class="kpi-card"><div class="label">Total Region-Wide Congestion</div>
        <div class="value">{_fmt2(worst_day.get("total_mile_hours"))}</div>
        <div class="sub">mile-hours that day</div></div>
      <div class="kpi-card"><div class="label">Worst Corridor That Day</div>
        <div class="value">{location_label}</div>
        <div class="sub">{html.escape(str(worst_day.get("county") or "-"))} / {html.escape(str(worst_day.get("cid_name") or "-"))}</div></div>
      <div class="kpi-card"><div class="label">AADT (peak segment)</div>
        <div class="value">{worst_day.get("peak_segment_aadt") or "-"}</div>
        <div class="sub">annual average daily traffic</div></div>
      <div class="kpi-card"><div class="label">Severity Index (AADT-weighted)</div>
        <div class="value">{_fmt2(worst_day.get("aadt_weighted_severity_index"))}</div>
        <div class="sub">this bottleneck's annual figure</div></div>
      <div class="kpi-card"><div class="label">Hours of Delay That Day</div>
        <div class="value">{_fmt2(hours_of_delay)}</div>
        <div class="sub">active congestion duration</div></div>
    </div>
    """

    hourly_card = (
        _chart_card(
            hourly_fig,
            "Same proportional-overlap hourly slicing as the Congestion by Hour of Day chart "
            "above, but scoped to just this one date rather than summed across the whole "
            "year — shows how this specific day's congestion built up and cleared.",
        )
        if hourly_fig is not None
        else "<div class='card'><p class='muted'>No hourly detail available for this date.</p></div>"
    )
    map_card = (
        f"<div class='card map-card'>{_canvas_map_html(map_records, height=460, width=700, interactive=True)}"
        "<p class='chart-note'>Every ranking-eligible bottleneck that actually occurred on this "
        "date, colored on the same region-wide AADT-weighted severity scale as the Region Map "
        "tab.</p></div>"
        if map_records
        else "<div class='card'><p class='muted'>No mapped bottleneck activity on this date.</p></div>"
    )

    return f"""
    <h3 class="section-title">Highest Regional Congestion Day</h3>
    <p class="muted">The single calendar date with the highest total region-wide congestion
    (event mile-hours) of the analysis period — a different, more specific question than the
    "Worst Day of Week" KPI above, which averages across every date sharing that weekday.</p>
    {stats_html}
    <div class="chart-grid">{hourly_card}{map_card}</div>
    """


def _kv_table(rows: list[tuple[str, str]]) -> str:
    body = "".join(
        f"<tr><td class='k'>{html.escape(str(k))}</td><td class='v'>{html.escape(str(v))}</td></tr>"
        for k, v in rows
    )
    return f"<table class='kv'>{body}</table>"


def _bottleneck_table(ranked: pl.DataFrame) -> str:
    if ranked.is_empty():
        return (
            "<p class='muted'>No single location stood out as a discrete recurring "
            "bottleneck on this corridor — this usually means congestion here is spread "
            "fairly evenly along the corridor's length rather than concentrated at one "
            "point (common on beltways/loops). See the segment congestion profile above "
            "for where and how much.</p>"
        )

    header = (
        "<tr><th>Rank</th><th>Location</th><th>County</th><th>AADT</th>"
        "<th>Occurrence</th><th>Peak Hour</th><th>Peak Day</th>"
        "<th>Annual Mile-Hrs</th><th>Avg Speed Ratio</th><th>Severity</th></tr>"
    )
    rows = "".join(
        "<tr>"
        f"<td>{row['severity_rank']}</td>"
        f"<td>{html.escape(str(row['representative_intersection'] or ''))}</td>"
        f"<td>{html.escape(str(row['county'] or '-'))}</td>"
        f"<td>{row['aadt'] or '-'}</td>"
        f"<td>{_fmt2(row['occurrence_pct'])}%</td>"
        f"<td>{html.escape(str(row['peak_hour_label'] or '-'))}</td>"
        f"<td>{html.escape(str(row['peak_weekday_name'] or '-'))}</td>"
        f"<td>{_fmt2(row['annual_queue_mile_hours'])}</td>"
        f"<td>{_fmt2(row['avg_congested_speed_ratio'])}</td>"
        f"<td>{_fmt2(row['severity_index'])}</td>"
        "</tr>"
        for row in ranked.iter_rows(named=True)
    )
    return f"<table class='data'>{header}{rows}</table>"


def _watch_table(df: pl.DataFrame, road_class: str) -> str:
    subset = df.filter(pl.col("road_class") == road_class)
    header = (
        "<tr><th>#</th><th>Road</th><th>Direction</th><th>From</th><th>To</th>"
        "<th>Length (mi)</th><th>County</th><th>CID</th><th>AADT</th><th>Occurrence %</th>"
        "<th>Peak Hour</th><th>Peak Day</th><th>Annual Mile-Hrs</th><th>Avg Speed Ratio</th></tr>"
    )
    rows = "".join(
        "<tr>"
        f"<td>{row['rank_in_class']}</td>"
        f"<td>{html.escape(str(row['road']))}</td>"
        f"<td>{html.escape(str(row['direction'] or ''))}</td>"
        f"<td>{html.escape(str(row['from_intersection'] or '-'))}</td>"
        f"<td>{html.escape(str(row['to_intersection'] or 'end of probe network'))}</td>"
        f"<td>{_fmt2(row['miles'])}</td>"
        f"<td>{html.escape(str(row['county'] or ''))}</td>"
        f"<td>{html.escape(str(row['cid_name'] or '-'))}</td>"
        f"<td>{row['aadt'] or ''}</td>"
        f"<td>{_fmt2(row['occurrence_pct'])}</td>"
        f"<td>{html.escape(str(row['peak_hour_label'] or '-'))}</td>"
        f"<td>{html.escape(str(row['peak_weekday_name'] or '-'))}</td>"
        f"<td>{_fmt2(row['annual_segment_mile_hours'])}</td>"
        f"<td>{_fmt2(row['average_congested_speed_ratio'])}</td>"
        "</tr>"
        for row in subset.iter_rows(named=True)
    )
    return (
        "<p class='muted'>Each row is a single probe segment (TMC), not a multi-segment "
        "corridor — <strong>From</strong> is the segment's own reference intersection; "
        "<strong>To</strong> is the next intersection along the same road and direction, "
        "spanning the length shown. <strong>CID</strong> is the Community Improvement District "
        "the segment's midpoint falls within (ARC Open Data, region-wide) — shown as '-' for "
        "segments outside every CID boundary, which is most of the region since CIDs are a "
        "small share of total land area.</p>"
        f"<table class='data'>{header}{rows}</table>"
    )


_PAGE_CSS = f"""
:root {{
  color-scheme: light;
  --surface: {SURFACE}; --page: {PAGE}; --ink: {INK_PRIMARY};
  --ink-secondary: {INK_SECONDARY}; --ink-muted: {INK_MUTED}; --grid: {GRID};
  --navy: {NAVY}; --blue: {SERIES_BLUE};
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; background: var(--page); color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}}
header.masthead {{
  background: var(--navy); color: #fff; padding: 16px 28px;
  display: flex; align-items: center; gap: 18px;
}}
header.masthead img.agency-logo {{ height: 48px; width: auto; flex-shrink: 0; }}
header.masthead h1 {{ margin: 0 0 4px 0; font-size: 22px; }}
header.masthead p {{ margin: 0; opacity: 0.85; font-size: 13px; }}
nav.tabs {{
  display: flex; gap: 4px; background: var(--surface);
  border-bottom: 1px solid var(--grid); padding: 0 24px; position: sticky; top: 0; z-index: 10;
}}
nav.tabs button {{
  border: none; background: none; padding: 14px 16px; font-size: 14px; cursor: pointer;
  color: var(--ink-secondary); border-bottom: 3px solid transparent;
}}
nav.tabs button.active {{ color: var(--navy); border-bottom-color: var(--blue); font-weight: 600; }}
main {{ padding: 24px 28px 60px; }}
section.tabpanel {{ display: none; }}
section.tabpanel.active {{ display: block; }}
.kpi-row {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }}
.kpi-card {{
  background: var(--surface); border: 1px solid var(--grid); border-radius: 8px;
  padding: 16px 20px; min-width: 220px; flex: 1;
}}
.kpi-card .label {{ font-size: 12px; color: var(--ink-muted); text-transform: uppercase; letter-spacing: .04em; }}
.kpi-card .value {{ font-size: 26px; font-weight: 600; color: var(--navy); margin-top: 4px; }}
.kpi-card .sub {{ font-size: 12px; color: var(--ink-secondary); margin-top: 2px; }}
.chart-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; }}
.card {{ background: var(--surface); border: 1px solid var(--grid); border-radius: 8px; padding: 8px; overflow-x: auto; }}
.chart-note {{ font-size: 11.5px; line-height: 1.5; color: var(--ink-muted); margin: 6px 4px 0; }}
.corridor-layout {{ display: flex; gap: 20px; align-items: flex-start; }}
.corridor-sidebar {{
  width: 280px; flex-shrink: 0; max-height: 80vh; overflow-y: auto;
  background: var(--surface); border: 1px solid var(--grid); border-radius: 8px; padding: 8px;
}}
.corridor-sidebar .group-label {{
  font-size: 11px; text-transform: uppercase; color: var(--ink-muted); padding: 8px 10px 2px;
}}
.corridor-sidebar button {{
  display: block; width: 100%; text-align: left; border: none; background: none;
  padding: 8px 10px; font-size: 13px; cursor: pointer; border-radius: 4px; color: var(--ink-secondary);
}}
.corridor-sidebar button.active {{ background: var(--page); color: var(--navy); font-weight: 600; }}
.corridor-detail {{ display: none; flex: 1; min-width: 0; }}
.corridor-detail.active {{ display: block; }}
.corridor-detail h2 {{ color: var(--navy); margin-top: 0; }}
.download-report-btn {{
  display: inline-block; margin: 0 0 16px; padding: 8px 16px; border-radius: 6px;
  background: var(--navy); color: #fff; font-size: 13px; font-weight: 600;
  text-decoration: none;
}}
.download-report-btn:hover {{ background: var(--blue); }}
table.kv {{ border-collapse: collapse; margin-bottom: 16px; }}
table.kv td {{ padding: 4px 12px 4px 0; font-size: 13px; }}
table.kv td.k {{ color: var(--ink-muted); }}
table.kv td.v {{ font-weight: 600; }}
table.data {{ border-collapse: collapse; width: 100%; font-size: 13px; margin-bottom: 20px; }}
table.data th {{
  text-align: left; border-bottom: 2px solid var(--grid); padding: 8px 10px;
  color: var(--ink-muted); font-weight: 600; font-size: 11px; text-transform: uppercase;
}}
table.data td {{ padding: 7px 10px; border-bottom: 1px solid var(--grid); }}
h3.section-title {{ color: var(--navy); margin: 24px 0 10px; }}
p.muted {{ color: var(--ink-muted); }}
footer {{ padding: 20px 28px; color: var(--ink-muted); font-size: 12px; }}
.prose {{ max-width: 900px; line-height: 1.6; font-size: 14px; color: var(--ink-secondary); }}
.prose h3 {{ color: var(--navy); margin: 28px 0 8px; }}
.prose h3:first-child {{ margin-top: 0; }}
.prose ul {{ padding-left: 20px; }}
.prose li {{ margin-bottom: 6px; }}
.audience-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; margin: 12px 0 8px; }}
.audience-card {{
  background: var(--surface); border: 1px solid var(--grid); border-radius: 8px; padding: 14px 16px;
}}
.audience-card .who {{ font-weight: 600; color: var(--navy); font-size: 13px; margin-bottom: 4px; }}
.audience-card .what {{ font-size: 13px; color: var(--ink-secondary); }}
.attribution-box {{
  border: 1px solid var(--grid); border-left: 4px solid var(--navy); background: var(--page);
  padding: 14px 18px; border-radius: 6px; margin: 4px 0 20px; font-size: 13px; line-height: 1.6;
}}
.card.map-card {{ padding: 0; overflow: hidden; }}
.tile-map {{ position: relative; max-width: 100%; }}
.tile-map canvas {{ display: block; max-width: 100%; }}
.tile-map canvas:nth-child(2) {{ position: absolute; top: 0; left: 0; }}
.tile-map-attribution {{
  position: absolute; top: 0; left: 0; right: 0; z-index: 4;
  font-size: 10px; color: var(--ink-secondary); padding: 4px 8px;
  background: rgba(252,252,251,0.85); backdrop-filter: blur(2px);
}}
.tile-map-attribution a {{ color: var(--ink-secondary); }}
.tile-map-zoom {{
  position: absolute; top: 40px; right: 12px; z-index: 5;
  display: flex; flex-direction: column; border-radius: 6px; overflow: hidden;
  box-shadow: 0 1px 4px rgba(11,11,11,0.25);
}}
.tile-map-zoom button {{
  width: 30px; height: 30px; border: none; background: rgba(252,252,251,0.95);
  color: var(--ink); font-size: 16px; font-weight: 600; cursor: pointer; line-height: 1;
  border-bottom: 1px solid var(--grid);
}}
.tile-map-zoom button:last-child {{ border-bottom: none; }}
.tile-map-zoom button:hover {{ background: var(--page); }}
.tile-map-tooltip {{
  position: absolute; z-index: 6; pointer-events: none; max-width: 240px;
  background: rgba(20,24,31,0.92); color: #fff; font-size: 11.5px; line-height: 1.5;
  padding: 8px 10px; border-radius: 6px; box-shadow: 0 2px 8px rgba(11,11,11,0.35);
}}
.tile-map-tooltip strong {{ display: block; font-size: 12.5px; margin-bottom: 3px; }}
.tile-map-tooltip .hint {{ color: #ffcc80; margin-top: 4px; font-weight: 600; }}
.map-with-legend {{ display: grid; grid-template-columns: 340px minmax(0, 1fr) 240px; gap: 0; align-items: start; width: 100%; }}
.map-explain {{
  width: 340px; flex-shrink: 0; padding: 14px 20px; font-size: 11.5px; line-height: 1.65;
  color: var(--ink-muted); border-right: 1px solid var(--grid); max-height: 760px; overflow-y: auto;
}}
.map-explain h4 {{
  margin: 0 0 8px; font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
  color: var(--ink-muted); font-weight: 600;
}}
.map-explain h4:not(:first-child) {{ margin-top: 18px; }}
.map-explain p {{ margin: 0 0 10px; }}
.map-legend {{
  width: 240px; flex-shrink: 0; padding: 14px 16px; font-size: 12px; color: var(--ink-secondary);
  border-left: 1px solid var(--grid); max-height: 760px; overflow-y: auto;
}}
.map-legend h4 {{
  margin: 0 0 8px; font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
  color: var(--ink-muted); font-weight: 600;
}}
.map-legend h4:not(:first-child) {{ margin-top: 18px; }}
.map-legend-item {{ display: flex; align-items: center; gap: 8px; margin-bottom: 7px; }}
.map-legend-item .swatch {{ display: inline-block; width: 26px; border-radius: 2px; flex-shrink: 0; background: var(--ink-secondary); }}
.map-legend-item .swatch.dot {{ width: 10px; height: 10px; border-radius: 50%; }}
.map-legend-note {{ font-size: 11px; line-height: 1.5; color: var(--ink-muted); margin: 10px 0 0; }}
.bottleneck-spotlight {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; margin: 16px 0 28px; }}
.bottleneck-card {{
  display: flex; gap: 14px; background: var(--surface); border: 1px solid var(--grid);
  border-left: 4px solid var(--navy); border-radius: 8px; padding: 14px 16px;
}}
.bottleneck-card .rank {{ font-size: 22px; font-weight: 700; color: var(--navy); flex-shrink: 0; width: 34px; }}
.bottleneck-card .name {{ font-weight: 600; font-size: 14px; color: var(--ink); }}
.bottleneck-card .corridor {{ font-size: 12px; color: var(--ink-muted); margin: 2px 0 8px; }}
.bottleneck-card .desc {{ font-size: 12px; color: var(--ink-secondary); line-height: 1.5; }}
table.data tr.top10 {{ background: rgba(31,56,100,0.05); font-weight: 600; }}
"""

_CANVAS_MAP_JS = """
// Hand-rolled slippy map (Web Mercator, CARTO -> OSM tile fallback), adapted
// from a known-working reference file. Tiles load via plain <img> tags,
// which browsers display cross-origin without needing CORS headers —
// unlike Plotly's MapLibre-based map trace, which fetches tiles as raw
// bitmap data for a WebGL texture and requires the tile server to send
// CORS headers granting that, which none of the free tile providers tried
// here actually do. That mismatch, not network/domain blocking, is why
// the Plotly map stayed blank across several attempts.
(function () {
  var TILE_SIZE = 256;
  function lonToWorldX(lon) { return (lon + 180) / 360 * TILE_SIZE; }
  function latToWorldY(lat) {
    var r = lat * Math.PI / 180;
    return (1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * TILE_SIZE;
  }

  var CARTO_SUB = ['a', 'b', 'c', 'd'];
  var OSM_SUB = ['a', 'b', 'c'];
  function cartoUrl(z, x, y) {
    var s = CARTO_SUB[(x + y) % CARTO_SUB.length];
    return 'https://' + s + '.basemaps.cartocdn.com/light_all/' + z + '/' + x + '/' + y + '.png';
  }
  function osmUrl(z, x, y) {
    var s = OSM_SUB[(x + y) % OSM_SUB.length];
    return 'https://' + s + '.tile.openstreetmap.org/' + z + '/' + x + '/' + y + '.png';
  }

  var tileCache = {};
  var TILE_LOAD_TIMEOUT = 8000;
  function getTile(z, x, y, onReady) {
    var n = Math.pow(2, z);
    if (y < 0 || y >= n) return null;
    x = ((x % n) + n) % n;
    var key = z + '/' + x + '/' + y;
    var entry = tileCache[key];
    if (entry) return entry;
    entry = { img: new Image(), status: 'loading', source: 'carto' };
    tileCache[key] = entry;
    entry.img.crossOrigin = 'anonymous';
    var timedOut = false;
    var timer = setTimeout(function () {
      timedOut = true;
      if (entry.status === 'loading') tryFallback();
    }, TILE_LOAD_TIMEOUT);
    function tryFallback() {
      if (entry.source === 'carto') {
        entry.source = 'osm';
        entry.img = new Image();
        entry.img.crossOrigin = 'anonymous';
        entry.img.onload = onOk;
        entry.img.onerror = onFail;
        entry.img.src = osmUrl(z, x, y);
      } else {
        entry.status = 'failed';
        onReady();
      }
    }
    function onOk() { if (timedOut) return; clearTimeout(timer); entry.status = 'ok'; onReady(); }
    function onFail() { clearTimeout(timer); tryFallback(); }
    entry.img.onload = onOk;
    entry.img.onerror = onFail;
    entry.img.src = cartoUrl(z, x, y);
    return entry;
  }

  function createTileMap(cfg) {
    var tileCanvas = document.getElementById(cfg.tileCanvasId);
    var dataCanvas = document.getElementById(cfg.dataCanvasId);
    var wrapEl = cfg.wrapId && document.getElementById(cfg.wrapId);
    if (!tileCanvas || !dataCanvas) return;
    var tctx = tileCanvas.getContext('2d');
    var dctx = dataCanvas.getContext('2d');
    var dpr = Math.max(1, Math.min(window.devicePixelRatio || 1, 2));
    var h = cfg.height;

    // Responsive maps size their canvas buffer to the wrapper's *actual*
    // measured width rather than the fixed cfg.width baked in at report
    // build time. Without this, the canvas can end up displayed at a CSS
    // size different from its internal pixel buffer whenever the grid
    // column it sits in isn't exactly cfg.width wide (leaves the extra
    // space as blank white area, and — because the browser then has to
    // scale the whole bitmap to fit — visibly distorts stroke widths that
    // were computed assuming a 1:1 buffer-to-CSS-pixel mapping). Height
    // stays fixed; only width tracks the container.
    function measuredWidth() {
      if (!cfg.responsive || !wrapEl) return cfg.width;
      var rect = wrapEl.getBoundingClientRect();
      return rect.width > 0 ? rect.width : cfg.width;
    }

    var w = measuredWidth();

    function applyCanvasSize() {
      [tileCanvas, dataCanvas].forEach(function (c) {
        c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
        c.style.width = w + 'px'; c.style.height = h + 'px';
      });
    }
    applyCanvasSize();

    var view = { cx: 0, cy: 0, zoom: 10 };

    function fitBounds(b) {
      if (!b) return;
      var padPx = 30;
      var dataW = Math.max(1e-6, b.maxX - b.minX), dataH = Math.max(1e-6, b.maxY - b.minY);
      var zx = Math.log2((w * dpr - padPx * 2 * dpr) / dataW);
      var zy = Math.log2((h * dpr - padPx * 2 * dpr) / dataH);
      var z = Math.min(zx, zy) - Math.log2(dpr);
      view.zoom = Math.max(2.5, Math.min(18, z));
      view.cx = (b.minX + b.maxX) / 2;
      view.cy = (b.minY + b.maxY) / 2;
    }

    // Each segment is [points, color, width], points a [[lat,lon],...]
    // polyline (2 points for a straight-line fallback, more for a real
    // road curve resolved from data/tmc_roads.topojson). Bottleneck-layer
    // segments carry two more elements — corridorId (4) and popup (5, a
    // {name, severity, worstDate, worstTime} object) — used for the hover
    // tooltip and click-to-navigate-to-corridor feature below; segments
    // without a popup (the Collector/Local watch layer, which has no
    // corresponding Corridors-tab section) simply aren't hit-testable.
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    var segs = (cfg.segments || []).map(function (s) {
      var worldPts = s[0].map(function (pt) {
        var wx = lonToWorldX(pt[1]), wy = latToWorldY(pt[0]);
        if (wx < minX) minX = wx; if (wx > maxX) maxX = wx;
        if (wy < minY) minY = wy; if (wy > maxY) maxY = wy;
        return [wx, wy];
      });
      return { points: worldPts, color: s[1], width: s[2], corridorId: s[3], popup: s[4] };
    }).sort(function (a, b) { return a.width - b.width; }); // widest (most important) drawn last

    // Point markers — [lat, lon, color, radiusPx, label] — for maps that
    // rank discrete locations (e.g. the Intersection Severity tab) rather
    // than coloring road segments. radiusPx is a fixed screen size (not
    // scaled by zoom, same reasoning as line width: a marker should stay
    // a consistent, readable size at any zoom level).
    var markers = (cfg.points || []).map(function (p) {
      var wx = lonToWorldX(p[1]), wy = latToWorldY(p[0]);
      if (wx < minX) minX = wx; if (wx > maxX) maxX = wx;
      if (wy < minY) minY = wy; if (wy > maxY) maxY = wy;
      return { wx: wx, wy: wy, color: p[2], radius: p[3], label: p[4] };
    });

    var countyRings = (cfg.countyRings || []).map(function (ring) {
      return ring.map(function (pt) { return [lonToWorldX(pt[1]), latToWorldY(pt[0])]; });
    });

    if (minX < Infinity) {
      fitBounds({ minX: minX, minY: minY, maxX: maxX, maxY: maxY });
    }

    function currentScale() { return Math.pow(2, view.zoom) * dpr; }
    function screenToWorld(sx, sy) {
      var s = currentScale();
      return [view.cx + (sx * dpr - (w * dpr) / 2) / s, view.cy + (sy * dpr - (h * dpr) / 2) / s];
    }

    // Perpendicular distance (world units) from point p to segment a->b,
    // clamped to the segment's own extent rather than the infinite line.
    function distanceToSegmentWorld(px, py, ax, ay, bx, by) {
      var dx = bx - ax, dy = by - ay;
      var lengthSq = dx * dx + dy * dy;
      var t = lengthSq > 0 ? ((px - ax) * dx + (py - ay) * dy) / lengthSq : 0;
      t = Math.max(0, Math.min(1, t));
      var cx = ax + t * dx, cy = ay + t * dy;
      var ex = px - cx, ey = py - cy;
      return Math.sqrt(ex * ex + ey * ey);
    }

    var HOVER_PX = 6; // hit-test tolerance, in CSS pixels regardless of zoom
    function hitTestSegment(worldX, worldY) {
      var thresholdWorld = HOVER_PX / currentScale();
      var best = null, bestDist = thresholdWorld;
      segs.forEach(function (seg) {
        if (!seg.popup) return;
        for (var i = 1; i < seg.points.length; i++) {
          var d = distanceToSegmentWorld(
            worldX, worldY,
            seg.points[i - 1][0], seg.points[i - 1][1], seg.points[i][0], seg.points[i][1]
          );
          if (d < bestDist) { bestDist = d; best = seg; }
        }
      });
      return best;
    }

    var renderScheduled = false;
    function scheduleRender() {
      if (renderScheduled) return;
      renderScheduled = true;
      requestAnimationFrame(function () { renderScheduled = false; render(); });
    }

    function drawTiles() {
      var W = w * dpr, H = h * dpr;
      tctx.setTransform(1, 0, 0, 1, 0, 0);
      tctx.fillStyle = '#e8edf1';
      tctx.fillRect(0, 0, W, H);
      var z = Math.max(2.5, Math.min(18, view.zoom));
      var tileZ = Math.max(0, Math.min(19, Math.round(z)));
      var scaleAdj = Math.pow(2, view.zoom - tileZ) * dpr;
      var centerXAtTileZ = view.cx * Math.pow(2, tileZ);
      var centerYAtTileZ = view.cy * Math.pow(2, tileZ);
      var topLeftX = centerXAtTileZ - (W / 2) / scaleAdj;
      var topLeftY = centerYAtTileZ - (H / 2) / scaleAdj;
      var bottomRightX = centerXAtTileZ + (W / 2) / scaleAdj;
      var bottomRightY = centerYAtTileZ + (H / 2) / scaleAdj;
      var xStart = Math.floor(topLeftX / TILE_SIZE) - 1, xEnd = Math.floor(bottomRightX / TILE_SIZE) + 1;
      var yStart = Math.floor(topLeftY / TILE_SIZE) - 1, yEnd = Math.floor(bottomRightY / TILE_SIZE) + 1;
      for (var tx = xStart; tx <= xEnd; tx++) {
        for (var ty = yStart; ty <= yEnd; ty++) {
          var entry = getTile(tileZ, tx, ty, scheduleRender);
          if (!entry) continue;
          var px = (tx * TILE_SIZE - topLeftX) * scaleAdj;
          var py = (ty * TILE_SIZE - topLeftY) * scaleAdj;
          var sz = TILE_SIZE * scaleAdj;
          if (entry.status === 'ok') {
            tctx.drawImage(entry.img, Math.round(px), Math.round(py), Math.ceil(sz) + 1, Math.ceil(sz) + 1);
          }
        }
      }
    }

    function drawData() {
      var W = w * dpr, H = h * dpr;
      dctx.setTransform(1, 0, 0, 1, 0, 0);
      dctx.clearRect(0, 0, W, H);
      var s = currentScale();
      dctx.setTransform(s, 0, 0, s, -view.cx * s + W / 2, -view.cy * s + H / 2);
      dctx.lineCap = 'round';
      dctx.lineJoin = 'round';

      dctx.strokeStyle = 'rgba(82,81,78,0.55)';
      dctx.lineWidth = 1.2 / s;
      countyRings.forEach(function (ring) {
        dctx.beginPath();
        ring.forEach(function (pt, i) {
          if (i === 0) dctx.moveTo(pt[0], pt[1]); else dctx.lineTo(pt[0], pt[1]);
        });
        dctx.stroke();
      });

      segs.forEach(function (seg) {
        dctx.strokeStyle = seg.color;
        dctx.lineWidth = Math.max(0.6, seg.width) / s;
        dctx.beginPath();
        seg.points.forEach(function (pt, i) {
          if (i === 0) dctx.moveTo(pt[0], pt[1]); else dctx.lineTo(pt[0], pt[1]);
        });
        dctx.stroke();
      });

      markers.forEach(function (m) {
        var r = (m.radius || 6) / s; // fixed screen-pixel size regardless of zoom, same reasoning as line width
        dctx.fillStyle = m.color;
        dctx.strokeStyle = '#ffffff';
        dctx.lineWidth = 1.5 / s;
        dctx.beginPath();
        dctx.arc(m.wx, m.wy, r, 0, Math.PI * 2);
        dctx.fill();
        dctx.stroke();
      });
    }

    function render() { drawTiles(); drawData(); }

    if (cfg.interactive) {
      var tooltipEl = document.createElement('div');
      tooltipEl.className = 'tile-map-tooltip';
      tooltipEl.style.display = 'none';
      if (wrapEl) wrapEl.appendChild(tooltipEl);

      function showTooltip(seg, sx, sy) {
        var p = seg.popup;
        var severityText = (typeof p.severity === 'number')
          ? p.severity.toLocaleString(undefined, { maximumFractionDigits: 2 })
          : p.severity;
        // Only advertise the click-to-navigate action when this segment
        // actually has a corridor to jump to — pages with no Corridors
        // tab (e.g. the standalone Intersection Congestion Severity page)
        // leave corridorId null on every segment, and a click there is a
        // no-op, so showing the hint would be misleading.
        var hintHtml = seg.corridorId
          ? '<div class="hint">Click to view corridor details &rarr;</div>'
          : '';
        tooltipEl.innerHTML =
          '<strong>' + p.name + '</strong>' +
          '<div>Severity Index: ' + severityText + '</div>' +
          '<div>Worst congestion: ' + p.worstTime + ' on ' + p.worstDate + '</div>' +
          hintHtml;
        tooltipEl.style.display = 'block';
        // Keep the tooltip on-screen near the cursor, flipping to the left
        // of the pointer once it would otherwise overflow the map's right
        // edge (tooltip width is capped at 240px in CSS).
        var left = (sx + 254 > w) ? sx - 254 : sx + 14;
        tooltipEl.style.left = Math.max(4, left) + 'px';
        tooltipEl.style.top = Math.max(4, sy - 10) + 'px';
      }
      function hideTooltip() { tooltipEl.style.display = 'none'; }

      var isDragging = false, lastX = 0, lastY = 0, dragStartX = 0, dragStartY = 0, dragMoved = false;
      dataCanvas.style.cursor = 'grab';
      dataCanvas.addEventListener('mousedown', function (e) {
        isDragging = true; lastX = e.clientX; lastY = e.clientY;
        dragStartX = e.clientX; dragStartY = e.clientY; dragMoved = false;
        dataCanvas.style.cursor = 'grabbing';
        hideTooltip();
      });
      window.addEventListener('mousemove', function (e) {
        if (!isDragging) return;
        var dx = e.clientX - lastX, dy = e.clientY - lastY;
        lastX = e.clientX; lastY = e.clientY;
        if (Math.abs(e.clientX - dragStartX) > 3 || Math.abs(e.clientY - dragStartY) > 3) dragMoved = true;
        var s = currentScale();
        view.cx -= dx * dpr / s; view.cy -= dy * dpr / s;
        scheduleRender();
      });
      window.addEventListener('mouseup', function (e) {
        var wasClick = isDragging && !dragMoved;
        isDragging = false; dataCanvas.style.cursor = 'grab';
        if (!wasClick) return;
        var rect = dataCanvas.getBoundingClientRect();
        var sx = e.clientX - rect.left, sy = e.clientY - rect.top;
        if (sx < 0 || sx > w || sy < 0 || sy > h) return;
        var world = screenToWorld(sx, sy);
        var hit = hitTestSegment(world[0], world[1]);
        if (hit && hit.corridorId && window.showTab && window.showCorridor) {
          window.showTab('tab-corridors');
          window.showCorridor('corridor-' + hit.corridorId);
          var target = document.getElementById('corridor-' + hit.corridorId);
          if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      });
      dataCanvas.addEventListener('mousemove', function (e) {
        if (isDragging) return;
        var rect = dataCanvas.getBoundingClientRect();
        var sx = e.clientX - rect.left, sy = e.clientY - rect.top;
        var world = screenToWorld(sx, sy);
        var hit = hitTestSegment(world[0], world[1]);
        if (hit) {
          dataCanvas.style.cursor = 'pointer';
          showTooltip(hit, sx, sy);
        } else {
          dataCanvas.style.cursor = 'grab';
          hideTooltip();
        }
      });
      dataCanvas.addEventListener('mouseleave', hideTooltip);
      dataCanvas.addEventListener('wheel', function (e) {
        e.preventDefault();
        var rect = dataCanvas.getBoundingClientRect();
        var sx = e.clientX - rect.left, sy = e.clientY - rect.top;
        var before = screenToWorld(sx, sy);
        view.zoom = Math.max(2.5, Math.min(18, view.zoom - e.deltaY * 0.0022));
        var after = screenToWorld(sx, sy);
        view.cx += before[0] - after[0]; view.cy += before[1] - after[1];
        scheduleRender();
      }, { passive: false });

      var zoomInBtn = cfg.zoomInId && document.getElementById(cfg.zoomInId);
      var zoomOutBtn = cfg.zoomOutId && document.getElementById(cfg.zoomOutId);
      function stepZoom(delta) {
        view.zoom = Math.max(2.5, Math.min(18, view.zoom + delta));
        scheduleRender();
      }
      if (zoomInBtn) zoomInBtn.addEventListener('click', function () { stepZoom(1); });
      if (zoomOutBtn) zoomOutBtn.addEventListener('click', function () { stepZoom(-1); });
    }

    if (cfg.responsive && wrapEl && typeof ResizeObserver !== 'undefined') {
      var lastWidth = w;
      var resizeObserver = new ResizeObserver(function () {
        var next = measuredWidth();
        // Ignore sub-pixel jitter (ResizeObserver can fire on rounding
        // noise) and skip work while the tab/section is hidden (width 0).
        if (next <= 0 || Math.abs(next - lastWidth) < 1) return;
        lastWidth = w = next;
        applyCanvasSize();
        scheduleRender(); // pan/zoom (view.cx/cy/zoom) untouched on resize
      });
      resizeObserver.observe(wrapEl);
    }

    scheduleRender();
    var pollCount = 0;
    var pollTimer = setInterval(function () {
      // Also re-check size here, not just on ResizeObserver: this map's
      // tab can be display:none at page load (Region Map isn't the
      // default-active tab), where getBoundingClientRect() measures 0 —
      // handled by measuredWidth()'s cfg.width fallback at init — and
      // while ResizeObserver generally does fire once the tab becomes
      // visible and the wrapper gets real layout size, this poll is a
      // belt-and-suspenders catch for that transition regardless of any
      // given browser's exact display:none/ResizeObserver timing.
      if (cfg.responsive && wrapEl) {
        var current = measuredWidth();
        if (current > 0 && Math.abs(current - w) >= 1) {
          w = current;
          applyCanvasSize();
        }
      }
      scheduleRender();
      pollCount++;
      if (pollCount > 30) clearInterval(pollTimer); // ~15s covers tile load stragglers
    }, 500);
  }

  window.createTileMap = createTileMap;
})();
"""

_TAB_JS = """
// Plotly sizes a chart to its container at draw time. A container that is
// display:none at that moment (every tab/corridor panel except the one
// active on page load) yields a zero-size chart that never fixes itself
// when later shown — so every time a panel becomes visible we have to
// explicitly ask Plotly to resize the plots inside it.
function resizePlotsIn(root) {
  if (!root) return;
  root.querySelectorAll('.plotly-graph-div').forEach(function (el) {
    if (window.Plotly) {
      try { window.Plotly.Plots.resize(el); } catch (e) { /* not yet drawn */ }
    }
  });
}
function showTab(id) {
  document.querySelectorAll('section.tabpanel').forEach(function (el) {
    el.classList.toggle('active', el.id === id);
  });
  document.querySelectorAll('nav.tabs button').forEach(function (btn) {
    btn.classList.toggle('active', btn.dataset.tab === id);
  });
  resizePlotsIn(document.getElementById(id));
}
function showCorridor(id) {
  document.querySelectorAll('.corridor-detail').forEach(function (el) {
    el.classList.toggle('active', el.id === id);
  });
  document.querySelectorAll('.corridor-sidebar button').forEach(function (btn) {
    btn.classList.toggle('active', btn.dataset.corridor === id);
  });
  resizePlotsIn(document.getElementById(id));
}
document.addEventListener('DOMContentLoaded', function () {
  resizePlotsIn(document.getElementById('tab-metadata'));
});
"""


def _kpi_from_max(df: pl.DataFrame, label_col: str, value_col: str = "total_mile_hours"):
    if df.is_empty():
        return "N/A", "N/A"
    row = df.sort(value_col, descending=True).row(0, named=True)
    return row[label_col], row[value_col]


def _int(n) -> str:
    return f"{n:,}"


def _build_metadata_tab_html(counts: dict) -> str:
    audiences = [
        (
            "ARC Transportation Planners",
            "Identify where recurring congestion concentrates across the 21-county Atlanta "
            "region — by corridor, county, and time of day — to ground Atlanta Region's Plan "
            "and Transportation Improvement Program (TIP) investment choices in measured "
            "severity rather than anecdote.",
        ),
        (
            "GDOT & Local Traffic Engineers",
            "Pinpoint the exact metro Atlanta segments and hours where queues form — on I-285, "
            "the Downtown Connector, GA-400, and the region's other arterials — to target "
            "signal retiming, geometric improvements, ramp metering, or incident management.",
        ),
        (
            "ARC (MPO) & GDOT Program Managers",
            "Road project prioritization and STIP/TIP funding decisions for the region in "
            "measured, corridor-by-corridor congestion severity specific to metro Atlanta.",
        ),
        (
            "Regional Policy Makers",
            "Use Atlanta-region seasonal, weekday, and hourly congestion patterns as evidence "
            "when weighing policy levers — pricing, land use, freight routing, work-hour "
            "flexibility — for the 21-county planning area.",
        ),
        (
            "County & City Officials",
            "Answer constituents' questions about specific roads and intersections in Fulton, "
            "DeKalb, Cobb, Gwinnett, and the other 17 counties in ARC's region with data, and "
            "communicate where and why improvements are (or aren't yet) planned.",
        ),
        (
            "Researchers & the Public",
            "A transparent, reproducible baseline of how congestion actually behaves across "
            "metro Atlanta's Interstate, Arterial, Collector, and Local network.",
        ),
    ]
    audience_html = "".join(
        f"<div class='audience-card'><div class='who'>{html.escape(who)}</div>"
        f"<div class='what'>{html.escape(what)}</div></div>"
        for who, what in audiences
    )

    groups = counts["corridors_by_group"]
    watch = counts["watch_by_class"]
    date_range = (
        f"{counts['date_min'].isoformat()} to {counts['date_max'].isoformat()} "
        f"({counts['date_count']} analyzed days)"
        if counts["date_min"]
        else "N/A"
    )

    counts_rows = [
        ("Road segments in the source network (TMC records)", _int(counts["tmc_total"])),
        (
            "Speed observations read region-wide (5-minute probe records)",
            f"{_int(counts['probe_total'])} "
            f"({counts['probe_min'].date().isoformat()} – {counts['probe_max'].date().isoformat()})",
        ),
        (
            "Speed observations analyzed for this report (corridors + watch segments)",
            _int(counts["probe_analyzed"]),
        ),
        (
            "Corridors analyzed (full pipeline)",
            f"{_int(counts['corridors_total'])} "
            f"({groups.get('Interstate', 0)} Interstate, {groups.get('Expressway', 0)} Expressway, "
            f"{groups.get('Arterial', 0)} Arterial)",
        ),
        ("Corridor segments analyzed", _int(counts["segments_total"])),
        (
            "Watch-list segments (lighter per-segment summary)",
            f"{_int(counts['watch_total'])} "
            f"({watch.get('Collector', 0)} Collector, {watch.get('Local', 0)} Local)",
        ),
        ("Congestion events detected", _int(counts["events_total"])),
        ("Recurring bottlenecks identified", _int(counts["bottlenecks_total"])),
        ("Daily bottleneck characterization records", _int(counts["daily_metrics_total"])),
        ("Analysis period", date_range),
    ]
    counts_table = "".join(
        f"<tr><td class='k'>{html.escape(k)}</td><td class='v'>{v}</td></tr>"
        for k, v in counts_rows
    )

    return f"""
    <div class="prose">
      <h3>What this report is</h3>
      <p>This is the Atlanta Regional Commission's (ARC) CBI regional congestion report: an
      automated, data-driven analysis of recurring traffic congestion across Interstates,
      Arterials, and a watch list of the busiest Collector and Local roads in ARC's
      21-county metro Atlanta planning area. It is built directly from 5-minute interval
      probe vehicle speed data, not survey or anecdotal input — every number in the other
      tabs traces back to the record counts below.</p>

      <div class="attribution-box">
        <strong>Methodology attribution.</strong> The bottleneck-detection methodology
        applied here — recurring-bottleneck identification from probe-speed thresholds,
        and a severity index combining queue mile-hours, occurrence frequency, and speed
        drop — originates from the <strong>Congestion and Bottleneck Identification (CBI)
        Tool</strong>, developed by the <strong>Federal Highway Administration (FHWA)</strong>,
        U.S. Department of Transportation, Turner-Fairbank Highway Research Center. FHWA is
        acknowledged and credited as the source of the CBI methodology this report applies.
        <br /><br />
        This report is <strong>not</strong> the official FHWA CBI Tool software and should
        not be represented as such — it is an independent, custom-built implementation of
        the published CBI methodology, adapted and altered by ARC for the Atlanta region's
        specific corridor network, data sources, and reporting needs. Per the CBI Tool
        Software License Agreement, this adaptation is plainly identified here as an
        altered version of the FHWA CBI concept, not a redistribution of the original
        software, and FHWA / the U.S. Government do not endorse this implementation.
      </div>

      <h3>Data source</h3>
      <p><strong>NPMRDS (National Performance Management Research Data Set) from INRIX</strong>,
      covering passenger vehicles and trucks. Scope: the 21 counties in ARC's Atlanta region
      planning area, 17,388 TMC (Traffic Message Channel) road segments, weekdays only
      (Monday&ndash;Friday — no weekend data exists in the source, which is why weekday and
      hourly breakdowns in this report never include Saturday/Sunday) from January 1
      through December 31, 2025. Source probe fields: speed, historical average speed,
      reference (free-flow) speed, travel time, and data density; volume/AADT figures are
      from NPMRDS2 2025. Road functional classification (Interstate / Expressway / Arterial
      / Collector / Local) follows the FHWA <code>f_system</code> code carried in the TMC
      metadata.</p>

      <h3>Methodology, in brief</h3>
      <p>For each analyzed corridor, every day's speed readings are scanned for periods
      where traffic drops below 70% of free-flow reference speed. Recurring patterns
      across the year are grouped into discrete <strong>recurring bottlenecks</strong> —
      specific locations that congest on a predictable basis — and ranked by a severity
      index combining how often they occur, how large a queue they produce, and how much
      speed drops. A small number of corridors — typically beltways like I-285, where
      congestion runs fairly uniformly along a long stretch rather than concentrating at
      one point — fall back to a segment-by-segment occurrence profile instead of a single
      ranked list when no one location stands out enough to qualify as a discrete peak.</p>

      <div class="attribution-box">
        <strong>Reading Arterial results: signals and stop signs.</strong> The 70%-of-free-flow
        threshold was designed for freeways, where free-flow speed is a meaningful baseline
        because nothing routinely stops traffic. On signalized Arterials it isn't as clean a
        signal &mdash; a normal stop at a red light or stop sign already pulls speed well below
        free flow, and this methodology has no way to separate that from a real, abnormal
        bottleneck at the same location. The data bears this out directly: across this
        region's analyzed corridors, the <em>average</em> Interstate segment sits at 59.6%
        occurrence (the wide majority of days it stays above the congestion threshold, as
        expected on a freeway), while the <em>average</em> Arterial segment sits at 92.5%
        occurrence, and 89.5% of all Arterial segments are flagged &ge;80% of analyzed days
        &mdash; versus 44% for Interstates. A same-day sample of raw probe speeds shows why:
        average speed runs about 89% of free-flow on Interstates, but only about 62&ndash;68%
        of free-flow on Arterials &mdash; already under the 70% congestion line on an
        ordinary day, before counting any actual incident or overflow congestion.
        <br /><br />
        In practice, this means an Arterial's <strong>occurrence percentage</strong> should be
        read as "how often this location sees sustained slow travel, including routine
        signal/stop-sign operation" rather than "how often something is abnormally wrong"
        the way it can be read on a freeway. The <strong>ranking</strong> within Arterials is
        still meaningful for comparing locations against each other, and the peak hour/day
        patterns still point at real commute-driven congestion layered on top of that
        baseline &mdash; but absolute occurrence numbers are not directly comparable between
        Arterials and Interstates/Expressways, and a 95&ndash;100% Arterial occurrence figure
        does not mean that location is congested nearly as severely as a 95&ndash;100%
        Interstate figure would. This is a known, general limitation of applying
        freeway-oriented bottleneck detection to signalized roads, not specific to this
        implementation — field verification (signal timing plans, turning movement counts)
        is recommended before acting on any single Arterial location in isolation.
        <br /><br />
        Because this effect is strongest on rural, lower-lane-count Arterials — fewer through
        lanes concentrate traffic through the same signals and stop signs that inflate the
        occurrence baseline — <strong>Arterial corridors with any rural segment are excluded
        from the region-wide Top Bottlenecks ranking</strong> shown elsewhere in this report,
        so they don't outrank genuinely severe freeway interchange bottlenecks on a metric
        that isn't directly comparable. Their own bottlenecks are still fully analyzed and
        reported in each corridor's own section and Word report — only their participation
        in the network-wide ranking is affected.
        <br /><br />
        That corridor-level rule catches rural Arterials, but the same underlying mechanism —
        a flat, non-time-varying reference speed that misreads ordinary signal-cycle or short
        ramp-segment noise as congestion — also shows up on individual bottlenecks located on
        otherwise fully-urban Arterial corridors. A second, more direct check now runs
        per-bottleneck: any Arterial-group bottleneck where at least 15% of overnight (12
        AM&ndash;4 AM) readings are still flagged "congested" — when there is essentially no
        traffic to cause a real queue — is excluded from the ranking and from the region map's
        bottleneck layer individually, regardless of which corridor it's on. Two other proxies
        were tried and rejected before this: a bottleneck's extent spanning its entire corridor
        (rejected — flagged genuine short interchange-approach bottlenecks, e.g. Camp Creek
        Pkwy at I-285) and low AADT as a stand-in for sparse, unreliable sampling (rejected —
        tested directly against each reading's data-density classification, and the two run in
        the <em>opposite</em> direction from what that theory predicts). The overnight-reading
        test is the one signal confirmed to track the actual mechanism directly.
      </div>

      <div class="attribution-box">
        <strong>AADT-weighted severity.</strong> severity_index (occurrence &times; annual queue
        mile-hours &times; speed drop) has no traffic-volume term — it's purely how many
        roadway-miles read below threshold speed, how often, and how far below, regardless of how
        many vehicles are actually on that road. Investigated directly: two Arterial bottlenecks
        (Jot Em Down Rd on US-19, Sharon Rd on GA-141, 37,000&ndash;42,000 AADT) ranked in the
        region-wide top 10 alongside Interstate interchanges carrying 163,000&ndash;371,000 AADT —
        a 5&ndash;9x volume gap — with comparable or higher severity_index despite affecting far
        fewer vehicles. Checked whether this was the same reference-speed-threshold artifact via
        occurrence-profile flatness across each bottleneck's own extent; it wasn't a reliable
        signal — confirmed-genuine short bottlenecks (Camp Creek Pkwy, GA-5-Conn, Abernathy Rd)
        and even the #1&ndash;2 Interstate entries are equally flat, so flatness doesn't
        distinguish artifact from genuine. Volume does: <strong>aadt_weighted_severity_index</strong>
        multiplies severity_index by (peak-segment AADT &divide; 100,000 — close to the median AADT
        across ranking-eligible bottlenecks), leaving a typical-volume bottleneck roughly unchanged
        while discounting low-volume ones and boosting high-volume ones proportional to actual
        vehicles affected. This is what the region-wide ranking, the Intersection Severity Index,
        and the region map's color classification are now ordered/clustered by — severity_index
        itself is unchanged and still shown alongside for methodological transparency against the
        FHWA-attributed original formula.
      </div>

      <h3>How this helps ARC</h3>
      <div class="audience-grid">{audience_html}</div>

      <h3>Data structure and scope of this report</h3>
      <p>Records "read" are the full regional dataset queried; records "analyzed" are the
      subset that fed into the corridors, segments, and events shown in this report.</p>
      <table class="kv">{counts_table}</table>
    </div>
    """


def _load_intersection_severity_index() -> pl.DataFrame:
    """Arterial bottlenecks grouped by physical intersection (representative_
    intersection) so a location that congests in both directions — or where
    a cross corridor was independently analyzed — reads as one combined
    score instead of competing with itself as separate directional entries.
    Only ranking_eligible bottlenecks contribute; see
    sql/008_intersection_severity_index.sql."""
    return pl.read_database_uri(
        query='SELECT * FROM "Year_2025".vw_intersection_severity_index ORDER BY intersection_severity_rank',
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )


def _build_intersection_severity_tab_html(
    intersections: pl.DataFrame, bottleneck_segments: pl.DataFrame
) -> str:
    if intersections.is_empty():
        return "<p class='muted'>No intersection-level data available yet.</p>"

    # Same segment-layer treatment as the Region Map tab (bottleneck extents
    # colored by K-means-clustered, AADT-weighted severity on the Google
    # Maps traffic ramp), just filtered to the Arterial-group bottlenecks
    # that actually feed the intersection index — real road geometry, not
    # a schematic point marker per intersection.
    arterial_segments = bottleneck_segments.filter(pl.col("corridor_group") == "Arterial")
    segment_records, legend = _bottleneck_records_and_legend(arterial_segments)

    map_html = _canvas_map_html(
        segments=segment_records, height=560, width=1180, interactive=True,
        county_rings=_load_county_boundary_rings(), responsive=True,
    )
    severity_items = "".join(
        f"<div class='map-legend-item'><span class='swatch dot' style='background:{color}'></span>{label}</div>"
        for color, label in legend
    )
    map_with_legend = f"""
    <div class='map-with-legend'>
      <div class='map-explain'>
        <h4>Reading this map</h4>
        <p>Colored segments are the exact bottleneck extents contributing to each intersection's
        score — same real road geometry, same Google Maps-style traffic ramp, and same K-means
        classing as the Region Map tab, so the two are directly comparable at a glance.</p>
        <p>Only Arterial-road bottlenecks that passed the overnight-congestion false-flag check
        are included (see the Metadata tab) — Interstates and Expressways don't have
        signal-controlled cross intersections in this sense, so they're out of scope for this
        specific view (they're still fully covered in Top Bottlenecks and the Region Map).</p>
      </div>
      {map_html}
      <div class='map-legend'>
        <h4>Intersection Severity<br/>(AADT-weighted, K-means clustered)</h4>
        {severity_items}
      </div>
    </div>
    """

    header = (
        "<tr><th>Rank</th><th>Intersection</th><th>Contributing Directions</th>"
        "<th>Corridors</th><th>County</th><th>CID</th><th>Max AADT</th><th>Avg Occurrence %</th>"
        "<th>Total Annual Mile-Hrs</th><th>Avg Speed Ratio</th><th>Severity (geometric)</th>"
        "<th>Severity (AADT-weighted)</th></tr>"
    )
    rows = "".join(
        f"<tr>"
        f"<td>{row['intersection_severity_rank']}</td>"
        f"<td>{html.escape(str(row['representative_intersection'] or ''))}</td>"
        f"<td>{row['contributing_bottlenecks']}</td>"
        f"<td>{html.escape(str(row['corridors']))}</td>"
        f"<td>{html.escape(str(row['county'] or '-'))}</td>"
        f"<td>{html.escape(str(row['cid_name'] or '-'))}</td>"
        f"<td>{row['max_contributing_aadt'] or '-'}</td>"
        f"<td>{_fmt2(row['avg_occurrence_pct'])}%</td>"
        f"<td>{_fmt2(row['total_annual_queue_mile_hours'])}</td>"
        f"<td>{_fmt2(row['avg_congested_speed_ratio'])}</td>"
        f"<td>{_fmt2(row['intersection_severity_index'])}</td>"
        f"<td>{_fmt2(row['aadt_weighted_intersection_severity_index'])}</td>"
        "</tr>"
        for row in intersections.iter_rows(named=True)
    )

    return f"""
    <h3 class="section-title">Intersection Severity Index — Arterial Roads</h3>
    <p class="muted">The Top Bottlenecks tab ranks individual, per-direction bottlenecks. This
    combines bottlenecks that share the same physical intersection across directions or
    corridors into a single location-level score, restricted to Arterial roads and to
    bottlenecks not already excluded by the overnight-congestion false-flag check — so an
    intersection genuinely congested in both directions ranks above one that only looks bad in a
    single direction, without artifacts inflating the score. <strong>CID</strong> is the
    Community Improvement District (ARC Open Data, region-wide) the intersection's peak segment
    falls within, shown as '-' outside every CID boundary.</p>
    <div class="card map-card">{map_with_legend}</div>
    <p class="map-legend-note"><strong>Occurrence caution:</strong> On signalized corridors,
    occurrence includes sustained low-speed observations associated with normal signal-cycle
    operation as well as demand-driven queueing. A 100% occurrence value should therefore not be
    interpreted as continuous or abnormal congestion on every analyzed day. See Methodology for
    details.</p>
    <h3 class="section-title">Ranked Intersections ({intersections.height})</h3>
    <table class="data">{header}{rows}</table>
    """


def _build_top_bottlenecks_tab_html(rankings: pl.DataFrame) -> str:
    if rankings.is_empty():
        return "<p class='muted'>No recurring bottlenecks identified yet.</p>"

    top10 = rankings.head(10)
    spotlight_cards = "".join(
        f"""<div class='bottleneck-card'>
              <div class='rank'>#{row['network_severity_rank']}</div>
              <div class='body'>
                <div class='name'>{html.escape(str(row['representative_intersection'] or ''))}</div>
                <div class='corridor'>{html.escape(str(row['corridor_name']))}
                  &middot; {html.escape(str(row['county'] or 'unknown county'))} County</div>
                <div class='desc'>Analytics show that this segment experiences congestion on
                  {_fmt2(row['occurrence_pct'])}% of the {row['analyzed_days'] or 261} weekdays
                  analyzed, with congestion peaking at
                  {html.escape(str(row['peak_hour_label'] or 'an unrecorded time'))} on
                  {html.escape(str(row['peak_weekday_name']) + 's') if row['peak_weekday_name'] else 'an unrecorded day'}.
                  The total annual queue mile-hours are {_fmt2(row['annual_queue_mile_hours'])},
                  with an average speed reduction of
                  {_fmt2((1 - row['avg_congested_speed_ratio']) * 100 if row['avg_congested_speed_ratio'] is not None else None)}%
                  below free-flow speed.</div>
              </div>
            </div>"""
        for row in top10.iter_rows(named=True)
    )

    header = (
        "<tr><th>Rank</th><th>Location</th><th>Corridor</th><th>Class</th><th>County</th><th>CID</th>"
        "<th>AADT</th><th>Occurrence %</th><th>Peak Hour</th><th>Peak Day</th>"
        "<th>Annual Mile-Hrs</th><th>Avg Speed Ratio</th><th>Severity (geometric)</th>"
        "<th>Severity (AADT-weighted)</th></tr>"
    )
    rows = "".join(
        f"<tr class='{'top10' if row['network_severity_rank'] <= 10 else ''}'>"
        f"<td>{row['network_severity_rank']}</td>"
        f"<td>{html.escape(str(row['representative_intersection'] or ''))}</td>"
        f"<td>{html.escape(str(row['corridor_name']))}</td>"
        f"<td>{html.escape(str(row['corridor_group']))}</td>"
        f"<td>{html.escape(str(row['county'] or '-'))}</td>"
        f"<td>{html.escape(str(row['cid_name'] or '-'))}</td>"
        f"<td>{row['aadt'] or '-'}</td>"
        f"<td>{_fmt2(row['occurrence_pct'])}%</td>"
        f"<td>{html.escape(str(row['peak_hour_label'] or '-'))}</td>"
        f"<td>{html.escape(str(row['peak_weekday_name'] or '-'))}</td>"
        f"<td>{_fmt2(row['annual_queue_mile_hours'])}</td>"
        f"<td>{_fmt2(row['avg_congested_speed_ratio'])}</td>"
        f"<td>{_fmt2(row['severity_index'])}</td>"
        f"<td>{_fmt2(row['aadt_weighted_severity_index'])}</td>"
        "</tr>"
        for row in rankings.iter_rows(named=True)
    )

    return f"""
    <p class="muted">Every recurring bottleneck identified across all {rankings.height} analyzed
    corridors, ranked region-wide by AADT-weighted severity (geometric severity index — occurrence
    &times; annual queue mile-hours &times; speed drop — scaled by peak-segment traffic volume
    relative to the region's typical bottleneck; see the Metadata tab for the full methodology and
    why volume weighting was added). The top 10 are highlighted below and in the full list.</p>
    <div class="bottleneck-spotlight">{spotlight_cards}</div>
    <h3 class="section-title">Full Regional Ranking ({rankings.height} bottlenecks)</h3>
    <table class="data">{header}{rows}</table>
    """


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def generate_regional_report(
    connection: psycopg.Connection,
    output_root: Path,
) -> Path:
    global _FIG_COUNTER
    _FIG_COUNTER = 0

    metadata_counts = _load_metadata_counts(connection)

    monthly = _load_region_monthly()
    weekday = _load_region_weekday()
    hourly = _load_region_hourly()
    county = _load_region_county()
    watch = _load_watch_segments()

    corridor_segments = _load_corridor_map_segments()
    bottleneck_segments = _load_bottleneck_map_segments()
    # Classified once, region-wide (not per corridor/day) — every map in
    # this report that needs to highlight a subset (Corridors tab
    # mini-maps, the Worst Day spotlight map below) filters this same
    # already-fitted record set rather than re-fitting K-means on a small
    # subset, which would rescale the color ramp locally and break "one
    # color means the same severity everywhere in this report."
    all_bottleneck_records, _ = _bottleneck_records_and_legend(bottleneck_segments)

    corridors = get_active_corridors(connection)

    # --- Metadata tab --------------------------------------------------------
    metadata_html = _build_metadata_tab_html(metadata_counts)

    # --- Top Bottlenecks tab --------------------------------------------------
    bottleneck_rankings = _load_regional_bottleneck_rankings()
    intersection_severity = _load_intersection_severity_index()
    top_bottlenecks_html = _build_top_bottlenecks_tab_html(bottleneck_rankings)
    intersection_severity_html = _build_intersection_severity_tab_html(intersection_severity, bottleneck_segments)

    # --- General tab -------------------------------------------------------
    month_labels = [d.strftime("%b %Y") for d in monthly["month"].to_list()]
    highest_month_label, highest_month_value = _kpi_from_max(
        monthly.with_columns(pl.Series("month_label", month_labels)), "month_label"
    )

    weekday_sorted = weekday.with_columns(
        pl.col("weekday_name").cast(pl.Enum(WEEKDAY_ORDER)).alias("_order")
    ).sort("_order")
    worst_weekday_label, worst_weekday_value = _kpi_from_max(weekday, "weekday_name")

    hour_labels = [f"{h:02d}:00" for h in hourly["hour_of_day"].to_list()]
    worst_hour_label, worst_hour_value = _kpi_from_max(
        hourly.with_columns(pl.Series("hour_label", hour_labels)), "hour_label"
    )

    fig_month = _single_series_bar(
        month_labels, monthly["total_mile_hours"].to_list(),
        "Congestion by Month — Total Mile-Hours", width=700, height=400,
    )
    fig_weekday = _single_series_bar(
        weekday_sorted["weekday_name"].to_list(),
        weekday_sorted["total_mile_hours"].to_list(),
        "Congestion by Day of Week — Total Mile-Hours",
        width=700, height=400,
    )
    fig_hour = _single_series_bar(
        hour_labels, hourly["total_mile_hours"].to_list(),
        "Congestion by Hour of Day — Total Mile-Hours", width=700, height=400,
    )
    county_top = county.head(15)
    fig_county = _single_series_bar(
        county_top["county"].to_list(), county_top["total_mile_hours"].to_list(),
        "Congestion by County — Total Mile-Hours (Top 15)", width=700, height=400,
    )

    hourly_speed_baselines = _load_hourly_baselines(connection)
    fig_hourly_speed = _hourly_speed_by_class_figure(hourly_speed_baselines)

    general_html = f"""
    <div class="kpi-row">
      <div class="kpi-card"><div class="label">Highest Month of Congestion</div>
        <div class="value">{html.escape(str(highest_month_label))}</div>
        <div class="sub">{_fmt2(highest_month_value)} mile-hours</div></div>
      <div class="kpi-card"><div class="label">Worst Day of Week</div>
        <div class="value">{html.escape(str(worst_weekday_label))}</div>
        <div class="sub">{_fmt2(worst_weekday_value)} mile-hours</div></div>
      <div class="kpi-card"><div class="label">Worst Time of Day</div>
        <div class="value">{html.escape(str(worst_hour_label))}</div>
        <div class="sub">{_fmt2(worst_hour_value)} mile-hours</div></div>
    </div>
    <div class="chart-grid">
      {_chart_card(fig_month, "Each bar sums every detected congestion event's mile-hours "
        "(one mile of roadway congested for one hour, added up across every event) for that "
        "calendar month, across all analyzed Interstate and Arterial corridors.")}
      {_chart_card(fig_weekday, "Same mile-hours total, grouped by day of week across the "
        "full analysis period. This is a total, not a per-day average, so a period with more "
        "Fridays than Mondays will naturally weight Friday higher.")}
      {_chart_card(fig_hour, "Each event is sliced into the calendar hours it actually "
        "overlaps, with its mile-hours split proportionally to how many minutes fall in each "
        "hour, then summed by hour of day — this avoids crediting an entire multi-hour event "
        "(e.g. a 7-hour overnight incident) to just its start hour.")}
      {_chart_card(fig_county, "Same mile-hours total, attributed to the county of each "
        "event's upstream (first) segment. Only Interstate and Arterial corridors are "
        "analyzed, so this reflects the network studied here, not each county's full "
        "roadway congestion.")}
    </div>
    {_chart_card(fig_hourly_speed, "Average probe speed by hour of day, region-wide — one line "
      "per FHWA functional road class (Interstate/Expressway/Arterial/Collector/Local, by each "
      "TMC's own direct functional classification, not inferred) plus the overall system-wide "
      "average. Unlike the four charts above, this measures raw speed, not mile-hours of "
      "congestion, so it shows WHEN each road class slows down and by how much, rather than how "
      "much total congestion accumulated. The Arterial curve's highest-speed hour and lowest-"
      "speed hour set the free-flow/peak-hour reference used by the Intersection Congestion "
      "Severity page's methodology.")}
    """

    # Worst Day spotlight is built and appended AFTER the four charts above
    # rather than alongside them, specifically so its own chart's
    # _fig_html() call consumes a LATER _FIG_COUNTER value than theirs:
    # _fig_html() only inlines the full plotly.js library once, for
    # whichever figure happens to be rendered FIRST (_FIG_COUNTER == 0),
    # and browsers execute <script> tags in document order — so that
    # figure must also be the first one that actually appears in the page,
    # not just the first one built in Python. Building this after means
    # its chart's script runs later in the document too, once Plotly is
    # already loaded (confirmed the hard way: building it first, even
    # though its own HTML gets placed later via {worst_day_html}, caused
    # every earlier chart's inline script to call Plotly.newPlot() before
    # the <script> that defines Plotly had executed).
    worst_day = _load_worst_day()
    fig_worst_day_hourly = None
    worst_day_map_records: list[list] = []
    if worst_day.get("analysis_date") is not None:
        worst_day_hourly = _load_worst_day_hourly(worst_day["analysis_date"])
        if not worst_day_hourly.is_empty():
            fig_worst_day_hourly = _single_series_bar(
                [f"{h:02d}:00" for h in worst_day_hourly["hour_of_day"].to_list()],
                worst_day_hourly["total_mile_hours"].to_list(),
                f"Hourly Profile — {cbi_map_geometry.format_popup_date(worst_day['analysis_date'])}",
                width=700, height=400,
            )
        worst_day_active_ids = _load_worst_day_active_bottleneck_ids(worst_day["analysis_date"])
        worst_day_map_records = [
            record[:3] for record in all_bottleneck_records
            if record[4]["bottleneckId"] in worst_day_active_ids
        ]
    general_html += _build_worst_day_section_html(worst_day, fig_worst_day_hourly, worst_day_map_records)

    # --- Region map tab ------------------------------------------------------
    region_map_html = f"<div class='card map-card'>{_build_region_map_html(bottleneck_segments)}</div>"

    # --- Corridors tab -------------------------------------------------------

    corridor_group_by_id = {
        row["corridor_id"]: row["corridor_group"]
        for row in corridor_segments.select(["corridor_id", "corridor_group"]).unique().iter_rows(named=True)
    }

    sidebar_items = []
    detail_blocks = []
    for group in ("Interstate", "Expressway", "Arterial"):
        group_corridors = [c for c in corridors if corridor_group_by_id.get(c.corridor_id) == group]
        if not group_corridors:
            continue
        sidebar_items.append(f"<div class='group-label'>{group}s</div>")
        for corridor in group_corridors:
            slug = f"corridor-{corridor.corridor_id}"
            sidebar_items.append(
                f"<button data-corridor='{slug}' onclick=\"showCorridor('{slug}')\">"
                f"{html.escape(corridor.corridor_name)}</button>"
            )
            detail_blocks.append(
                _corridor_detail_block(
                    corridor, slug, corridor_segments, all_bottleneck_records, output_root
                )
            )

    if detail_blocks:
        detail_blocks[0] = detail_blocks[0].replace(
            'class="corridor-detail"', 'class="corridor-detail active"', 1
        )
    for i, item in enumerate(sidebar_items):
        if "data-corridor=" in item:
            sidebar_items[i] = item.replace("<button ", "<button class='active' ", 1)
            break

    corridors_html = f"""
    <div class="corridor-layout">
      <div class="corridor-sidebar">{''.join(sidebar_items)}</div>
      <div style="flex:1; min-width:0;">{''.join(detail_blocks)}</div>
    </div>
    """

    # --- Watch segments tab ----------------------------------------------
    watch_html = f"""
    <h3 class="section-title">Top 25 Collector Segments (by AADT)</h3>
    {_watch_table(watch, 'Collector')}
    <h3 class="section-title">Top 10 Local Segments (by AADT)</h3>
    {_watch_table(watch, 'Local')}
    """
    watch_tab_button = (
        '<button data-tab="tab-watch" onclick="showTab(\'tab-watch\')">Watch Segments</button>'
        if SHOW_WATCH_SEGMENTS_TAB
        else ""
    )
    watch_tab_section = (
        f'<section id="tab-watch" class="tabpanel">{watch_html}</section>'
        if SHOW_WATCH_SEGMENTS_TAB
        else ""
    )

    generated_on = date.today().isoformat()
    logo_uri = _agency_logo_data_uri()
    logo_html = (
        f'<img class="agency-logo" src="{logo_uri}" alt="Atlanta Regional Commission logo" />'
        if logo_uri
        else ""
    )
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>CBI: Congestion and Bottleneck Identification — {generated_on}</title>
<style>{_PAGE_CSS}</style>
</head>
<body>
<script>{_CANVAS_MAP_JS}</script>
<header class="masthead">
  {logo_html}
  <h1>CBI: Congestion and Bottleneck Identification</h1>
</header>
<nav class="tabs">
  <button class="active" data-tab="tab-metadata" onclick="showTab('tab-metadata')">Metadata</button>
  <button data-tab="tab-general" onclick="showTab('tab-general')">Regional Overview</button>
  <button data-tab="tab-map" onclick="showTab('tab-map')">Region Map</button>
  <button data-tab="tab-bottlenecks" onclick="showTab('tab-bottlenecks')">Top Bottlenecks</button>
  <button data-tab="tab-intersections" onclick="showTab('tab-intersections')">Intersection Severity</button>
  <button data-tab="tab-corridors" onclick="showTab('tab-corridors')">Corridors</button>
  {watch_tab_button}
</nav>
<main>
  <section id="tab-metadata" class="tabpanel active">{metadata_html}</section>
  <section id="tab-general" class="tabpanel">{general_html}</section>
  <section id="tab-map" class="tabpanel">{region_map_html}</section>
  <section id="tab-bottlenecks" class="tabpanel">{top_bottlenecks_html}</section>
  <section id="tab-intersections" class="tabpanel">{intersection_severity_html}</section>
  <section id="tab-corridors" class="tabpanel">{corridors_html}</section>
  {watch_tab_section}
</main>
<footer>Data source: NPMRDS from INRIX (passenger vehicles and trucks), 21 Georgia counties,
  17,388 TMC segments, weekdays (Mon&ndash;Fri) Jan 1&ndash;Dec 31, 2025. Bottleneck-detection
  methodology based on the FHWA Congestion and Bottleneck Identification (CBI) Tool; this is an
  independent ARC implementation of that methodology for the Atlanta region, not the official
  FHWA CBI Tool software. See the Metadata tab for full source and attribution detail.
  &mdash; Atlanta Regional Commission, CBI regional congestion report.</footer>
<script>{_TAB_JS}</script>
</body>
</html>
"""

    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"regional_congestion_report_{generated_on}.html"
    output_path.write_text(doc, encoding="utf-8")

    return output_path


def _find_corridor_docx(output_root: Path, corridor_slug: str) -> str | None:
    """SharePoint URL to this corridor's Word report, if one has been
    generated locally — for the "Download Report" link. The LOCAL output
    folder is still globbed (rather than reconstructing today's date) just
    to discover the exact current filename/date-suffix, since the Word doc
    and this HTML report aren't guaranteed to have been generated on the
    same day and the user moves the actual .docx files to OneDrive/
    SharePoint separately, keeping them off the public GitHub Pages
    deployment. The URL is built from SHAREPOINT_CORRIDOR_REPORTS_BASE_URL
    plus that same "{slug}/{filename}" relative path — see the constant's
    own comment for what's unverified about this."""
    corridor_dir = output_root / corridor_slug
    if not corridor_dir.is_dir():
        return None
    matches = sorted(
        corridor_dir.glob(f"{corridor_slug}_congestion_report_*.docx"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        return None
    relative_path = f"{corridor_slug}/{matches[0].name}"
    return f"{SHAREPOINT_CORRIDOR_REPORTS_BASE_URL}/{quote(relative_path)}"


def _corridor_detail_block(
    corridor: CorridorContext,
    slug: str,
    all_segments: pl.DataFrame,
    all_bottleneck_records: list[list],
    output_root: Path,
) -> str:
    ranked = _load_ranked_bottlenecks_detailed(corridor)
    summary = _load_corridor_summary(corridor)
    from_intersection, to_intersection = _load_corridor_extent(corridor)

    severity_fig = None
    if not ranked.is_empty():
        severity_fig = _single_series_bar(
            [f"#{r}" for r in ranked["severity_rank"].to_list()],
            ranked["severity_index"].to_list(),
            "Bottleneck Severity Index",
            highlight_max=False,
            width=560,
        )
    else:
        # No discrete peak found (e.g. uniformly-congested beltways) — fall
        # back to the raw per-segment occurrence profile so the corridor
        # still shows a meaningful chart instead of nothing.
        profile = _load_segment_occurrence_profile(corridor)
        if not profile.is_empty():
            severity_fig = _single_series_bar(
                [str(s) for s in profile["segment_order"].to_list()],
                profile["weekday_occurrence_pct"].to_list(),
                "Segment Congestion Occurrence % (no discrete bottleneck detected)",
                highlight_max=True,
                width=560,
            )

    # Same treatment as the Region Map tab: only this corridor's bottleneck
    # extents, same Google Maps traffic colors — filtered from the already
    # region-wide-classified record set (record[3] is corridor_id; see
    # _bottleneck_records_and_legend) rather than showing the whole
    # corridor in the old blue occurrence-gradient scheme. A corridor with
    # no detected bottleneck (e.g. a uniformly-congested beltway falling
    # back to the segment-profile chart above) simply gets no map, matching
    # the Region Map tab's own "no colored bottleneck, no map clutter"
    # behavior for such corridors.
    corridor_records = [
        record[:3] for record in all_bottleneck_records if record[3] == corridor.corridor_id
    ]
    map_html = None
    if corridor_records:
        map_html = _canvas_map_html(corridor_records, height=320, width=560, interactive=False)

    chart_html = ""
    if severity_fig is not None:
        chart_html += f"<div class='card'>{_fig_html(severity_fig, fixed_size=True)}</div>"
    if map_html is not None:
        chart_html += f"<div class='card map-card'>{map_html}</div>"

    docx_href = _find_corridor_docx(output_root, corridor.slug)
    # No `download` attribute: docx_href is now an external, cross-origin
    # SharePoint URL (internal ARC access only), not a same-origin relative
    # path — browsers ignore `download` cross-origin anyway, and the
    # SharePoint ":w:/r/" URL format is meant to open in Word Online /
    # prompt SharePoint's own download flow, not force a raw file save.
    download_html = (
        f"<a class='download-report-btn' href='{html.escape(docx_href)}' "
        "target='_blank' rel='noopener noreferrer'>Download Report (ARC Only) (.docx)</a>"
        if docx_href and SHOW_DOWNLOAD_REPORT_BUTTON
        else ""
    )

    return f"""
    <div id="{slug}" class="corridor-detail">
      <h2>{html.escape(corridor.corridor_name)}</h2>
      {download_html}
      {_kv_table([
          ("Extent", f"From {from_intersection or '?'} to {to_intersection or '?'}"),
          ("Segments", summary["segments"]),
          ("Corridor Length (miles)", _fmt2(summary["corridor_miles"])),
          ("Days Analyzed", summary["analyzed_days"]),
          ("Total Congestion Events", summary["total_events"]),
          ("Recurring Bottlenecks Identified", ranked.height),
      ])}
      <p class="muted">Severity index (below and in the chart) ranks only the discrete
      bottleneck locations identified on this corridor, combining how often each occurs,
      how large a queue it produces, and how much speed drops — a different, more complete
      measure than the Region Map's color, which shows only how often each segment congests.
      This corridor's mini-map is colored by that same occurrence rate for comparison.</p>
      <div class="chart-grid">{chart_html}</div>
      {_bottleneck_table(ranked)}
    </div>
    """
