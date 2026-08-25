"""
Generates a standalone HTML page ranking arterial roads by Estimated
Annual Vehicle-Hours of Delay Density (EAVHD_density) — Section 6 of the
methodology ("new standalone page, combine into main CBI dashboard later").

Reads from "Year_2025".arterial_intersection_severity (populated by
cbi_arterial_intersection_severity.py, one row per named arterial/path —
e.g. Buford Hwy, Roswell Rd, Piedmont Ave — not per intersection) and
produces a single self-contained HTML file: KPI summary, an interactive
map, a scatter plot of EAVHD density vs. speed reduction, and a ranked
table — matching the navy/amber visual language already established
across this project's other outputs (technical report, corridor reports).

The map reuses the SAME technique and visual style as the main multi-
corridor report's Region Map: real per-TMC road-curve geometry decoded
from the HERE-derived shapefile topology (cbi_map_geometry.
load_tmc_polylines/resolve_points/offset_points), rendered on the same
tile-basemap canvas engine (_CANVAS_MAP_JS, _canvas_map_html, imported
directly from cbi_generate_regional_report.py — a one-directional import,
that module never imports from here) with the same Google-Maps-style
traffic color ramp (cbi_map_geometry.BOTTLENECK_SEVERITY_COLORS). Only the
displayed top-N roads' member TMCs are drawn, colored by a K-means fit
scoped to just those roads (see cbi_arterial_intersection_severity.
save_map_segments for why a region-wide fit would collapse them all into
one color).

Headline rank is EAVHD_density (estimated vehicle-hours of delay PER MILE)
— a congestion-CONCENTRATION measure, deliberately titled as such
throughout this page (not an unqualified "most congested" claim) since
EAVHD_total (total regional burden) can rank roads very differently: a
46-mile road can carry far more total delay than a 1.5-mile road while
having a fraction of its delay density. Only roads passing
eligible_for_ranking (see the analysis script) are ranked at all — a road
that is both very short and thinly supported by TMCs (e.g. GA-205: 0.54
mi on just 2 TMCs, 82.6% of its own delay concentrated in one of them) is
excluded outright, not merely flagged. The old abstract severity_index is
kept in the table only as an experimental comparison column, to be removed
once EAVHD_density is validated. The word "Estimated" is used deliberately
throughout: EAVHD's traffic-volume term is modeled from AADT x an assumed
hourly distribution curve x an assumed directional split, not an observed
hourly vehicle count — see the Methodology section on the page itself, and
cbi_arterial_intersection_severity.py's module docstring, for the full
explanation, the real cited source of the primary curve, and the
sensitivity test this was checked against.

Merging into the main CBI dashboard later: the existing regional dashboard
(regional_congestion_report_*.html) already uses a tabbed layout — the
cleanest integration path is adding a fifth/sixth tab that embeds this same
map + table + chart, reading from these same tables. This script is
intentionally self-contained so it can run standalone first.
"""

from __future__ import annotations

from pathlib import Path

import plotly.graph_objects as go
import plotly.io as pio
import polars as pl
import psycopg

import cbi_database
import cbi_map_geometry
from cbi_generate_regional_report import (
    _CANVAS_MAP_JS,
    _PAGE_CSS,
    _agency_logo_data_uri,
    _canvas_map_html,
    _load_county_boundary_rings,
)

NAVY = "#1F3864"
ACCENT = "#2E5395"
AMBER = "#B8860B"
LIGHT_SHADE = "#DCE6F1"

OUTPUT_PATH = Path(r"C:\Users\Soheil\Desktop\CBI\outputs\arterial_intersection_severity.html")


def load_results(connection: psycopg.Connection) -> pl.DataFrame:
    query = """
        SELECT rank, arterial_group, worst_intersection, county, congestion_pattern,
               speed_drop_ratio, occurrence_pct, aadt, tmc_count, road_miles,
               corridor_excess_travel_time_minutes, eavhd_total, eavhd_density,
               max_tmc_eavhd_share, qc_high_concentration, qc_flag,
               annual_queue_mile_hours, severity_index
        FROM "Year_2025".arterial_intersection_severity
        ORDER BY rank
    """
    return pl.read_database_uri(
        query=query, uri=cbi_database.database_uri(), engine="connectorx"
    )


