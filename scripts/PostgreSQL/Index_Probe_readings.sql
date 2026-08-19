CREATE INDEX idx_probe_tmc_time
ON probe_readings (tmc_code, measurement_tstamp);