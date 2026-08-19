"""
Shared map-rendering building blocks used by both cbi_generate_regional_
report.py (interactive HTML canvas map) and cbi_generate_corridor_report.py
(static PNG embedded in each corridor's Word doc) — kept in its own module,
imported by both, rather than one importing from the other, because
cbi_generate_regional_report.py already imports _load_corridor_summary from
cbi_generate_corridor_report.py; a reverse import would be circular.

Covers: real road-curve geometry from the HERE-derived TMC shapefile
(data/tmc_roads.topojson), the directional sideways offset so opposite-
direction segments don't draw on top of each other, and the Google Maps-
style traffic color ramp (K-means clustered on severity) — the same
methodology and same color-to-severity mapping in both the interactive
region map and every corridor's static map image, so a color means the
same thing everywhere in this report.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import polars as pl
import psycopg
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

import cbi_database

TMC_TOPOLOGY_PATH = Path(__file__).resolve().parent.parent / "data" / "tmc_roads.topojson"

# Line thickness per road class on every map in the report (region map,
# intersection tab, corridor mini-maps and Word-doc static images) — kept
# here rather than duplicated in each report generator so a road class
# reads at the same relative width everywhere.
ROAD_CLASS_WIDTH = {"Interstate": 3.4, "Expressway": 2.6, "Arterial": 1.7, "Collector": 1.2, "Local": 0.9}

DIRECTIONAL_OFFSET_METERS = 12.0
METERS_PER_DEGREE_LAT = 111_320.0

# Google Maps traffic-layer ramp (light orange -> amber -> orange -> red ->
# dark maroon), low -> high. Starts at light orange rather than green —
# every segment/marker colored with this ramp is a confirmed recurring
# bottleneck, so even the "least severe" class is still a real problem.
BOTTLENECK_SEVERITY_COLORS = ["#ffcc80", "#ffc107", "#ff9800", "#f4433a", "#8b0000"]
BOTTLENECK_KMEANS_CLUSTERS = 5

# staticmap's url_template.format() only supplies z/x/y (no subdomain
# rotation like the JS canvas engine's own cartoUrl() does) — a fixed
# subdomain still serves the same tiles, just without load-balancing across
# a/b/c/d, which doesn't matter for a single static image render.
CARTO_TILE_URL = "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"

DIRECTION_ABBREVIATIONS = {
    "NORTHBOUND": "NB",
    "SOUTHBOUND": "SB",
    "EASTBOUND": "EB",
    "WESTBOUND": "WB",
    "CLOCKWISE": "CW",
    "COUNTERCLOCKWISE": "CCW",
}


def abbreviate_direction(direction: str | None) -> str:
    """The region map popup's "wrapped format" segment name (per the user's
    own example, "I-285 CCW/CW wb/eb/nb/sb") uses these short codes rather
    than corridor_definitions' full raw values (NORTHBOUND, CLOCKWISE, ...),
    which are too long for a hover tooltip."""
    if not direction:
        return ""
    return DIRECTION_ABBREVIATIONS.get(direction.upper(), direction.title())


def segment_display_name(corridor: str, direction: str | None) -> str:
    """e.g. ("I-285", "CLOCKWISE") -> "I-285 CW"."""
    abbrev = abbreviate_direction(direction)
    return f"{corridor} {abbrev}".strip()


def format_popup_date(value) -> str:
    """"August 17, 2026" — built field-by-field rather than via strftime's
    %-d (no leading zero), since %-d is a glibc extension not supported by
    Windows' strftime, and this pipeline runs on Windows."""
    if value is None:
        return "N/A"
    return f"{value.strftime('%B')} {value.day}, {value.year}"


def format_popup_time(value) -> str:
    """"4:00 PM" — same leading-zero-hour concern as format_popup_date."""
    if value is None:
        return "N/A"
    hour12 = value.hour % 12 or 12
    period = "AM" if value.hour < 12 else "PM"
    return f"{hour12}:{value.minute:02d} {period}"


