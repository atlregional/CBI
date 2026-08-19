from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl
from scipy import ndimage


CUTOFF_RATIO = 0.70

MIN_EVENT_CELLS = 6
MIN_EVENT_DURATION_MINUTES = 10
MIN_EVENT_SEGMENTS = 2
MIN_MAX_EXTENT_MILES = 0.25

# Hard cap on how many simultaneous congested miles a single day-level event
# may count toward its reported extent/area, in miles.
#
# create_refined_labels() connects congested cells into events using 8-
# connected component labeling across the full day x segment matrix, with
# no upper bound on how large that connected blob can grow. On a corridor
# where a flat, non-time-varying reference_speed keeps many segments
# reading "congested" for many consecutive hours (the same root cause
# documented for segment_recurring_bottlenecks — see MAX_BOTTLENECK_MILES
# in cbi_corridor_bottlenecks.py), the labeling merges that into one event
# spanning nearly the whole corridor for nearly the whole day. Confirmed on
# real data: GA-20 Northbound produced repeated daily events with a
# contiguous congested extent up to 113.5 miles and a total active extent
# up to 121.7 miles (Suwanee Dam Rd to I-985/US-23) — this corridor is not
# remotely that long — which inflated the region-wide county/month/weekday/
# hour dashboards in sql/004_extended_road_classes.sql (built directly from
# congestion_events, unaffected by the bottleneck-level fix) enough to rank
# Gwinnett County above Fulton despite Fulton having far more events and
# far more total duration. This is architecturally the same missing-cap
# defect as the bottleneck one, one processing stage upstream, and was
# never examined until that ranking discrepancy surfaced it.
#
# Applied per time-slice rather than to the whole event, and to the total
# (not just contiguous) active mileage — event_area_mile_hours integrates
# congested segment-miles over time, so capping only the reported summary
# stat after the fact would leave the area figure itself uncapped.
MAX_EVENT_EXTENT_MILES = 5.0


def create_matrices(
    dataframe: pl.DataFrame,
    analysis_date: date,
):
    """
    Convert one corridor-day of long-form observations to 5-minute matrices.

    IMPORTANT:
    Unlike the original I-75 NB prototype, this implementation does NOT
    hard-code 101 segments. Matrix width is derived from the corridor data,
    so it works for every corridor in corridor_definitions.
    """

    if dataframe.is_empty():
        raise RuntimeError("Cannot create matrices from an empty dataframe.")

    segment_count = int(dataframe["segment_order"].max())

    if segment_count <= 0:
        raise RuntimeError(
            f"Invalid segment count derived from segment_order: {segment_count}"
        )

    start = np.datetime64(
        f"{analysis_date.isoformat()}T00:00",
        "m",
    )

    timestamps = [
        start + np.timedelta64(index * 5, "m")
        for index in range(288)
    ]

    timestamp_index = {
        timestamp: index
        for index, timestamp in enumerate(timestamps)
    }

    speed_matrix = np.full(
        (288, segment_count),
        np.nan,
        dtype=float,
    )

    reference_matrix = np.full(
        (288, segment_count),
        np.nan,
        dtype=float,
    )

    segment_miles: dict[int, float] = {}
    segment_tmc: dict[int, str] = {}
    segment_intersection: dict[int, str] = {}

    for row in dataframe.iter_rows(named=True):
        timestamp = np.datetime64(
            row["measurement_tstamp"],
            "m",
        )

        time_position = timestamp_index.get(timestamp)

        if time_position is None:
            continue

        segment_order = int(row["segment_order"])
        segment_position = segment_order - 1

        if segment_position < 0 or segment_position >= segment_count:
            continue

        if row["speed"] is not None:
            speed_matrix[
                time_position,
                segment_position,
            ] = float(row["speed"])

        if row["reference_speed"] is not None:
            reference_matrix[
                time_position,
                segment_position,
            ] = float(row["reference_speed"])

        segment_miles[segment_order] = float(
            row["miles"] or 0.0
        )

        segment_tmc[segment_order] = row["tmc"]

        segment_intersection[segment_order] = (
            row["intersection"] or ""
        )

    observed_matrix = (
        np.isfinite(speed_matrix)
        & np.isfinite(reference_matrix)
        & (reference_matrix > 0)
    )

    ratio_matrix = np.full_like(
        speed_matrix,
        np.nan,
    )

    ratio_matrix[observed_matrix] = (
        speed_matrix[observed_matrix]
        / reference_matrix[observed_matrix]
    )

    return (
        timestamps,
        speed_matrix,
        reference_matrix,
        ratio_matrix,
        observed_matrix,
        segment_miles,
        segment_tmc,
        segment_intersection,
    )


