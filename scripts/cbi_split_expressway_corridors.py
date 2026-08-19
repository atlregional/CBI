"""
One-time restructuring: split each Expressway-group corridor at
classification boundaries instead of analyzing it as one blended route.

Migration 004 registered a corridor for every road/direction with >= 3
f_system=2 (limited-access) TMCs, then corridor_segments pulled in every
TMC on that whole named route regardless of each segment's own
classification. For several of these routes (US-78, US-19, Sugarloaf Pkwy,
GA-141, GA-166, ...) the majority of the corridor's mileage — and most of
its detected bottlenecks — actually sits on Arterial-classified (signalized)
segments sharing the route name, inheriting the reference-speed-threshold
artifact already documented for the Arterial group while hidden inside a
corridor labeled "Expressway" and competing in the freeway-oriented
network-wide ranking.

This finds, for each active Expressway corridor, contiguous runs (ordered by
tmc_metadata.road_order) of matching classification bucket — limited-access
(f_system 1/2) vs signalized (f_system 3/4) — and registers each run of at
least MIN_RUN_SEGMENTS as its own corridor_definitions row, correctly
labeled Expressway or Arterial. The original blended row is deactivated and
its stale event/profile/bottleneck data removed; each new row gets its own
corridor_id and runs through the ordinary pipeline stages like any other
corridor. Requires sql/006_corridor_split_schema.sql to have been applied
first (adds source_road / segment_order_start / segment_order_end).
"""

from __future__ import annotations

import psycopg
import polars as pl

import cbi_database

MIN_RUN_SEGMENTS = 3


def _bucket(f_system: int | None) -> str | None:
    if f_system in (1, 2):
        return "Expressway"
    if f_system in (3, 4):
        return "Arterial"
    return None


def plan_splits() -> list[dict]:
    """Read-only: return one plan dict per active Expressway corridor that
    has a genuine classification mix worth splitting."""

    corridors = pl.read_database_uri(
        query="""
            SELECT corridor_id, road, direction
            FROM "Year_2025".corridor_definitions
            WHERE corridor_group = 'Expressway' AND is_active
            ORDER BY road, direction
        """,
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )

    plans = []

    for row in corridors.iter_rows(named=True):
        segments = pl.read_database_uri(
            query=f"""
                SELECT road_order, tmc, intersection, f_system
                FROM "Year_2025".tmc_metadata
                WHERE road = '{row["road"]}' AND direction = '{row["direction"]}'
                ORDER BY road_order, tmc
            """,
            uri=cbi_database.database_uri(),
            engine="connectorx",
        )

        buckets = [_bucket(f) for f in segments["f_system"].to_list()]
        road_orders = segments["road_order"].to_list()
        intersections = segments["intersection"].to_list()

        runs = []
        run_start = 0
        for i in range(1, len(buckets) + 1):
            if i == len(buckets) or buckets[i] != buckets[run_start]:
                runs.append((run_start, i - 1))
                run_start = i

        eligible_runs = [
            (s, e) for s, e in runs
            if buckets[s] is not None and (e - s + 1) >= MIN_RUN_SEGMENTS
        ]

        distinct_buckets = {buckets[s] for s, e in eligible_runs}
        if len(eligible_runs) <= 1 and len(distinct_buckets) <= 1:
            # Already homogeneous (or the only surviving run covers
            # essentially the whole route) — nothing to split.
            continue

        plans.append({
            "road": row["road"],
            "direction": row["direction"],
            "corridor_id": row["corridor_id"],
            "total_segments": len(buckets),
            "runs": [
                {
                    "bucket": buckets[s],
                    "start_order": road_orders[s],
                    "end_order": road_orders[e],
                    "segment_count": e - s + 1,
                    "from_intersection": intersections[s],
                    "to_intersection": intersections[e],
                }
                for s, e in eligible_runs
            ],
        })

    return plans


def apply_splits(connection: psycopg.Connection, plans: list[dict]) -> list[tuple[str, str]]:
    """Deactivate each blended corridor, remove its now-superseded
    event/profile/bottleneck data, and register one new corridor_definitions
    row per surviving run. Returns the list of newly created
    (road, direction) pairs, ready to hand to the ordinary pipeline
    (cbi_run_all_corridors.run_corridor)."""

    created: list[tuple[str, str]] = []

    with connection.cursor() as cursor:
        for plan in plans:
            old_road, direction = plan["road"], plan["direction"]

            cursor.execute(
                """
                UPDATE "Year_2025".corridor_definitions
                SET is_active = false
                WHERE road = %s AND direction = %s
                """,
                (old_road, direction),
            )

            cursor.execute(
                'DELETE FROM "Year_2025".segment_recurring_bottlenecks '
                "WHERE corridor = %s AND direction = %s",
                (old_road, direction),
            )
            cursor.execute(
                'DELETE FROM "Year_2025".congestion_events '
                "WHERE corridor = %s AND direction = %s",
                (old_road, direction),
            )
            cursor.execute(
                'DELETE FROM "Year_2025".segment_daily WHERE corridor_id = %s',
                (plan["corridor_id"],),
            )
            cursor.execute(
                'DELETE FROM "Year_2025".segment_profile WHERE corridor_id = %s',
                (plan["corridor_id"],),
            )

            for index, run in enumerate(plan["runs"], start=1):
                new_road = f"{old_road}-{index}"
                corridor_name = (
                    f"{old_road} {direction.title()} "
                    f"({run['from_intersection']} to {run['to_intersection']})"
                )

                cursor.execute(
                    """
                    INSERT INTO "Year_2025".corridor_definitions (
                        road, direction, corridor_name, corridor_group,
                        is_active, source_road, segment_order_start, segment_order_end
                    )
                    VALUES (%s, %s, %s, %s, true, %s, %s, %s)
                    ON CONFLICT (road, direction) DO UPDATE SET
                        corridor_name = EXCLUDED.corridor_name,
                        corridor_group = EXCLUDED.corridor_group,
                        is_active = true,
                        source_road = EXCLUDED.source_road,
                        segment_order_start = EXCLUDED.segment_order_start,
                        segment_order_end = EXCLUDED.segment_order_end
                    """,
                    (
                        new_road, direction, corridor_name, run["bucket"],
                        old_road, run["start_order"], run["end_order"],
                    ),
                )

                created.append((new_road, direction))

    connection.commit()
    return created