def kmeans_severity_classes(values: list[float]) -> tuple[list[int], list[tuple[str, str]]]:
    """Cluster severity values into BOTTLENECK_KMEANS_CLUSTERS groups with
    K-means rather than fixed percent bins — severity_index (geometric or
    AADT-weighted) has no natural 0-100 scale, so a hand-picked bin scheme
    would need re-tuning by hand every time the underlying bottleneck mix
    changes; K-means finds whatever natural breaks actually exist. Returns
    (ranks in the same order as the input values, and a
    [(color, range_label), ...] legend list in ascending-severity order).
    Falls back to a single cluster if there are fewer points than clusters."""
    n = len(values)
    if n == 0:
        return [], []

    k = min(BOTTLENECK_KMEANS_CLUSTERS, n)
    array = [[v] for v in values]
    labels = KMeans(n_clusters=k, n_init=10, random_state=0).fit_predict(array)

    cluster_values: dict[int, list[float]] = {}
    for value, label in zip(values, labels):
        cluster_values.setdefault(int(label), []).append(value)

    ranked_labels = sorted(
        cluster_values, key=lambda label: sum(cluster_values[label]) / len(cluster_values[label])
    )
    rank_by_label = {label: rank for rank, label in enumerate(ranked_labels)}
    ranks = [rank_by_label[int(label)] for label in labels]

    legend = [
        (
            BOTTLENECK_SEVERITY_COLORS[rank],
            f"{min(cluster_values[label]):,.0f}–{max(cluster_values[label]):,.0f}",
        )
        for rank, label in enumerate(ranked_labels)
    ]

    return ranks, legend


def kmeans_multi_feature_classes(
    rows: list[list[float]], higher_is_worse: list[bool]
) -> tuple[list[int], list[tuple[str, str]], list[float]]:
    """Same fit-then-rank-then-color-ramp pattern as kmeans_severity_classes,
    generalized to several features at once (e.g. speed-drop ratio,
    occurrence, AADT) instead of one severity value. Each feature is
    standardized (z-score) before fitting so no single feature dominates
    purely because its raw values happen to be larger — AADT runs in the
    tens of thousands while a ratio runs 0-1, and without standardizing,
    K-means would effectively cluster on AADT alone. higher_is_worse marks,
    per feature/column, whether a HIGHER standardized value means more
    severe (True — e.g. occurrence, AADT) or a LOWER one does (False — e.g.
    a speed-drop ratio, where closer to 0 is worse); each feature's
    standardized value is negated when its entry is False, so cluster
    ranking can always sort by a plain ascending mean of the now uniformly-
    oriented standardized features. Legend labels show only the cluster's
    rank position, not a value range, since a multi-feature cluster has no
    single number to describe its span.

    Returns (ranks, legend, scores) — scores is each row's own oriented
    mean (the same quantity clusters are ranked by), for callers that need
    to order rows WITHIN a cluster (e.g. picking a top-N cut that doesn't
    land exactly on a cluster boundary) rather than just by cluster rank."""
    n = len(rows)
    if n == 0:
        return [], [], []

    k = min(BOTTLENECK_KMEANS_CLUSTERS, n)
    scaled = StandardScaler().fit_transform(np.asarray(rows, dtype=float))
    oriented = np.where(higher_is_worse, scaled, -scaled)
    scores = oriented.mean(axis=1)

    labels = KMeans(n_clusters=k, n_init=10, random_state=0).fit_predict(oriented)

    cluster_scores: dict[int, list[float]] = {}
    for score, label in zip(scores, labels):
        cluster_scores.setdefault(int(label), []).append(float(score))

    ranked_labels = sorted(
        cluster_scores, key=lambda label: sum(cluster_scores[label]) / len(cluster_scores[label])
    )
    rank_by_label = {label: rank for rank, label in enumerate(ranked_labels)}
    ranks = [rank_by_label[int(label)] for label in labels]

    legend = [
        (BOTTLENECK_SEVERITY_COLORS[rank], f"Cluster {rank + 1} of {len(ranked_labels)}")
        for rank in range(len(ranked_labels))
    ]

    return ranks, legend, [float(s) for s in scores]


def load_region_wide_severity_values(connection: psycopg.Connection) -> list[float]:
    """Every ranking-eligible bottleneck's AADT-weighted severity, region-
    wide — the same population the Region Map tab clusters on. Reused as
    the K-means fit population for corridor-level static maps too, so a
    color means the same severity in a corridor's Word doc as it does on
    the interactive HTML map, not a locally-rescaled one from just that
    corridor's handful of bottlenecks."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT aadt_weighted_severity_index
            FROM "Year_2025".vw_bottleneck_dashboard_ranked
            WHERE ranking_eligible AND aadt_weighted_severity_index IS NOT NULL
            """
        )
        return [float(row[0]) for row in cursor.fetchall()]


