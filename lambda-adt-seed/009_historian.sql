-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- IoT Historian: Time-series sensor data for Indianapolis and Detroit plants
-- Equipment IDs match MQTT topic names. One machine has three system-local ids,
-- joined by no foreign key and no seeded graph rule:
--   5-Axis-Milling-CMX-441 = MES L3-M1 = CMMS 1001-CMX-441
-- equipment_name carries the machine's name: for 5-Axis-Milling-CMX-441 the same
-- text as the CMMS assets.description and the MES equipment.equipment_name.
-- CMX-441 shows escalating vibration Feb 10-14 correlating with batch B-2847 bearing failure;
-- bearing replaced 2026-02-15 12:30 under cmms_indianapolis work order 1195 (deferred PM: work order 1182)

CREATE SCHEMA IF NOT EXISTS iot_historian;

CREATE TABLE IF NOT EXISTS iot_historian.equipment_registry (
    equipment_id    VARCHAR(100) PRIMARY KEY,
    equipment_name  VARCHAR(200) NOT NULL,
    plant           VARCHAR(50)  NOT NULL,
    area            VARCHAR(100),
    line            VARCHAR(100),
    equipment_type  VARCHAR(50),
    installed_date  DATE,
    status          VARCHAR(20)  DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS iot_historian.sensor_readings (
    id               SERIAL PRIMARY KEY,
    timestamp        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    equipment_id     VARCHAR(100) NOT NULL REFERENCES iot_historian.equipment_registry(equipment_id),
    plant            VARCHAR(50)  NOT NULL,
    measurement_type VARCHAR(50)  NOT NULL,
    value            NUMERIC,
    unit             VARCHAR(20),
    quality          VARCHAR(20)  DEFAULT 'good'
);
CREATE INDEX IF NOT EXISTS idx_sensor_readings_equip_ts ON iot_historian.sensor_readings(equipment_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_sensor_readings_type     ON iot_historian.sensor_readings(measurement_type, timestamp DESC);

CREATE TABLE IF NOT EXISTS iot_historian.aggregated_metrics (
    id           SERIAL PRIMARY KEY,
    equipment_id VARCHAR(100) NOT NULL REFERENCES iot_historian.equipment_registry(equipment_id),
    metric       VARCHAR(50)  NOT NULL,
    period_start TIMESTAMPTZ  NOT NULL,
    period_end   TIMESTAMPTZ  NOT NULL,
    min_val      NUMERIC,
    max_val      NUMERIC,
    avg_val      NUMERIC,
    sample_count INTEGER
);

-- ============================================================
-- Equipment Registry
-- ============================================================
INSERT INTO iot_historian.equipment_registry VALUES
-- Indianapolis — Line 3
('5-Axis-Milling-CMX-441', '5-Axis Milling Center CMX-441', 'Indianapolis', 'Production', 'Line3', 'CNC_MILL',  '2022-06-15', 'active'),
('Hydraulic-Press-402',    'Hydraulic Press 402',           'Indianapolis', 'Production', 'Line3', 'PRESS',     '2019-11-05', 'active'),
('Surface-Grinder-501',    'Surface Grinder 501',           'Indianapolis', 'Production', 'Line3', 'GRINDER',   '2021-07-22', 'active'),
('Cylindrical-Grinder-502','Cylindrical Grinder 502',       'Indianapolis', 'Production', 'Line3', 'GRINDER',   '2021-07-22', 'active'),
('Assembly-Conveyor-A',    'Main Assembly Conveyor A',      'Indianapolis', 'Production', 'Line3', 'CONVEYOR',  '2020-08-01', 'active'),
('CNC-Tool-Presetter',     'CNC Tool Presetter',            'Indianapolis', 'Production', 'Line3', 'INSPECTION','2021-05-01', 'active'),
-- Detroit — Line 4/5
('CNC-Mill-04',                 'CNC Milling Center 04',        'Detroit', 'Production', 'Line4', 'CNC_MILL',  '2022-03-10', 'active'),
('CNC-Turning-Center-01',       'CNC Turning Center 01',        'Detroit', 'Production', 'Line4', 'CNC_LATHE', '2021-09-15', 'active'),
('Assembly-Robot-101',          'Assembly Robot 101',           'Detroit', 'Production', 'Line4', 'ROBOT',     '2023-01-20', 'active'),
('CMM-Inspection-Station-701',  'CMM Inspection Station 701',   'Detroit', 'Production', 'Line4', 'INSPECTION','2022-11-01', 'active'),
('Robotic-Welding-Cell',        'Robotic Welding Cell',         'Detroit', 'Production', 'Line4', 'ROBOT',     '2023-06-01', 'active'),
('Stamping-Press-701',          'Stamping Press 701',           'Detroit', 'Production', 'Line4', 'PRESS',     '2022-08-15', 'active'),
('Conveyor-System-Line-4',      'Conveyor System Line 4',       'Detroit', 'Production', 'Line4', 'CONVEYOR',  '2021-07-01', 'active'),
('Conveyor-System-Line-5',      'Conveyor System Line 5',       'Detroit', 'Production', 'Line5', 'CONVEYOR',  '2021-07-01', 'active')
ON CONFLICT (equipment_id) DO NOTHING;

-- ============================================================
-- Sensor Readings — generated via generate_series
-- ~500 readings per plant, Feb 10-15 2026, every 15 min per equipment
-- CMX-441 vibration escalates from 1.8 → 2.8 mm/s approaching Feb 14
-- ============================================================

-- Helper: Indianapolis equipment — vibration (normal range 0.5-1.5 mm/s)
INSERT INTO iot_historian.sensor_readings (timestamp, equipment_id, plant, measurement_type, value, unit, quality)
SELECT s.* FROM (
SELECT
    ts,
    eq,
    'Indianapolis',
    'vibration',
    ROUND((0.5 + random() * 1.0)::numeric, 3),
    'mm/s',
    'good'
FROM
    generate_series('2026-02-10 00:00:00+00'::timestamptz, '2026-02-15 23:45:00+00'::timestamptz, '2 hours'::interval) AS ts,
    unnest(ARRAY['Hydraulic-Press-402','Surface-Grinder-501','Cylindrical-Grinder-502','Assembly-Conveyor-A','CNC-Tool-Presetter']) AS eq
) AS s (timestamp, equipment_id, plant, measurement_type, value, unit, quality)
WHERE NOT EXISTS (SELECT 1 FROM iot_historian.sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_type = s.measurement_type AND t.timestamp = s.timestamp);

-- CMX-441 vibration — escalating pattern: normal Feb 10-12, elevated Feb 13, critical Feb 14,
-- recovery after the Feb 15 12:30 bearing replacement (cmms_indianapolis work order 1195)
INSERT INTO iot_historian.sensor_readings (timestamp, equipment_id, plant, measurement_type, value, unit, quality)
SELECT s.* FROM (
SELECT
    ts,
    '5-Axis-Milling-CMX-441',
    'Indianapolis',
    'vibration',
    ROUND(CASE
        WHEN ts < '2026-02-12 00:00:00+00' THEN 0.8 + random() * 0.6          -- normal: 0.8-1.4
        WHEN ts < '2026-02-13 00:00:00+00' THEN 1.2 + random() * 0.5          -- slightly elevated: 1.2-1.7
        WHEN ts < '2026-02-13 18:00:00+00' THEN 1.6 + random() * 0.5          -- elevated: 1.6-2.1
        WHEN ts < '2026-02-14 06:00:00+00' THEN 1.9 + random() * 0.5          -- warning: 1.9-2.4
        WHEN ts < '2026-02-14 14:00:00+00' THEN 2.3 + random() * 0.4          -- critical: 2.3-2.7
        WHEN ts < '2026-02-15 12:30:00+00' THEN 2.5 + random() * 0.3          -- failure: 2.5-2.8
        ELSE                                     0.9 + random() * 0.4          -- post-repair (CMMS WO 1195): 0.9-1.3
    END::numeric, 3),
    'mm/s',
    CASE
        WHEN ts < '2026-02-14 06:00:00+00' THEN 'good'
        WHEN ts < '2026-02-15 12:30:00+00' THEN 'uncertain'
        ELSE 'good'
    END
FROM generate_series('2026-02-10 00:00:00+00'::timestamptz, '2026-02-15 23:45:00+00'::timestamptz, '30 minutes'::interval) AS ts
) AS s (timestamp, equipment_id, plant, measurement_type, value, unit, quality)
WHERE NOT EXISTS (SELECT 1 FROM iot_historian.sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_type = s.measurement_type AND t.timestamp = s.timestamp);

-- Indianapolis temperature readings (35-65°C normal, CMX-441 elevated on Feb 14)
INSERT INTO iot_historian.sensor_readings (timestamp, equipment_id, plant, measurement_type, value, unit, quality)
SELECT s.* FROM (
SELECT
    ts,
    eq,
    'Indianapolis',
    'temperature',
    ROUND((35 + random() * 30)::numeric, 1),
    '°C',
    'good'
FROM
    generate_series('2026-02-10 00:00:00+00'::timestamptz, '2026-02-15 23:45:00+00'::timestamptz, '2 hours'::interval) AS ts,
    unnest(ARRAY['Hydraulic-Press-402','Surface-Grinder-501','Cylindrical-Grinder-502','Assembly-Conveyor-A','CNC-Tool-Presetter']) AS eq
) AS s (timestamp, equipment_id, plant, measurement_type, value, unit, quality)
WHERE NOT EXISTS (SELECT 1 FROM iot_historian.sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_type = s.measurement_type AND t.timestamp = s.timestamp);

INSERT INTO iot_historian.sensor_readings (timestamp, equipment_id, plant, measurement_type, value, unit, quality)
SELECT s.* FROM (
SELECT
    ts,
    '5-Axis-Milling-CMX-441',
    'Indianapolis',
    'temperature',
    ROUND(CASE
        WHEN ts < '2026-02-13 00:00:00+00' THEN 42 + random() * 15    -- normal: 42-57
        WHEN ts < '2026-02-14 06:00:00+00' THEN 55 + random() * 15    -- elevated: 55-70
        WHEN ts < '2026-02-15 12:30:00+00' THEN 68 + random() * 12    -- high: 68-80
        ELSE                                     44 + random() * 12    -- post-repair (CMMS WO 1195): 44-56
    END::numeric, 1),
    '°C',
    'good'
FROM generate_series('2026-02-10 00:00:00+00'::timestamptz, '2026-02-15 23:45:00+00'::timestamptz, '2 hours'::interval) AS ts
) AS s (timestamp, equipment_id, plant, measurement_type, value, unit, quality)
WHERE NOT EXISTS (SELECT 1 FROM iot_historian.sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_type = s.measurement_type AND t.timestamp = s.timestamp);

-- Indianapolis current (15-45A)
INSERT INTO iot_historian.sensor_readings (timestamp, equipment_id, plant, measurement_type, value, unit, quality)
SELECT s.* FROM (
SELECT
    ts,
    eq,
    'Indianapolis',
    'current',
    ROUND((15 + random() * 30)::numeric, 1),
    'A',
    'good'
FROM
    generate_series('2026-02-10 00:00:00+00'::timestamptz, '2026-02-15 23:45:00+00'::timestamptz, '2 hours'::interval) AS ts,
    unnest(ARRAY['5-Axis-Milling-CMX-441','Hydraulic-Press-402','Surface-Grinder-501','Cylindrical-Grinder-502']) AS eq
) AS s (timestamp, equipment_id, plant, measurement_type, value, unit, quality)
WHERE NOT EXISTS (SELECT 1 FROM iot_historian.sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_type = s.measurement_type AND t.timestamp = s.timestamp);

-- Indianapolis pressure — presses only (2.0-6.0 bar)
INSERT INTO iot_historian.sensor_readings (timestamp, equipment_id, plant, measurement_type, value, unit, quality)
SELECT s.* FROM (
SELECT
    ts,
    'Hydraulic-Press-402',
    'Indianapolis',
    'pressure',
    ROUND((2.0 + random() * 4.0)::numeric, 2),
    'bar',
    'good'
FROM generate_series('2026-02-10 00:00:00+00'::timestamptz, '2026-02-15 23:45:00+00'::timestamptz, '2 hours'::interval) AS ts
) AS s (timestamp, equipment_id, plant, measurement_type, value, unit, quality)
WHERE NOT EXISTS (SELECT 1 FROM iot_historian.sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_type = s.measurement_type AND t.timestamp = s.timestamp);

-- ---- Detroit sensor readings ----

-- Detroit vibration (normal 0.5-1.5 mm/s)
INSERT INTO iot_historian.sensor_readings (timestamp, equipment_id, plant, measurement_type, value, unit, quality)
SELECT s.* FROM (
SELECT
    ts,
    eq,
    'Detroit',
    'vibration',
    ROUND((0.5 + random() * 1.0)::numeric, 3),
    'mm/s',
    'good'
FROM
    generate_series('2026-02-10 00:00:00+00'::timestamptz, '2026-02-15 23:45:00+00'::timestamptz, '2 hours'::interval) AS ts,
    unnest(ARRAY['CNC-Mill-04','CNC-Turning-Center-01','Assembly-Robot-101','Robotic-Welding-Cell','Stamping-Press-701','Conveyor-System-Line-4','Conveyor-System-Line-5']) AS eq
) AS s (timestamp, equipment_id, plant, measurement_type, value, unit, quality)
WHERE NOT EXISTS (SELECT 1 FROM iot_historian.sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_type = s.measurement_type AND t.timestamp = s.timestamp);

-- Detroit temperature (35-65°C)
INSERT INTO iot_historian.sensor_readings (timestamp, equipment_id, plant, measurement_type, value, unit, quality)
SELECT s.* FROM (
SELECT
    ts,
    eq,
    'Detroit',
    'temperature',
    ROUND((35 + random() * 30)::numeric, 1),
    '°C',
    'good'
FROM
    generate_series('2026-02-10 00:00:00+00'::timestamptz, '2026-02-15 23:45:00+00'::timestamptz, '2 hours'::interval) AS ts,
    unnest(ARRAY['CNC-Mill-04','CNC-Turning-Center-01','Assembly-Robot-101','CMM-Inspection-Station-701','Robotic-Welding-Cell','Stamping-Press-701']) AS eq
) AS s (timestamp, equipment_id, plant, measurement_type, value, unit, quality)
WHERE NOT EXISTS (SELECT 1 FROM iot_historian.sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_type = s.measurement_type AND t.timestamp = s.timestamp);

-- Detroit current (15-45A)
INSERT INTO iot_historian.sensor_readings (timestamp, equipment_id, plant, measurement_type, value, unit, quality)
SELECT s.* FROM (
SELECT
    ts,
    eq,
    'Detroit',
    'current',
    ROUND((15 + random() * 30)::numeric, 1),
    'A',
    'good'
FROM
    generate_series('2026-02-10 00:00:00+00'::timestamptz, '2026-02-15 23:45:00+00'::timestamptz, '2 hours'::interval) AS ts,
    unnest(ARRAY['CNC-Mill-04','CNC-Turning-Center-01','Stamping-Press-701']) AS eq
) AS s (timestamp, equipment_id, plant, measurement_type, value, unit, quality)
WHERE NOT EXISTS (SELECT 1 FROM iot_historian.sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_type = s.measurement_type AND t.timestamp = s.timestamp);

-- Detroit pressure — stamping press (2.0-6.0 bar)
INSERT INTO iot_historian.sensor_readings (timestamp, equipment_id, plant, measurement_type, value, unit, quality)
SELECT s.* FROM (
SELECT
    ts,
    'Stamping-Press-701',
    'Detroit',
    'pressure',
    ROUND((2.0 + random() * 4.0)::numeric, 2),
    'bar',
    'good'
FROM generate_series('2026-02-10 00:00:00+00'::timestamptz, '2026-02-15 23:45:00+00'::timestamptz, '2 hours'::interval) AS ts
) AS s (timestamp, equipment_id, plant, measurement_type, value, unit, quality)
WHERE NOT EXISTS (SELECT 1 FROM iot_historian.sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_type = s.measurement_type AND t.timestamp = s.timestamp);

-- ============================================================
-- Aggregated Metrics — hourly rollups from sensor_readings
-- ============================================================
INSERT INTO iot_historian.aggregated_metrics (equipment_id, metric, period_start, period_end, min_val, max_val, avg_val, sample_count)
SELECT s.* FROM (
SELECT
    equipment_id,
    measurement_type                                                        AS metric,
    date_trunc('hour', timestamp)                                           AS period_start,
    date_trunc('hour', timestamp) + INTERVAL '1 hour'                      AS period_end,
    ROUND(MIN(value)::numeric, 3)                                           AS min_val,
    ROUND(MAX(value)::numeric, 3)                                           AS max_val,
    ROUND(AVG(value)::numeric, 3)                                           AS avg_val,
    COUNT(*)                                                                AS sample_count
FROM iot_historian.sensor_readings
WHERE timestamp >= '2026-02-10 00:00:00+00'
  AND timestamp <  '2026-02-16 00:00:00+00'
GROUP BY equipment_id, measurement_type, date_trunc('hour', timestamp)
) AS s (equipment_id, metric, period_start, period_end, min_val, max_val, avg_val, sample_count)
WHERE NOT EXISTS (SELECT 1 FROM iot_historian.aggregated_metrics t WHERE t.equipment_id = s.equipment_id AND t.metric = s.metric AND t.period_start = s.period_start);
