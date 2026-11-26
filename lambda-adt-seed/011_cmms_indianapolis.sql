-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- CMMS Indianapolis: maintenance data modeled after IBM Maximo for the Indianapolis plant
-- Third-party product names below describe the naming-convention style only (nominative use); not affiliated/endorsed. See repo NOTICE.
-- Contrasts with Detroit SAP PM (1001-CMX-441 vs DET-EQ-00441, WAPPR/APPR/INPRG/COMP
-- vs CRTD/REL/TECO) — see the naming-convention note in 008_cmms_detroit.sql.
--
-- Where each identifier of the Line 3 milling center lives (no foreign key and
-- no seeded graph rule joins them):
--   CMMS assets.asset_num 1001-CMX-441, assets.description
--     '5-Axis Milling Center CMX-441';
--   MES equipment.equipment_id L3-M1 (mes_line3, and the live MES equipment
--     view), equipment.equipment_name the same text as assets.description;
--   historian equipment_registry.equipment_id 5-Axis-Milling-CMX-441,
--     equipment_name the same text again.
--   (site prefix '1001' = IBM Maximo site id for Indianapolis; see agents/resolve.py
--   known-prefix list and the 009_historian.sql header.)
--
-- KEY DEFECT SCENARIO (batch B-2847, 2026-02-10):
--   * PM work order 1182 (90-day spindle bearing PM) was originally due
--     2025-12-30 — ~6 weeks BEFORE the B-2847 run — but was DEFERRED at
--     production planning's request (status WAPPR, never performed).
--   * 2026-02-10: SPC diameter drift stops batch B-2847 on L3-M1; inspection
--     work order 1187 replaces the toolholder and returns the machine to
--     service the same day (MES B-2851 note "returned to service").
--   * Vibration keeps escalating (iot_historian, Feb 10-14) until the spindle
--     bearing fails; emergency work order 1195 replaces the bearing set in the
--     Feb 14 22:00 -> Feb 15 12:30 production gap (MES L3-M1 idle window).

CREATE SCHEMA IF NOT EXISTS cmms_indianapolis;
SET search_path TO cmms_indianapolis;

-- ─────────────────────────────────────────────────────────────────────────────
-- assets: asset registry modeled after IBM Maximo ({SITEID}-{ASSET} numbering, site 1001)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS assets (
    asset_num       VARCHAR(30)  PRIMARY KEY,       -- IBM Maximo ASSETNUM: 1001-CMX-441
    description     VARCHAR(120) NOT NULL,
    site_id         VARCHAR(10)  NOT NULL DEFAULT '1001',   -- Indianapolis
    location        VARCHAR(30),                    -- functional location
    asset_type      VARCHAR(30),
    status          VARCHAR(15)  NOT NULL DEFAULT 'OPERATING',  -- OPERATING / NOT READY / DECOMMISSIONED
    manufacturer    VARCHAR(100),
    model_number    VARCHAR(50),
    serial_number   VARCHAR(50),
    install_date    DATE,
    priority        SMALLINT     CHECK (priority BETWEEN 1 AND 4)   -- 1 = highest
);