def load_bottleneck_map_segments() -> pl.DataFrame:
    """Only the segments that are actually part of a detected recurring
    bottleneck (segment_bottleneck_membership) — every bottleneck, region-
    wide, regardless of which corridor it belongs to. Both report generators
    (the interactive HTML region map and every corridor's static Word-doc
    map) source this same query, then filter its already-K-means-classified
    output down to what each one needs (see bottleneck_records_and_legend)
    — never re-querying or re-fitting per corridor, so a color means the
    same severity everywhere in the report. Excludes bottlenecks with
    ranking_eligible = false (confirmed false-flagged by either the rural-
    Arterial corridor rule or the overnight-congestion diagnostic).

    The query's explicit ORDER BY matters beyond readability: the corridor
    Word-doc generator calls this function from its own separate process,
    re-querying from scratch rather than reusing the HTML report's
    in-memory result. Without a stable row order, Postgres is free to
    return rows in a different sequence on a different invocation, which
    silently changes the K-means fit even though nothing about the
    underlying data changed — confirmed directly: without this ORDER BY, a
    corridor's Word-doc map picked up an extra, wrong color class that
    wasn't present in that same run's HTML region map. A fixed order makes
    the fit, and therefore every color, identical no matter which process
    or how many times this runs."""
    return pl.read_database_uri(
        query="""
            SELECT
                b.bottleneck_id, d.corridor_id, d.corridor_group, d.corridor_name, c.tmc,
                c.start_latitude, c.start_longitude,
                c.end_latitude, c.end_longitude,
                r.aadt_weighted_severity_index AS severity_pct,
                r.corridor AS bottleneck_corridor,
                r.direction AS bottleneck_direction,
                worst.analysis_date AS worst_date,
                worst.peak_time AS worst_time
            FROM "Year_2025".segment_recurring_bottlenecks AS b
            JOIN "Year_2025".segment_bottleneck_membership AS m
              ON m.bottleneck_id = b.bottleneck_id
            JOIN "Year_2025".corridor_definitions AS d
              ON d.road = b.corridor AND d.direction = b.direction AND d.is_active
            JOIN "Year_2025".corridor_segments AS c
              ON c.corridor_id = d.corridor_id AND c.segment_order = m.segment_order
            JOIN "Year_2025".vw_bottleneck_dashboard_ranked AS r
              ON r.bottleneck_id = b.bottleneck_id
            LEFT JOIN LATERAL (
                SELECT bdm.analysis_date, bdm.peak_time
                FROM "Year_2025".bottleneck_daily_metrics AS bdm
                WHERE bdm.bottleneck_id = b.bottleneck_id AND bdm.occurrence
                ORDER BY bdm.queue_mile_hours DESC
                LIMIT 1
            ) AS worst ON true
            WHERE c.start_latitude IS NOT NULL AND c.start_longitude IS NOT NULL
              AND c.end_latitude IS NOT NULL AND c.end_longitude IS NOT NULL
              AND r.ranking_eligible
            ORDER BY b.bottleneck_id, m.segment_order
        """,
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )


def bottleneck_records_and_legend(df: pl.DataFrame) -> tuple[list[list], list[tuple[str, str]]]:
    """Segment records for the bottleneck layer used on every map in the
    report: color follows one K-means clustering of AADT-weighted severity
    across every road class (Google Maps traffic ramp — see
    kmeans_severity_classes), width follows ROAD_CLASS_WIDTH per row's own
    corridor_group. Rows with no severity_pct are dropped (shouldn't
    happen — every row here came from an actual detected bottleneck — but
    guards the K-means fit against nulls regardless).

    Each record is [points, color, width, corridor_id, popup] — the last
    two exist purely so callers can reuse ONE region-wide fit instead of
    refitting per corridor (which would rescale the color ramp locally and
    break the "one color means the same severity everywhere" guarantee):
    corridor_id lets a caller filter the full record set down to one
    corridor (mini-maps, Word-doc static images) without a second DB round
    trip or K-means fit; popup is a {name, severity, worstDate, worstTime,
    bottleneckId} dict — bottleneckId lets a caller filter the full record
    set down to whichever specific bottlenecks were active on one date
    (e.g. the Worst Day spotlight map), same reuse-the-fit reasoning as
    corridor_id. The canvas JS renderer only reads indices 0-2;
    render_static_map_png only reads 0-2 as well (callers slice down to
    record[:3] for it)."""
    scored = df.filter(pl.col("severity_pct").is_not_null())
    if scored.is_empty():
        return [], []

    ranks, legend = kmeans_severity_classes(scored["severity_pct"].to_list())

    records = []
    polylines = load_tmc_polylines()
    for row, rank in zip(scored.iter_rows(named=True), ranks):
        points = offset_points(
            resolve_points(
                row.get("tmc"), row["start_latitude"], row["start_longitude"],
                row["end_latitude"], row["end_longitude"], polylines,
            )
        )
        records.append(
            [
                points,
                BOTTLENECK_SEVERITY_COLORS[rank],
                ROAD_CLASS_WIDTH.get(row["corridor_group"], 3.0),
                row["corridor_id"],
                {
                    "name": segment_display_name(
                        row["bottleneck_corridor"], row["bottleneck_direction"]
                    ),
                    "severity": float(row["severity_pct"]),
                    "worstDate": format_popup_date(row.get("worst_date")),
                    "worstTime": format_popup_time(row.get("worst_time")),
                    "bottleneckId": int(row["bottleneck_id"]),
                },
            ]
        )
    return records, legend