def create_refined_labels(
    ratio_matrix: np.ndarray,
    observed_matrix: np.ndarray,
) -> np.ndarray:

    raw_congestion = (
        observed_matrix
        & np.isfinite(ratio_matrix)
        & (ratio_matrix < CUTOFF_RATIO)
    )

    cross_structure = np.array(
        [
            [0, 1, 0],
            [1, 1, 1],
            [0, 1, 0],
        ],
        dtype=bool,
    )

    eight_neighbor_structure = np.ones(
        (3, 3),
        dtype=bool,
    )

    repaired = ndimage.binary_closing(
        raw_congestion,
        structure=cross_structure,
        iterations=1,
    )

    cores = ndimage.binary_erosion(
        repaired,
        structure=cross_structure,
        iterations=1,
        border_value=0,
    )

    core_labels, _ = ndimage.label(
        cores,
        structure=eight_neighbor_structure,
    )

    component_labels, component_count = ndimage.label(
        repaired,
        structure=eight_neighbor_structure,
    )

    final_labels = np.zeros_like(
        core_labels,
        dtype=np.int32,
    )

    next_event_id = 0

    component_slices = ndimage.find_objects(
        component_labels
    )

    for component_id in range(
        1,
        component_count + 1,
    ):
        component_slice = component_slices[
            component_id - 1
        ]

        if component_slice is None:
            continue

        local_component = (
            component_labels[component_slice]
            == component_id
        )

        local_core_labels = (
            core_labels[component_slice].copy()
        )

        local_core_labels[~local_component] = 0

        core_ids = np.unique(local_core_labels)
        core_ids = core_ids[core_ids > 0]

        local_output = np.zeros_like(
            local_core_labels,
            dtype=np.int32,
        )

        if len(core_ids) <= 1:
            next_event_id += 1
            local_output[
                local_component
            ] = next_event_id
        else:
            core_mask = local_core_labels > 0

            _, nearest_indices = (
                ndimage.distance_transform_edt(
                    ~core_mask,
                    return_indices=True,
                )
            )

            nearest_core_labels = local_core_labels[
                nearest_indices[0],
                nearest_indices[1],
            ]

            for core_id in core_ids:
                next_event_id += 1

                assigned = (
                    local_component
                    & (
                        nearest_core_labels
                        == core_id
                    )
                )

                local_output[
                    assigned
                ] = next_event_id

        target = final_labels[component_slice]
        target[local_component] = local_output[
            local_component
        ]
        final_labels[component_slice] = target

    return final_labels


def build_segment_distance_lookup(
    segment_miles: dict[int, float],
):
    starts: dict[int, float] = {}
    ends: dict[int, float] = {}

    cumulative = 0.0

    for segment_order in sorted(
        segment_miles
    ):
        starts[segment_order] = cumulative
        cumulative += segment_miles.get(
            segment_order,
            0.0,
        )
        ends[segment_order] = cumulative

    return starts, ends


def split_contiguous_runs(
    segment_orders: np.ndarray,
) -> list[np.ndarray]:

    if len(segment_orders) == 0:
        return []

    ordered = np.sort(
        np.unique(segment_orders)
    )

    breaks = np.where(
        np.diff(ordered) > 1
    )[0] + 1

    return list(
        np.split(
            ordered,
            breaks,
        )
    )


def calculate_run_miles(
    segment_orders: np.ndarray,
    segment_miles: dict[int, float],
) -> float:

    return float(
        sum(
            segment_miles.get(
                int(segment_order),
                0.0,
            )
            for segment_order
            in segment_orders
        )
    )


