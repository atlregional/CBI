"""
Top-level entry point: runs event detection, segment profiling, recurring
bottleneck detection, characterization, and report generation for every
active corridor in corridor_definitions.

One failing corridor does not stop the others — each stage for each
corridor is wrapped and logged to "Year_2025".corridor_pipeline_runs, and
a summary is printed (and written to a log file) at the end.

Usage:
    python cbi_run_all_corridors.py
    python cbi_run_all_corridors.py --only "I-75,NORTHBOUND"
    python cbi_run_all_corridors.py --skip-report

Requires sql/003_multicorridor_migration.sql to have been run once.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

import psycopg

import cbi_database
import cbi_corridor_bottlenecks
import cbi_corridor_characterization
import cbi_corridor_events
import cbi_corridor_profile
import cbi_corridor_registry
import cbi_generate_corridor_report
import cbi_generate_regional_report
import cbi_watch_segments
from cbi_corridor_registry import CorridorContext

OUTPUT_ROOT = Path(r"C:\Users\Soheil\Desktop\CBI\outputs\multi_corridor")
ERROR_LOG = OUTPUT_ROOT / "pipeline_errors.txt"


def _log_stage(
    connection: psycopg.Connection,
    corridor: CorridorContext,
    stage: str,
    status: str,
    detail: str,
) -> None:
    """Best-effort write to corridor_pipeline_runs; never raises."""

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO "Year_2025".corridor_pipeline_runs (
                    corridor_id, stage, finished_at, status, detail
                )
                VALUES (%s, %s, now(), %s, %s)
                """,
                (corridor.corridor_id, stage, status, detail[:2000]),
            )
        connection.commit()
    except Exception:  # noqa: BLE001
        connection.rollback()


