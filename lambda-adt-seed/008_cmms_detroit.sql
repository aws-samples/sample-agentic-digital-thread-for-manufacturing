-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- CMMS Detroit: SAP PM-style maintenance data
-- Third-party product names below describe the naming-convention style only (nominative use); not affiliated/endorsed. See repo NOTICE.
-- Contrasts with the Indianapolis CMMS modeled after IBM Maximo (CMX-441 vs DET-EQ-00441, WAPPR/APPR vs CRTD/REL/TECO)
SET search_path TO cmms_detroit;

CREATE TABLE IF NOT EXISTS equipment (
    equipment_id        VARCHAR(15) PRIMARY KEY,
    description         VARCHAR(100) NOT NULL,
    equipment_category  CHAR(1) CHECK (equipment_category IN ('M','T','I')),  -- Machine/Tool/Instrument
    functional_location VARCHAR(20),
    plant               VARCHAR(4) DEFAULT '2000',
    status              VARCHAR(4) CHECK (status IN ('AVLB','INAC','DLFL')),
    manufacturer        VARCHAR(50),
    model_number        VARCHAR(30),
    acquisition_date    DATE,
    acquisition_cost    NUMERIC(12,2)
);

INSERT INTO equipment VALUES
('DET-EQ-00441', '5-Axis CNC Milling Center',          'M', 'DET-PROD-CNC-01', '2000', 'AVLB', 'AnyCompany Machine Tools', 'MC-600',   '2022-06-15', 485000.00),
('DET-EQ-00442', '5-Axis CNC Milling Center',          'M', 'DET-PROD-CNC-02', '2000', 'AVLB', 'AnyCompany Machine Tools', 'MC-600',   '2022-06-15', 485000.00),
('DET-EQ-00443', '3-Axis CNC Milling Center',          'M', 'DET-PROD-CNC-03', '2000', 'AVLB', 'AnyCompany Machine Tools', 'MC-45',    '2023-01-10', 195000.00),
('DET-EQ-00444', '5-Axis CNC Milling Center',          'M', 'DET-PROD-CNC-04', '2000', 'INAC', 'AnyCompany Machine Tools', 'MC-1100',  '2024-03-20', 620000.00),
('DET-EQ-00445', 'CNC Turning Center',                 'M', 'DET-PROD-LAT-01', '2000', 'AVLB', 'AnyCompany Machine Tools', 'TC-200',   '2022-09-01', 210000.00),
('DET-EQ-00446', 'CNC Turning Center',                 'M', 'DET-PROD-LAT-02', '2000', 'AVLB', 'AnyCompany Machine Tools', 'TC-200',   '2022-09-01', 210000.00),
('DET-EQ-00447', 'CNC Turning Center',                 'M', 'DET-PROD-LAT-03', '2000', 'AVLB', 'AnyCompany Machine Tools', 'TC-350',   '2023-04-15', 265000.00),
('DET-EQ-00448', 'EDM Wire Cutting Machine',           'M', 'DET-PROD-EDM-01', '2000', 'AVLB', 'AnyCompany Machine Tools', 'WC-2400',  '2023-07-20', 320000.00),
('DET-EQ-00449', 'Surface Grinding Machine',           'M', 'DET-PROD-GRD-01', '2000', 'AVLB', 'AnyCompany Grinders',      'SG-1224',  '2021-11-05', 145000.00),
('DET-EQ-00450', 'Coordinate Measuring Machine',       'I', 'DET-QUAL-CMM-01', '2000', 'AVLB', 'AnyCompany Metrology',     'CM-200',   '2022-03-10', 380000.00),
('DET-EQ-00451', 'Coordinate Measuring Machine',       'I', 'DET-QUAL-CMM-02', '2000', 'AVLB', 'AnyCompany Measurement',   'CM-710',   '2023-08-22', 295000.00),
('DET-EQ-00452', 'Hydraulic Press 200T',               'M', 'DET-PROD-PRS-01', '2000', 'AVLB', 'AnyCompany Presses',       'PR-200',   '2021-06-30', 175000.00),
('DET-EQ-00453', 'Robotic Welding Cell',               'M', 'DET-PROD-WLD-01', '2000', 'AVLB', 'AnyCompany Robotics',      'RW-120',   '2023-02-14', 420000.00),
('DET-EQ-00454', 'Robotic Welding Cell',               'M', 'DET-PROD-WLD-02', '2000', 'INAC', 'AnyCompany Robotics',      'RW-120',   '2023-02-14', 420000.00),
('DET-EQ-00455', 'Conveyor System Line 4',             'M', 'DET-PROD-CNV-04', '2000', 'AVLB', 'AnyCompany Conveyors',     'CV-190',   '2021-09-01', 85000.00),
('DET-EQ-00456', 'Conveyor System Line 5',             'M', 'DET-PROD-CNV-05', '2000', 'AVLB', 'AnyCompany Conveyors',     'CV-190',   '2021-09-01', 85000.00),
('DET-EQ-00457', 'Air Compressor 150HP',               'M', 'DET-UTIL-CMP-01', '2000', 'AVLB', 'AnyCompany Industrial',    'CMP-110',  '2020-12-01', 68000.00),
('DET-EQ-00458', 'Air Compressor 150HP',               'M', 'DET-UTIL-CMP-02', '2000', 'AVLB', 'AnyCompany Industrial',    'CMP-110',  '2020-12-01', 68000.00),
('DET-EQ-00459', 'Coolant Filtration System',          'M', 'DET-UTIL-CLT-01', '2000', 'AVLB', 'AnyCompany Filtration',    'FS-200',   '2022-01-15', 42000.00),
('DET-EQ-00460', 'Industrial Chiller 60T',             'M', 'DET-UTIL-CHL-01', '2000', 'AVLB', 'AnyCompany HVAC',          'CHL-60T',  '2021-05-20', 95000.00),
('DET-EQ-00461', 'Overhead Crane 10T',                 'M', 'DET-PROD-CRN-01', '2000', 'AVLB', 'AnyCompany Cranes',        'CR-2L',    '2020-08-10', 120000.00),
('DET-EQ-00462', 'Laser Marking System',               'T', 'DET-PROD-LSR-01', '2000', 'AVLB', 'AnyCompany Lasers',        'LM-5020',  '2023-05-18', 58000.00),
('DET-EQ-00463', 'Ultrasonic Cleaning System',         'M', 'DET-PROD-ULT-01', '2000', 'AVLB', 'AnyCompany Ultrasonics',   'UC-2800',  '2022-11-30', 32000.00),
('DET-EQ-00464', 'Spindle Vibration Monitor',          'I', 'DET-PROD-CNC-01', '2000', 'AVLB', 'AnyCompany Instruments',   'VM-3560',  '2023-09-01', 28000.00),
('DET-EQ-00465', 'CNC Tool Presetter',                 'T', 'DET-PROD-TLS-01', '2000', 'DLFL', 'AnyCompany Tooling',       'TP-450',   '2019-04-12', 75000.00)
ON CONFLICT (equipment_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS maintenance_notifications (
    notification_id VARCHAR(16) PRIMARY KEY,
    equipment_id    VARCHAR(15) REFERENCES equipment(equipment_id),
    notification_type CHAR(2) CHECK (notification_type IN ('M1','M2','M3')),  -- M1=breakdown, M2=corrective, M3=preventive
    priority        SMALLINT CHECK (priority BETWEEN 1 AND 4),
    description     TEXT,
    reported_by     VARCHAR(50),
    reported_date   TIMESTAMP,
    status          VARCHAR(4) CHECK (status IN ('OSNO','INPR','COMP','NOAC'))
);

INSERT INTO maintenance_notifications VALUES
-- DET-EQ-00441 (CNC-01) — vibration/spindle history
('NTF-2026-003301', 'DET-EQ-00441', 'M3', 3, 'Scheduled PM: lubrication and alignment check',                    'Kwaku Mensah',    '2026-01-15 07:00:00', 'COMP'),
('NTF-2026-003302', 'DET-EQ-00441', 'M2', 2, 'Spindle noise above baseline — schedule inspection',               'Kwaku Mensah',    '2026-02-10 09:15:00', 'COMP'),
('NTF-2026-003303', 'DET-EQ-00441', 'M1', 1, 'Spindle bearing failure — machine down, production stopped',       'Kwaku Mensah',    '2026-02-14 22:45:00', 'COMP'),
('NTF-2026-003304', 'DET-EQ-00441', 'M2', 2, 'Post-repair vibration recheck — values within spec',               'Terry Whitlock',   '2026-02-16 14:00:00', 'COMP'),
('NTF-2026-003305', 'DET-EQ-00441', 'M3', 3, 'Scheduled PM: coolant filter replacement',                         'Kwaku Mensah',    '2026-03-01 07:00:00', 'OSNO'),
-- DET-EQ-00442 (CNC-02)
('NTF-2026-003306', 'DET-EQ-00442', 'M3', 3, 'Scheduled PM: lubrication and alignment check',                    'Kwaku Mensah',    '2026-01-15 07:00:00', 'COMP'),
('NTF-2026-003307', 'DET-EQ-00442', 'M2', 3, 'Tool changer gripper wear — replace before next PM window',        'Diego Ramirez',    '2026-02-05 11:30:00', 'COMP'),
('NTF-2026-003308', 'DET-EQ-00442', 'M3', 3, 'Scheduled PM: spindle load monitoring calibration',                'Kwaku Mensah',    '2026-03-01 07:00:00', 'OSNO'),
-- DET-EQ-00443 (CNC-03)
('NTF-2026-003309', 'DET-EQ-00443', 'M3', 4, 'Scheduled PM: way lube system check',                              'Diego Ramirez',    '2026-01-20 07:00:00', 'COMP'),
('NTF-2026-003310', 'DET-EQ-00443', 'M2', 3, 'X-axis ballscrew backlash exceeds tolerance — corrective required', 'Diego Ramirez',   '2026-02-18 10:00:00', 'INPR'),
('NTF-2026-003311', 'DET-EQ-00443', 'M3', 4, 'Scheduled PM: coolant system flush',                               'Kwaku Mensah',    '2026-03-05 07:00:00', 'OSNO'),
-- DET-EQ-00444 (CNC-04 — INAC)
('NTF-2026-003312', 'DET-EQ-00444', 'M1', 1, 'Servo drive fault — axis 4 unresponsive, machine taken offline',   'Terry Whitlock',   '2026-02-20 14:30:00', 'INPR'),
('NTF-2026-003313', 'DET-EQ-00444', 'M2', 2, 'Servo drive replacement — awaiting parts from AnyCompany Machine Tools', 'Terry Whitlock',   '2026-02-21 08:00:00', 'INPR'),
-- DET-EQ-00445 (Lathe-01)
('NTF-2026-003314', 'DET-EQ-00445', 'M3', 4, 'Scheduled PM: chuck jaw inspection and lubrication',               'Diego Ramirez',    '2026-01-25 07:00:00', 'COMP'),
('NTF-2026-003315', 'DET-EQ-00445', 'M2', 3, 'Coolant pump seal leak — minor, schedule repair',                  'Kwaku Mensah',    '2026-02-12 09:00:00', 'COMP'),
('NTF-2026-003316', 'DET-EQ-00445', 'M3', 4, 'Scheduled PM: turret indexing accuracy check',                     'Diego Ramirez',    '2026-03-01 07:00:00', 'OSNO'),
-- DET-EQ-00446 (Lathe-02)
('NTF-2026-003317', 'DET-EQ-00446', 'M3', 4, 'Scheduled PM: chuck jaw inspection and lubrication',               'Diego Ramirez',    '2026-01-25 07:00:00', 'COMP'),
('NTF-2026-003318', 'DET-EQ-00446', 'M3', 4, 'Scheduled PM: turret indexing accuracy check',                     'Diego Ramirez',    '2026-03-01 07:00:00', 'OSNO'),
-- DET-EQ-00447 (Lathe-03)
('NTF-2026-003319', 'DET-EQ-00447', 'M3', 4, 'Scheduled PM: spindle bearing preload check',                      'Kwaku Mensah',    '2026-02-01 07:00:00', 'COMP'),
('NTF-2026-003320', 'DET-EQ-00447', 'M2', 3, 'Bar feeder alignment drift — parts out of tolerance',              'Diego Ramirez',    '2026-02-22 13:00:00', 'COMP'),
-- DET-EQ-00448 (EDM)
('NTF-2026-003321', 'DET-EQ-00448', 'M3', 4, 'Scheduled PM: wire guide and filter replacement',                  'Terry Whitlock',   '2026-01-28 07:00:00', 'COMP'),
('NTF-2026-003322', 'DET-EQ-00448', 'M2', 3, 'Dielectric fluid conductivity out of range — flush and refill',    'Terry Whitlock',   '2026-02-25 10:30:00', 'INPR'),
-- DET-EQ-00449 (Grinder)
('NTF-2026-003323', 'DET-EQ-00449', 'M3', 4, 'Scheduled PM: wheel dress and balance check',                      'Kwaku Mensah',    '2026-02-05 07:00:00', 'COMP'),
('NTF-2026-003324', 'DET-EQ-00449', 'M2', 3, 'Magnetic chuck demagnetization cycle required',                    'Kwaku Mensah',    '2026-02-28 11:00:00', 'OSNO'),
-- DET-EQ-00450 (CMM-01)
('NTF-2026-003325', 'DET-EQ-00450', 'M3', 4, 'Scheduled PM: probe calibration and qualification',                'Terry Whitlock',   '2026-01-10 07:00:00', 'COMP'),
('NTF-2026-003326', 'DET-EQ-00450', 'M3', 4, 'Scheduled PM: probe calibration and qualification',                'Terry Whitlock',   '2026-03-10 07:00:00', 'OSNO'),
-- DET-EQ-00451 (CMM-02)
('NTF-2026-003327', 'DET-EQ-00451', 'M3', 4, 'Scheduled PM: probe calibration and qualification',                'Terry Whitlock',   '2026-01-10 07:00:00', 'COMP'),
('NTF-2026-003328', 'DET-EQ-00451', 'M2', 2, 'Air bearing pressure drop — investigate and correct',              'Terry Whitlock',   '2026-02-17 15:00:00', 'COMP'),
-- DET-EQ-00452 (Press)
('NTF-2026-003329', 'DET-EQ-00452', 'M3', 4, 'Scheduled PM: hydraulic fluid analysis and filter change',         'Diego Ramirez',    '2026-01-20 07:00:00', 'COMP'),
('NTF-2026-003330', 'DET-EQ-00452', 'M2', 3, 'Pressure relief valve chattering — replace valve',                 'Diego Ramirez',    '2026-02-26 09:00:00', 'INPR'),
-- DET-EQ-00453 (Weld Cell-01)
('NTF-2026-003331', 'DET-EQ-00453', 'M3', 4, 'Scheduled PM: TCP calibration and wire liner replacement',         'Kwaku Mensah',    '2026-02-01 07:00:00', 'COMP'),
('NTF-2026-003332', 'DET-EQ-00453', 'M2', 3, 'Weld spatter buildup on nozzle — clean and inspect',               'Kwaku Mensah',    '2026-02-19 14:00:00', 'COMP'),
-- DET-EQ-00454 (Weld Cell-02 — INAC)
('NTF-2026-003333', 'DET-EQ-00454', 'M1', 1, 'Robot controller E-stop fault — cell offline pending investigation','Terry Whitlock',   '2026-02-23 16:00:00', 'INPR'),
-- DET-EQ-00455 (Conveyor L4)
('NTF-2026-003334', 'DET-EQ-00455', 'M3', 4, 'Scheduled PM: belt tension and roller inspection',                 'Diego Ramirez',    '2026-02-10 07:00:00', 'COMP'),
('NTF-2026-003335', 'DET-EQ-00455', 'M2', 3, 'Drive motor bearing temperature elevated — monitor and plan',      'Diego Ramirez',    '2026-02-27 10:00:00', 'INPR'),
-- DET-EQ-00456 (Conveyor L5)
('NTF-2026-003336', 'DET-EQ-00456', 'M3', 4, 'Scheduled PM: belt tension and roller inspection',                 'Diego Ramirez',    '2026-02-10 07:00:00', 'COMP'),
-- DET-EQ-00457 (Compressor-01)
('NTF-2026-003337', 'DET-EQ-00457', 'M3', 4, 'Scheduled PM: oil change and air filter replacement',              'Kwaku Mensah',    '2026-01-15 07:00:00', 'COMP'),
('NTF-2026-003338', 'DET-EQ-00457', 'M3', 4, 'Scheduled PM: oil change and air filter replacement',              'Kwaku Mensah',    '2026-03-15 07:00:00', 'OSNO'),
-- DET-EQ-00458 (Compressor-02)
('NTF-2026-003339', 'DET-EQ-00458', 'M3', 4, 'Scheduled PM: oil change and air filter replacement',              'Kwaku Mensah',    '2026-01-15 07:00:00', 'COMP'),
('NTF-2026-003340', 'DET-EQ-00458', 'M2', 3, 'Inlet valve wear — replace at next PM window',                     'Kwaku Mensah',    '2026-02-24 11:00:00', 'OSNO'),
-- DET-EQ-00459 (Coolant)
('NTF-2026-003341', 'DET-EQ-00459', 'M3', 4, 'Scheduled PM: coolant concentration check and top-off',            'Diego Ramirez',    '2026-02-01 07:00:00', 'COMP'),
('NTF-2026-003342', 'DET-EQ-00459', 'M3', 4, 'Scheduled PM: coolant concentration check and top-off',            'Diego Ramirez',    '2026-03-01 07:00:00', 'OSNO'),
-- DET-EQ-00460 (Chiller)
('NTF-2026-003343', 'DET-EQ-00460', 'M3', 4, 'Scheduled PM: refrigerant level and condenser coil inspection',    'Terry Whitlock',   '2026-01-20 07:00:00', 'COMP'),
('NTF-2026-003344', 'DET-EQ-00460', 'M2', 2, 'Condenser fan motor vibration — inspect and balance',              'Terry Whitlock',   '2026-02-28 09:00:00', 'INPR'),
-- DET-EQ-00461 (Crane)
('NTF-2026-003345', 'DET-EQ-00461', 'M3', 3, 'Scheduled PM: hoist brake and limit switch inspection',            'Diego Ramirez',    '2026-01-10 07:00:00', 'COMP'),
('NTF-2026-003346', 'DET-EQ-00461', 'M3', 3, 'Scheduled PM: hoist brake and limit switch inspection',            'Diego Ramirez',    '2026-03-10 07:00:00', 'OSNO'),
-- DET-EQ-00462 (Laser)
('NTF-2026-003347', 'DET-EQ-00462', 'M3', 4, 'Scheduled PM: lens cleaning and beam alignment check',             'Terry Whitlock',   '2026-02-15 07:00:00', 'COMP'),
-- DET-EQ-00463 (Ultrasonic)
('NTF-2026-003348', 'DET-EQ-00463', 'M3', 4, 'Scheduled PM: transducer inspection and solution change',          'Kwaku Mensah',    '2026-02-01 07:00:00', 'COMP'),
('NTF-2026-003349', 'DET-EQ-00463', 'M3', 4, 'Scheduled PM: transducer inspection and solution change',          'Kwaku Mensah',    '2026-03-01 07:00:00', 'OSNO'),
-- DET-EQ-00464 (Vibration Monitor)
('NTF-2026-003350', 'DET-EQ-00464', 'M3', 4, 'Scheduled PM: sensor calibration verification',                    'Terry Whitlock',   '2026-01-05 07:00:00', 'COMP'),
('NTF-2026-003351', 'DET-EQ-00464', 'M2', 2, 'Sensor channel 3 drift — recalibrate against reference standard',  'Terry Whitlock',   '2026-02-16 10:00:00', 'COMP'),
('NTF-2026-003352', 'DET-EQ-00464', 'M3', 4, 'Scheduled PM: sensor calibration verification',                    'Terry Whitlock',   '2026-03-05 07:00:00', 'OSNO'),
-- Additional M1 breakdown notifications for narrative richness
('NTF-2026-003353', 'DET-EQ-00441', 'M2', 2, 'Coolant nozzle clogged — reduced flow to spindle',                 'Kwaku Mensah',    '2026-01-28 13:00:00', 'COMP'),
('NTF-2026-003354', 'DET-EQ-00443', 'M1', 1, 'Spindle overload fault — auto-stop triggered during cut',          'Diego Ramirez',    '2026-01-30 15:45:00', 'COMP'),
('NTF-2026-003355', 'DET-EQ-00445', 'M1', 1, 'Chuck pressure loss — machine halted, safety interlock engaged',   'Kwaku Mensah',    '2026-01-22 11:30:00', 'COMP'),
('NTF-2026-003356', 'DET-EQ-00457', 'M1', 2, 'Compressor high-temp shutdown — ambient temp exceeded limit',      'Terry Whitlock',   '2026-02-07 14:00:00', 'COMP'),
('NTF-2026-003357', 'DET-EQ-00460', 'M1', 2, 'Chiller low-flow alarm — coolant supply to CNC line interrupted',  'Terry Whitlock',   '2026-01-18 09:30:00', 'COMP'),
('NTF-2026-003358', 'DET-EQ-00452', 'M1', 2, 'Hydraulic pressure loss — press halted mid-cycle',                 'Diego Ramirez',    '2026-01-12 10:00:00', 'COMP'),
('NTF-2026-003359', 'DET-EQ-00448', 'M1', 2, 'Wire break during cut — auto-thread retry failed',                 'Terry Whitlock',   '2026-02-03 16:00:00', 'COMP'),
('NTF-2026-003360', 'DET-EQ-00453', 'M1', 2, 'Weld wire feed jam — robot paused, manual clear required',         'Kwaku Mensah',    '2026-02-11 08:30:00', 'COMP')
ON CONFLICT (notification_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS pm_orders (
    order_id        VARCHAR(14) PRIMARY KEY,
    notification_id VARCHAR(16) REFERENCES maintenance_notifications(notification_id),
    equipment_id    VARCHAR(15) REFERENCES equipment(equipment_id),
    order_type      VARCHAR(4) CHECK (order_type IN ('PM01','PM02','PM03')),  -- PM01=preventive, PM02=corrective, PM03=emergency
    status          VARCHAR(4) CHECK (status IN ('CRTD','REL','TECO','CLSD')),
    planned_start   TIMESTAMP,
    planned_end     TIMESTAMP,
    actual_start    TIMESTAMP,
    actual_end      TIMESTAMP,
    damage_code     VARCHAR(20),
    cause_code      VARCHAR(20),
    activity_type   VARCHAR(20)
);

INSERT INTO pm_orders VALUES
-- DET-EQ-00441 orders (spindle bearing failure narrative)
('PM-2026-004401', 'NTF-2026-003301', 'DET-EQ-00441', 'PM01', 'CLSD', '2026-01-15 06:00', '2026-01-15 14:00', '2026-01-15 06:30', '2026-01-15 13:45', NULL,       NULL,       'PREVENTIVE_MAINT'),
('PM-2026-004402', 'NTF-2026-003353', 'DET-EQ-00441', 'PM02', 'CLSD', '2026-01-28 12:00', '2026-01-28 16:00', '2026-01-28 13:15', '2026-01-28 15:30', 'CLOG',     'WEAR',     'CORRECTIVE_MAINT'),
('PM-2026-004403', 'NTF-2026-003302', 'DET-EQ-00441', 'PM02', 'CLSD', '2026-02-11 06:00', '2026-02-11 14:00', '2026-02-11 07:00', '2026-02-11 13:00', 'NOISE',    'WEAR',     'CORRECTIVE_MAINT'),
('PM-2026-004404', 'NTF-2026-003303', 'DET-EQ-00441', 'PM03', 'CLSD', '2026-02-14 23:00', '2026-02-15 18:00', '2026-02-14 23:30', '2026-02-15 17:45', 'BEARING',  'WEAR',     'EMERGENCY_REPAIR'),
('PM-2026-004405', 'NTF-2026-003304', 'DET-EQ-00441', 'PM02', 'CLSD', '2026-02-16 13:00', '2026-02-16 17:00', '2026-02-16 14:00', '2026-02-16 16:30', NULL,       NULL,       'INSPECTION'),
('PM-2026-004406', 'NTF-2026-003305', 'DET-EQ-00441', 'PM01', 'CRTD', '2026-03-01 06:00', '2026-03-01 10:00', NULL,               NULL,               NULL,       NULL,       'PREVENTIVE_MAINT'),
-- DET-EQ-00442
('PM-2026-004407', 'NTF-2026-003306', 'DET-EQ-00442', 'PM01', 'CLSD', '2026-01-15 06:00', '2026-01-15 14:00', '2026-01-15 06:30', '2026-01-15 13:30', NULL,       NULL,       'PREVENTIVE_MAINT'),
('PM-2026-004408', 'NTF-2026-003307', 'DET-EQ-00442', 'PM02', 'CLSD', '2026-02-06 06:00', '2026-02-06 12:00', '2026-02-06 07:00', '2026-02-06 11:30', 'WEAR',     'WEAR',     'CORRECTIVE_MAINT'),
('PM-2026-004409', 'NTF-2026-003308', 'DET-EQ-00442', 'PM01', 'CRTD', '2026-03-01 06:00', '2026-03-01 14:00', NULL,               NULL,               NULL,       NULL,       'PREVENTIVE_MAINT'),
-- DET-EQ-00443
('PM-2026-004410', 'NTF-2026-003354', 'DET-EQ-00443', 'PM03', 'CLSD', '2026-01-30 15:45', '2026-01-30 20:00', '2026-01-30 16:00', '2026-01-30 19:30', 'OVERLOAD', 'PROCESS',  'EMERGENCY_REPAIR'),
('PM-2026-004411', 'NTF-2026-003309', 'DET-EQ-00443', 'PM01', 'CLSD', '2026-01-20 06:00', '2026-01-20 12:00', '2026-01-20 06:30', '2026-01-20 11:45', NULL,       NULL,       'PREVENTIVE_MAINT'),
('PM-2026-004412', 'NTF-2026-003310', 'DET-EQ-00443', 'PM02', 'REL',  '2026-02-19 06:00', '2026-02-20 14:00', '2026-02-19 07:30', NULL,               'BACKLASH', 'WEAR',     'CORRECTIVE_MAINT'),
('PM-2026-004413', 'NTF-2026-003311', 'DET-EQ-00443', 'PM01', 'CRTD', '2026-03-05 06:00', '2026-03-05 10:00', NULL,               NULL,               NULL,       NULL,       'PREVENTIVE_MAINT'),
-- DET-EQ-00444 (INAC — servo fault)
('PM-2026-004414', 'NTF-2026-003312', 'DET-EQ-00444', 'PM03', 'REL',  '2026-02-20 15:00', '2026-02-22 18:00', '2026-02-20 15:30', NULL,               'ELEC',     'COMPONENT','EMERGENCY_REPAIR'),
('PM-2026-004415', 'NTF-2026-003313', 'DET-EQ-00444', 'PM02', 'CRTD', '2026-02-28 06:00', '2026-03-05 18:00', NULL,               NULL,               'ELEC',     'COMPONENT','CORRECTIVE_MAINT'),
-- DET-EQ-00445
('PM-2026-004416', 'NTF-2026-003355', 'DET-EQ-00445', 'PM03', 'CLSD', '2026-01-22 11:30', '2026-01-22 15:00', '2026-01-22 12:00', '2026-01-22 14:30', 'PRESSURE', 'WEAR',     'EMERGENCY_REPAIR'),
('PM-2026-004417', 'NTF-2026-003314', 'DET-EQ-00445', 'PM01', 'CLSD', '2026-01-25 06:00', '2026-01-25 12:00', '2026-01-25 06:30', '2026-01-25 11:30', NULL,       NULL,       'PREVENTIVE_MAINT'),
('PM-2026-004418', 'NTF-2026-003315', 'DET-EQ-00445', 'PM02', 'CLSD', '2026-02-13 06:00', '2026-02-13 12:00', '2026-02-13 07:00', '2026-02-13 11:00', 'LEAK',     'WEAR',     'CORRECTIVE_MAINT'),
('PM-2026-004419', 'NTF-2026-003316', 'DET-EQ-00445', 'PM01', 'CRTD', '2026-03-01 06:00', '2026-03-01 12:00', NULL,               NULL,               NULL,       NULL,       'PREVENTIVE_MAINT'),
-- DET-EQ-00446
('PM-2026-004420', 'NTF-2026-003317', 'DET-EQ-00446', 'PM01', 'CLSD', '2026-01-25 06:00', '2026-01-25 12:00', '2026-01-25 06:30', '2026-01-25 11:45', NULL,       NULL,       'PREVENTIVE_MAINT'),
('PM-2026-004421', 'NTF-2026-003318', 'DET-EQ-00446', 'PM01', 'CRTD', '2026-03-01 06:00', '2026-03-01 12:00', NULL,               NULL,               NULL,       NULL,       'PREVENTIVE_MAINT'),
-- DET-EQ-00447
('PM-2026-004422', 'NTF-2026-003319', 'DET-EQ-00447', 'PM01', 'CLSD', '2026-02-01 06:00', '2026-02-01 12:00', '2026-02-01 06:30', '2026-02-01 11:30', NULL,       NULL,       'PREVENTIVE_MAINT'),
('PM-2026-004423', 'NTF-2026-003320', 'DET-EQ-00447', 'PM02', 'CLSD', '2026-02-23 06:00', '2026-02-23 14:00', '2026-02-23 07:00', '2026-02-23 13:30', 'ALIGN',    'ADJUST',   'CORRECTIVE_MAINT'),
-- DET-EQ-00448 (EDM)
('PM-2026-004424', 'NTF-2026-003359', 'DET-EQ-00448', 'PM03', 'CLSD', '2026-02-03 16:00', '2026-02-03 20:00', '2026-02-03 16:30', '2026-02-03 19:00', 'WIRE',     'PROCESS',  'EMERGENCY_REPAIR'),
('PM-2026-004425', 'NTF-2026-003321', 'DET-EQ-00448', 'PM01', 'CLSD', '2026-01-28 06:00', '2026-01-28 14:00', '2026-01-28 06:30', '2026-01-28 13:45', NULL,       NULL,       'PREVENTIVE_MAINT'),
('PM-2026-004426', 'NTF-2026-003322', 'DET-EQ-00448', 'PM02', 'REL',  '2026-02-26 06:00', '2026-02-26 14:00', '2026-02-26 07:00', NULL,               'FLUID',    'CONTAM',   'CORRECTIVE_MAINT'),
-- DET-EQ-00449 (Grinder)
('PM-2026-004427', 'NTF-2026-003323', 'DET-EQ-00449', 'PM01', 'CLSD', '2026-02-05 06:00', '2026-02-05 12:00', '2026-02-05 06:30', '2026-02-05 11:30', NULL,       NULL,       'PREVENTIVE_MAINT'),
('PM-2026-004428', 'NTF-2026-003324', 'DET-EQ-00449', 'PM02', 'CRTD', '2026-03-01 06:00', '2026-03-01 10:00', NULL,               NULL,               'MAGNET',   'WEAR',     'CORRECTIVE_MAINT'),
-- DET-EQ-00450 (CMM-01)
('PM-2026-004429', 'NTF-2026-003325', 'DET-EQ-00450', 'PM01', 'CLSD', '2026-01-10 06:00', '2026-01-10 12:00', '2026-01-10 06:30', '2026-01-10 11:30', NULL,       NULL,       'CALIBRATION'),
('PM-2026-004430', 'NTF-2026-003326', 'DET-EQ-00450', 'PM01', 'CRTD', '2026-03-10 06:00', '2026-03-10 12:00', NULL,               NULL,               NULL,       NULL,       'CALIBRATION'),
-- DET-EQ-00451 (CMM-02)
('PM-2026-004431', 'NTF-2026-003327', 'DET-EQ-00451', 'PM01', 'CLSD', '2026-01-10 06:00', '2026-01-10 12:00', '2026-01-10 06:30', '2026-01-10 11:45', NULL,       NULL,       'CALIBRATION'),
('PM-2026-004432', 'NTF-2026-003328', 'DET-EQ-00451', 'PM02', 'CLSD', '2026-02-18 06:00', '2026-02-18 14:00', '2026-02-18 07:00', '2026-02-18 13:00', 'PRESSURE', 'WEAR',     'CORRECTIVE_MAINT'),
-- DET-EQ-00452 (Press)
('PM-2026-004433', 'NTF-2026-003358', 'DET-EQ-00452', 'PM03', 'CLSD', '2026-01-12 10:00', '2026-01-12 16:00', '2026-01-12 10:30', '2026-01-12 15:30', 'PRESSURE', 'WEAR',     'EMERGENCY_REPAIR'),
('PM-2026-004434', 'NTF-2026-003329', 'DET-EQ-00452', 'PM01', 'CLSD', '2026-01-20 06:00', '2026-01-20 14:00', '2026-01-20 06:30', '2026-01-20 13:30', NULL,       NULL,       'PREVENTIVE_MAINT'),
('PM-2026-004435', 'NTF-2026-003330', 'DET-EQ-00452', 'PM02', 'REL',  '2026-02-27 06:00', '2026-02-27 14:00', '2026-02-27 07:30', NULL,               'VALVE',    'WEAR',     'CORRECTIVE_MAINT'),
-- DET-EQ-00453 (Weld-01)
('PM-2026-004436', 'NTF-2026-003360', 'DET-EQ-00453', 'PM03', 'CLSD', '2026-02-11 08:30', '2026-02-11 12:00', '2026-02-11 09:00', '2026-02-11 11:30', 'JAM',      'CONTAM',   'EMERGENCY_REPAIR'),
('PM-2026-004437', 'NTF-2026-003331', 'DET-EQ-00453', 'PM01', 'CLSD', '2026-02-01 06:00', '2026-02-01 14:00', '2026-02-01 06:30', '2026-02-01 13:30', NULL,       NULL,       'PREVENTIVE_MAINT'),
('PM-2026-004438', 'NTF-2026-003332', 'DET-EQ-00453', 'PM02', 'CLSD', '2026-02-20 06:00', '2026-02-20 12:00', '2026-02-20 07:00', '2026-02-20 11:00', 'CONTAM',   'PROCESS',  'CORRECTIVE_MAINT'),
-- DET-EQ-00454 (Weld-02 — INAC)
('PM-2026-004439', 'NTF-2026-003333', 'DET-EQ-00454', 'PM03', 'REL',  '2026-02-23 16:00', '2026-02-25 18:00', '2026-02-23 16:30', NULL,               'ELEC',     'UNKNOWN',  'EMERGENCY_REPAIR'),
-- DET-EQ-00455 (Conveyor L4)
('PM-2026-004440', 'NTF-2026-003334', 'DET-EQ-00455', 'PM01', 'CLSD', '2026-02-10 06:00', '2026-02-10 12:00', '2026-02-10 06:30', '2026-02-10 11:30', NULL,       NULL,       'PREVENTIVE_MAINT'),
('PM-2026-004441', 'NTF-2026-003335', 'DET-EQ-00455', 'PM02', 'REL',  '2026-02-28 06:00', '2026-02-28 14:00', '2026-02-28 07:00', NULL,               'TEMP',     'WEAR',     'CORRECTIVE_MAINT'),
-- DET-EQ-00456 (Conveyor L5)
('PM-2026-004442', 'NTF-2026-003336', 'DET-EQ-00456', 'PM01', 'CLSD', '2026-02-10 06:00', '2026-02-10 12:00', '2026-02-10 06:30', '2026-02-10 11:45', NULL,       NULL,       'PREVENTIVE_MAINT'),
-- DET-EQ-00457 (Compressor-01)
('PM-2026-004443', 'NTF-2026-003356', 'DET-EQ-00457', 'PM03', 'CLSD', '2026-02-07 14:00', '2026-02-07 18:00', '2026-02-07 14:30', '2026-02-07 17:30', 'TEMP',     'ENVIRON',  'EMERGENCY_REPAIR'),
('PM-2026-004444', 'NTF-2026-003337', 'DET-EQ-00457', 'PM01', 'CLSD', '2026-01-15 06:00', '2026-01-15 12:00', '2026-01-15 06:30', '2026-01-15 11:30', NULL,       NULL,       'PREVENTIVE_MAINT'),
('PM-2026-004445', 'NTF-2026-003338', 'DET-EQ-00457', 'PM01', 'CRTD', '2026-03-15 06:00', '2026-03-15 12:00', NULL,               NULL,               NULL,       NULL,       'PREVENTIVE_MAINT'),
-- DET-EQ-00458 (Compressor-02)
('PM-2026-004446', 'NTF-2026-003339', 'DET-EQ-00458', 'PM01', 'CLSD', '2026-01-15 06:00', '2026-01-15 12:00', '2026-01-15 06:30', '2026-01-15 11:30', NULL,       NULL,       'PREVENTIVE_MAINT'),
('PM-2026-004447', 'NTF-2026-003340', 'DET-EQ-00458', 'PM02', 'CRTD', '2026-03-01 06:00', '2026-03-01 12:00', NULL,               NULL,               'VALVE',    'WEAR',     'CORRECTIVE_MAINT'),
-- DET-EQ-00459 (Coolant)
('PM-2026-004448', 'NTF-2026-003341', 'DET-EQ-00459', 'PM01', 'CLSD', '2026-02-01 06:00', '2026-02-01 10:00', '2026-02-01 06:30', '2026-02-01 09:30', NULL,       NULL,       'PREVENTIVE_MAINT'),
('PM-2026-004449', 'NTF-2026-003342', 'DET-EQ-00459', 'PM01', 'CRTD', '2026-03-01 06:00', '2026-03-01 10:00', NULL,               NULL,               NULL,       NULL,       'PREVENTIVE_MAINT'),
-- DET-EQ-00460 (Chiller)
('PM-2026-004450', 'NTF-2026-003357', 'DET-EQ-00460', 'PM03', 'CLSD', '2026-01-18 09:30', '2026-01-18 14:00', '2026-01-18 10:00', '2026-01-18 13:30', 'FLOW',     'WEAR',     'EMERGENCY_REPAIR'),
('PM-2026-004451', 'NTF-2026-003343', 'DET-EQ-00460', 'PM01', 'CLSD', '2026-01-20 06:00', '2026-01-20 14:00', '2026-01-20 06:30', '2026-01-20 13:30', NULL,       NULL,       'PREVENTIVE_MAINT'),
('PM-2026-004452', 'NTF-2026-003344', 'DET-EQ-00460', 'PM02', 'REL',  '2026-03-01 06:00', '2026-03-01 14:00', '2026-03-01 07:00', NULL,               'VIBRATION','UNBALANCE', 'CORRECTIVE_MAINT'),
-- DET-EQ-00461 (Crane)
('PM-2026-004453', 'NTF-2026-003345', 'DET-EQ-00461', 'PM01', 'CLSD', '2026-01-10 06:00', '2026-01-10 14:00', '2026-01-10 06:30', '2026-01-10 13:30', NULL,       NULL,       'PREVENTIVE_MAINT'),
('PM-2026-004454', 'NTF-2026-003346', 'DET-EQ-00461', 'PM01', 'CRTD', '2026-03-10 06:00', '2026-03-10 14:00', NULL,               NULL,               NULL,       NULL,       'PREVENTIVE_MAINT'),
-- DET-EQ-00462 (Laser)
('PM-2026-004455', 'NTF-2026-003347', 'DET-EQ-00462', 'PM01', 'CLSD', '2026-02-15 06:00', '2026-02-15 10:00', '2026-02-15 06:30', '2026-02-15 09:30', NULL,       NULL,       'PREVENTIVE_MAINT'),
-- DET-EQ-00463 (Ultrasonic)
('PM-2026-004456', 'NTF-2026-003348', 'DET-EQ-00463', 'PM01', 'CLSD', '2026-02-01 06:00', '2026-02-01 10:00', '2026-02-01 06:30', '2026-02-01 09:30', NULL,       NULL,       'PREVENTIVE_MAINT'),
('PM-2026-004457', 'NTF-2026-003349', 'DET-EQ-00463', 'PM01', 'CRTD', '2026-03-01 06:00', '2026-03-01 10:00', NULL,               NULL,               NULL,       NULL,       'PREVENTIVE_MAINT'),
-- DET-EQ-00464 (Vibration Monitor)
('PM-2026-004458', 'NTF-2026-003350', 'DET-EQ-00464', 'PM01', 'CLSD', '2026-01-05 06:00', '2026-01-05 10:00', '2026-01-05 06:30', '2026-01-05 09:30', NULL,       NULL,       'CALIBRATION'),
('PM-2026-004459', 'NTF-2026-003351', 'DET-EQ-00464', 'PM02', 'CLSD', '2026-02-16 09:00', '2026-02-16 13:00', '2026-02-16 10:00', '2026-02-16 12:30', 'DRIFT',    'CALIBRATION','CALIBRATION'),
('PM-2026-004460', 'NTF-2026-003352', 'DET-EQ-00464', 'PM01', 'CRTD', '2026-03-05 06:00', '2026-03-05 10:00', NULL,               NULL,               NULL,       NULL,       'CALIBRATION')
ON CONFLICT (order_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS sensor_readings (
    reading_id       SERIAL PRIMARY KEY,
    equipment_id     VARCHAR(15) REFERENCES equipment(equipment_id),
    measurement_point VARCHAR(30),
    reading_type     VARCHAR(12) CHECK (reading_type IN ('VIBRATION','TEMPERATURE','PRESSURE','CURRENT')),
    reading_value    NUMERIC(10,4),
    unit             VARCHAR(10),
    reading_time     TIMESTAMP,
    status           VARCHAR(6) CHECK (status IN ('NORMAL','ALERT','ALARM'))
);

-- ============================================================
-- SENSOR READINGS (400 rows)
-- DET-EQ-00441: vibration escalation leading to bearing failure (Feb 14)
-- then recovery after repair (Feb 15-16), then normal
-- ============================================================

-- DET-EQ-00441 VIBRATION — pre-failure escalation (Jan 15 – Feb 14), 4-hour intervals
INSERT INTO sensor_readings (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
SELECT s.* FROM (
SELECT
    'DET-EQ-00441',
    'DET-EQ-00441-MP-VIB',
    'VIBRATION',
    ROUND(CAST(
        CASE
            WHEN ts < '2026-02-10 00:00:00' THEN 1.1 + random() * 0.4          -- baseline normal
            WHEN ts < '2026-02-13 00:00:00' THEN 1.5 + random() * 0.4          -- slight rise
            WHEN ts < '2026-02-14 20:00:00' THEN 2.1 + random() * 0.6          -- ALERT zone
            ELSE                                  2.8 + random() * 0.8          -- ALARM / failure
        END AS NUMERIC), 4),
    'mm/s',
    ts,
    CASE
        WHEN ts < '2026-02-10 00:00:00' THEN 'NORMAL'
        WHEN ts < '2026-02-13 00:00:00' THEN 'NORMAL'
        WHEN ts < '2026-02-14 20:00:00' THEN 'ALERT'
        ELSE 'ALARM'
    END
FROM generate_series('2026-01-15 06:00:00'::timestamp, '2026-02-14 22:00:00'::timestamp, '4 hours'::interval) AS ts
) AS s (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
WHERE NOT EXISTS (SELECT 1 FROM sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_point = s.measurement_point AND t.reading_type = s.reading_type AND t.reading_time = s.reading_time);

-- DET-EQ-00441 VIBRATION — post-repair recovery (Feb 16 – Mar 1), 4-hour intervals
INSERT INTO sensor_readings (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
SELECT s.* FROM (
SELECT
    'DET-EQ-00441',
    'DET-EQ-00441-MP-VIB',
    'VIBRATION',
    ROUND(CAST(0.8 + random() * 0.4 AS NUMERIC), 4),
    'mm/s',
    ts,
    'NORMAL'
FROM generate_series('2026-02-16 06:00:00'::timestamp, '2026-03-01 06:00:00'::timestamp, '6 hours'::interval) AS ts
) AS s (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
WHERE NOT EXISTS (SELECT 1 FROM sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_point = s.measurement_point AND t.reading_type = s.reading_type AND t.reading_time = s.reading_time);

-- DET-EQ-00441 TEMPERATURE — spindle temp, 4-hour intervals Jan 15 – Mar 1
INSERT INTO sensor_readings (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
SELECT s.* FROM (
SELECT
    'DET-EQ-00441',
    'DET-EQ-00441-MP-TMP',
    'TEMPERATURE',
    ROUND(CAST(
        CASE
            WHEN ts BETWEEN '2026-02-14 18:00:00' AND '2026-02-14 23:00:00' THEN 88 + random() * 12
            ELSE 62 + random() * 10
        END AS NUMERIC), 4),
    'degC',
    ts,
    CASE
        WHEN ts BETWEEN '2026-02-14 18:00:00' AND '2026-02-14 23:00:00' THEN 'ALARM'
        ELSE 'NORMAL'
    END
FROM generate_series('2026-01-15 06:00:00'::timestamp, '2026-03-01 06:00:00'::timestamp, '4 hours'::interval) AS ts
) AS s (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
WHERE NOT EXISTS (SELECT 1 FROM sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_point = s.measurement_point AND t.reading_type = s.reading_type AND t.reading_time = s.reading_time);

-- DET-EQ-00441 CURRENT — spindle motor current, 4-hour intervals
INSERT INTO sensor_readings (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
SELECT s.* FROM (
SELECT
    'DET-EQ-00441',
    'DET-EQ-00441-MP-CUR',
    'CURRENT',
    ROUND(CAST(
        CASE
            WHEN ts BETWEEN '2026-02-13 00:00:00' AND '2026-02-14 23:00:00' THEN 28 + random() * 8
            ELSE 18 + random() * 6
        END AS NUMERIC), 4),
    'A',
    ts,
    CASE
        WHEN ts BETWEEN '2026-02-13 00:00:00' AND '2026-02-14 23:00:00' THEN 'ALERT'
        ELSE 'NORMAL'
    END
FROM generate_series('2026-01-15 06:00:00'::timestamp, '2026-03-01 06:00:00'::timestamp, '6 hours'::interval) AS ts
) AS s (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
WHERE NOT EXISTS (SELECT 1 FROM sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_point = s.measurement_point AND t.reading_type = s.reading_type AND t.reading_time = s.reading_time);

-- DET-EQ-00442 VIBRATION — normal throughout, 8-hour intervals
INSERT INTO sensor_readings (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
SELECT s.* FROM (
SELECT
    'DET-EQ-00442',
    'DET-EQ-00442-MP-VIB',
    'VIBRATION',
    ROUND(CAST(0.9 + random() * 0.5 AS NUMERIC), 4),
    'mm/s',
    ts,
    'NORMAL'
FROM generate_series('2026-01-15 06:00:00'::timestamp, '2026-03-01 06:00:00'::timestamp, '8 hours'::interval) AS ts
) AS s (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
WHERE NOT EXISTS (SELECT 1 FROM sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_point = s.measurement_point AND t.reading_type = s.reading_type AND t.reading_time = s.reading_time);

-- DET-EQ-00444 VIBRATION — normal then ALARM at servo fault (Feb 20)
INSERT INTO sensor_readings (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
SELECT s.* FROM (
SELECT
    'DET-EQ-00444',
    'DET-EQ-00444-MP-VIB',
    'VIBRATION',
    ROUND(CAST(
        CASE WHEN ts >= '2026-02-20 14:00:00' THEN 4.5 + random() * 2.0
             ELSE 1.0 + random() * 0.4
        END AS NUMERIC), 4),
    'mm/s',
    ts,
    CASE WHEN ts >= '2026-02-20 14:00:00' THEN 'ALARM' ELSE 'NORMAL' END
FROM generate_series('2026-01-20 06:00:00'::timestamp, '2026-02-23 06:00:00'::timestamp, '8 hours'::interval) AS ts
) AS s (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
WHERE NOT EXISTS (SELECT 1 FROM sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_point = s.measurement_point AND t.reading_type = s.reading_type AND t.reading_time = s.reading_time);

-- DET-EQ-00457 TEMPERATURE — compressor, high-temp event Feb 7
INSERT INTO sensor_readings (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
SELECT s.* FROM (
SELECT
    'DET-EQ-00457',
    'DET-EQ-00457-MP-TMP',
    'TEMPERATURE',
    ROUND(CAST(
        CASE WHEN ts BETWEEN '2026-02-07 12:00:00' AND '2026-02-07 18:00:00' THEN 105 + random() * 15
             ELSE 72 + random() * 8
        END AS NUMERIC), 4),
    'degC',
    ts,
    CASE WHEN ts BETWEEN '2026-02-07 12:00:00' AND '2026-02-07 18:00:00' THEN 'ALARM' ELSE 'NORMAL' END
FROM generate_series('2026-01-15 06:00:00'::timestamp, '2026-03-01 06:00:00'::timestamp, '6 hours'::interval) AS ts
) AS s (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
WHERE NOT EXISTS (SELECT 1 FROM sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_point = s.measurement_point AND t.reading_type = s.reading_type AND t.reading_time = s.reading_time);

-- DET-EQ-00457 PRESSURE — compressor outlet pressure, 6-hour intervals
INSERT INTO sensor_readings (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
SELECT s.* FROM (
SELECT
    'DET-EQ-00457',
    'DET-EQ-00457-MP-PRS',
    'PRESSURE',
    ROUND(CAST(6.8 + random() * 0.4 AS NUMERIC), 4),
    'bar',
    ts,
    'NORMAL'
FROM generate_series('2026-01-15 06:00:00'::timestamp, '2026-03-01 06:00:00'::timestamp, '6 hours'::interval) AS ts
) AS s (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
WHERE NOT EXISTS (SELECT 1 FROM sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_point = s.measurement_point AND t.reading_type = s.reading_type AND t.reading_time = s.reading_time);

-- DET-EQ-00460 TEMPERATURE — chiller, low-flow event Jan 18
INSERT INTO sensor_readings (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
SELECT s.* FROM (
SELECT
    'DET-EQ-00460',
    'DET-EQ-00460-MP-TMP',
    'TEMPERATURE',
    ROUND(CAST(
        CASE WHEN ts BETWEEN '2026-01-18 08:00:00' AND '2026-01-18 14:00:00' THEN 18 + random() * 4
             ELSE 7 + random() * 2
        END AS NUMERIC), 4),
    'degC',
    ts,
    CASE WHEN ts BETWEEN '2026-01-18 08:00:00' AND '2026-01-18 14:00:00' THEN 'ALERT' ELSE 'NORMAL' END
FROM generate_series('2026-01-15 06:00:00'::timestamp, '2026-03-01 06:00:00'::timestamp, '8 hours'::interval) AS ts
) AS s (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
WHERE NOT EXISTS (SELECT 1 FROM sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_point = s.measurement_point AND t.reading_type = s.reading_type AND t.reading_time = s.reading_time);

-- DET-EQ-00455 VIBRATION — conveyor motor bearing temp rise (Feb 27)
INSERT INTO sensor_readings (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
SELECT s.* FROM (
SELECT
    'DET-EQ-00455',
    'DET-EQ-00455-MP-VIB',
    'VIBRATION',
    ROUND(CAST(
        CASE WHEN ts >= '2026-02-25 00:00:00' THEN 1.8 + random() * 0.6
             ELSE 0.6 + random() * 0.3
        END AS NUMERIC), 4),
    'mm/s',
    ts,
    CASE WHEN ts >= '2026-02-25 00:00:00' THEN 'ALERT' ELSE 'NORMAL' END
FROM generate_series('2026-02-10 06:00:00'::timestamp, '2026-03-01 06:00:00'::timestamp, '8 hours'::interval) AS ts
) AS s (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
WHERE NOT EXISTS (SELECT 1 FROM sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_point = s.measurement_point AND t.reading_type = s.reading_type AND t.reading_time = s.reading_time);

-- DET-EQ-00452 PRESSURE — hydraulic press, pressure loss event Jan 12
INSERT INTO sensor_readings (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
SELECT s.* FROM (
SELECT
    'DET-EQ-00452',
    'DET-EQ-00452-MP-PRS',
    'PRESSURE',
    ROUND(CAST(
        CASE WHEN ts BETWEEN '2026-01-12 09:00:00' AND '2026-01-12 12:00:00' THEN 80 + random() * 20
             ELSE 185 + random() * 10
        END AS NUMERIC), 4),
    'bar',
    ts,
    CASE WHEN ts BETWEEN '2026-01-12 09:00:00' AND '2026-01-12 12:00:00' THEN 'ALARM' ELSE 'NORMAL' END
FROM generate_series('2026-01-10 06:00:00'::timestamp, '2026-03-01 06:00:00'::timestamp, '8 hours'::interval) AS ts
) AS s (equipment_id, measurement_point, reading_type, reading_value, unit, reading_time, status)
WHERE NOT EXISTS (SELECT 1 FROM sensor_readings t WHERE t.equipment_id = s.equipment_id AND t.measurement_point = s.measurement_point AND t.reading_type = s.reading_type AND t.reading_time = s.reading_time);
