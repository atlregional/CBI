CREATE MATERIALIZED VIEW cbi_observations AS
SELECT
    r.tmc_code,
    r.measurement_tstamp,
    r.speed,
    r.historical_average_speed,
    r.reference_speed,
    r.travel_time_minutes,
    m.road,
    m.direction,
    m.miles,
    m.road_order,
    CASE
        WHEN r.reference_speed IS NULL OR r.reference_speed = 0
            THEN NULL
        ELSE r.speed / r.reference_speed
    END AS speed_ratio,
    CASE
        WHEN r.speed < 0.7 * r.reference_speed THEN true
        ELSE false
    END AS is_congested,
    GREATEST(r.reference_speed - r.speed, 0) AS speed_drop
FROM probe_readings r
JOIN tmc_metadata m
    ON r.tmc_code = m.tmc;