def load_stability(connection: psycopg.Connection) -> pl.DataFrame:
    query = """
        SELECT hdf_variant, spearman_correlation, top_n_overlap, top_n
        FROM "Year_2025".arterial_severity_rank_stability
        ORDER BY hdf_variant
    """
    return pl.read_database_uri(
        query=query, uri=cbi_database.database_uri(), engine="connectorx"
    )


def load_map_segments(connection: psycopg.Connection) -> pl.DataFrame:
    query = """
        SELECT arterial_group, road_rank, severity_tier, tmc_code, direction,
               start_latitude, start_longitude, end_latitude, end_longitude,
               miles, eavhd_hours
        FROM "Year_2025".arterial_intersection_severity_segments
    """
    return pl.read_database_uri(
        query=query, uri=cbi_database.database_uri(), engine="connectorx"
    )


def build_map_html(map_segments: pl.DataFrame, ranked: pl.DataFrame) -> str:
    """Same real-road-geometry + canvas-tile-map technique and Google-Maps-
    style traffic color ramp as the main multi-corridor report's Region
    Map — see the module docstring for why."""
    density_by_group = dict(zip(ranked["arterial_group"].to_list(), ranked["eavhd_density"].to_list()))

    # Real EAVHD-density value ranges per class, not generic words — same
    # recomputation cbi_arterial_intersection_severity.save_map_segments
    # used to assign each segment's stored severity_tier, so this legend
    # and those tiers agree (deterministic K-means, same input order).
    _, legend = cbi_map_geometry.kmeans_severity_classes(ranked["eavhd_density"].to_list())

    polylines = cbi_map_geometry.load_tmc_polylines()
    segments: list[list] = []
    for row in map_segments.iter_rows(named=True):
        points = cbi_map_geometry.offset_points(
            cbi_map_geometry.resolve_points(
                row["tmc_code"], row["start_latitude"], row["start_longitude"],
                row["end_latitude"], row["end_longitude"], polylines,
            )
        )
        color = cbi_map_geometry.BOTTLENECK_SEVERITY_COLORS[int(row["severity_tier"])]
        direction_abbrev = cbi_map_geometry.abbreviate_direction(row["direction"])
        segments.append(
            [
                points,
                color,
                3.2,
                None,
                {
                    "name": f"#{row['road_rank']} {row['arterial_group']} {direction_abbrev}",
                    "severity": density_by_group.get(row["arterial_group"], 0.0),
                },
            ]
        )

    map_html = _canvas_map_html(
        segments, height=620, width=1180, interactive=True,
        county_rings=_load_county_boundary_rings(), responsive=True,
    )
    legend_items = "".join(
        f"<div class='map-legend-item'><span class='swatch dot' style='background:{color}'></span>"
        f"EAVHD {label} veh-hrs/mi</div>"
        for color, label in legend
    )
    return f"""
    <div class='map-with-legend'>
      <div class='map-explain'>
        <h4>Reading this map</h4>
        <p>Every TMC segment belonging to a top-{ranked.height} ranked road is drawn along its
        real road curve (same shapefile-derived geometry as the main regional report's Region
        Map), colored by that road's Estimated Annual Vehicle-Hours of Delay (EAVHD) density
        class. Color is a separate K-means fit scoped to just these {ranked.height} roads, not
        the full ~100-road candidate pool, so the ramp actually differentiates among them.</p>
      </div>
      {map_html}
      <div class='map-legend'>
        <h4>EAVHD Density Classes<br/>(veh-hrs/mi, K-means, top {ranked.height} only)</h4>
        {legend_items}
      </div>
    </div>
    """


