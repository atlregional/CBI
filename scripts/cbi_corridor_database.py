"""
Corridor-parameterized version of cbi_database.py.

Every function here takes a CorridorContext instead of reading a
module-level CORRIDOR / DIRECTION constant, so the same functions work for
any corridor in corridor_definitions, not just I-75 Northbound.

Requires: sql/003_multicorridor_migration.sql has been run once.

This module intentionally reuses cbi_database.connection_kwargs() and
cbi_database.database_uri() unchanged — those two are already environment
driven and have nothing corridor-specific about them.
"""

from __future__ import annotations

from datetime import date, datetime

import polars as pl
import psycopg

import cbi_database
from cbi_corridor_registry import CorridorContext


def get_available_dates(
    connection: psycopg.Connection,
    corridor: CorridorContext,
) -> list[date]:
    """All dates with at least one probe observation on this corridor."""

    query = """
        SELECT DISTINCT r.measurement_tstamp::date AS analysis_date
        FROM "Year_2025".probe_readings AS r
        JOIN "Year_2025".corridor_segments AS c
          ON c.tmc = r.tmc_code
         AND c.corridor_id = %s
        ORDER BY analysis_date
    """

    with connection.cursor() as cursor:
        cursor.execute(query, (corridor.corridor_id,))
        return [row[0] for row in cursor.fetchall()]


def get_completed_dates(
    connection: psycopg.Connection,
    corridor: CorridorContext,
) -> set[date]:
    """Dates already saved to congestion_events for this corridor."""

    query = """
        SELECT DISTINCT analysis_date
        FROM "Year_2025".congestion_events
        WHERE corridor = %s
          AND direction = %s
    """

    with connection.cursor() as cursor:
        cursor.execute(query, (corridor.road, corridor.direction))
        return {row[0] for row in cursor.fetchall()}


def load_corridor_day(
    corridor: CorridorContext,
    analysis_date: date,
) -> pl.DataFrame:
    """
    Load exactly one analysis date for one corridor, in the same column
    shape cbi_detector.detect_events() already expects (segment_order, tmc,
    intersection, miles, measurement_tstamp, speed, reference_speed) — so
    detect_events() itself needs no changes to work with any corridor.
    """

    date_text = analysis_date.isoformat()

    query = f"""
        SELECT
            c.segment_order::integer AS segment_order,
            c.tmc,
            c.intersection,
            c.miles::double precision AS miles,
            r.measurement_tstamp,
            r.speed::double precision AS speed,
            r.reference_speed::double precision AS reference_speed
        FROM "Year_2025".corridor_segments AS c
        JOIN "Year_2025".probe_readings AS r
          ON r.tmc_code = c.tmc
        WHERE c.corridor_id = {corridor.corridor_id}
          AND r.measurement_tstamp >=
              TIMESTAMP '{date_text} 00:00:00'
          AND r.measurement_tstamp <
              TIMESTAMP '{date_text} 00:00:00'
              + INTERVAL '1 day'
        ORDER BY r.measurement_tstamp, c.segment_order
    """

    dataframe = pl.read_database_uri(
        query=query,
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )

    if dataframe.is_empty():
        raise RuntimeError(
            f"No {corridor.corridor_name} observations found for "
            f"{analysis_date}."
        )

    actual_dates = (
        dataframe
        .select(pl.col("measurement_tstamp").dt.date().unique())
        .to_series()
        .to_list()
    )

    if actual_dates != [analysis_date]:
        raise RuntimeError(
            f"Requested {analysis_date}, but database returned "
            f"dates {actual_dates}."
        )

    return dataframe


def replace_daily_events(
    connection: psycopg.Connection,
    corridor: CorridorContext,
    analysis_date: date,
    events: pl.DataFrame,
) -> None:
    """
    Same behavior as cbi_database.replace_daily_events, with corridor and
    direction coming from the CorridorContext rather than module-level
    constants. congestion_events already has (analysis_date, corridor,
    direction, event_id) as its primary key, so different corridors never
    collide here — unlike segment_recurring_bottlenecks before migration 003.
    """

    delete_query = """
        DELETE FROM "Year_2025".congestion_events
        WHERE analysis_date = %s
          AND corridor = %s
          AND direction = %s
    """

    insert_query = """
        INSERT INTO "Year_2025".congestion_events (
            analysis_date, corridor, direction, event_id,
            start_time, end_time, duration_minutes, cell_count,
            segment_count, first_segment_order, last_segment_order,
            first_tmc, last_tmc, first_intersection, last_intersection,
            maximum_contiguous_extent_miles, maximum_total_active_miles,
            event_area_mile_hours, average_speed_mph, minimum_speed_ratio,
            average_speed_ratio, average_speed_drop_mph,
            maximum_speed_drop_mph, estimated_upstream_boundary_speed_mph,
            corridor_start_mile, corridor_end_mile
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s
        )
    """

    with connection.cursor() as cursor:
        cursor.execute(
            delete_query,
            (analysis_date, corridor.road, corridor.direction),
        )

        if events.is_empty():
            return

        rows = []

        for event in events.to_dicts():
            rows.append(
                (
                    analysis_date,
                    corridor.road,
                    corridor.direction,
                    int(event["event_id"]),
                    datetime.fromisoformat(event["start_time"]),
                    datetime.fromisoformat(event["end_time"]),
                    int(event["duration_minutes"]),
                    int(event["cell_count"]),
                    int(event["segment_count"]),
                    int(event["first_segment_order"]),
                    int(event["last_segment_order"]),
                    event.get("first_tmc"),
                    event.get("last_tmc"),
                    event.get("first_intersection"),
                    event.get("last_intersection"),
                    event.get("maximum_contiguous_extent_miles"),
                    event.get("maximum_total_active_miles"),
                    event.get("event_area_mile_hours"),
                    event.get("average_speed_mph"),
                    event.get("minimum_speed_ratio"),
                    event.get("average_speed_ratio"),
                    event.get("average_speed_drop_mph"),
                    event.get("maximum_speed_drop_mph"),
                    event.get("estimated_upstream_boundary_speed_mph"),
                    event.get("corridor_start_mile"),
                    event.get("corridor_end_mile"),
                )
            )

        cursor.executemany(insert_query, rows)