INSERT INTO assets VALUES
-- Line 3 production equipment (matches iot_historian.equipment_registry, Indianapolis/Line3)
('1001-CMX-441', '5-Axis Milling Center CMX-441',  '1001', 'IND-L3-MC1',  'CNC_MILL',   'OPERATING', 'AnyCompany Machine Tools', 'MC-600',  'MC600-IND-01', '2022-06-15', 1),
('1001-HYP-402', 'Hydraulic Press 402',            '1001', 'IND-L3-PRS1', 'PRESS',      'OPERATING', 'AnyCompany Presses',       'PR-200',  'PR200-IND-01', '2019-11-05', 2),
('1001-SGR-501', 'Surface Grinder 501',            '1001', 'IND-L3-GRD1', 'GRINDER',    'OPERATING', 'AnyCompany Grinders',      'SG-1224', 'SG1224-IND-01','2021-07-22', 2),
('1001-CGR-502', 'Cylindrical Grinder 502',        '1001', 'IND-L3-GRD2', 'GRINDER',    'OPERATING', 'AnyCompany Grinders',      'CG-350',  'CG350-IND-01', '2021-07-22', 2),
('1001-CVA-100', 'Main Assembly Conveyor A',       '1001', 'IND-L3-CNV1', 'CONVEYOR',   'OPERATING', 'AnyCompany Conveyors',     'CV-190',  'CV190-IND-01', '2020-08-01', 3),
('1001-TPS-100', 'CNC Tool Presetter',             '1001', 'IND-L3-INS1', 'INSPECTION', 'OPERATING', 'AnyCompany Tooling',       'TP-450',  'TP450-IND-01', '2021-05-01', 3)
ON CONFLICT (asset_num) DO NOTHING;

-- ─────────────────────────────────────────────────────────────────────────────
-- pm_schedules: PM template records modeled after IBM Maximo
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS pm_schedules (
    pm_num              VARCHAR(30)  PRIMARY KEY,
    asset_num           VARCHAR(30)  NOT NULL REFERENCES assets(asset_num),
    description         VARCHAR(200) NOT NULL,
    frequency           INTEGER      NOT NULL,
    frequency_units     VARCHAR(10)  NOT NULL DEFAULT 'DAYS',
    assigned_crew       VARCHAR(50),
    last_completion_date DATE,
    next_due_date       DATE,
    pm_status           VARCHAR(10)  NOT NULL DEFAULT 'ACTIVE'
);

INSERT INTO pm_schedules VALUES
-- THE deferred PM: 90-day spindle bearing PM on CMX-441. Last completed
-- 2025-10-01; next due 2025-12-30 — 42 days (~6 weeks) before the 2026-02-10
-- B-2847 run. Work order 1182 was generated for this due date and deferred.
('PM-1001-0007', '1001-CMX-441', '90-day spindle bearing inspection, lubrication and preload check', 90, 'DAYS', 'IND-MECH-A', '2025-10-01', '2025-12-30', 'ACTIVE'),
('PM-1001-0008', '1001-CMX-441', '180-day way cover and axis lubrication service',                   180, 'DAYS', 'IND-MECH-A', '2025-11-12', '2026-05-11', 'ACTIVE'),
('PM-1001-0011', '1001-HYP-402', '90-day hydraulic fluid analysis and filter change',                 90, 'DAYS', 'IND-MECH-B', '2026-01-08', '2026-04-08', 'ACTIVE'),
('PM-1001-0012', '1001-SGR-501', '60-day wheel dress and spindle runout check',                       60, 'DAYS', 'IND-MECH-B', '2026-01-20', '2026-03-21', 'ACTIVE'),
('PM-1001-0013', '1001-CGR-502', '60-day wheel dress and spindle runout check',                       60, 'DAYS', 'IND-MECH-B', '2026-01-22', '2026-03-23', 'ACTIVE'),
('PM-1001-0014', '1001-CVA-100', '30-day belt tension and roller inspection',                         30, 'DAYS', 'IND-MECH-B', '2026-02-02', '2026-03-04', 'ACTIVE'),
('PM-1001-0015', '1001-TPS-100', '180-day probe calibration and qualification',                       180, 'DAYS', 'IND-INST-A', '2025-12-10', '2026-06-08', 'ACTIVE')
ON CONFLICT (pm_num) DO NOTHING;

