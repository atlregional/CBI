"""
Flags individual Arterial-group bottlenecks that show the false-flag
signature confirmed this session (Griffin St/Macon St on GA-20, Fayetteville
Rd/North Ave on GA-54, and — after the probe_readings reload made the full
picture visible — Camp Creek Pkwy, GA-5-Conn, and Abernathy Rd too): a flat,
non-time-varying reference_speed sets too aggressive a bar for a
signal-controlled or ramp-heavy road, so ordinary signal-cycle deceleration
or short-segment probe noise alone clears the 70%-of-reference-speed
congestion test even in the dead of night with no real traffic.

The reliable diagnostic is the overnight (12 AM-4 AM) congested-reading
rate — confirmed false flags run 16-46%, well above genuine background
noise. Two other proxies were tried and rejected this session:
"whole corridor consumed" (flagged genuine short interchange-approach
bottlenecks) and "rural segments present" (missed genuine false flags on
fully-urban corridors). A "low AADT + sparse sampling" theory was also
tested directly against data_density and rejected — the correlation between
AADT and low-density-sample rate runs the opposite direction (+0.40) from
what that theory predicts, so it isn't the mechanism at play. This
diagnostic measures the actual overnight-congestion mechanism directly
instead of proxying it, applied to every Arterial bottleneck regardless of
corridor length — the length restriction used in an earlier version of this
script was never load-bearing; the mechanism isn't tied to how long the
corridor is.

Requires sql/007_bottleneck_ranking_eligibility.sql to have been applied
(adds overnight_congested_pct / ranking_eligible to
segment_recurring_bottlenecks).
"""

from __future__ import annotations

import psycopg
import polars as pl

import cbi_database

OVERNIGHT_HOURS = (0, 1, 2, 3, 4)
OVERNIGHT_CONGESTION_THRESHOLD_PCT = 15.0


def compute_bottleneck_ranking_eligibility(connection: psycopg.Connection) -> dict:
    candidates = pl.read_database_uri(
        query="""
            SELECT b.bottleneck_id, b.corridor, b.direction,
                   b.representative_intersection, cs.tmc
            FROM "Year_2025".segment_recurring_bottlenecks AS b
            JOIN "Year_2025".corridor_definitions AS cd
              ON cd.road = b.corridor AND cd.direction = b.direction AND cd.is_active
            JOIN "Year_2025".corridor_segments AS cs
              ON cs.corridor_id = cd.corridor_id
             AND cs.segment_order BETWEEN b.start_segment_order AND b.end_segment_order
            WHERE cd.corridor_group = 'Arterial'
        """,
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )

    updates: list[tuple[float, bool, int]] = []
    checked = 0
    flagged = 0

    for bottleneck_id in candidates["bottleneck_id"].unique().to_list():
        tmcs = (
            candidates.filter(pl.col("bottleneck_id") == bottleneck_id)["tmc"]
            .to_list()
        )

        tmc_list = ", ".join(f"'{tmc}'" for tmc in tmcs)
        overnight = pl.read_database_uri(
            query=f"""
                SELECT
                    100.0 * AVG(
                        CASE WHEN speed < 0.70 * reference_speed THEN 1.0 ELSE 0.0 END
                    ) AS pct_congested
                FROM "Year_2025".probe_readings
                WHERE tmc_code IN ({tmc_list})
                  AND EXTRACT(HOUR FROM measurement_tstamp)::int IN {OVERNIGHT_HOURS}
                  AND reference_speed > 0
            """,
            uri=cbi_database.database_uri(),
            engine="connectorx",
        )

        pct = overnight["pct_congested"][0]
        if pct is None:
            continue

        is_eligible = pct < OVERNIGHT_CONGESTION_THRESHOLD_PCT
        updates.append((float(pct), is_eligible, int(bottleneck_id)))
        checked += 1
        if not is_eligible:
            flagged += 1

    with connection.cursor() as cursor:
        cursor.executemany(
            """
            UPDATE "Year_2025".segment_recurring_bottlenecks
            SET overnight_congested_pct = %s, ranking_eligible = %s
            WHERE bottleneck_id = %s
            """,
            updates,
        )
    connection.commit()

    return {
        "stage": "bottleneck_ranking_eligibility",
        "candidates_checked": checked,
        "flagged_false_positive": flagged,
    }
