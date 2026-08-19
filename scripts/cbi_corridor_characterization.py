"""
Corridor-parameterized version of cbi_bottleneck_characterization.py.

Logic (load, growth/dissipation linear fit, daily metric calculation) is
unchanged from the validated I-75 NB script — only the corridor filter and
the segment source (corridor_segments instead of corridor_i75_nb) are
parameterized.
"""

from __future__ import annotations

import time
from datetime import timedelta

import numpy as np
import polars as pl
import psycopg

import cbi_database
from cbi_corridor_registry import CorridorContext

CONGESTION_RATIO = 0.70
MIN_ACTIVE_MINUTES = 30


def load_bottlenecks(
    corridor: CorridorContext,
) -> pl.DataFrame:
    query = f"""
        SELECT
            bottleneck_id, start_segment_order, end_segment_order,
            representative_intersection
        FROM "Year_2025".segment_recurring_bottlenecks
        WHERE corridor = '{corridor.road}'
          AND direction = '{corridor.direction}'
        ORDER BY bottleneck_id
    """

    dataframe = pl.read_database_uri(
        query=query,
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )

    # A corridor can legitimately have zero detected bottlenecks (e.g. genuinely
    # light traffic, or a corridor whose congestion is too spread out to form a
    # discrete peak — see cbi_generate_regional_report.py's fallback profile
    # chart for that case) — characterize_corridor() below handles an empty
    # dataframe by simply characterizing nothing, so this must not raise and
    # abort the corridor's pipeline over what is often a valid outcome.
    return dataframe


def load_bottleneck_observations(
    corridor: CorridorContext,
    start_segment_order: int,
    end_segment_order: int,
) -> pl.DataFrame:
    query = f"""
        SELECT
            r.measurement_tstamp,
            COUNT(*) FILTER (
                WHERE r.speed IS NOT NULL
                  AND r.reference_speed > 0
                  AND r.speed < {CONGESTION_RATIO} * r.reference_speed
            )::integer AS congested_segments,
            COALESCE(
                SUM(c.miles) FILTER (
                    WHERE r.speed IS NOT NULL
                      AND r.reference_speed > 0
                      AND r.speed < {CONGESTION_RATIO} * r.reference_speed
                ),
                0
            )::double precision AS queue_miles,
            AVG(r.speed / NULLIF(r.reference_speed, 0)) FILTER (
                WHERE r.speed IS NOT NULL
                  AND r.reference_speed > 0
                  AND r.speed < {CONGESTION_RATIO} * r.reference_speed
            )::double precision AS average_congested_speed_ratio,
            MIN(r.speed / NULLIF(r.reference_speed, 0)) FILTER (
                WHERE r.speed IS NOT NULL
                  AND r.reference_speed > 0
            )::double precision AS minimum_speed_ratio
        FROM "Year_2025".corridor_segments AS c
        JOIN "Year_2025".probe_readings AS r
          ON r.tmc_code = c.tmc
        WHERE c.corridor_id = {corridor.corridor_id}
          AND c.segment_order BETWEEN {start_segment_order} AND {end_segment_order}
        GROUP BY r.measurement_tstamp
        ORDER BY r.measurement_tstamp
    """

    dataframe = pl.read_database_uri(
        query=query,
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )

    if dataframe.is_empty():
        raise RuntimeError(
            f"No probe records for {corridor.corridor_name} segments "
            f"{start_segment_order}\u2013{end_segment_order}."
        )

    return dataframe.with_columns(
        pl.col("measurement_tstamp").dt.date().alias("analysis_date")
    )


def calculate_linear_rate(
    timestamps: list,
    values: list[float],
) -> float | None:
    if len(timestamps) < 3:
        return None

    start_timestamp = timestamps[0]
    elapsed_hours = np.asarray(
        [(t - start_timestamp).total_seconds() / 3600.0 for t in timestamps],
        dtype=float,
    )
    queue_values = np.asarray(values, dtype=float)

    if np.ptp(elapsed_hours) <= 0:
        return None

    slope, _ = np.polyfit(elapsed_hours, queue_values, 1)
    return float(slope)


