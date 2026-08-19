"""
Loads corridors to process from "Year_2025".corridor_definitions.

This is the single place every other multi-corridor script gets its list
of (corridor_id, road, direction) from, instead of each script hardcoding
CORRIDOR = "I-75" / DIRECTION = "NORTHBOUND" the way the original
single-corridor scripts do.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class CorridorContext:
    corridor_id: int
    road: str
    direction: str
    corridor_name: str
    corridor_group: str = "Interstate"
    congestion_ratio: float = 0.70

    @property
    def corridor(self) -> str:
        """Alias matching the CORRIDOR value used throughout the original
        single-corridor scripts (e.g. congestion_events.corridor)."""
        return self.road

    @property
    def slug(self) -> str:
        """Filesystem/URL-safe identifier, e.g. 'i75-nb'."""
        return f"{self.road.lower().replace('/', '-')}-{self.direction[:2].lower()}"


def get_active_corridors(
    connection: psycopg.Connection,
) -> list[CorridorContext]:
    """Return every corridor marked active in corridor_definitions."""

    query = """
        SELECT corridor_id, road, direction, corridor_name, corridor_group
        FROM "Year_2025".corridor_definitions
        WHERE is_active
        ORDER BY corridor_name
    """

    with connection.cursor() as cursor:
        cursor.execute(query)
        rows = cursor.fetchall()

    if not rows:
        raise RuntimeError(
            "No active rows in corridor_definitions. "
            "Seed it (SQL master statement 027) before running the "
            "multi-corridor pipeline."
        )

    return [
        CorridorContext(
            corridor_id=row[0],
            road=row[1],
            direction=row[2],
            corridor_name=row[3],
            corridor_group=row[4],
        )
        for row in rows
    ]


def get_corridor(
    connection: psycopg.Connection,
    road: str,
    direction: str,
) -> CorridorContext:
    """Look up a single corridor by road/direction (e.g. for a manual re-run)."""

    query = """
        SELECT corridor_id, road, direction, corridor_name, corridor_group
        FROM "Year_2025".corridor_definitions
        WHERE road = %s AND direction = %s
    """

    with connection.cursor() as cursor:
        cursor.execute(query, (road, direction))
        row = cursor.fetchone()

    if row is None:
        raise RuntimeError(
            f"No corridor_definitions row for road={road!r} "
            f"direction={direction!r}."
        )

    return CorridorContext(
        corridor_id=row[0],
        road=row[1],
        direction=row[2],
        corridor_name=row[3],
        corridor_group=row[4],
    )
