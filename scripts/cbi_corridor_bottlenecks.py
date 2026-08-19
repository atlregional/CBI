"""
Corridor-parameterized recurring-bottleneck detection.

IMPORTANT — read before using on a real corridor:
This module is a RECONSTRUCTION of cbi_segment_bottlenecks.py's documented
behavior (Part II, Section 9.1 of the CBI technical report): box-car
smoothing, scipy.signal.find_peaks peak detection, boundary expansion until
occurrence drops below a threshold, and valley-splitting of overlapping
boundaries. It is written from the report's description, not copied from
the original script's exact source, because the original file's precise
threshold constants (minimum prominence, minimum spacing, boundary cutoff)
were not available when this was written.

Before running this against a second corridor and trusting the output:
compare MIN_PEAK_OCCURRENCE_PCT / MIN_PROMINENCE_PCT / MIN_SPACING_SEGMENTS /
BOUNDARY_RELATIVE_THRESHOLD below against the constants actually used in
your validated cbi_segment_bottlenecks.py, and adjust to match. Consider
this a scaffold to reconcile with the original, not a drop-in replacement
for it.
"""

from __future__ import annotations

import time

import numpy as np
import polars as pl
import psycopg
from scipy.signal import find_peaks

import cbi_database
from cbi_corridor_registry import CorridorContext

# --- Tunable thresholds — reconcile with the validated I-75 NB script -----
SMOOTHING_WINDOW = 3               # segments, box-car moving average
MIN_PEAK_OCCURRENCE_PCT = 20.0     # a peak below this occurrence is ignored
MIN_PROMINENCE_PCT = 10.0          # scipy find_peaks prominence, in pct points
MIN_SPACING_SEGMENTS = 3           # minimum segments between distinct peaks
BOUNDARY_RELATIVE_THRESHOLD = 0.5  # expand while occurrence >= 50% of peak
MIN_ACTIVE_DAYS_FOR_PROFILE = 30   # segments with fewer analyzed days are skipped

# Hard cap on how far boundary expansion may grow one bottleneck, in miles.
#
# BOUNDARY_RELATIVE_THRESHOLD alone has no absolute stopping condition — it
# only requires each neighboring segment to stay above 50% of the PEAK's own
# occurrence. On a corridor where occurrence is elevated for a long
# consecutive stretch (confirmed for both signalized arterials, where
# reference_speed appears not to reflect a physically achievable baseline —
# see the "signals and stop signs" note in cbi_generate_regional_report.py's
# Metadata tab — and for chronically congested Interstate segments like
# I-285), that condition is satisfied for miles on end and expansion never
# finds a valley. Investigated case: GA-20 Northbound's "US-23/GA-42/Griffin
# St/Macon St" bottleneck (McDonough town square, Henry County) swept in 15
# segments / 24.83 miles — including a full I-75 interchange crossing —
# inflating its severity_index above every Interstate in the region. This
# was not an isolated case: region-wide, bottleneck extent (by actual miles,
# not segment count — segment length varies from ~0.02 to 7+ mi in this
# dataset, so segment count alone doesn't reliably flag this) had a median
# of ~8 mi and a long unbroken tail to 48 mi, with no natural gap separating
# "normal" from "runaway". 5.0 mi is a data-driven judgment call (most
# bottlenecks already fall under this; it decisively cuts the exposed
# runaway cases) pending reconciliation against the original validated
# script's actual behavior — adjust if that reference becomes available.
MAX_BOTTLENECK_MILES = 5.0


def _smooth(values: np.ndarray, window: int) -> np.ndarray:
    kernel = np.ones(window) / window
    return np.convolve(values, kernel, mode="same")


