"""
Corridor-parameterized version of SQL master statements 016-017
(i75_nb_segment_daily / i75_nb_segment_profile), rewritten against the
shared segment_daily / segment_profile tables from
sql/003_multicorridor_migration.sql and corridor_segments instead of
corridor_i75_nb.

Run after cbi_corridor_events.run_events_for_corridor() for a corridor,
before cbi_corridor_bottlenecks.py, since bottleneck peak-detection reads
segment_profile.
"""

from __future__ import annotations

import time

import psycopg

from cbi_corridor_registry import CorridorContext


def build_segment_profile_for_corridor(
    connection: psycopg.Connection,
    corridor: CorridorContext,
) -> dict:
    started_at = time.perf_counter()

    with connection.cursor() as cursor:
        # --- Step 1: segment_daily (one row per segment per day) ---------
        cursor.execute(
            """
            DELETE FROM "Year_2025".segment_daily
            WHERE corridor_id = %s
            """,
            (corridor.corridor_id,),
        )

        cursor.execute(
            """
            INSERT INTO "Year_2025".segment_daily (
                corridor_id, analysis_date, segment_order, tmc,
                intersection, miles, observed_intervals,
                congested_intervals, congested_minutes,
                average_speed_ratio, congested_average_speed_ratio
            )
            SELECT
                %(corridor_id)s,
                r.measurement_tstamp::date AS analysis_date,
                c.segment_order::integer AS segment_order,
                c.tmc,
                c.intersection,
                c.miles,
                COUNT(*) FILTER (
                    WHERE r.speed IS NOT NULL
                      AND r.reference_speed > 0
                ) AS observed_intervals,
                COUNT(*) FILTER (
                    WHERE r.speed IS NOT NULL
                      AND r.reference_speed > 0
                      AND r.speed < %(ratio)s * r.reference_speed
                ) AS congested_intervals,
                COUNT(*) FILTER (
                    WHERE r.speed IS NOT NULL
                      AND r.reference_speed > 0
                      AND r.speed < %(ratio)s * r.reference_speed
                ) * 5 AS congested_minutes,
                AVG(r.speed / NULLIF(r.reference_speed, 0)) FILTER (
                    WHERE r.speed IS NOT NULL
                      AND r.reference_speed > 0
                ) AS average_speed_ratio,
                AVG(r.speed / NULLIF(r.reference_speed, 0)) FILTER (
                    WHERE r.speed IS NOT NULL
                      AND r.reference_speed > 0
                      AND r.speed < %(ratio)s * r.reference_speed
                ) AS congested_average_speed_ratio
            FROM "Year_2025".corridor_segments AS c
            JOIN "Year_2025".probe_readings AS r
              ON r.tmc_code = c.tmc
            WHERE c.corridor_id = %(corridor_id)s
            GROUP BY
                r.measurement_tstamp::date,
                c.segment_order,
                c.tmc,
                c.intersection,
                c.miles
            """,
            {"corridor_id": corridor.corridor_id, "ratio": corridor.congestion_ratio},
        )

        segment_daily_rows = cursor.rowcount

        # --- Step 2: segment_profile (one row per segment, annual) -------
        cursor.execute(
            """
            DELETE FROM "Year_2025".segment_profile
            WHERE corridor_id = %s
            """,
            (corridor.corridor_id,),
        )

        cursor.execute(
            """
            INSERT INTO "Year_2025".segment_profile (
                corridor_id, segment_order, tmc, intersection, miles,
                analyzed_days, occurrence_days, weekday_occurrence_pct,
                avg_congested_minutes_occurrence_day,
                median_congested_minutes, p95_congested_minutes,
                annual_segment_mile_hours, average_congested_speed_ratio
            )
            SELECT
                %(corridor_id)s,
                segment_order,
                MAX(tmc) AS tmc,
                MAX(intersection) AS intersection,
                MAX(miles) AS miles,
                COUNT(*) AS analyzed_days,
                COUNT(*) FILTER (
                    WHERE congested_minutes >= 30
                ) AS occurrence_days,
                ROUND(
                    (
                        100.0
                        * COUNT(*) FILTER (WHERE congested_minutes >= 30)
                        / COUNT(*)
                    )::numeric,
                    2
                ) AS weekday_occurrence_pct,
                ROUND(
                    AVG(congested_minutes)
                    FILTER (WHERE congested_minutes >= 30)::numeric,
                    1
                ) AS avg_congested_minutes_occurrence_day,
                ROUND(
                    PERCENTILE_CONT(0.50) WITHIN GROUP (
                        ORDER BY congested_minutes
                    ) FILTER (WHERE congested_minutes >= 30)::numeric,
                    1
                ) AS median_congested_minutes,
                ROUND(
                    PERCENTILE_CONT(0.95) WITHIN GROUP (
                        ORDER BY congested_minutes
                    ) FILTER (WHERE congested_minutes >= 30)::numeric,
                    1
                ) AS p95_congested_minutes,
                ROUND(
                    (
                        SUM(congested_intervals)
                        * MAX(miles)
                        * 5.0 / 60.0
                    )::numeric,
                    3
                ) AS annual_segment_mile_hours,
                ROUND(
                    AVG(congested_average_speed_ratio)
                    FILTER (WHERE congested_intervals > 0)::numeric,
                    3
                ) AS average_congested_speed_ratio
            FROM "Year_2025".segment_daily
            WHERE corridor_id = %(corridor_id)s
            GROUP BY segment_order
            """,
            {"corridor_id": corridor.corridor_id},
        )

        segment_profile_rows = cursor.rowcount

    connection.commit()

    elapsed = time.perf_counter() - started_at

    return {
        "stage": "profile",
        "corridor": corridor.corridor_name,
        "segment_daily_rows": segment_daily_rows,
        "segment_profile_rows": segment_profile_rows,
        "runtime_minutes": round(elapsed / 60, 2),
    }
