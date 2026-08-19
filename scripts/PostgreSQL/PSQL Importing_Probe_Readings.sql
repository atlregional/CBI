CREATE UNLOGGED TABLE probe_readings_stage (
    tmc_code text,
    measurement_tstamp text,
    speed text,
    historical_average_speed text,
    reference_speed text,
    travel_time_minutes text,
    data_density text,
    npmrd_s2_2025 text
);