-- ─────────────────────────────────────────────────────────────────────────────
-- work_orders: work orders modeled after IBM Maximo (numeric WONUM, WAPPR/APPR/INPRG/
-- WMATL/COMP/CLOSE status vocabulary)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS work_orders (
    wonum           VARCHAR(15)  PRIMARY KEY,       -- IBM Maximo WONUM (numeric)
    asset_num       VARCHAR(30)  NOT NULL REFERENCES assets(asset_num),
    work_type       VARCHAR(5)   NOT NULL CHECK (work_type IN ('PM','CM','EM','CAL')),
    description     VARCHAR(250) NOT NULL,
    status          VARCHAR(10)  NOT NULL CHECK (status IN ('WAPPR','APPR','INPRG','WMATL','COMP','CLOSE','CAN')),
    priority        SMALLINT     CHECK (priority BETWEEN 1 AND 4),
    pm_num          VARCHAR(30)  REFERENCES pm_schedules(pm_num),
    reported_by     VARCHAR(60),
    reported_date   TIMESTAMP,
    target_start    TIMESTAMP,                      -- original due date (does NOT move on deferral)
    sched_start     TIMESTAMP,                      -- current schedule (moves on deferral)
    actual_start    TIMESTAMP,
    actual_finish   TIMESTAMP,
    failure_code    VARCHAR(20),
    crew            VARCHAR(50)
);