def run_corridor(
    connection: psycopg.Connection,
    corridor: CorridorContext,
    skip_report: bool,
) -> dict:
    print(f"\n{'=' * 70}")
    print(f"CORRIDOR: {corridor.corridor_name}")
    print(f"{'=' * 70}")

    stage_results = {}

    stages = [
        (
            "events",
            lambda: cbi_corridor_events.run_events_for_corridor(
                connection, corridor, ERROR_LOG
            ),
        ),
        (
            "profile",
            lambda: cbi_corridor_profile.build_segment_profile_for_corridor(
                connection, corridor
            ),
        ),
        (
            "bottlenecks",
            lambda: cbi_corridor_bottlenecks.detect_bottlenecks_for_corridor(
                connection, corridor
            ),
        ),
        (
            "characterization",
            lambda: cbi_corridor_characterization.characterize_corridor(
                corridor
            ),
        ),
    ]

    for stage_name, stage_function in stages:
        print(f"\n[{corridor.corridor_name}] Stage: {stage_name}...")

        try:
            result = stage_function()
            stage_results[stage_name] = result
            print(f"  OK: {result}")
            _log_stage(connection, corridor, stage_name, "success", str(result))

        except Exception as error:  # noqa: BLE001
            connection.rollback()
            error_text = f"{error}\n{traceback.format_exc()}"
            print(f"  FAILED: {error}")
            _log_stage(connection, corridor, stage_name, "failed", error_text)

            ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
            with ERROR_LOG.open("a", encoding="utf-8") as file:
                file.write(f"\n{'=' * 80}\n{corridor.corridor_name} / {stage_name}\n")
                file.write(error_text)

            # Downstream stages (profile needs events, bottlenecks needs
            # profile, characterization needs bottlenecks) can't proceed
            # meaningfully if an earlier one failed — stop this corridor,
            # move on to the next.
            stage_results["stopped_at"] = stage_name
            return stage_results

    if not skip_report:
        print(f"\n[{corridor.corridor_name}] Stage: report...")
        try:
            report_path = cbi_generate_corridor_report.generate_report(
                connection, corridor, OUTPUT_ROOT
            )
            stage_results["report"] = str(report_path)
            print(f"  OK: {report_path}")
            _log_stage(
                connection, corridor, "report", "success", str(report_path)
            )
        except Exception as error:  # noqa: BLE001
            connection.rollback()
            error_text = f"{error}\n{traceback.format_exc()}"
            print(f"  FAILED: {error}")
            _log_stage(connection, corridor, "report", "failed", error_text)

    return stage_results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--only",
        help="Comma-separated 'road,direction' to run just one corridor, "
        "e.g. --only 'I-75,NORTHBOUND'",
    )
    parser.add_argument(
        "--group",
        help="Only process corridors whose corridor_group matches exactly, "
        "e.g. --group Arterial. Useful for processing a newly-added road "
        "class without re-running bottleneck detection on corridors "
        "already completed (cbi_corridor_bottlenecks.py appends rather "
        "than replacing, so re-running an already-processed corridor "
        "duplicates its bottleneck rows).",
    )
    parser.add_argument(
        "--skip-report",
        action="store_true",
        help="Run the data pipeline stages but skip Word report generation.",
    )
    parser.add_argument(
        "--export-fabric",
        action="store_true",
        help="After all corridors finish, export combined dashboard data "
        "to Azure Data Lake / Fabric OneLake (requires "
        "cbi_azure_credentials.ini — see FABRIC_DASHBOARD_GUIDE.md).",
    )
    args = parser.parse_args()

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    started_at = time.perf_counter()

    with psycopg.connect(**cbi_database.connection_kwargs()) as connection:
        if args.only:
            road, direction = [part.strip() for part in args.only.split(",")]
            corridors = [
                cbi_corridor_registry.get_corridor(connection, road, direction)
            ]
        else:
            corridors = cbi_corridor_registry.get_active_corridors(connection)

        if args.group:
            corridors = [c for c in corridors if c.corridor_group == args.group]

        print(f"Corridors to process: {len(corridors)}")
        for corridor in corridors:
            print(f"  - {corridor.corridor_name}")

        all_results = {}
        for corridor in corridors:
            all_results[corridor.corridor_name] = run_corridor(
                connection, corridor, args.skip_report
            )

        print(f"\n{'=' * 70}")
        print("REGION-WIDE STAGES")
        print(f"{'=' * 70}")

        print("\nStage: watch_segments (Collector/Local congestion summary)...")
        try:
            result = cbi_watch_segments.compute_watch_segment_metrics(connection)
            print(f"  OK: {result}")
        except Exception as error:  # noqa: BLE001
            connection.rollback()
            error_text = f"{error}\n{traceback.format_exc()}"
            print(f"  FAILED: {error}")
            ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
            with ERROR_LOG.open("a", encoding="utf-8") as file:
                file.write(f"\n{'=' * 80}\nwatch_segments\n{error_text}")

        if not args.skip_report:
            print("\nStage: regional_report (master HTML export)...")
            try:
                report_path = cbi_generate_regional_report.generate_regional_report(
                    connection, OUTPUT_ROOT
                )
                print(f"  OK: {report_path}")
            except Exception as error:  # noqa: BLE001
                connection.rollback()
                error_text = f"{error}\n{traceback.format_exc()}"
                print(f"  FAILED: {error}")
                ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
                with ERROR_LOG.open("a", encoding="utf-8") as file:
                    file.write(f"\n{'=' * 80}\nregional_report\n{error_text}")

    elapsed = time.perf_counter() - started_at

    print(f"\n{'=' * 70}")
    print("ALL CORRIDORS FINISHED")
    print(f"{'=' * 70}")
    print(f"Total runtime: {elapsed / 60:.1f} minutes")
    for corridor_name, result in all_results.items():
        stopped_at = result.get("stopped_at")
        status = f"stopped at '{stopped_at}'" if stopped_at else "completed"
        print(f"  {corridor_name}: {status}")

    if ERROR_LOG.exists():
        print(f"\nErrors were logged to: {ERROR_LOG}")

    if args.export_fabric:
        print(f"\n{'=' * 70}")
        print("EXPORTING TO FABRIC / AZURE DATA LAKE")
        print(f"{'=' * 70}")
        try:
            import cbi_export_to_fabric

            cbi_export_to_fabric.export_and_upload()
        except Exception as error:  # noqa: BLE001
            print(f"Fabric export FAILED: {error}")
            print("Data pipeline results above are unaffected either way.")


if __name__ == "__main__":
    main()
