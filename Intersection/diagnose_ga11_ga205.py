"""
Diagnostic: prints the underlying per-TMC data for GA-11 and GA-205, using
the exact same functions as cbi_arterial_intersection_severity.py's
production run (imported directly, not reimplemented), so these numbers
are guaranteed consistent with the published ranking.
"""

from __future__ import annotations

import polars as pl
import psycopg

import cbi_arterial_intersection_severity as aisev
import cbi_database


def main() -> None:
    with psycopg.connect(**cbi_database.connection_kwargs()) as conn:
        print("Computing free-flow hour(s)...")
        arterial_hourly = aisev.compute_arterial_hourly_speed(conn)
        free_flow = aisev.identify_free_flow_hours(arterial_hourly)
        print(f"  free-flow hour(s): {free_flow.free_flow_hours}")

        segment_hourly = aisev.compute_segment_hourly_speed(conn)
        segments = aisev.compute_segment_drop_ratios(segment_hourly, free_flow)
        segments = aisev.attach_occurrence(conn, segments)

        am_min = (
            segment_hourly.filter(pl.col("hr").is_in(list(aisev.AM_PEAK_HOURS)))
            .group_by("tmc_code").agg(pl.col("avg_speed").min().alias("am_min_speed"))
        )
        pm_min = (
            segment_hourly.filter(pl.col("hr").is_in(list(aisev.PM_PEAK_HOURS)))
            .group_by("tmc_code").agg(pl.col("avg_speed").min().alias("pm_min_speed"))
        )
        segments = segments.join(am_min, on="tmc_code", how="left").join(pm_min, on="tmc_code", how="left")

        hourly_excess = aisev.compute_excess_travel_time_by_hour(conn, segments)
        segments = aisev.compute_segment_eavhd(
            segments, hourly_excess, aisev.HOURLY_DISTRIBUTION_FACTOR, "eavhd_hours"
        )

        segments = aisev.attach_named_arterial_groups(conn, segments)

        for road in ["GA-11", "GA-205"]:
            subset = segments.filter(pl.col("arterial_group") == road).sort("tmc_code")
            print(f"\n{'=' * 70}")
            print(f"{road}  —  {subset.height} TMCs")
            print(f"{'=' * 70}")
            print(
                f"{'TMC':<14}{'Miles':>8}{'AADT':>10}{'FreeFlow':>10}{'AMmin':>8}{'PMmin':>8}"
                f"{'Ratio':>8}{'Occ%':>8}{'AnalyzedD':>11}{'CongD':>7}{'EAVHD(hrs)':>12}"
            )
            for row in subset.iter_rows(named=True):
                print(
                    f"{row['tmc_code']:<14}{row['miles']:>8.3f}{row['aadt']:>10.0f}"
                    f"{row['free_flow_speed']:>10.2f}{row['am_min_speed']:>8.2f}{row['pm_min_speed']:>8.2f}"
                    f"{row['peak_drop_ratio']:>8.3f}{row['occurrence_pct']:>8.2f}"
                    f"{row['analyzed_days']:>11}{row['congested_days']:>7}{row['eavhd_hours']:>12.1f}"
                )

            total_miles = subset["miles"].sum()
            congested_mile_days = (subset["congested_days"] * subset["miles"]).sum()
            observed_mile_days = (subset["analyzed_days"] * subset["miles"]).sum()
            road_recurrence = 100.0 * congested_mile_days / observed_mile_days
            road_speed_ratio = subset["peak_drop_ratio"].mean()
            road_aadt_median = subset["aadt"].median()
            eavhd_total = subset["eavhd_hours"].sum()
            eavhd_density = eavhd_total / total_miles if total_miles else float("nan")

            print(f"{'-' * 70}")
            print(f"Number of TMCs:              {subset.height}")
            print(f"Total exact miles:           {total_miles:.4f}")
            print(f"AADT (median across TMCs):   {road_aadt_median:,.0f}")
            print(f"Speed ratio (mean of TMCs):  {road_speed_ratio:.3f}  (Intensity = {1 - road_speed_ratio:.1%})")
            print(f"Recurrence (mile-day-wtd):   {road_recurrence:.2f}%")
            print(f"EAVHD total:                 {eavhd_total:,.1f} veh-hrs")
            print(f"EAVHD density:                {eavhd_density:,.1f} veh-hrs/mi")


if __name__ == "__main__":
    main()