-- A re-run gives a row the account already holds the seed's reported_by and
-- leaves every other column as it is: the live story moves the status, dates
-- and descriptions of these rows (erp_simulation/story_history.py).
INSERT INTO work_orders VALUES
-- ── 1001-CMX-441: the B-2847 story ──────────────────────────────────────────
-- Completed 90-day spindle PMs (history establishing the cadence)
('1004', '1001-CMX-441', 'PM', '90-day spindle bearing inspection, lubrication and preload check', 'CLOSE', 2, 'PM-1001-0007', 'R. Roe',       '2025-06-27 07:00', '2025-07-03 06:00', '2025-07-03 06:00', '2025-07-03 06:30', '2025-07-03 14:15', NULL,      'IND-MECH-A'),
('1096', '1001-CMX-441', 'PM', '90-day spindle bearing inspection, lubrication and preload check', 'CLOSE', 2, 'PM-1001-0007', 'R. Roe',       '2025-09-24 07:00', '2025-10-01 06:00', '2025-10-01 06:00', '2025-10-01 06:15', '2025-10-01 14:30', NULL,      'IND-MECH-A'),
-- THE DEFERRED PM — work order 1182. Generated for the 2025-12-30 due date
-- (~6 weeks before the 2026-02-10 B-2847 run), pushed back to 2026-02-24 at
-- production planning's request, and STILL WAPPR (never approved, never
-- performed) when B-2847 ran and the spindle bearing failed. See work_order_log.
('1182', '1001-CMX-441', 'PM', '90-day spindle bearing inspection, lubrication and preload check — DEFERRED at production planning request (Line 3 at full loading)', 'WAPPR', 2, 'PM-1001-0007', 'R. Roe', '2025-12-22 07:00', '2025-12-30 06:00', '2026-02-24 06:00', NULL, NULL, NULL, 'IND-MECH-A'),
-- Post-SPC-stop inspection (same day as the B-2847 stop; machine returned to
-- service that evening — matches MES B-2851 "returned to service" note)
('1187', '1001-CMX-441', 'CM', 'SPC diameter drift on batch B-2847 / MES work order WO-9921 — inspect spindle and toolholder. Findings: toolholder taper fretting, replaced; spindle runout marginal. Recommend expediting deferred PM 1182.', 'COMP', 1, NULL, 'OP-0101 via MES', '2026-02-10 11:45', '2026-02-10 12:30', '2026-02-10 12:30', '2026-02-10 13:00', '2026-02-10 16:30', 'DIM-DRIFT', 'IND-MECH-A'),
-- Emergency bearing replacement in the Feb 14 22:00 -> Feb 15 14:00 MES idle
-- window (historian vibration reached ~2.8 mm/s ALARM earlier that shift)
('1195', '1001-CMX-441', 'EM', 'Spindle vibration alarm 2.8 mm/s — spindle bearing set failed. Replace bearing set, realign spindle, verify runout. Root cause: deferred 90-day bearing PM (work order 1182, originally due 2025-12-30).', 'COMP', 1, NULL, 'R. Roe', '2026-02-14 21:50', '2026-02-14 22:30', '2026-02-14 22:30', '2026-02-14 22:30', '2026-02-15 12:30', 'BEARING', 'IND-MECH-A'),
-- Next regular PM cycle re-baselined after the repair
('1201', '1001-CMX-441', 'PM', '90-day spindle bearing inspection, lubrication and preload check', 'APPR', 2, 'PM-1001-0007', 'R. Roe',       '2026-02-15 13:30', '2026-05-16 06:00', '2026-05-16 06:00', NULL, NULL, NULL, 'IND-MECH-A'),
-- ── Other Line 3 assets: routine history ─────────────────────────────────────
('1105', '1001-HYP-402', 'PM', '90-day hydraulic fluid analysis and filter change',                 'CLOSE', 3, 'PM-1001-0011', 'D. Ramirez',   '2026-01-02 07:00', '2026-01-08 06:00', '2026-01-08 06:00', '2026-01-08 06:30', '2026-01-08 13:00', NULL,      'IND-MECH-B'),
('1132', '1001-SGR-501', 'PM', '60-day wheel dress and spindle runout check',                       'CLOSE', 3, 'PM-1001-0012', 'D. Ramirez',   '2026-01-14 07:00', '2026-01-20 06:00', '2026-01-20 06:00', '2026-01-20 06:30', '2026-01-20 11:45', NULL,      'IND-MECH-B'),
('1139', '1001-CGR-502', 'PM', '60-day wheel dress and spindle runout check',                       'CLOSE', 3, 'PM-1001-0013', 'D. Ramirez',   '2026-01-16 07:00', '2026-01-22 06:00', '2026-01-22 06:00', '2026-01-22 06:30', '2026-01-22 12:00', NULL,      'IND-MECH-B'),
('1160', '1001-CVA-100', 'PM', '30-day belt tension and roller inspection',                         'CLOSE', 3, 'PM-1001-0014', 'D. Ramirez',   '2026-01-27 07:00', '2026-02-02 06:00', '2026-02-02 06:00', '2026-02-02 06:30', '2026-02-02 09:45', NULL,      'IND-MECH-B'),
('1171', '1001-HYP-402', 'CM', 'Pressure relief valve weeping at fitting — reseal',                 'COMP',  3, NULL,           'D. Ramirez',   '2026-02-05 09:20', '2026-02-06 06:00', '2026-02-06 06:00', '2026-02-06 06:30', '2026-02-06 10:00', 'LEAK',    'IND-MECH-B'),
('1198', '1001-TPS-100', 'CAL','Probe repeatability drift flagged during tool setup — recalibrate', 'APPR',  3, NULL,           'OP-0102 via MES','2026-02-16 08:10', '2026-02-20 06:00', '2026-02-20 06:00', NULL,               NULL,               'DRIFT',   'IND-INST-A'),
('1204', '1001-CVA-100', 'PM', '30-day belt tension and roller inspection',                         'WAPPR', 3, 'PM-1001-0014', 'D. Ramirez',   '2026-02-26 07:00', '2026-03-04 06:00', '2026-03-04 06:00', NULL,               NULL,               NULL,      'IND-MECH-B')
ON CONFLICT (wonum) DO UPDATE SET reported_by = EXCLUDED.reported_by
WHERE work_orders.reported_by IS DISTINCT FROM EXCLUDED.reported_by;

-- ─────────────────────────────────────────────────────────────────────────────
-- work_order_log: entries modeled after the IBM Maximo WORKLOG (the deferral paper trail)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS work_order_log (
    log_id          SERIAL PRIMARY KEY,
    wonum           VARCHAR(15) NOT NULL REFERENCES work_orders(wonum),
    log_date        TIMESTAMP   NOT NULL,
    author          VARCHAR(60),
    log_type        VARCHAR(15) DEFAULT 'UPDATE',
    entry           TEXT        NOT NULL
);

