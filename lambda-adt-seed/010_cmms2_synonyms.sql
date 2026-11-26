-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- ADT demo CMMS2 Synonym Seed Data (NAME-INDEPENDENCE GENERALIZATION VARIANT)
-- A SECOND maintenance system with the SAME real-world meaning as cmms_maintenance
-- but DELIBERATELY DIFFERENT table/column names (synonyms). It exists to prove the
-- mapping layer keys on field MEANING — not on memorized field names (see ADR 0012,
-- name-independence section).
--
-- ZERO column-name overlap with the cmms must-map fields:
--   cmms must-map     ->  cmms2 synonym
--   asset_tag         ->  tag_code         (-> equipment-id)
--   asset_type        ->  machine_class    (-> asset-class)
--   operational_status->  run_state        (-> lifecycle-state)
--   criticality       ->  crit_level       (-> criticality)
--   wo_number         ->  ticket_no        (-> work-order)
--   failure_code      ->  fault_code       (-> failure-mode)
--   maintenance_type  ->  pm_kind          (-> maintenance-schedule)
--   part_number       ->  sku              (-> spare-part)
--   quantity_on_hand  ->  on_hand_qty      (-> inventory-level)  [the synonym F9 tracer]
--
-- Style mirrors 004_cmms_maintenance.sql (schema/table pattern, ON CONFLICT idempotency,
-- realistic multi-site maintenance rows so discover's sampling is meaningful). This schema
-- routes to the DEFAULT demo database via catalog._pg_conn_str('cmms2_maintenance').

CREATE SCHEMA IF NOT EXISTS cmms2_maintenance;
SET search_path TO cmms2_maintenance;

-- ─────────────────────────────────────────────────────────────────────────────
-- equipment  (synonym of cmms_maintenance.assets)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS equipment (
    equip_uid       SERIAL       PRIMARY KEY,        -- ≈ asset_id (surrogate)
    tag_code        VARCHAR(30)  NOT NULL UNIQUE,    -- ≈ asset_tag        -> equipment-id
    equip_desc      VARCHAR(120) NOT NULL,           -- ≈ asset_name
    machine_class   VARCHAR(50)  NOT NULL,           -- ≈ asset_type       -> asset-class
    plant_site      VARCHAR(50)  NOT NULL,           -- ≈ facility
    cell            VARCHAR(50)  NOT NULL,           -- ≈ area             -> production-line
    vendor          VARCHAR(100),                    -- ≈ manufacturer
    run_state       VARCHAR(30)  NOT NULL DEFAULT 'running',  -- ≈ operational_status -> lifecycle-state
    crit_level      VARCHAR(20)  NOT NULL DEFAULT 'medium',   -- ≈ criticality
    commissioned_on DATE                             -- ≈ install_date
);

-- ─────────────────────────────────────────────────────────────────────────────
-- repair_tickets  (synonym of cmms_maintenance.work_orders)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS repair_tickets (
    ticket_no       VARCHAR(30)  PRIMARY KEY,        -- ≈ wo_number        -> work-order
    equip_uid       INTEGER      NOT NULL REFERENCES equipment(equip_uid),
    ticket_class    VARCHAR(50)  NOT NULL,           -- ≈ wo_type
    urgency         VARCHAR(20)  NOT NULL DEFAULT 'medium',   -- ≈ priority -> criticality
    summary         TEXT         NOT NULL,           -- ≈ description
    tech            VARCHAR(100),                    -- ≈ assigned_to      -> operator
    state           VARCHAR(30)  NOT NULL DEFAULT 'open',     -- ≈ status   -> order-status
    fault_code      VARCHAR(50)                      -- ≈ failure_code     -> failure-mode
);

-- ─────────────────────────────────────────────────────────────────────────────
-- pm_plan  (synonym of cmms_maintenance.maintenance_schedules)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS pm_plan (
    plan_id         VARCHAR(30)  PRIMARY KEY,        -- ≈ schedule_id
    pm_kind         VARCHAR(50)  NOT NULL,           -- ≈ maintenance_type -> maintenance-schedule
    interval_days   INTEGER      NOT NULL,           -- ≈ frequency_days
    crew            VARCHAR(100),                    -- ≈ assigned_team
    due_on          DATE                             -- ≈ next_due
);

-- ─────────────────────────────────────────────────────────────────────────────
-- stockroom  (synonym of cmms_maintenance.spare_parts)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS stockroom (
    sku             VARCHAR(50)  PRIMARY KEY,        -- ≈ part_number      -> spare-part
    sku_desc        VARCHAR(120) NOT NULL,           -- ≈ part_name
    fits_class      VARCHAR(50)  NOT NULL,           -- ≈ compatible_asset_type
    on_hand_qty     INTEGER      NOT NULL DEFAULT 0, -- ≈ quantity_on_hand -> inventory-level (synonym F9 tracer)
    min_qty         INTEGER      NOT NULL DEFAULT 5, -- ≈ reorder_point
    unit_price      DECIMAL(10,2),                   -- ≈ unit_cost        -> cost
    vendor          VARCHAR(100),                    -- ≈ supplier
    lead_days       INTEGER      DEFAULT 7           -- ≈ lead_time_days
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Seed rows — realistic multi-site maintenance data (synonym naming convention:
-- {SITE}-{CLASS}-{SEQ}, distinct from the cmms MKE/DET/CRR tag scheme).
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO equipment (tag_code, equip_desc, machine_class, plant_site, cell, vendor, run_state, crit_level, commissioned_on) VALUES
('AUS-PRESS-01', 'Hydraulic Stamping Press 1',   'press',        'Austin',     'Stamping-C1', 'AnyCompany Press Works', 'running',     'critical', '2021-02-12'),
('AUS-PRESS-02', 'Hydraulic Stamping Press 2',   'press',        'Austin',     'Stamping-C1', 'AnyCompany Press Works', 'running',     'high',     '2021-02-12'),
('AUS-WELD-01',  'Robotic Spot Welder A',        'welder',       'Austin',     'Weld-C2',     'AnyCompany Robotics',    'running',     'high',     '2021-03-05'),
('AUS-CONV-01',  'Main Transfer Conveyor',       'conveyor',     'Austin',     'Weld-C2',     'AnyCompany Conveyance',  'running',     'medium',   '2021-03-05'),
('AUS-PAINT-01', 'Electrostatic Paint Booth',    'paint_booth',  'Austin',     'Finish-C3',   'AnyCompany Coatings',    'down',        'high',     '2021-04-18'),
('REN-LATHE-01', 'CNC Turning Lathe 1',          'lathe',        'Reno',       'Machine-C1',  'AnyCompany Machine Tools','running',    'critical', '2020-11-30'),
('REN-LATHE-02', 'CNC Turning Lathe 2',          'lathe',        'Reno',       'Machine-C1',  'AnyCompany Machine Tools','running',    'critical', '2020-11-30'),
('REN-GRIND-01', 'Surface Grinder A',            'grinder',      'Reno',       'Machine-C2',  'AnyCompany Abrasives',   'running',     'medium',   '2021-01-22'),
('REN-CMM-01',   'Coordinate Measuring Machine', 'cmm',          'Reno',       'Quality-C3',  'AnyCompany Metrology',   'maintenance', 'high',     '2021-05-10'),
('TUL-DRYER-01', 'Spray Dryer Tower 1',          'dryer',        'Tulsa',      'Process-C1',  'AnyCompany Process Eq',  'running',     'critical', '2020-08-14'),
('TUL-MILL-01',  'Hammer Mill A',                'mill',         'Tulsa',      'Process-C2',  'AnyCompany Milling',     'running',     'high',     '2020-09-01'),
('TUL-SIEVE-01', 'Rotary Sifter / Sieve',        'sieve',        'Tulsa',      'Process-C2',  'AnyCompany Screening',   'running',     'low',      '2020-09-01'),
('TUL-BAGR-01',  'Open-Mouth Bagging Machine',   'bagger',       'Tulsa',      'Pack-C3',     'AnyCompany Bagging',     'running',     'medium',   '2020-10-20')
ON CONFLICT (tag_code) DO NOTHING;

INSERT INTO repair_tickets (ticket_no, equip_uid, ticket_class, urgency, summary, tech, state, fault_code) VALUES
('TKT-AUS-5001', (SELECT equip_uid FROM cmms2_maintenance.equipment WHERE tag_code='AUS-PAINT-01'), 'corrective', 'critical', 'Paint booth exhaust fan tripped on overcurrent — booth offline pending motor inspection', 'crew_red',   'in_progress', 'OVERCURRENT'),
('TKT-AUS-5002', (SELECT equip_uid FROM cmms2_maintenance.equipment WHERE tag_code='AUS-PRESS-01'), 'preventive', 'medium',   'Scheduled 500k-cycle ram packing replacement on stamping press 1',                         'crew_red',   'open',        NULL),
('TKT-AUS-5003', (SELECT equip_uid FROM cmms2_maintenance.equipment WHERE tag_code='AUS-WELD-01'),  'corrective', 'high',     'Weld tip wear out of spec on 4% of joints — replace electrode caps and recalibrate',       'crew_red',   'open',        'TIP-WEAR'),
('TKT-REN-3010', (SELECT equip_uid FROM cmms2_maintenance.equipment WHERE tag_code='REN-LATHE-01'), 'corrective', 'critical', 'Spindle bearing temperature rising above 70C under load — inspect lubrication circuit',     'crew_blue',  'in_progress', 'BEARING-TEMP'),
('TKT-REN-3011', (SELECT equip_uid FROM cmms2_maintenance.equipment WHERE tag_code='REN-LATHE-02'), 'preventive', 'medium',   'Quarterly way-oil system flush and filter change on lathe 2',                              'crew_blue',  'open',        NULL),
('TKT-REN-3012', (SELECT equip_uid FROM cmms2_maintenance.equipment WHERE tag_code='REN-CMM-01'),   'inspection', 'high',     'Annual CMM probe accuracy verification against reference sphere — calibration due',        'crew_blue',  'completed',   NULL),
('TKT-TUL-7020', (SELECT equip_uid FROM cmms2_maintenance.equipment WHERE tag_code='TUL-DRYER-01'), 'corrective', 'high',     'Spray dryer outlet temperature drifting 6C low — inspect burner and combustion air damper', 'crew_green', 'in_progress', 'TEMP-LOW'),
('TKT-TUL-7021', (SELECT equip_uid FROM cmms2_maintenance.equipment WHERE tag_code='TUL-MILL-01'),  'preventive', 'medium',   'Hammer mill screen and hammer wear inspection — replace if past 80% wear limit',           'crew_green', 'open',        NULL),
('TKT-TUL-7022', (SELECT equip_uid FROM cmms2_maintenance.equipment WHERE tag_code='TUL-BAGR-01'),  'corrective', 'low',      'Bag clamp solenoid intermittently slow to actuate — clean and retime valve',               'crew_green', 'open',        'SOLENOID-SLOW'),
('TKT-AUS-5004', (SELECT equip_uid FROM cmms2_maintenance.equipment WHERE tag_code='AUS-CONV-01'),  'inspection', 'medium',   'Monthly transfer conveyor belt tracking and roller bearing inspection',                    'crew_red',   'completed',   NULL)
ON CONFLICT (ticket_no) DO NOTHING;

INSERT INTO pm_plan (plan_id, pm_kind, interval_days, crew, due_on) VALUES
('PM-AUS-PRESS-01', 'preventive',  180, 'crew_red',   '2026-07-01'),
('PM-AUS-WELD-01',  'calibration', 30,  'crew_red',   '2026-06-20'),
('PM-AUS-PAINT-01', 'inspection',  90,  'crew_red',   '2026-06-15'),
('PM-REN-LATHE-01', 'preventive',  90,  'crew_blue',  '2026-06-25'),
('PM-REN-LATHE-02', 'preventive',  90,  'crew_blue',  '2026-07-10'),
('PM-REN-CMM-01',   'calibration', 365, 'crew_blue',  '2026-06-18'),
('PM-TUL-DRYER-01', 'preventive',  60,  'crew_green', '2026-06-22'),
('PM-TUL-MILL-01',  'inspection',  45,  'crew_green', '2026-06-30')
ON CONFLICT (plan_id) DO NOTHING;

INSERT INTO stockroom (sku, sku_desc, fits_class, on_hand_qty, min_qty, unit_price, vendor, lead_days) VALUES
('SK-RAM-PACK',   'Press Ram Packing Seal Kit',      'press',       10, 4,  68.00,  'AnyCompany Press Works',  10),
('SK-WELD-CAP',   'Weld Electrode Cap Set (20pc)',   'welder',      40, 15,  22.00,  'AnyCompany Robotics',      4),
('SK-PAINT-FAN',  'Exhaust Fan Motor 5HP',           'paint_booth',  2, 1, 540.00,  'AnyCompany Coatings',     21),
('SK-CONV-ROLL',  'Conveyor Idler Roller',           'conveyor',     8, 3,  47.00,  'AnyCompany Conveyance',    7),
('SK-LATHE-BRG',  'Lathe Spindle Bearing Set',       'lathe',        3, 2, 410.00,  'AnyCompany Machine Tools',21),
('SK-LATHE-OIL',  'Way Oil 20L Drum',                'lathe',       12, 5,  95.00,  'AnyCompany Lubricants',    5),
('SK-GRIND-WHL',  'Surface Grinder Wheel 14in',      'grinder',      6, 3, 130.00,  'AnyCompany Abrasives',    10),
('SK-CMM-PROBE',  'CMM Touch Probe Stylus',          'cmm',          4, 2, 215.00,  'AnyCompany Metrology',    14),
('SK-DRYER-NOZ',  'Spray Dryer Atomizer Nozzle',     'dryer',        5, 2, 320.00,  'AnyCompany Process Eq',   21),
('SK-MILL-HAMR',  'Hammer Mill Hammer Set',          'mill',         9, 4,  88.00,  'AnyCompany Milling',       7),
('SK-SIEVE-MESH', 'Rotary Sifter Screen Mesh',       'sieve',        7, 3,  54.00,  'AnyCompany Screening',    10),
('SK-BAGR-SOL',   'Bagger Clamp Solenoid Valve',     'bagger',       3, 2, 76.00,   'AnyCompany Bagging',      10)
ON CONFLICT (sku) DO NOTHING;