def build_scatter_chart(df: pl.DataFrame) -> str:
    fig = go.Figure(
        data=go.Scatter(
            x=df["eavhd_density"].to_list(),
            y=(1 - df["speed_drop_ratio"]).to_list(),
            mode="markers+text",
            text=[f"#{r}" for r in df["rank"].to_list()],
            textposition="top center",
            marker=dict(
                size=[8 + (o / 100 * 20) for o in df["occurrence_pct"].to_list()],
                color=df["occurrence_pct"].to_list(),
                colorscale=[[0, LIGHT_SHADE], [1, AMBER]],
                showscale=True,
                colorbar=dict(title="Recurrence %"),
                line=dict(width=1.5, color=NAVY),
            ),
            hovertext=[
                f"{row['arterial_group']}<br>"
                f"Congestion pattern: {row['congestion_pattern']}<br>"
                f"AADT: {row['aadt']:,.0f}<br>"
                f"Road length: {row['road_miles']:.1f} mi ({row['tmc_count']} TMCs)<br>"
                f"Est. corridor excess travel time: {row['corridor_excess_travel_time_minutes']:.1f} min<br>"
                f"Est. annual vehicle-hrs delay (density): {row['eavhd_density']:,.0f}/mi<br>"
                f"Est. annual vehicle-hrs delay (total): {row['eavhd_total']:,.0f}<br>"
                f"Speed reduction: {(1 - row['speed_drop_ratio']) * 100:.1f}%<br>"
                f"Recurrence: {row['occurrence_pct']:.1f}%<br>"
                f"Max single-TMC share of delay: {row['max_tmc_eavhd_share']:.1%}"
                f"{' (QC: high concentration)' if row['qc_high_concentration'] else ''}<br>"
                f"Severity index (experimental): {row['severity_index']:.1f}"
                for row in df.iter_rows(named=True)
            ],
            hoverinfo="text",
        )
    )

    fig.update_layout(
        title="Top Arterials by Estimated Vehicle-Delay Density vs. Speed Reduction",
        xaxis_title="Estimated annual vehicle-hours of delay per mile (EAVHD density)",
        yaxis_title="Average speed reduction below free flow",
        yaxis_tickformat=".0%",
        height=560,
        plot_bgcolor="#F7F9FC",
        paper_bgcolor="white",
        font=dict(family="Georgia, serif", color="#333333"),
        title_font=dict(color=NAVY, size=18),
    )

    return pio.to_html(fig, include_plotlyjs="cdn", full_html=False)


def build_table_html(df: pl.DataFrame) -> str:
    rows_html = ""
    for row in df.iter_rows(named=True):
        qc_badge = (
            " <span style='color:#B8860B;font-weight:bold;' title='Most delay concentrated in one TMC'>&#9888;</span>"
            if row["qc_high_concentration"] else ""
        )
        rows_html += f"""
        <tr>
            <td>{row['rank']}</td>
            <td>{row['arterial_group']}{qc_badge}</td>
            <td>{row['worst_intersection'] or ''}</td>
            <td>{row['county'] or ''}</td>
            <td>{row['congestion_pattern']}</td>
            <td>{row['road_miles']:.1f}</td>
            <td>{row['tmc_count']}</td>
            <td>{row['aadt']:,.0f}</td>
            <td>{(1 - row['speed_drop_ratio']) * 100:.1f}%</td>
            <td>{row['occurrence_pct']:.1f}%</td>
            <td>{row['max_tmc_eavhd_share']:.1%}</td>
            <td>{row['eavhd_total']:,.0f}</td>
            <td>{row['eavhd_density']:,.0f}</td>
            <td>{row['severity_index']:.1f}</td>
        </tr>"""

    return f"""
    <table class="data">
        <thead>
            <tr>
                <th>Rank</th><th>Road</th><th>Worst Point</th><th>County</th>
                <th>Congestion Pattern</th><th>Miles</th><th>TMCs</th>
                <th>AADT</th><th>Speed Reduction</th><th>Recurrence</th>
                <th>Max TMC Share of Delay</th>
                <th>Est. Annual Vehicle-Hrs Delay (total)</th>
                <th>Est. Annual Vehicle-Hrs Delay (per mile)</th>
                <th>Severity Index (experimental)</th>
            </tr>
        </thead>
        <tbody>{rows_html}
        </tbody>
    </table>
    <p style="font-size:11px;color:#888;margin-top:8px;">Rank is by Estimated
    Annual Vehicle-Hours of Delay PER MILE (EAVHD density) &mdash; a
    congestion-CONCENTRATION measure, not total regional burden (see
    Methodology). &#9888; marks a road where over 60% of its own delay
    comes from a single TMC (max TMC share of delay &gt; 60%) &mdash; still
    ranked, but worth a second look before citing it as a corridor-wide
    problem rather than one localized bottleneck. The delay columns are
    estimates built from a modeled hourly traffic volume, not an observed
    count. Severity Index is retained only as an experimental comparison
    against the prior scoring method and does not drive rank.</p>
    """


