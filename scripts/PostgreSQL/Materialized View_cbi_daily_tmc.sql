CREATE MATERIALIZED VIEW cbi_daily_tmc AS
SELECT
    tmc_code,
    measurement_tstamp::date AS analysis_date,
    COUNT(*) AS observation_count,
    COUNT(*) FILTER (WHERE is_congested) AS congested_intervals,
    COUNT(*) FILTER (WHERE is_congested) * 5 AS congested_minutes,
    AVG(speed) AS average_speed,
    AVG(speed_drop) AS average_speed_drop,
    MAX(speed_drop) AS maximum_speed_drop
FROM cbi_observations
GROUP BY
    tmc_code,
    measurement_tstamp::date;