def detect_bottlenecks_for_corridor(
    connection: psycopg.Connection,
    corridor: CorridorContext,
) -> dict:
    started_at = time.perf_counter()

    profile = pl.read_database_uri(
        query=f"""
            SELECT segment_order, tmc, intersection, miles,
                   analyzed_days, weekday_occurrence_pct,
                   annual_segment_mile_hours, average_congested_speed_ratio
            FROM "Year_2025".segment_profile
            WHERE corridor_id = {corridor.corridor_id}
            ORDER BY segment_order
        """,
        uri=cbi_database.database_uri(),
        engine="connectorx",
    )

    if profile.is_empty():
        raise RuntimeError(
            f"No segment_profile rows for {corridor.corridor_name}. "
            "Run cbi_corridor_profile.build_segment_profile_for_corridor() first."
        )

    profile = profile.filter(pl.col("analyzed_days") >= MIN_ACTIVE_DAYS_FOR_PROFILE)

    occurrence = profile["weekday_occurrence_pct"].fill_null(0.0).to_numpy()
    segment_orders = profile["segment_order"].to_numpy()
    miles = profile["miles"].fill_null(0.0).to_numpy()

    smoothed = _smooth(occurrence, SMOOTHING_WINDOW)

    peak_indices, _ = find_peaks(
        smoothed,
        height=MIN_PEAK_OCCURRENCE_PCT,
        prominence=MIN_PROMINENCE_PCT,
        distance=MIN_SPACING_SEGMENTS,
    )

    bottlenecks = []

    for peak_index in peak_indices:
        peak_value = smoothed[peak_index]
        cutoff = peak_value * BOUNDARY_RELATIVE_THRESHOLD
        accumulated_miles = float(miles[peak_index])

        start_index = peak_index
        while start_index > 0 and smoothed[start_index - 1] >= cutoff:
            candidate_miles = accumulated_miles + float(miles[start_index - 1])
            if candidate_miles > MAX_BOTTLENECK_MILES:
                break
            start_index -= 1
            accumulated_miles = candidate_miles

        end_index = peak_index
        while (
            end_index < len(smoothed) - 1
            and smoothed[end_index + 1] >= cutoff
        ):
            candidate_miles = accumulated_miles + float(miles[end_index + 1])
            if candidate_miles > MAX_BOTTLENECK_MILES:
                break
            end_index += 1
            accumulated_miles = candidate_miles

        bottlenecks.append(
            {
                "peak_index": int(peak_index),
                "start_index": int(start_index),
                "end_index": int(end_index),
                # Boundary expansion above already enforces MAX_BOTTLENECK_MILES.
                # Valley-splitting below must never re-extend a window past what
                # expansion decided — only these two are the mileage-capped
                # reference points; start_index/end_index get mutated in place
                # by the split loop and must not be read back as "the cap".
                "capped_start_index": int(start_index),
                "capped_end_index": int(end_index),
            }
        )

    # Split overlapping boundaries at their shared valley.
    #
    # Sorted by start_index, but a bottleneck's boundary expansion can reach
    # backwards past a neighboring peak — so "current" (earlier start_index)
    # does not always have the earlier peak_index too. Indexing smoothed[]
    # directly by current/following peak_index in that inverted case builds
    # a backwards (empty) slice and np.argmin() raises "attempt to get
    # argmin of an empty sequence" — reproduced on real data for I-20
    # Eastbound / I-285 Counterclockwise / I-85 Southbound, where it
    # silently zeroed out real, high-prominence bottlenecks for the whole
    # corridor. Resolve left/right by peak_index instead of assuming it
    # matches the start_index sort.
    bottlenecks.sort(key=lambda b: b["start_index"])
    for i in range(len(bottlenecks) - 1):
        current, following = bottlenecks[i], bottlenecks[i + 1]
        if current["end_index"] >= following["start_index"]:
            left, right = (
                (current, following)
                if current["peak_index"] <= following["peak_index"]
                else (following, current)
            )
            valley_slice = smoothed[left["peak_index"] : right["peak_index"] + 1]
            valley_offset = int(np.argmin(valley_slice))
            valley_index = left["peak_index"] + valley_offset
            # Clamp to each side's own mileage-capped boundary (never widen
            # past it) — otherwise a valley found further out than where
            # MAX_BOTTLENECK_MILES stopped expansion silently re-inflates the
            # window back past the cap. Confirmed on real data: US-29
            # Southbound's "Boulevard/Monroe Dr" bottleneck (26 segments,
            # 5.96 mi) sat flush against its neighbors on both sides with no
            # gap, because the valley cut extended it out to touch them
            # exactly, bypassing the cap that had already bounded its own
            # expansion.
            left["end_index"] = min(valley_index, left["capped_end_index"])
            right["start_index"] = max(valley_index + 1, right["capped_start_index"])

    rows_written = 0
    membership_written = 0

    with connection.cursor() as cursor:
        # Re-running detection for a corridor (retries, re-seeded thresholds,
        # etc.) must not accumulate duplicate rows alongside the previous
        # run's — clear this corridor's prior detections first, the same way
        # every other stage (segment_daily, segment_profile, congestion_events)
        # already replaces rather than appends. CASCADE via the FK ON DELETE
        # CASCADE on segment_bottleneck_membership / bottleneck_daily_metrics
        # takes care of their dependent rows.
        cursor.execute(
            """
            DELETE FROM "Year_2025".segment_recurring_bottlenecks
            WHERE corridor = %s AND direction = %s
            """,
            (corridor.road, corridor.direction),
        )

        for entry in bottlenecks:
            segment_slice = profile.slice(
                entry["start_index"], entry["end_index"] - entry["start_index"] + 1
            )

            peak_row = profile.row(entry["peak_index"], named=True)

            insert_query = """
                INSERT INTO "Year_2025".segment_recurring_bottlenecks (
                    corridor, direction, peak_segment_order,
                    start_segment_order, end_segment_order,
                    start_mile, end_mile, segment_count,
                    representative_intersection, peak_occurrence_pct,
                    maximum_raw_occurrence_pct, annual_mile_hours,
                    average_congested_minutes, p95_congested_minutes,
                    average_congested_speed_ratio
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING bottleneck_id
            """

            cursor.execute(
                insert_query,
                (
                    corridor.road,
                    corridor.direction,
                    int(peak_row["segment_order"]),
                    int(segment_slice["segment_order"].min()),
                    int(segment_slice["segment_order"].max()),
                    None,  # start_mile — populate once corridor_segments carries
                           # cumulative mile position; not computed here.
                    None,  # end_mile — same caveat.
                    segment_slice.height,
                    peak_row["intersection"],
                    float(smoothed[entry["peak_index"]]),
                    float(occurrence[entry["start_index"] : entry["end_index"] + 1].max()),
                    float(segment_slice["annual_segment_mile_hours"].sum()),
                    None,  # average_congested_minutes — needs segment_daily join;
                           # left for characterization step (Phase 5) to compute.
                    None,  # p95_congested_minutes — same.
                    float(
                        segment_slice["average_congested_speed_ratio"]
                        .drop_nulls()
                        .mean()
                        or 0.0
                    ),
                ),
            )

            bottleneck_id = cursor.fetchone()[0]
            rows_written += 1

            membership_rows = [
                (bottleneck_id, int(order))
                for order in segment_slice["segment_order"].to_list()
            ]

            cursor.executemany(
                """
                INSERT INTO "Year_2025".segment_bottleneck_membership (
                    bottleneck_id, segment_order
                )
                VALUES (%s, %s)
                """,
                membership_rows,
            )

            membership_written += len(membership_rows)

    connection.commit()

    elapsed = time.perf_counter() - started_at

    return {
        "stage": "bottlenecks",
        "corridor": corridor.corridor_name,
        "bottlenecks_found": rows_written,
        "membership_rows": membership_written,
        "runtime_minutes": round(elapsed / 60, 2),
    }