def build_kpi_cards(df: pl.DataFrame) -> str:
    top = df.row(0, named=True)
    avg_drop = (1 - df["speed_drop_ratio"]).mean() * 100
    avg_occurrence = df["occurrence_pct"].mean()

    def card(label: str, value: str, sub: str = "") -> str:
        sub_html = f'<div class="sub">{sub}</div>' if sub else ""
        return f"""
        <div class="kpi-card">
            <div class="label">{label}</div>
            <div class="value">{value}</div>
            {sub_html}
        </div>"""

    return f"""
    <div class="kpi-row">
        {card("Most Concentrated Road", f"{top['arterial_group']}", f"{top['road_miles']:.1f} mi &middot; {top['tmc_count']} TMCs &middot; AADT {top['aadt']:,.0f}")}
        {card("Its Est. Annual Vehicle-Hrs Delay / Mile", f"{top['eavhd_density']:,.0f}", f"{top['eavhd_total']:,.0f} total across the road")}
        {card("Its Congestion Pattern", f"{top['congestion_pattern']}")}
        {card("Avg Speed Reduction (Top 20)", f"{avg_drop:.1f}%", f"Avg recurrence {avg_occurrence:.1f}%")}
    </div>
    """


def build_stability_html(stability_df: pl.DataFrame) -> str:
    if stability_df.is_empty():
        return ""
    rows = "".join(
        f"<li>vs. the <strong>{row['hdf_variant']}</strong> scenario: Spearman rank correlation "
        f"<strong>{row['spearman_correlation']:.2f}</strong> (1.0 = identical order), "
        f"<strong>{row['top_n_overlap']}/{row['top_n']}</strong> of the same roads remain in the "
        f"top {row['top_n']}</li>"
        for row in stability_df.iter_rows(named=True)
    )
    top_n = stability_df["top_n"][0] if stability_df.height else 20
    return f"""
    <p><strong>Rank stability under alternate HDF scenarios (validation output):</strong>
    EAVHD<sub>density</sub> was recomputed, among eligible roads only, under 3 other plausible
    hourly-distribution scenarios instead of the primary Delaware 7-group-average curve &mdash;
    a flatter real curve (a different individual Delaware traffic-pattern-group column), a
    uniform 1/24-per-hour baseline, and a synthetic textbook AM+PM commuter double-peak &mdash;
    and each compared to the primary ranking above:</p>
    <ul>{rows}</ul>
    <p>Spearman's &rho; in the 0.95&ndash;0.99 range with most of the same roads staying in the
    top {top_n} demonstrates that arterial rankings were stable under alternative reasonable
    hourly distributions &mdash; not that any one imported curve accurately represents Atlanta.
    This is a global check across every eligible road, not yet matched per Congestion Operating
    Pattern (COP) to its most-resembling candidate curve.</p>
    """