def calculate_daily_metrics(
    bottleneck_id: int,
    dataframe: pl.DataFrame,
) -> list[dict]:
    daily_results: list[dict] = []

    analysis_dates = dataframe["analysis_date"].unique().sort().to_list()

    for analysis_date in analysis_dates:
        daily = (
            dataframe.filter(pl.col("analysis_date") == analysis_date)
            .sort("measurement_tstamp")
        )
        active = daily.filter(pl.col("queue_miles") > 0)
        active_minutes = active.height * 5

        if active.is_empty() or active_minutes < MIN_ACTIVE_MINUTES:
            daily_results.append(
                {
                    "bottleneck_id": bottleneck_id,
                    "analysis_date": analysis_date,
                    "occurrence": False,
                    "onset_time": None,
                    "peak_time": None,
                    "clearance_time": None,
                    "active_congestion_minutes": active_minutes,
                    "episode_duration_minutes": 0,
                    "maximum_queue_miles": 0.0,
                    "average_queue_miles": 0.0,
                    "queue_mile_hours": 0.0,
                    "maximum_congested_segments": 0,
                    "average_congested_speed_ratio": None,
                    "minimum_speed_ratio": None,
                    "queue_growth_rate_mph": None,
                    "queue_dissipation_rate_mph": None,
                }
            )
            continue

        onset_time = active["measurement_tstamp"].min()
        last_active_time = active["measurement_tstamp"].max()
        clearance_time = last_active_time + timedelta(minutes=5)
        episode_duration_minutes = int(
            (clearance_time - onset_time).total_seconds() / 60
        )

        peak_row = active.sort(
            ["queue_miles", "measurement_tstamp"], descending=[True, False]
        ).row(0, named=True)
        peak_time = peak_row["measurement_tstamp"]

        growth = active.filter(pl.col("measurement_tstamp") <= peak_time)
        dissipation = active.filter(pl.col("measurement_tstamp") >= peak_time)

        growth_rate = calculate_linear_rate(
            growth["measurement_tstamp"].to_list(), growth["queue_miles"].to_list()
        )
        dissipation_rate = calculate_linear_rate(
            dissipation["measurement_tstamp"].to_list(),
            dissipation["queue_miles"].to_list(),
        )

        queue_mile_hours = float(active["queue_miles"].sum() * 5.0 / 60.0)

        daily_results.append(
            {
                "bottleneck_id": bottleneck_id,
                "analysis_date": analysis_date,
                "occurrence": True,
                "onset_time": onset_time,
                "peak_time": peak_time,
                "clearance_time": clearance_time,
                "active_congestion_minutes": active_minutes,
                "episode_duration_minutes": episode_duration_minutes,
                "maximum_queue_miles": round(float(active["queue_miles"].max()), 3),
                "average_queue_miles": round(float(active["queue_miles"].mean()), 3),
                "queue_mile_hours": round(queue_mile_hours, 3),
                "maximum_congested_segments": int(
                    active["congested_segments"].max()
                ),
                "average_congested_speed_ratio": round(
                    float(active["average_congested_speed_ratio"].mean()), 3
                ),
                "minimum_speed_ratio": round(
                    float(active["minimum_speed_ratio"].min()), 3
                ),
                "queue_growth_rate_mph": (
                    round(growth_rate, 3) if growth_rate is not None else None
                ),
                "queue_dissipation_rate_mph": (
                    round(dissipation_rate, 3)
                    if dissipation_rate is not None
                    else None
                ),
            }
        )

    return daily_results


def save_daily_metrics(rows: list[dict]) -> None:
    query = """
        INSERT INTO "Year_2025".bottleneck_daily_metrics (
            bottleneck_id, analysis_date, occurrence, onset_time, peak_time,
            clearance_time, active_congestion_minutes,
            episode_duration_minutes, maximum_queue_miles,
            average_queue_miles, queue_mile_hours,
            maximum_congested_segments, average_congested_speed_ratio,
            minimum_speed_ratio, queue_growth_rate_mph,
            queue_dissipation_rate_mph
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (bottleneck_id, analysis_date) DO UPDATE SET
            occurrence = EXCLUDED.occurrence,
            onset_time = EXCLUDED.onset_time,
            peak_time = EXCLUDED.peak_time,
            clearance_time = EXCLUDED.clearance_time,
            active_congestion_minutes = EXCLUDED.active_congestion_minutes,
            episode_duration_minutes = EXCLUDED.episode_duration_minutes,
            maximum_queue_miles = EXCLUDED.maximum_queue_miles,
            average_queue_miles = EXCLUDED.average_queue_miles,
            queue_mile_hours = EXCLUDED.queue_mile_hours,
            maximum_congested_segments = EXCLUDED.maximum_congested_segments,
            average_congested_speed_ratio =
                EXCLUDED.average_congested_speed_ratio,
            minimum_speed_ratio = EXCLUDED.minimum_speed_ratio,
            queue_growth_rate_mph = EXCLUDED.queue_growth_rate_mph,
            queue_dissipation_rate_mph = EXCLUDED.queue_dissipation_rate_mph
    """

    values = [
        (
            row["bottleneck_id"], row["analysis_date"], row["occurrence"],
            row["onset_time"], row["peak_time"], row["clearance_time"],
            row["active_congestion_minutes"], row["episode_duration_minutes"],
            row["maximum_queue_miles"], row["average_queue_miles"],
            row["queue_mile_hours"], row["maximum_congested_segments"],
            row["average_congested_speed_ratio"], row["minimum_speed_ratio"],
            row["queue_growth_rate_mph"], row["queue_dissipation_rate_mph"],
        )
        for row in rows
    ]

    with psycopg.connect(**cbi_database.connection_kwargs()) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(query, values)
        connection.commit()


def characterize_corridor(corridor: CorridorContext) -> dict:
    started_at = time.perf_counter()

    bottlenecks = load_bottlenecks(corridor)
    all_rows: list[dict] = []

    for bottleneck in bottlenecks.iter_rows(named=True):
        observations = load_bottleneck_observations(
            corridor=corridor,
            start_segment_order=int(bottleneck["start_segment_order"]),
            end_segment_order=int(bottleneck["end_segment_order"]),
        )
        daily_rows = calculate_daily_metrics(
            bottleneck_id=int(bottleneck["bottleneck_id"]),
            dataframe=observations,
        )
        all_rows.extend(daily_rows)

    save_daily_metrics(all_rows)

    elapsed = time.perf_counter() - started_at

    return {
        "stage": "characterization",
        "corridor": corridor.corridor_name,
        "bottlenecks_characterized": bottlenecks.height,
        "daily_rows_saved": len(all_rows),
        "runtime_minutes": round(elapsed / 60, 2),
    }
