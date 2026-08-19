INSERT INTO probe_readings (
    tmc_code,
    measurement_tstamp,
    speed,
    historical_average_speed,
    reference_speed,
    travel_time_minutes,
    data_density
)
SELECT
    tmc_code,
    measurement_tstamp::timestamp,
    NULLIF(speed, '')::real,
    NULLIF(historical_average_speed, '')::real,
    NULLIF(reference_speed, '')::real,
    NULLIF(travel_time_minutes, '')::real,
    NULLIF(data_density, '')
FROM probe_readings_stage;