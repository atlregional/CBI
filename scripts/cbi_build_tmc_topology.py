"""
Builds data/tmc_roads.topojson — real road-curve geometry for every TMC used
in the analysis, sourced from the HERE-derived shapefile bundled with the
NPMRDS download (Georgia.shp, ~11,700 statewide TMCs, each a full multi-point
polyline rather than a straight line between endpoints).

Why this exists: tmc_metadata only carries each TMC's start/end lat/lon —
no curve data — so the region map previously drew every road as a straight
line between two points, which visibly cut across curves and interchange
ramps instead of following them. The shapefile has what's missing.

Scoped to the TMCs actually referenced by corridor_segments and
watch_segments (a few thousand, not the full statewide ~11,700) to keep the
output file a reasonable size for embedding in the report. A handful of
analyzed TMCs (~430 of ~2,800, mostly ramp/connector "P"/"N" suffix codes)
aren't in the shapefile at all — those fall back to a straight line between
tmc_metadata's start/end coordinates at render time, handled in
cbi_generate_regional_report.py, not here.

Output format is real TopoJSON (quantized, delta-encoded arcs — see
https://github.com/topojson/topojson-specification) — one arc per TMC, no
cross-feature arc sharing (the input roads aren't topologically adjacent
polygons where sharing would matter, so this keeps the encoder simple
without losing anything the renderer needs). Quantized to 1e6 (~0.11m
precision at this latitude), which shrinks the coordinate stream to small
integer deltas that compress well as JSON text.
"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
import shapefile

import cbi_database

SHAPEFILE_PATH = r"C:\Users\Soheil\Desktop\CBI\Georgia\Georgia.shp"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "tmc_roads.topojson"
QUANTIZATION = 1_000_000


def _needed_tmcs(connection: psycopg.Connection) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute('SELECT DISTINCT tmc FROM "Year_2025".corridor_segments')
        tmcs = {row[0] for row in cursor.fetchall()}
        cursor.execute('SELECT DISTINCT tmc FROM "Year_2025".watch_segments')
        tmcs |= {row[0] for row in cursor.fetchall()}

    # Also cover the Intersection Congestion Severity page's named
    # arterials (Buford Hwy, Roswell Rd, Piedmont Rd, and others) — those
    # roads were never added to corridor_definitions/corridor_segments, so
    # without this every one of their map segments fell back to a straight
    # line instead of the real road curve.
    import cbi_intersection_congestion_severity as intersection_severity

    all_tmcs = intersection_severity._load_backfilled_tmc_metadata()
    named_tmcs = intersection_severity._select_named_arterial_tmcs(all_tmcs)
    tmcs |= set(named_tmcs["tmc"].to_list())

    return tmcs


def _read_shapefile_geometries(tmcs: set[str]) -> dict[str, list[tuple[float, float]]]:
    """Returns {tmc: [(lon, lat), ...]} for every requested TMC found in the
    shapefile. Points are already in the shapefile's own travel-direction
    order (start to end), matching tmc_metadata's start/end convention."""
    reader = shapefile.Reader(SHAPEFILE_PATH)
    geometries: dict[str, list[tuple[float, float]]] = {}

    for shape_record in reader.iterShapeRecords():
        tmc = shape_record.record["Tmc"]
        if tmc not in tmcs:
            continue
        points = shape_record.shape.points
        if len(points) >= 2:
            geometries[tmc] = [(float(x), float(y)) for x, y in points]

    return geometries


def _quantize_arc(points: list[tuple[float, float]]) -> list[list[int]]:
    """Delta-encode a (lon, lat) point sequence into TopoJSON's integer arc
    format: each point after the first is the delta from the previous one,
    in quantized grid units."""
    arc = []
    prev_qx, prev_qy = None, None
    for lon, lat in points:
        qx = round(lon * QUANTIZATION)
        qy = round(lat * QUANTIZATION)
        if prev_qx is None:
            arc.append([qx, qy])
        else:
            arc.append([qx - prev_qx, qy - prev_qy])
        prev_qx, prev_qy = qx, qy
    return arc


def build_topology(connection: psycopg.Connection) -> dict:
    tmcs = _needed_tmcs(connection)
    geometries = _read_shapefile_geometries(tmcs)

    arcs = []
    line_geometries = []
    for tmc, points in geometries.items():
        arc_index = len(arcs)
        arcs.append(_quantize_arc(points))
        line_geometries.append(
            {"type": "LineString", "arcs": [arc_index], "properties": {"tmc": tmc}}
        )

    topology = {
        "type": "Topology",
        "transform": {
            "scale": [1.0 / QUANTIZATION, 1.0 / QUANTIZATION],
            "translate": [0.0, 0.0],
        },
        "arcs": arcs,
        "objects": {
            "roads": {"type": "GeometryCollection", "geometries": line_geometries}
        },
    }

    return topology, len(tmcs), len(geometries)


def main() -> None:
    with psycopg.connect(**cbi_database.connection_kwargs()) as connection:
        topology, needed_count, found_count = build_topology(connection)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(topology, f, separators=(",", ":"))

    print(f"TMCs needed: {needed_count}")
    print(f"TMCs found in shapefile: {found_count} ({needed_count - found_count} will fall back to straight lines)")
    print(f"Wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