def calculate_events(
    labels: np.ndarray,
    ratio_matrix: np.ndarray,
    speed_matrix: np.ndarray,
    reference_matrix: np.ndarray,
    timestamps,
    segment_miles: dict[int, float],
    segment_tmc: dict[int, str],
    segment_intersection: dict[int, str],
) -> pl.DataFrame:

    segment_start_mile, segment_end_mile = (
        build_segment_distance_lookup(
            segment_miles
        )
    )

    summaries: list[dict] = []
    retained_event_id = 0

    source_event_ids = np.unique(labels)
    source_event_ids = source_event_ids[
        source_event_ids > 0
    ]

    for source_event_id in source_event_ids:
        positions = np.argwhere(
            labels == source_event_id
        )

        if len(positions) == 0:
            continue

        time_positions = positions[:, 0]
        segment_positions = positions[:, 1]
        segment_orders = segment_positions + 1

        unique_times = np.unique(
            time_positions
        )
        unique_segments = np.unique(
            segment_orders
        )

        cell_count = len(positions)
        duration_minutes = (
            len(unique_times) * 5
        )
        segment_count = len(
            unique_segments
        )

        event_ratios = ratio_matrix[
            time_positions,
            segment_positions,
        ]

        event_speeds = speed_matrix[
            time_positions,
            segment_positions,
        ]

        event_reference_speeds = (
            reference_matrix[
                time_positions,
                segment_positions,
            ]
        )

        speed_drops = (
            event_reference_speeds
            - event_speeds
        )

        maximum_contiguous_extent = 0.0
        maximum_total_extent = 0.0
        capped_area_mile_minutes = 0.0

        elapsed_hours: list[float] = []
        upstream_positions: list[float] = []

        first_time_position = int(
            unique_times.min()
        )

        for time_position in unique_times:
            active_positions = (
                segment_positions[
                    time_positions
                    == time_position
                ]
            )

            active_orders = np.sort(
                np.unique(
                    active_positions + 1
                )
            )

            contiguous_runs = (
                split_contiguous_runs(
                    active_orders
                )
            )

            run_miles = [
                calculate_run_miles(
                    run,
                    segment_miles,
                )
                for run in contiguous_runs
            ]

            maximum_run_miles = max(
                run_miles,
                default=0.0,
            )

            total_active_miles = (
                calculate_run_miles(
                    active_orders,
                    segment_miles,
                )
            )

            maximum_contiguous_extent = max(
                maximum_contiguous_extent,
                maximum_run_miles,
            )

            maximum_total_extent = max(
                maximum_total_extent,
                total_active_miles,
            )

            # MAX_EVENT_EXTENT_MILES applied per time-slice, not to the
            # final summary stat — event_area_mile_hours integrates
            # congested segment-miles over time (see below), so capping
            # only the reported max would leave the accumulated area
            # itself unbounded.
            capped_area_mile_minutes += (
                min(
                    total_active_miles,
                    MAX_EVENT_EXTENT_MILES,
                )
                * 5.0
            )

            upstream_segment = int(
                active_orders.min()
            )

            elapsed = (
                int(time_position)
                - first_time_position
            ) * 5.0 / 60.0

            elapsed_hours.append(elapsed)

            upstream_positions.append(
                segment_start_mile.get(
                    upstream_segment,
                    np.nan,
                )
            )

        maximum_contiguous_extent = min(
            maximum_contiguous_extent,
            MAX_EVENT_EXTENT_MILES,
        )

        maximum_total_extent = min(
            maximum_total_extent,
            MAX_EVENT_EXTENT_MILES,
        )

        if cell_count < MIN_EVENT_CELLS:
            continue

        if (
            duration_minutes
            < MIN_EVENT_DURATION_MINUTES
        ):
            continue

        if (
            segment_count
            < MIN_EVENT_SEGMENTS
        ):
            continue

        if (
            maximum_contiguous_extent
            < MIN_MAX_EXTENT_MILES
        ):
            continue

        retained_event_id += 1

        first_time = int(
            time_positions.min()
        )
        last_time = int(
            time_positions.max()
        )

        first_segment = int(
            segment_orders.min()
        )
        last_segment = int(
            segment_orders.max()
        )

        # Was: sum(segment_miles) over every (time, segment) cell in the
        # event, uncapped — the same connected blob that could span 100+
        # miles could integrate that whole span into the area figure.
        # capped_area_mile_minutes (accumulated per time-slice above, each
        # slice's contribution capped at MAX_EVENT_EXTENT_MILES) bounds it.
        event_area_mile_hours = (
            capped_area_mile_minutes
            / 60.0
        )

        propagation_speed = None

        if (
            len(elapsed_hours) >= 3
            and np.ptp(
                elapsed_hours
            ) > 0
            and np.isfinite(
                upstream_positions
            ).all()
        ):
            slope, _ = np.polyfit(
                np.asarray(
                    elapsed_hours
                ),
                np.asarray(
                    upstream_positions
                ),
                1,
            )

            propagation_speed = float(
                slope
            )

        summaries.append(
            {
                "event_id":
                    retained_event_id,
                "start_time":
                    str(
                        timestamps[
                            first_time
                        ]
                    ),
                "end_time":
                    str(
                        timestamps[
                            last_time
                        ]
                    ),
                "duration_minutes":
                    duration_minutes,
                "cell_count":
                    cell_count,
                "segment_count":
                    segment_count,
                "first_segment_order":
                    first_segment,
                "last_segment_order":
                    last_segment,
                "first_tmc":
                    segment_tmc.get(
                        first_segment
                    ),
                "last_tmc":
                    segment_tmc.get(
                        last_segment
                    ),
                "first_intersection":
                    segment_intersection.get(
                        first_segment
                    ),
                "last_intersection":
                    segment_intersection.get(
                        last_segment
                    ),
                "maximum_contiguous_extent_miles":
                    round(
                        maximum_contiguous_extent,
                        3,
                    ),
                "maximum_total_active_miles":
                    round(
                        maximum_total_extent,
                        3,
                    ),
                "event_area_mile_hours":
                    round(
                        event_area_mile_hours,
                        3,
                    ),
                "average_speed_mph":
                    round(
                        float(
                            np.nanmean(
                                event_speeds
                            )
                        ),
                        2,
                    ),
                "minimum_speed_ratio":
                    round(
                        float(
                            np.nanmin(
                                event_ratios
                            )
                        ),
                        3,
                    ),
                "average_speed_ratio":
                    round(
                        float(
                            np.nanmean(
                                event_ratios
                            )
                        ),
                        3,
                    ),
                "average_speed_drop_mph":
                    round(
                        float(
                            np.nanmean(
                                speed_drops
                            )
                        ),
                        2,
                    ),
                "maximum_speed_drop_mph":
                    round(
                        float(
                            np.nanmax(
                                speed_drops
                            )
                        ),
                        2,
                    ),
                "estimated_upstream_boundary_speed_mph":
                    (
                        round(
                            propagation_speed,
                            3,
                        )
                        if propagation_speed
                        is not None
                        else None
                    ),
                "corridor_start_mile":
                    round(
                        segment_start_mile.get(
                            first_segment,
                            0.0,
                        ),
                        3,
                    ),
                "corridor_end_mile":
                    round(
                        segment_end_mile.get(
                            last_segment,
                            0.0,
                        ),
                        3,
                    ),
            }
        )

    if not summaries:
        return pl.DataFrame()

    return pl.DataFrame(
        summaries
    ).sort(
        [
            "event_area_mile_hours",
            "duration_minutes",
            "maximum_contiguous_extent_miles",
        ],
        descending=True,
    )


def detect_events(
    dataframe: pl.DataFrame,
    analysis_date: date,
) -> pl.DataFrame:

    (
        timestamps,
        speed_matrix,
        reference_matrix,
        ratio_matrix,
        observed_matrix,
        segment_miles,
        segment_tmc,
        segment_intersection,
    ) = create_matrices(
        dataframe=dataframe,
        analysis_date=analysis_date,
    )

    labels = create_refined_labels(
        ratio_matrix=ratio_matrix,
        observed_matrix=observed_matrix,
    )

    return calculate_events(
        labels=labels,
        ratio_matrix=ratio_matrix,
        speed_matrix=speed_matrix,
        reference_matrix=reference_matrix,
        timestamps=timestamps,
        segment_miles=segment_miles,
        segment_tmc=segment_tmc,
        segment_intersection=segment_intersection,
    )
