"""
Lightweight per-segment congestion summary for the Collector/Local "watch
list" seeded by sql/004_extended_road_classes.sql (top 25 Collector + top 10
Local TMCs by AADT).

Unlike Interstates/Arterials, most Collector/Local road/direction pairs are
only 1-2 TMCs long region-wide, so the spatial recurring-bottleneck detection
in cbi_corridor_bottlenecks.py (which needs a multi-segment corridor to find
queue extent) doesn't apply. Instead this computes the same annual
occurrence-day statistics as cbi_corridor_profile.py's segment_profile step,
directly per TMC, with no corridor_id grouping and no segment_daily storage.
"""

from __future__ import annotations

import time

import psycopg

CONGESTION_RATIO = 0.70


def compute_watch_segment_metrics(connection: psycopg.Connection) -> dict:
    started_at = time.perf_counter()

    with connection.cursor() as cursor:
        cursor.execute(
            """
            WITH daily AS (
                SELECT
                    w.tmc,
                    w.miles,
                    r.measurement_tstamp::date AS analysis_date,
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
                          AND r.speed < %(ratio)s * r.reference_speed
                    ) AS congested_average_speed_ratio
                FROM "Year_2025".watch_segments AS w
                JOIN "Year_2025".probe_readings AS r
                  ON r.tmc_code = w.tmc
                GROUP BY w.tmc, w.miles, r.measurement_tstamp::date
            ),
            annual AS (
                SELECT
                    tmc,
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
                    ) AS occurrence_pct,
                    ROUND(
                        AVG(congested_minutes)
                        FILTER (WHERE congested_minutes >= 30)::numeric,
                        1
                    ) AS avg_congested_minutes_occurrence_day,
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
                FROM daily
                GROUP BY tmc
            ),
            congested_readings AS (
                SELECT
                    w.tmc,
                    EXTRACT(HOUR FROM r.measurement_tstamp)::integer AS hour,
                    EXTRACT(ISODOW FROM r.measurement_tstamp)::integer AS dow
                FROM "Year_2025".watch_segments AS w
                JOIN "Year_2025".probe_readings AS r
                  ON r.tmc_code = w.tmc
                WHERE r.speed IS NOT NULL
                  AND r.reference_speed > 0
                  AND r.speed < %(ratio)s * r.reference_speed
            ),
            hour_ranked AS (
                SELECT tmc, hour, COUNT(*) AS n,
                       ROW_NUMBER() OVER (PARTITION BY tmc ORDER BY COUNT(*) DESC) AS rn
                FROM congested_readings
                GROUP BY tmc, hour
            ),
            dow_ranked AS (
                SELECT tmc, dow, COUNT(*) AS n,
                       ROW_NUMBER() OVER (PARTITION BY tmc ORDER BY COUNT(*) DESC) AS rn
                FROM congested_readings
                GROUP BY tmc, dow
            ),
            peak AS (
                SELECT
                    h.tmc,
                    h.hour AS peak_hour,
                    TO_CHAR(make_time(h.hour, 0, 0), 'HH12:00 AM') AS peak_hour_label,
                    (ARRAY['Monday', 'Tuesday', 'Wednesday', 'Thursday',
                           'Friday', 'Saturday', 'Sunday'])[d.dow] AS peak_weekday_name
                FROM hour_ranked AS h
                JOIN dow_ranked AS d ON d.tmc = h.tmc AND d.rn = 1
                WHERE h.rn = 1
            )
            INSERT INTO "Year_2025".watch_segment_metrics (
                tmc, analyzed_days, occurrence_days, occurrence_pct,
                avg_congested_minutes_occurrence_day,
                annual_segment_mile_hours, average_congested_speed_ratio,
                peak_hour, peak_hour_label, peak_weekday_name,
                computed_at
            )
            SELECT
                a.tmc, a.analyzed_days, a.occurrence_days, a.occurrence_pct,
                a.avg_congested_minutes_occurrence_day,
                a.annual_segment_mile_hours, a.average_congested_speed_ratio,
                p.peak_hour, p.peak_hour_label, p.peak_weekday_name,
                now()
            FROM annual AS a
            LEFT JOIN peak AS p ON p.tmc = a.tmc
            ON CONFLICT (tmc) DO UPDATE SET
                analyzed_days = EXCLUDED.analyzed_days,
                occurrence_days = EXCLUDED.occurrence_days,
                occurrence_pct = EXCLUDED.occurrence_pct,
                avg_congested_minutes_occurrence_day =
                    EXCLUDED.avg_congested_minutes_occurrence_day,
                annual_segment_mile_hours =
                    EXCLUDED.annual_segment_mile_hours,
                average_congested_speed_ratio =
                    EXCLUDED.average_congested_speed_ratio,
                peak_hour = EXCLUDED.peak_hour,
                peak_hour_label = EXCLUDED.peak_hour_label,
                peak_weekday_name = EXCLUDED.peak_weekday_name,
                computed_at = EXCLUDED.computed_at
            """,
            {"ratio": CONGESTION_RATIO},
        )

        rows_written = cursor.rowcount

    connection.commit()

    elapsed = time.perf_counter() - started_at

    return {
        "stage": "watch_segments",
        "segments_updated": rows_written,
        "runtime_minutes": round(elapsed / 60, 2),
    }
