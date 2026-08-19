"""
Corridor-parameterized version of cbi_annual_runner.py.

Runs cbi_detector.detect_events() (unchanged) for every remaining date on
one corridor, tracking completion the same way the original single-corridor
runner does — safe to re-run, only processes dates not already saved.
"""

from __future__ import annotations

import time
import traceback
from datetime import date
from pathlib import Path

import psycopg

import cbi_corridor_database as corridor_db
import cbi_detector
from cbi_corridor_registry import CorridorContext


def run_events_for_corridor(
    connection: psycopg.Connection,
    corridor: CorridorContext,
    error_log: Path,
    day_limit: int | None = None,
) -> dict:
    """
    Detect and save congestion events for every remaining day on one
    corridor. Returns a small summary dict for the orchestrator's log.
    """

    started_at = time.perf_counter()

    available_dates = corridor_db.get_available_dates(connection, corridor)
    completed_dates = corridor_db.get_completed_dates(connection, corridor)

    remaining_dates = [
        analysis_date
        for analysis_date in available_dates
        if analysis_date not in completed_dates
    ]

    if day_limit is not None:
        remaining_dates = remaining_dates[:day_limit]

    successful_days = 0
    failed_days = 0
    inserted_events = 0

    for position, analysis_date in enumerate(remaining_dates, start=1):
        try:
            dataframe = corridor_db.load_corridor_day(corridor, analysis_date)

            # cbi_detector.detect_events() is unchanged from the I-75 NB
            # pipeline — it only needs the dataframe shape, which
            # cbi_corridor_database.load_corridor_day() already matches.
            events = cbi_detector.detect_events(
                dataframe=dataframe,
                analysis_date=analysis_date,
            )

            corridor_db.replace_daily_events(
                connection=connection,
                corridor=corridor,
                analysis_date=analysis_date,
                events=events,
            )

            connection.commit()

            successful_days += 1
            inserted_events += events.height

        except Exception as error:  # noqa: BLE001 - logged, loop continues
            connection.rollback()
            failed_days += 1
            _write_error(error_log, corridor, analysis_date, error)

    elapsed = time.perf_counter() - started_at

    return {
        "stage": "events",
        "corridor": corridor.corridor_name,
        "available_days": len(available_dates),
        "already_completed": len(completed_dates),
        "processed_this_run": len(remaining_dates),
        "successful_days": successful_days,
        "failed_days": failed_days,
        "events_inserted": inserted_events,
        "runtime_minutes": round(elapsed / 60, 2),
    }


def _write_error(
    error_log: Path,
    corridor: CorridorContext,
    analysis_date: date,
    error: BaseException,
) -> None:
    error_log.parent.mkdir(parents=True, exist_ok=True)

    with error_log.open("a", encoding="utf-8") as file:
        file.write("\n" + "=" * 80 + "\n")
        file.write(f"Corridor: {corridor.corridor_name}\n")
        file.write(f"Date: {analysis_date}\n")
        file.write(f"Error: {error}\n")
        file.write(traceback.format_exc())
        file.write("\n")