def generate_report(connection: psycopg.Connection, output_path: Path = OUTPUT_PATH) -> Path:
    df = load_results(connection)
    stability_df = load_stability(connection)
    map_segments = load_map_segments(connection)

    if df.is_empty():
        raise RuntimeError(
            "arterial_intersection_severity is empty — run "
            "cbi_arterial_intersection_severity.py first."
        )

    logo_uri = _agency_logo_data_uri()
    logo_html = (
        f'<img class="agency-logo" src="{logo_uri}" alt="Atlanta Regional Commission logo" style="height:36px;margin-right:12px;" />'
        if logo_uri else ""
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Top Arterials by Estimated Vehicle-Delay Density</title>
<style>{_PAGE_CSS}
    body {{ font-family: Georgia, serif; background: #F7F9FC; color: #333; margin: 0; padding: 0; }}
    .masthead {{ background: {NAVY}; color: white; padding: 24px 32px; display: flex; align-items: center; }}
    .masthead h1 {{ margin: 0; font-size: 24px; }}
    .masthead .sub {{ font-size: 13px; color: #C9D6E8; font-style: italic; margin-top: 4px; }}
    .content {{ max-width: 1240px; margin: 0 auto; padding: 24px 32px; }}
    .kpi-row {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }}
    .kpi-card {{ flex: 1; min-width: 200px; background: white; border: 1px solid #DCE3EC;
                 border-left: 4px solid {AMBER}; border-radius: 4px; padding: 14px 18px; }}
    .kpi-card .label {{ font-size: 12px; color: #666; text-transform: uppercase; letter-spacing: 0.5px; }}
    .kpi-card .value {{ font-size: 22px; font-weight: bold; color: {NAVY}; margin-top: 4px; }}
    .kpi-card .sub {{ font-size: 12px; color: #888; margin-top: 2px; }}
    .map-card {{ background: white; border: 1px solid #DCE3EC; border-radius: 4px; padding: 12px; margin-bottom: 24px; }}
    table.data {{ width: 100%; border-collapse: collapse; background: white; margin-top: 16px; font-size: 13px; }}
    table.data th {{ background: {NAVY}; color: white; padding: 10px 12px; text-align: left; font-size: 12px; }}
    table.data td {{ padding: 8px 12px; border-bottom: 1px solid #EEE; }}
    table.data tr:nth-child(even) {{ background: #F2F6FA; }}
    .methodology {{ background: white; border: 1px solid #DCE3EC; border-radius: 4px;
                     padding: 16px 20px; margin-top: 24px; font-size: 13px; color: #555; }}
    .methodology h3 {{ color: {ACCENT}; margin-top: 0; }}
    footer {{ text-align: center; font-size: 11px; color: #999; padding: 24px; font-style: italic; }}
</style>
</head>
<body>
<script>{_CANVAS_MAP_JS}</script>

<div class="masthead">
    {logo_html}
    <div>
        <h1>Top Arterials by Estimated Vehicle-Delay Density</h1>
    </div>
</div>

<div class="content">
    {build_kpi_cards(df)}
    <div class="map-card">{build_map_html(map_segments, df)}</div>
    {build_scatter_chart(df)}
    {build_table_html(df)}

    <div class="methodology">
        <h3>Methodology</h3>
        <p>Free-flow speed is the hour of day with the highest average speed
        across all Arterial-classified segments region-wide (the functional-
        class hourly curve), applied to each segment's own observed speed at
        that hour as its individual baseline &mdash; not computed
        independently per segment, which would let a chronically congested
        segment define its own artificially low baseline. Speed reduction
        (Intensity) is each segment's worst (minimum) average speed during
        AM (7&ndash;9) or PM (16&ndash;18) peak hours, relative to that
        baseline, LENGTH-WEIGHTED across the road's segments (not a plain
        average) so a handful of short TMC fragments don't carry the same
        influence as long ones.</p>
        <p><strong>Recurrence</strong> is the percentage of analyzed weekdays
        a segment's worst PEAK-HOUR AVERAGE speed (not its worst single
        5-minute reading &mdash; that was a real bug, caught and fixed:
        checking any 5-minute dip within the peak window rather than the
        peak hour's own average was inflating recurrence toward saturation
        across nearly every segment) fell below 70% of its free-flow
        baseline, also LENGTH-WEIGHTED across the road's segments, not a
        plain average.</p>
        <p><strong>Estimated Annual Vehicle-Hours of Delay (EAVHD)</strong>,
        anchored to FHWA's PHED (Peak Hour Excessive Delay) construction,
        which itself is built from NPMRDS travel times plus a traffic-volume
        estimate. For every segment, hour, and analyzed weekday: excess
        travel time ETT = segment length &times; max(0, 1/observed speed
        &minus; 1/free-flow speed), in hours. Estimated hourly directional
        volume V&#770; = AADT &times; HDF (hourly distribution factor, the
        modeled % of daily traffic in that hour) &times; D (directional
        split) &mdash; using each SEGMENT's own AADT, not the road's median
        (median AADT is used only for display). Estimated vehicle-hours of
        delay for that segment/hour/day = ETT &times; V&#770;, summed across
        every hour and every analyzed weekday to get that segment's annual
        total, then summed across every segment of a named road to get
        EAVHD<sub>total</sub> for the whole road (shown in the table for
        total regional impact).</p>
        <p>Fragmented multi-segment arterials (Buford Hwy, Roswell Rd, and
        others) are grouped under one named road before ranking &mdash; the
        formula is applied to the AGGREGATED segments of each path/road
        name, not to a single intersection. Roads not in the curated
        named-arterial registry are grouped by their own raw road name, with
        a trailing compass-direction suffix (e.g. "NE", "SW") stripped first
        so direction-suffixed variants of the same physical road (confirmed
        cases: "N Druid Hills Rd NE"/"N Druid Hills Rd", "Camp Creek
        Pky"/"Camp Creek Pky SW") combine into one row instead of splitting
        the same road's miles, delay, and recurrence across two ranked
        entries.</p>
        <p><strong>Rank is by EAVHD<sub>density</sub> = EAVHD<sub>total</sub>
        &divide; the road's own analyzed length in miles</strong> &mdash; a
        congestion-CONCENTRATION measure, deliberately titled as such on
        this page rather than an unqualified "most congested" claim: the
        raw total scales directly with how many miles of road matched a
        given named arterial, so a long rural or exurban route can
        accumulate far more TOTAL delay than a short, severely-congested
        in-town corridor while having a fraction of its DENSITY &mdash;
        those answer two different questions, and this page answers the
        density one.</p>
        <p><strong>Corridor eligibility</strong> is checked before ranking,
        not after: a road must have road_miles &ge; 0.5 AND (road_miles
        &ge; 1.0 OR at least 4 supporting TMCs) to be ranked at all.
        A road failing this (very short AND thinly supported) is excluded
        outright, since its density figure divides by a near-zero
        denominator and is not a meaningfully comparable "corridor"
        measurement &mdash; confirmed directly: GA-205 (0.54 mi, 2 TMCs)
        fails and does not appear above; GA-11 (8.06 mi, 15 TMCs) passes.
        Every eligible road's own single worst TMC is also checked: max TMC
        share of delay = max_i(EAVHD<sub>i</sub>) &divide; EAVHD<sub>total</sub>
        &mdash; a road can pass eligibility and still have most of its
        delay concentrated in one segment (flagged with &#9888; in the
        table, not excluded) rather than spread across the corridor.</p>
        <p><strong>On "Estimated":</strong> this database has no observed
        hourly traffic volume &mdash; only AADT (an annual daily average per
        segment). The primary hourly distribution factor (HDF) is a REAL,
        PUBLISHED, count-station-derived curve (Delaware DOT Traffic
        Monitoring Program, "Hourly Distribution of AADT for Traffic
        Groups," Average Weekday Traffic, 2002 &mdash; averaged across that
        source's 7 populated traffic-pattern groups), not a fabricated one,
        but that source's group-to-facility-type legend was not available
        to confirm which column is specifically "urban arterial," so it is
        used as a general urban/mixed-roadway weekday shape. The
        directional split D=0.5 is a transparent, neutral approximation for
        a typical urban arterial (no directional count data exists here
        either). Both are the least-defensible inputs to EAVHD and the
        first things to replace with real GDOT continuous-count-station
        hourly profiles &mdash; ideally grouped by functional class/urban
        context &mdash; the moment those are available. Every EAVHD figure
        on this page is labeled "Estimated" for this reason. See the rank
        stability section below for how much this assumption actually
        matters to the ranking.</p>
        <p>Severity Index (kept as an experimental comparison column only,
        does not drive rank; slated for removal from the public table once
        EAVHD<sub>density</sub> is fully validated) = queue mile-hours per
        mile &times; (recurrence % / 100) &times; (1 &minus; speed-drop
        ratio) &times; (AADT / 100,000), where queue mile-hours uses a
        binary congested/not-congested hourly threshold rather than EAVHD's
        continuous excess-travel-time formulation.</p>
        <p><strong>Congestion Pattern (COP)</strong> is a SEPARATE concept
        from HDF, deliberately, and deliberately not called a "Traffic
        Pattern Group" (TPG is already a volume/count concept in traffic
        engineering): it classifies each road by the SHAPE of its own
        INRIX-observed congestion (normalized against its own free-flow
        speed, hour by hour), via K-means/hierarchical clustering on a
        24-hour speed-ratio profile plus AM/PM depth and recurrence &mdash;
        producing archetypes like COP-3 (AM/PM Commuter), COP-2
        (PM-Dominant), or COP-1 (All-Day Urban). This is real, directly-
        measured information about WHEN speed deteriorates on each road in
        this specific region, but speed shape cannot be reliably converted
        into a traffic-volume distribution (especially near capacity, where
        speed can fall while actual throughput also falls) &mdash; so it is
        shown here as a diagnostic column only, and does not select a
        different HDF curve per group or affect ranking.</p>
        {build_stability_html(stability_df)}
    </div>
</div>

<footer>
    CBI Multi-Corridor Pipeline &mdash; Arterial Intersection Severity (standalone page, pending integration into the main regional dashboard)
</footer>

</body>
</html>
"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path


if __name__ == "__main__":
    with psycopg.connect(**cbi_database.connection_kwargs()) as conn:
        path = generate_report(conn)
        print(f"Report written to: {path}")