-- An entry is matched on (wonum, log_date, log_type, entry), never its
-- author: a re-run gives an entry the account already holds the seed's
-- author instead of writing it a second time. An account seeded before this
-- insert had a duplicate guard (before 2026-09-30) can hold a seeded entry
-- twice: the re-run keeps the first copy (lowest log_id) and deletes the
-- later ones. The delete and the rename touch different rows, so they run in
-- one statement; rows the seed did not write are never touched.
WITH seed (wonum, log_date, author, log_type, entry) AS (VALUES
('1182', '2025-12-22 07:05', 'R. Roe',      'CREATE',  'PM work order generated from PM-1001-0007. Due 2025-12-30 per 90-day cycle (last completed 2025-10-01).'),
('1182', '2025-12-28 15:40', 'J. Stiles',   'DEFERRAL','Deferred at production planning request — Line 3 at full loading through Q1 demand peak; approval withheld. Rescheduled to 2026-02-24. Risk noted: spindle bearing PM will exceed the 90-day interval.'),
('1182', '2026-02-10 17:05', 'R. Roe',      'UPDATE',  'Inspection under work order 1187 (batch B-2847 SPC stop) recommends expediting this PM — still awaiting approval.'),
('1182', '2026-02-15 13:00', 'R. Roe',      'UPDATE',  'Overtaken by events: spindle bearing failed 2026-02-14 and was replaced under emergency work order 1195 — 46 days after this PM''s original 2025-12-30 due date.'),
('1187', '2026-02-10 16:35', 'R. Roe',      'COMPLETE','Toolholder replaced, runout marginal but within limits. Machine returned to service for 2026-02-11 06:00 shift. Escalated PM 1182 for expedite.'),
('1195', '2026-02-15 12:45', 'R. Roe',      'COMPLETE','Spindle bearing set replaced, spindle realigned, runout verified 0.008 mm. Post-repair vibration 0.9-1.3 mm/s. Root cause: bearing wear consistent with overdue lubrication/preload PM (deferred work order 1182).')
), later_copies AS (
    DELETE FROM work_order_log t
    USING seed s
    WHERE ROW(t.wonum, t.log_date, t.log_type, t.entry) IS NOT DISTINCT FROM ROW(s.wonum, s.log_date::TIMESTAMP, s.log_type, s.entry)
      AND EXISTS (SELECT 1 FROM work_order_log k
                  WHERE ROW(k.wonum, k.log_date, k.log_type, k.entry) IS NOT DISTINCT FROM ROW(t.wonum, t.log_date, t.log_type, t.entry)
                    AND k.log_id < t.log_id)
), authored AS (
    UPDATE work_order_log t SET author = s.author
    FROM seed s
    WHERE ROW(t.wonum, t.log_date, t.log_type, t.entry) IS NOT DISTINCT FROM ROW(s.wonum, s.log_date::TIMESTAMP, s.log_type, s.entry)
      AND t.author IS DISTINCT FROM s.author
      AND NOT EXISTS (SELECT 1 FROM work_order_log k
                      WHERE ROW(k.wonum, k.log_date, k.log_type, k.entry) IS NOT DISTINCT FROM ROW(t.wonum, t.log_date, t.log_type, t.entry)
                        AND k.log_id < t.log_id)
)
INSERT INTO work_order_log (wonum, log_date, author, log_type, entry)
SELECT s.wonum, s.log_date::TIMESTAMP, s.author, s.log_type, s.entry
FROM seed s
WHERE NOT EXISTS (SELECT 1 FROM work_order_log t WHERE ROW(t.wonum, t.log_date, t.log_type, t.entry) IS NOT DISTINCT FROM ROW(s.wonum, s.log_date::TIMESTAMP, s.log_type, s.entry));
