CREATE TABLE probe_readings (
    tmc_code text NOT NULL,
    measurement_tstamp timestamp NOT NULL,
    speed real,
    historical_average_speed real,
    reference_speed real,
    travel_time_minutes real,
    data_density text
);