_TMC_POLYLINE_CACHE: dict[str, list[list[float]]] | None = None


def load_tmc_polylines() -> dict[str, list[list[float]]]:
    """Real road-curve geometry per TMC, decoded from data/tmc_roads.topojson
    (built by cbi_build_tmc_topology.py from the HERE-derived shapefile
    bundled with the NPMRDS download). Returns {tmc: [[lat, lon], ...]}.
    TMCs not present in the topology (a shapefile coverage gap, mostly short
    ramp/connector segments) are simply absent — callers fall back to a
    straight line between tmc_metadata's own start/end coordinates."""
    global _TMC_POLYLINE_CACHE
    if _TMC_POLYLINE_CACHE is not None:
        return _TMC_POLYLINE_CACHE

    polylines: dict[str, list[list[float]]] = {}
    if TMC_TOPOLOGY_PATH.exists():
        with TMC_TOPOLOGY_PATH.open(encoding="utf-8") as f:
            topology = json.load(f)

        scale_x, scale_y = topology["transform"]["scale"]
        translate_x, translate_y = topology["transform"]["translate"]

        decoded_arcs: list[list[tuple[float, float]]] = []
        for arc in topology["arcs"]:
            points: list[tuple[float, float]] = []
            x = y = 0
            for dx, dy in arc:
                x += dx
                y += dy
                points.append((x * scale_x + translate_x, y * scale_y + translate_y))
            decoded_arcs.append(points)

        for geometry in topology["objects"]["roads"]["geometries"]:
            tmc = geometry["properties"]["tmc"]
            arc_index = geometry["arcs"][0]
            polylines[tmc] = [[lat, lon] for lon, lat in decoded_arcs[arc_index]]

    _TMC_POLYLINE_CACHE = polylines
    return polylines


def offset_points(points: list[list[float]]) -> list[list[float]]:
    """Shift an entire polyline sideways by a small, fixed real-world
    distance, perpendicular to its overall start->end travel bearing, to
    the right-hand side of travel — same convention as physically separate
    carriageways on a divided highway. Needs no explicit direction lookup:
    a segment's opposite-direction TMC has a travel bearing roughly 180 deg
    from this one everywhere along the route, so offsetting right-of-travel
    always separates a direction pair to opposite sides. Applied in lat/lon
    degrees (not screen pixels), so it represents a constant real-world
    distance regardless of zoom/scale."""
    if len(points) < 2:
        return points

    lat1, lon1 = points[0]
    lat2, lon2 = points[-1]
    bearing = math.atan2(lon2 - lon1, lat2 - lat1)
    perpendicular = bearing + math.pi / 2

    mid_lat_rad = math.radians((lat1 + lat2) / 2.0)
    meters_per_degree_lon = METERS_PER_DEGREE_LAT * max(0.1, math.cos(mid_lat_rad))

    d_lat = (DIRECTIONAL_OFFSET_METERS * math.cos(perpendicular)) / METERS_PER_DEGREE_LAT
    d_lon = (DIRECTIONAL_OFFSET_METERS * math.sin(perpendicular)) / meters_per_degree_lon

    return [[lat + d_lat, lon + d_lon] for lat, lon in points]


def resolve_points(
    tmc: str | None,
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
    polylines: dict[str, list[list[float]]],
) -> list[list[float]]:
    """The real road-curve polyline for this TMC if the HERE shapefile
    covers it; otherwise a straight line between its own start/end
    coordinates."""
    polyline = polylines.get(tmc) if tmc else None
    if polyline:
        return polyline
    return [[start_lat, start_lon], [end_lat, end_lon]]


def render_static_map_png(
    segments: list[tuple[list[list[float]], str, float]],
    output_path: str,
    width: int = 900,
    height: int = 560,
) -> None:
    """segments: [(points [[lat,lon],...], color, width_px), ...]. Renders
    onto the same CARTO Light basemap tiles the interactive HTML region map
    uses, as a PNG — for contexts that can't run the JS canvas engine
    (Word docs). Requires internet access to fetch tiles at render time,
    same as the HTML map does in a browser."""
    from staticmap import Line, StaticMap

    canvas = StaticMap(width, height, url_template=CARTO_TILE_URL)
    for points, color, line_width in segments:
        coords = [(lon, lat) for lat, lon in points]
        canvas.add_line(Line(coords, color, max(2, round(line_width * 2))))
    image = canvas.render()
    image.save(str(output_path))
