"""
Builds "Year_2025".tmc_cid — maps every TMC used in this analysis to the
Community Improvement District (CID) its location falls within, if any.

Source: data/cid_boundaries.geojson, downloaded from the Atlanta Regional
Commission's own Open Data portal (opendata.atlantaregional.com, dataset
"Community Improvement Districts", item 7059fca3378145fc8cc72a5c068e1cef) —
31 region-wide CIDs (Buckhead, Downtown Atlanta, Midtown, North/South
Fulton, Airport West/South, Cumberland, Perimeter, Gwinnett Place, and more)
covering the whole ARC planning area, not just Fulton County. This is the
authoritative source for the org this report is built for.

Matching is a simple point-in-polygon test (shapely) using each TMC's
midpoint (average of start/end lat/lon) against every CID polygon. Most
analyzed TMCs sit outside any CID boundary entirely (CIDs are a small
fraction of the region's land area, concentrated in commercial corridors) —
those get cid_name = NULL, which callers should render as '-', not a
guessed city/area name. Guessing at broader area names (Airport, Downtown,
etc.) for segments outside every real CID boundary would misrepresent them
as belonging to a CID that doesn't actually cover that location.
"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
from shapely.geometry import Point, shape

import cbi_database

CID_GEOJSON_PATH = Path(__file__).resolve().parent.parent / "data" / "cid_boundaries.geojson"


def _load_cid_polygons() -> list[tuple[str, object]]:
    with CID_GEOJSON_PATH.open(encoding="utf-8") as f:
        geojson = json.load(f)

    polygons = []
    for feature in geojson["features"]:
        name = feature["properties"].get("CID_NAME")
        if not name:
            continue
        polygons.append((name, shape(feature["geometry"])))
    return polygons


def _needed_tmcs(connection: psycopg.Connection) -> list[tuple[str, float, float]]:
    """(tmc, midpoint_lat, midpoint_lon) for every TMC that appears anywhere
    in the report — corridor_segments (bottleneck rankings, intersection
    index) plus watch_segments (Collector/Local tables)."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT tmc, (start_latitude + end_latitude) / 2.0, (start_longitude + end_longitude) / 2.0
            FROM "Year_2025".corridor_segments
            WHERE start_latitude IS NOT NULL AND end_latitude IS NOT NULL
            UNION
            SELECT t.tmc, (t.start_latitude + t.end_latitude) / 2.0, (t.start_longitude + t.end_longitude) / 2.0
            FROM "Year_2025".watch_segments AS w
            JOIN "Year_2025".tmc_metadata AS t ON t.tmc = w.tmc
            WHERE t.start_latitude IS NOT NULL AND t.end_latitude IS NOT NULL
            """
        )
        return cursor.fetchall()


def build_tmc_cid_lookup(connection: psycopg.Connection) -> dict:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS "Year_2025".tmc_cid (
                tmc text PRIMARY KEY,
                cid_name text
            )
            """
        )
    connection.commit()

    cid_polygons = _load_cid_polygons()
    tmcs = _needed_tmcs(connection)

    matched = 0
    rows = []
    for tmc, lat, lon in tmcs:
        point = Point(lon, lat)  # GeoJSON order: (x=lon, y=lat)
        cid_name = None
        for name, polygon in cid_polygons:
            if polygon.contains(point):
                cid_name = name
                matched += 1
                break
        rows.append((tmc, cid_name))

    with connection.cursor() as cursor:
        cursor.execute('TRUNCATE "Year_2025".tmc_cid')
        cursor.executemany(
            'INSERT INTO "Year_2025".tmc_cid (tmc, cid_name) VALUES (%s, %s)',
            rows,
        )
    connection.commit()

    return {
        "stage": "tmc_cid_lookup",
        "cid_polygons_loaded": len(cid_polygons),
        "tmcs_checked": len(tmcs),
        "tmcs_matched_to_a_cid": matched,
    }


def main() -> None:
    with psycopg.connect(**cbi_database.connection_kwargs()) as connection:
        result = build_tmc_cid_lookup(connection)
    print(result)


if __name__ == "__main__":
    main()
