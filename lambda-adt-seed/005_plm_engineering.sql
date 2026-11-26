-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- PLM Engineering: conventions modeled after Siemens Teamcenter
-- Third-party product names below describe the naming-convention style only (nominative use); not affiliated/endorsed. See repo NOTICE.
-- Item IDs in PN-XXXX-REV-X format, lifecycle states, owning groups
SET search_path TO plm_engineering;

-- Re-runnable on an existing schema WITHOUT data loss (the seed custom
-- resource re-runs every file when the SQL changes): IF NOT EXISTS DDL and
-- idempotent INSERTs, never DROP TABLE. A column change needs its own
-- ALTER TABLE ... IF [NOT] EXISTS migration here.

CREATE TABLE IF NOT EXISTS products (
    item_id VARCHAR(30) PRIMARY KEY,
    item_name VARCHAR(100) NOT NULL,
    description TEXT,
    item_type VARCHAR(30),
    revision VARCHAR(5),
    lifecycle_state VARCHAR(20),
    owning_group VARCHAR(30),
    release_date DATE
);

CREATE TABLE IF NOT EXISTS specifications (
    spec_id SERIAL PRIMARY KEY,
    item_id VARCHAR(30) REFERENCES products(item_id),
    parameter VARCHAR(20),
    nominal NUMERIC(10,4),
    tolerance_min NUMERIC(10,4),
    tolerance_max NUMERIC(10,4),
    unit VARCHAR(10),
    spec_class VARCHAR(10) CHECK (spec_class IN ('CRITICAL','MAJOR','MINOR')),
    inspection_method VARCHAR(40),
    revision VARCHAR(5)
);

CREATE TABLE IF NOT EXISTS bom_items (
    bom_id SERIAL PRIMARY KEY,
    parent_item VARCHAR(30) REFERENCES products(item_id),
    child_item VARCHAR(30),
    find_number INTEGER,
    quantity_per NUMERIC(8,3),
    unit VARCHAR(10),
    material_spec VARCHAR(30),
    approved_vendors TEXT,
    effectivity_date DATE
);

INSERT INTO products VALUES
-- Precision Shaft family
('PN-7742-REV-C','Precision Shaft','High-tolerance drive shaft for CMX-441 assembly','Part','C','Released','Drivetrain-Eng','2024-03-15'),
('PN-7742-REV-B','Precision Shaft','Drive shaft rev B - superseded','Part','B','Obsolete','Drivetrain-Eng','2023-01-10'),
('PN-7742-REV-A','Precision Shaft','Drive shaft rev A - superseded','Part','A','Obsolete','Drivetrain-Eng','2021-06-01'),
-- Drive Coupling family
('PN-8810-REV-B','Drive Coupling','Flexible coupling for shaft-to-gearbox interface','Part','B','Released','Drivetrain-Eng','2024-05-20'),
('PN-8810-REV-A','Drive Coupling','Drive coupling rev A - superseded','Part','A','Obsolete','Drivetrain-Eng','2022-09-14'),
-- Aerospace Bracket family
('PN-9901-REV-A','Aerospace Bracket','Structural mounting bracket, 7075-T6 aluminum','Part','A','Released','Structures-Eng','2024-07-01'),
-- Sub-components
('PN-7743-REV-A','Shaft Bearing Race','Inner race for PN-7742 bearing seat','Part','A','Released','Drivetrain-Eng','2024-02-01'),
('PN-7744-REV-A','Retaining Ring','External retaining ring, 35mm','Part','A','Released','Fasteners-Eng','2023-11-05'),
('PN-8811-REV-A','Coupling Insert','Polyurethane flex insert for PN-8810','Part','A','Released','Drivetrain-Eng','2024-04-10'),
('PN-8812-REV-A','Coupling Hub A','Drive-side hub for PN-8810','Part','A','Released','Drivetrain-Eng','2024-04-10'),
('PN-8813-REV-A','Coupling Hub B','Load-side hub for PN-8810','Part','A','Released','Drivetrain-Eng','2024-04-10'),
('PN-9902-REV-A','Bracket Gusset','Reinforcement gusset for PN-9901','Part','A','Released','Structures-Eng','2024-06-15'),
('PN-9903-REV-A','Bracket Bushing','Flanged bushing, press-fit','Part','A','Released','Structures-Eng','2024-06-15'),
-- Assemblies
('PN-CMX441-REV-C','CMX-441 Shaft Assembly','Top-level shaft assembly for Line 3','Assembly','C','Released','Systems-Eng','2024-08-01'),
('PN-CMX441-REV-B','CMX-441 Shaft Assembly','Shaft assembly rev B - superseded','Assembly','B','Obsolete','Systems-Eng','2023-03-01'),
('PN-DRV-001-REV-A','Drive Module Assembly','Integrated drive module with coupling','Assembly','A','Released','Systems-Eng','2024-09-01'),
-- Raw stock / material items
('PN-RAW-7075-REV-A','7075-T6 Bar Stock','Aluminum bar stock 40mm dia','Raw Material','A','Released','Materials-Eng','2023-01-01'),
('PN-RAW-4140-REV-A','4140 Steel Bar','Alloy steel bar stock 50mm dia','Raw Material','A','Released','Materials-Eng','2023-01-01'),
-- In-work items
('PN-7745-REV-A','Shaft End Cap','Protective end cap for PN-7742 shipping','Part','A','In Work','Drivetrain-Eng','2025-01-15'),
('PN-9904-REV-A','Bracket Stiffener','Additional stiffener - under review','Part','A','In Review','Structures-Eng','2025-02-10')
ON CONFLICT (item_id) DO NOTHING;

INSERT INTO specifications (item_id, parameter, nominal, tolerance_min, tolerance_max, unit, spec_class, inspection_method, revision)
SELECT v.item_id, v.parameter, v.nominal::NUMERIC(10,4), v.tolerance_min::NUMERIC(10,4), v.tolerance_max::NUMERIC(10,4), v.unit, v.spec_class, v.inspection_method, v.revision
FROM (VALUES
-- PN-7742-REV-C: Precision Shaft (primary focus, most specs)
('PN-7742-REV-C','OD_TOL',    35.0000, 34.9900, 35.0050, 'mm',  'CRITICAL','CMM',                  'C'),
('PN-7742-REV-C','BORE_DIA',  20.0000, 19.9950, 20.0050, 'mm',  'CRITICAL','CMM',                  'C'),
('PN-7742-REV-C','CONC',       0.0000,  0.0000,  0.0100, 'mm',  'CRITICAL','CMM Runout',           'C'),
('PN-7742-REV-C','SURF_RA',    0.8000,  0.0000,  1.0000, 'µm',  'CRITICAL','Profilometer',         'C'),
('PN-7742-REV-C','LEN_TOL',  250.0000,249.9500,250.0500, 'mm',  'MAJOR',  'Caliper',               'C'),
-- 'Rockwell Hardness': ISO 6508 hardness scale (HRC/HRB), a metallurgical test method — not a vendor reference.
('PN-7742-REV-C','HRC',       58.0000, 55.0000, 62.0000, 'HRC', 'CRITICAL','Rockwell Hardness',    'C'),
('PN-7742-REV-C','WALL_THK',   7.5000,  7.4000,  7.6000, 'mm',  'MAJOR',  'UT Thickness',          'C'),
('PN-7742-REV-C','OD_TOL',    35.0000, 34.9850, 35.0050, 'mm',  'CRITICAL','CMM',                  'B'),
('PN-7742-REV-C','BORE_DIA',  20.0000, 19.9900, 20.0100, 'mm',  'CRITICAL','CMM',                  'B'),
('PN-7742-REV-C','CONC',       0.0000,  0.0000,  0.0150, 'mm',  'CRITICAL','CMM Runout',           'B'),
('PN-7742-REV-C','SURF_RA',    0.8000,  0.0000,  1.2000, 'µm',  'MAJOR',  'Profilometer',          'B'),
('PN-7742-REV-C','LEN_TOL',  250.0000,249.9000,250.1000, 'mm',  'MAJOR',  'Caliper',               'B'),
('PN-7742-REV-C','HRC',       58.0000, 54.0000, 62.0000, 'HRC', 'CRITICAL','Rockwell Hardness',    'B'),
('PN-7742-REV-C','WALL_THK',   7.5000,  7.3000,  7.7000, 'mm',  'MAJOR',  'UT Thickness',          'B'),
-- PN-7742-REV-A specs (original, looser tolerances)
('PN-7742-REV-A','OD_TOL',    35.0000, 34.9750, 35.0250, 'mm',  'CRITICAL','CMM',                  'A'),
('PN-7742-REV-A','BORE_DIA',  20.0000, 19.9800, 20.0200, 'mm',  'CRITICAL','CMM',                  'A'),
('PN-7742-REV-A','CONC',       0.0000,  0.0000,  0.0250, 'mm',  'MAJOR',  'CMM Runout',            'A'),
('PN-7742-REV-A','SURF_RA',    1.6000,  0.0000,  2.0000, 'µm',  'MAJOR',  'Profilometer',          'A'),
('PN-7742-REV-A','LEN_TOL',  250.0000,249.8000,250.2000, 'mm',  'MINOR',  'Caliper',               'A'),
('PN-7742-REV-A','HRC',       56.0000, 52.0000, 60.0000, 'HRC', 'CRITICAL','Rockwell Hardness',    'A'),
-- PN-7743-REV-A: Shaft Bearing Race
('PN-7743-REV-A','OD_TOL',    52.0000, 51.9900, 52.0000, 'mm',  'CRITICAL','CMM',                  'A'),
('PN-7743-REV-A','BORE_DIA',  35.0000, 35.0000, 35.0100, 'mm',  'CRITICAL','CMM',                  'A'),
('PN-7743-REV-A','SURF_RA',    0.4000,  0.0000,  0.5000, 'µm',  'CRITICAL','Profilometer',         'A'),
('PN-7743-REV-A','CONC',       0.0000,  0.0000,  0.0050, 'mm',  'CRITICAL','CMM Runout',           'A'),
('PN-7743-REV-A','HRC',       62.0000, 60.0000, 65.0000, 'HRC', 'CRITICAL','Rockwell Hardness',    'A'),
-- PN-7744-REV-A: Retaining Ring
('PN-7744-REV-A','OD_TOL',    38.0000, 37.9500, 38.0500, 'mm',  'MAJOR',  'Caliper',               'A'),
('PN-7744-REV-A','WALL_THK',   1.5000,  1.4500,  1.5500, 'mm',  'MAJOR',  'Micrometer',            'A'),
('PN-7744-REV-A','HRC',       44.0000, 40.0000, 48.0000, 'HRC', 'MAJOR',  'Rockwell Hardness',     'A'),
-- PN-8810-REV-B: Drive Coupling
('PN-8810-REV-B','OD_TOL',    80.0000, 79.9500, 80.0500, 'mm',  'MAJOR',  'Caliper',               'B'),
('PN-8810-REV-B','BORE_DIA',  25.0000, 24.9900, 25.0100, 'mm',  'CRITICAL','CMM',                  'B'),
('PN-8810-REV-B','CONC',       0.0000,  0.0000,  0.0200, 'mm',  'MAJOR',  'CMM Runout',            'B'),
('PN-8810-REV-B','LEN_TOL',  120.0000,119.9000,120.1000, 'mm',  'MINOR',  'Caliper',               'B'),
('PN-8810-REV-B','SURF_RA',    1.6000,  0.0000,  2.0000, 'µm',  'MINOR',  'Profilometer',          'B'),
('PN-8810-REV-B','WALL_THK',  10.0000,  9.8000, 10.2000, 'mm',  'MAJOR',  'UT Thickness',          'B'),
-- PN-8810-REV-A specs
('PN-8810-REV-A','OD_TOL',    80.0000, 79.9000, 80.1000, 'mm',  'MAJOR',  'Caliper',               'A'),
('PN-8810-REV-A','BORE_DIA',  25.0000, 24.9800, 25.0200, 'mm',  'CRITICAL','CMM',                  'A'),
('PN-8810-REV-A','CONC',       0.0000,  0.0000,  0.0300, 'mm',  'MAJOR',  'CMM Runout',            'A'),
('PN-8810-REV-A','LEN_TOL',  120.0000,119.8000,120.2000, 'mm',  'MINOR',  'Caliper',               'A'),
-- PN-8812-REV-A: Coupling Hub A
('PN-8812-REV-A','BORE_DIA',  25.0000, 24.9900, 25.0050, 'mm',  'CRITICAL','CMM',                  'A'),
('PN-8812-REV-A','OD_TOL',    60.0000, 59.9500, 60.0500, 'mm',  'MAJOR',  'Caliper',               'A'),
('PN-8812-REV-A','CONC',       0.0000,  0.0000,  0.0150, 'mm',  'MAJOR',  'CMM Runout',            'A'),
('PN-8812-REV-A','SURF_RA',    1.6000,  0.0000,  2.0000, 'µm',  'MINOR',  'Profilometer',          'A'),
-- PN-8813-REV-A: Coupling Hub B
('PN-8813-REV-A','BORE_DIA',  25.0000, 24.9900, 25.0050, 'mm',  'CRITICAL','CMM',                  'A'),
('PN-8813-REV-A','OD_TOL',    60.0000, 59.9500, 60.0500, 'mm',  'MAJOR',  'Caliper',               'A'),
('PN-8813-REV-A','CONC',       0.0000,  0.0000,  0.0150, 'mm',  'MAJOR',  'CMM Runout',            'A'),
('PN-8813-REV-A','SURF_RA',    1.6000,  0.0000,  2.0000, 'µm',  'MINOR',  'Profilometer',          'A'),
-- PN-9901-REV-A: Aerospace Bracket
('PN-9901-REV-A','LEN_TOL',  300.0000,299.8000,300.2000, 'mm',  'MAJOR',  'CMM',                   'A'),
('PN-9901-REV-A','WALL_THK',   6.0000,  5.9000,  6.1000, 'mm',  'CRITICAL','UT Thickness',         'A'),
('PN-9901-REV-A','BORE_DIA',  12.0000, 11.9800, 12.0200, 'mm',  'MAJOR',  'CMM',                   'A'),
('PN-9901-REV-A','SURF_RA',    3.2000,  0.0000,  4.0000, 'µm',  'MINOR',  'Profilometer',          'A'),
('PN-9901-REV-A','CONC',       0.0000,  0.0000,  0.0500, 'mm',  'MINOR',  'CMM Runout',            'A'),
-- PN-9902-REV-A: Bracket Gusset
('PN-9902-REV-A','WALL_THK',   4.0000,  3.9000,  4.1000, 'mm',  'MAJOR',  'UT Thickness',          'A'),
('PN-9902-REV-A','LEN_TOL',  150.0000,149.8000,150.2000, 'mm',  'MINOR',  'Caliper',               'A'),
('PN-9902-REV-A','SURF_RA',    3.2000,  0.0000,  4.0000, 'µm',  'MINOR',  'Profilometer',          'A'),
-- PN-9903-REV-A: Bracket Bushing
('PN-9903-REV-A','OD_TOL',    18.0000, 17.9900, 18.0000, 'mm',  'CRITICAL','CMM',                  'A'),
('PN-9903-REV-A','BORE_DIA',  12.0000, 12.0000, 12.0100, 'mm',  'CRITICAL','CMM',                  'A'),
('PN-9903-REV-A','SURF_RA',    0.8000,  0.0000,  1.0000, 'µm',  'MAJOR',  'Profilometer',          'A'),
('PN-9903-REV-A','LEN_TOL',   20.0000, 19.9500, 20.0500, 'mm',  'MINOR',  'Caliper',               'A'),
-- PN-CMX441-REV-C: Assembly-level specs
('PN-CMX441-REV-C','CONC',     0.0000,  0.0000,  0.0200, 'mm',  'CRITICAL','CMM Runout',           'C'),
('PN-CMX441-REV-C','LEN_TOL',400.0000,399.8000,400.2000, 'mm',  'MAJOR',  'CMM',                   'C'),
('PN-CMX441-REV-C','SURF_RA',  0.8000,  0.0000,  1.0000, 'µm',  'CRITICAL','Profilometer',         'C'),
-- PN-CMX441-REV-B: Assembly rev B specs
('PN-CMX441-REV-B','CONC',     0.0000,  0.0000,  0.0300, 'mm',  'CRITICAL','CMM Runout',           'B'),
('PN-CMX441-REV-B','LEN_TOL',400.0000,399.7000,400.3000, 'mm',  'MAJOR',  'CMM',                   'B'),
-- PN-DRV-001-REV-A: Drive Module Assembly
('PN-DRV-001-REV-A','CONC',    0.0000,  0.0000,  0.0300, 'mm',  'CRITICAL','CMM Runout',           'A'),
('PN-DRV-001-REV-A','LEN_TOL',520.0000,519.7000,520.3000,'mm',  'MAJOR',  'CMM',                   'A'),
('PN-DRV-001-REV-A','SURF_RA', 1.6000,  0.0000,  2.0000, 'µm',  'MINOR',  'Profilometer',          'A'),
-- PN-RAW-7075-REV-A: Material cert specs
('PN-RAW-7075-REV-A','OD_TOL', 40.0000, 39.9000, 40.1000,'mm',  'MINOR',  'Caliper',               'A'),
('PN-RAW-7075-REV-A','HRC',    87.0000, 85.0000, 92.0000,'HRB', 'MAJOR',  'Rockwell Hardness',     'A'),
-- PN-RAW-4140-REV-A: Material cert specs
('PN-RAW-4140-REV-A','OD_TOL', 50.0000, 49.9000, 50.1000,'mm',  'MINOR',  'Caliper',               'A'),
('PN-RAW-4140-REV-A','HRC',    28.0000, 26.0000, 34.0000,'HRC', 'MAJOR',  'Rockwell Hardness',     'A'),
-- PN-7745-REV-A: Shaft End Cap (in-work, preliminary specs)
('PN-7745-REV-A','OD_TOL',    36.0000, 35.9500, 36.0500, 'mm',  'MINOR',  'Caliper',               'A'),
('PN-7745-REV-A','LEN_TOL',   15.0000, 14.8000, 15.2000, 'mm',  'MINOR',  'Caliper',               'A'),
-- PN-9904-REV-A: Bracket Stiffener (in-review)
('PN-9904-REV-A','WALL_THK',   3.0000,  2.9000,  3.1000, 'mm',  'MAJOR',  'UT Thickness',          'A'),
('PN-9904-REV-A','LEN_TOL',  200.0000,199.7000,200.3000, 'mm',  'MINOR',  'Caliper',               'A'),
-- PN-8811-REV-A: Coupling Insert
('PN-8811-REV-A','OD_TOL',    55.0000, 54.8000, 55.2000, 'mm',  'MAJOR',  'Caliper',               'A'),
('PN-8811-REV-A','BORE_DIA',  30.0000, 29.9500, 30.0500, 'mm',  'MAJOR',  'Caliper',               'A'),
('PN-8811-REV-A','WALL_THK',  12.5000, 12.2000, 12.8000, 'mm',  'MINOR',  'Caliper',               'A'),
-- Additional PN-7742-REV-C specs to reach 100 total
('PN-7742-REV-C','OD_TOL',    34.0000, 33.9900, 34.0050, 'mm',  'CRITICAL','CMM',                  'C'),
('PN-7742-REV-C','BORE_DIA',  18.0000, 17.9950, 18.0050, 'mm',  'CRITICAL','CMM',                  'C'),
('PN-7742-REV-C','SURF_RA',    0.4000,  0.0000,  0.6000, 'µm',  'CRITICAL','Profilometer',         'C'),
('PN-7742-REV-C','CONC',       0.0000,  0.0000,  0.0080, 'mm',  'CRITICAL','CMM Runout',           'C'),
('PN-7742-REV-C','LEN_TOL',  125.0000,124.9500,125.0500, 'mm',  'MAJOR',  'Caliper',               'C'),
('PN-7742-REV-C','HRC',       60.0000, 58.0000, 63.0000, 'HRC', 'CRITICAL','Rockwell Hardness',    'C'),
('PN-7742-REV-C','WALL_THK',   5.0000,  4.9000,  5.1000, 'mm',  'MAJOR',  'UT Thickness',          'C'),
('PN-9901-REV-A','HRC',       87.0000, 85.0000, 92.0000, 'HRB', 'MAJOR',  'Rockwell Hardness',     'A'),
('PN-9901-REV-A','WALL_THK',   8.0000,  7.8000,  8.2000, 'mm',  'MAJOR',  'UT Thickness',          'A'),
('PN-8810-REV-B','HRC',       28.0000, 26.0000, 34.0000, 'HRC', 'MAJOR',  'Rockwell Hardness',     'B')
) AS v (item_id, parameter, nominal, tolerance_min, tolerance_max, unit, spec_class, inspection_method, revision)
WHERE NOT EXISTS (SELECT 1 FROM specifications t WHERE ROW(t.item_id, t.parameter, t.nominal, t.tolerance_min, t.tolerance_max, t.unit, t.spec_class, t.inspection_method, t.revision) IS NOT DISTINCT FROM ROW(v.item_id, v.parameter, v.nominal::NUMERIC(10,4), v.tolerance_min::NUMERIC(10,4), v.tolerance_max::NUMERIC(10,4), v.unit, v.spec_class, v.inspection_method, v.revision));

INSERT INTO bom_items (parent_item, child_item, find_number, quantity_per, unit, material_spec, approved_vendors, effectivity_date)
SELECT v.parent_item, v.child_item, v.find_number::INTEGER, v.quantity_per::NUMERIC(8,3), v.unit, v.material_spec, v.approved_vendors, v.effectivity_date::DATE
FROM (VALUES
-- PN-CMX441-REV-C BOM (top-level shaft assembly)
('PN-CMX441-REV-C','PN-7742-REV-C', 10, 1.000,'EA','ASTM-A108-1045',  'AnyCompany Parts Inc,AnyCompany Machining',       '2024-08-01'),
('PN-CMX441-REV-C','PN-7743-REV-A', 20, 2.000,'EA','ASTM-A295-52100', 'AnyCompany Bearings,Example Corp Bearings,Example Corp Bearing Supply',                           '2024-08-01'),
('PN-CMX441-REV-C','PN-7744-REV-A', 30, 2.000,'EA','ASTM-A228',       'AnyCompany Retaining Rings,Example Corp Retaining Rings',                       '2024-08-01'),
('PN-CMX441-REV-C','PN-7742-REV-C', 40, 1.000,'EA','ASTM-A108-1045',  'AnyCompany Parts Inc',                      '2024-08-01'),
('PN-CMX441-REV-C','PN-RAW-4140-REV-A',50,0.850,'KG','ASTM-A322-4140','AnyCompany Metals,Example Corp Service Center A',              '2024-08-01'),
-- PN-CMX441-REV-B BOM (superseded)
('PN-CMX441-REV-B','PN-7742-REV-B', 10, 1.000,'EA','ASTM-A108-1045',  'AnyCompany Parts Inc',                      '2023-03-01'),
('PN-CMX441-REV-B','PN-7743-REV-A', 20, 2.000,'EA','ASTM-A295-52100', 'AnyCompany Bearings,Example Corp Bearings',                               '2023-03-01'),
('PN-CMX441-REV-B','PN-7744-REV-A', 30, 2.000,'EA','ASTM-A228',       'AnyCompany Retaining Rings',                                  '2023-03-01'),
-- PN-DRV-001-REV-A BOM (drive module assembly)
('PN-DRV-001-REV-A','PN-CMX441-REV-C',10,1.000,'EA','N/A',            'Internal',                                 '2024-09-01'),
('PN-DRV-001-REV-A','PN-8810-REV-B',  20,1.000,'EA','N/A',            'AnyCompany Couplings,Example Corp Couplings',                          '2024-09-01'),
('PN-DRV-001-REV-A','PN-8812-REV-A',  30,1.000,'EA','ASTM-B211-7075-T6','AnyCompany Parts Inc,AnyCompany Machining',     '2024-09-01'),
('PN-DRV-001-REV-A','PN-8813-REV-A',  40,1.000,'EA','ASTM-B211-7075-T6','AnyCompany Parts Inc,AnyCompany Machining',     '2024-09-01'),
('PN-DRV-001-REV-A','PN-RAW-4140-REV-A',50,1.200,'KG','ASTM-A322-4140','AnyCompany Metals',                             '2024-09-01'),
-- PN-8810-REV-B BOM (drive coupling)
('PN-8810-REV-B','PN-8811-REV-A', 10, 1.000,'EA','SHORE-A-98-PU',    'Example Corp Couplings,AnyCompany Coupling Supply',                           '2024-05-20'),
('PN-8810-REV-B','PN-8812-REV-A', 20, 1.000,'EA','ASTM-B211-7075-T6','AnyCompany Parts Inc',                      '2024-05-20'),
('PN-8810-REV-B','PN-8813-REV-A', 30, 1.000,'EA','ASTM-B211-7075-T6','AnyCompany Parts Inc',                      '2024-05-20'),
-- PN-8810-REV-A BOM (superseded coupling)
('PN-8810-REV-A','PN-8811-REV-A', 10, 1.000,'EA','SHORE-A-98-PU',    'Example Corp Couplings',                                  '2022-09-14'),
('PN-8810-REV-A','PN-8812-REV-A', 20, 1.000,'EA','ASTM-B211-7075-T6','AnyCompany Parts Inc',                      '2022-09-14'),
('PN-8810-REV-A','PN-8813-REV-A', 30, 1.000,'EA','ASTM-B211-7075-T6','AnyCompany Parts Inc',                      '2022-09-14'),
-- PN-9901-REV-A BOM (aerospace bracket)
('PN-9901-REV-A','PN-9902-REV-A', 10, 2.000,'EA','ASTM-B211-7075-T6','AnyCompany Machining,AnyCompany Aerospace',            '2024-07-01'),
('PN-9901-REV-A','PN-9903-REV-A', 20, 4.000,'EA','ASTM-B211-7075-T6','AnyCompany Machining',                           '2024-07-01'),
('PN-9901-REV-A','PN-RAW-7075-REV-A',30,1.500,'KG','ASTM-B211-7075-T6','AnyCompany Metals,Example Corp Metals',                   '2024-07-01'),
-- PN-7742-REV-C sub-BOM (raw material traceability)
('PN-7742-REV-C','PN-RAW-4140-REV-A',10,0.750,'KG','ASTM-A322-4140', 'AnyCompany Metals,Example Corp Service Center A',              '2024-03-15'),
('PN-7742-REV-C','PN-7743-REV-A',    20,1.000,'EA','ASTM-A295-52100','AnyCompany Bearings,Example Corp Bearings',                               '2024-03-15'),
('PN-7742-REV-C','PN-7744-REV-A',    30,1.000,'EA','ASTM-A228',      'AnyCompany Retaining Rings,Example Corp Retaining Rings',                       '2024-03-15'),
-- PN-7742-REV-B sub-BOM
('PN-7742-REV-B','PN-RAW-4140-REV-A',10,0.750,'KG','ASTM-A322-4140', 'AnyCompany Metals',                               '2023-01-10'),
('PN-7742-REV-B','PN-7743-REV-A',    20,1.000,'EA','ASTM-A295-52100','AnyCompany Bearings',                                      '2023-01-10'),
('PN-7742-REV-B','PN-7744-REV-A',    30,1.000,'EA','ASTM-A228',      'AnyCompany Retaining Rings',                                  '2023-01-10'),
-- PN-7742-REV-A sub-BOM
('PN-7742-REV-A','PN-RAW-4140-REV-A',10,0.800,'KG','ASTM-A322-4140', 'AnyCompany Metals',                               '2021-06-01'),
('PN-7742-REV-A','PN-7743-REV-A',    20,1.000,'EA','ASTM-A295-52100','AnyCompany Bearings',                                      '2021-06-01'),
('PN-7742-REV-A','PN-7744-REV-A',    30,1.000,'EA','ASTM-A228',      'AnyCompany Retaining Rings',                                  '2021-06-01'),
-- PN-7743-REV-A sub-BOM (bearing race raw material)
('PN-7743-REV-A','PN-RAW-4140-REV-A',10,0.200,'KG','ASTM-A295-52100','AnyCompany Metals,Example Corp Metals',                     '2024-02-01'),
-- PN-8812-REV-A sub-BOM
('PN-8812-REV-A','PN-RAW-7075-REV-A',10,0.400,'KG','ASTM-B211-7075-T6','AnyCompany Metals,Example Corp Metals',                   '2024-04-10'),
-- PN-8813-REV-A sub-BOM
('PN-8813-REV-A','PN-RAW-7075-REV-A',10,0.400,'KG','ASTM-B211-7075-T6','AnyCompany Metals,Example Corp Metals',                   '2024-04-10'),
-- PN-9902-REV-A sub-BOM
('PN-9902-REV-A','PN-RAW-7075-REV-A',10,0.300,'KG','ASTM-B211-7075-T6','AnyCompany Metals',                             '2024-06-15'),
-- PN-9903-REV-A sub-BOM
('PN-9903-REV-A','PN-RAW-7075-REV-A',10,0.050,'KG','ASTM-B211-7075-T6','AnyCompany Metals',                             '2024-06-15'),
-- Additional CMX441-REV-C BOM rows (fasteners, seals, etc.)
('PN-CMX441-REV-C','PN-7744-REV-A',  60, 4.000,'EA','ASTM-A228',      'AnyCompany Retaining Rings,Example Corp Retaining Rings',                      '2024-08-01'),
('PN-CMX441-REV-C','PN-9903-REV-A',  70, 2.000,'EA','ASTM-B211-7075-T6','AnyCompany Machining',                        '2024-08-01'),
('PN-CMX441-REV-C','PN-8811-REV-A',  80, 1.000,'EA','SHORE-A-98-PU',  'Example Corp Couplings,AnyCompany Coupling Supply',                          '2024-08-01'),
-- PN-DRV-001-REV-A additional BOM rows
('PN-DRV-001-REV-A','PN-7744-REV-A', 60, 4.000,'EA','ASTM-A228',      'AnyCompany Retaining Rings',                                 '2024-09-01'),
('PN-DRV-001-REV-A','PN-9903-REV-A', 70, 4.000,'EA','ASTM-B211-7075-T6','AnyCompany Machining',                        '2024-09-01'),
('PN-DRV-001-REV-A','PN-9902-REV-A', 80, 2.000,'EA','ASTM-B211-7075-T6','AnyCompany Machining,AnyCompany Aerospace',         '2024-09-01'),
('PN-DRV-001-REV-A','PN-RAW-7075-REV-A',90,2.000,'KG','ASTM-B211-7075-T6','AnyCompany Metals,Example Corp Metals',               '2024-09-01'),
-- PN-9901-REV-A additional BOM rows
('PN-9901-REV-A','PN-9904-REV-A',    40, 1.000,'EA','ASTM-B211-7075-T6','AnyCompany Machining',                        '2025-02-10'),
('PN-9901-REV-A','PN-9903-REV-A',    50, 2.000,'EA','ASTM-B211-7075-T6','AnyCompany Machining',                        '2024-07-01'),
('PN-9901-REV-A','PN-7744-REV-A',    60, 6.000,'EA','ASTM-A228',       'AnyCompany Retaining Rings',                                '2024-07-01'),
-- PN-7742-REV-C additional BOM rows (tooling/process materials)
('PN-7742-REV-C','PN-9903-REV-A',    40, 2.000,'EA','ASTM-B211-7075-T6','AnyCompany Machining',                        '2024-03-15'),
('PN-7742-REV-C','PN-8811-REV-A',    50, 1.000,'EA','SHORE-A-98-PU',   'Example Corp Couplings',                                '2024-03-15'),
-- PN-8810-REV-B additional rows
('PN-8810-REV-B','PN-7744-REV-A',    40, 2.000,'EA','ASTM-A228',       'AnyCompany Retaining Rings,Example Corp Retaining Rings',                     '2024-05-20'),
('PN-8810-REV-B','PN-RAW-7075-REV-A',50, 0.800,'KG','ASTM-B211-7075-T6','AnyCompany Metals',                           '2024-05-20'),
-- PN-CMX441-REV-B additional rows
('PN-CMX441-REV-B','PN-7744-REV-A',  40, 4.000,'EA','ASTM-A228',       'AnyCompany Retaining Rings',                               '2023-03-01'),
('PN-CMX441-REV-B','PN-8811-REV-A',  50, 1.000,'EA','SHORE-A-98-PU',   'Example Corp Couplings',                               '2023-03-01'),
('PN-CMX441-REV-B','PN-RAW-4140-REV-A',60,0.850,'KG','ASTM-A322-4140','AnyCompany Metals',                             '2023-03-01'),
-- PN-7745-REV-A sub-BOM (in-work end cap)
('PN-7745-REV-A','PN-RAW-7075-REV-A',10,0.050,'KG','ASTM-B211-7075-T6','AnyCompany Metals',                            '2025-01-15'),
-- PN-9904-REV-A sub-BOM (in-review stiffener)
('PN-9904-REV-A','PN-RAW-7075-REV-A',10,0.150,'KG','ASTM-B211-7075-T6','AnyCompany Metals,Example Corp Metals',                  '2025-02-10'),
-- PN-8811-REV-A sub-BOM (coupling insert raw)
('PN-8811-REV-A','PN-RAW-7075-REV-A',20,0.100,'KG','ASTM-B211-7075-T6','AnyCompany Metals',                            '2024-04-10')
) AS v (parent_item, child_item, find_number, quantity_per, unit, material_spec, approved_vendors, effectivity_date)
WHERE NOT EXISTS (SELECT 1 FROM bom_items t WHERE ROW(t.parent_item, t.child_item, t.find_number, t.quantity_per, t.unit, t.material_spec, t.approved_vendors, t.effectivity_date) IS NOT DISTINCT FROM ROW(v.parent_item, v.child_item, v.find_number::INTEGER, v.quantity_per::NUMERIC(8,3), v.unit, v.material_spec, v.approved_vendors, v.effectivity_date::DATE));

-- Additional specifications to reach 100 rows
INSERT INTO specifications (item_id, parameter, nominal, tolerance_min, tolerance_max, unit, spec_class, inspection_method, revision)
SELECT v.item_id, v.parameter, v.nominal::NUMERIC(10,4), v.tolerance_min::NUMERIC(10,4), v.tolerance_max::NUMERIC(10,4), v.unit, v.spec_class, v.inspection_method, v.revision
FROM (VALUES
('PN-7742-REV-C','OD_TOL',    33.0000, 32.9900, 33.0050, 'mm',  'CRITICAL','CMM',               'C'),
('PN-7742-REV-C','BORE_DIA',  16.0000, 15.9950, 16.0050, 'mm',  'CRITICAL','CMM',               'C'),
('PN-CMX441-REV-C','HRC',     58.0000, 55.0000, 62.0000, 'HRC', 'CRITICAL','Rockwell Hardness', 'C'),
('PN-CMX441-REV-C','BORE_DIA',35.0000, 34.9900, 35.0100, 'mm',  'CRITICAL','CMM',               'C'),
('PN-DRV-001-REV-A','HRC',    58.0000, 55.0000, 62.0000, 'HRC', 'CRITICAL','Rockwell Hardness', 'A'),
('PN-DRV-001-REV-A','BORE_DIA',35.0000,34.9900, 35.0100, 'mm',  'CRITICAL','CMM',               'A'),
('PN-9901-REV-A','LEN_TOL',  150.0000,149.8000,150.2000, 'mm',  'MINOR',  'Caliper',            'A'),
('PN-8810-REV-B','BORE_DIA',  30.0000, 29.9900, 30.0100, 'mm',  'CRITICAL','CMM',               'B'),
('PN-8812-REV-A','HRC',       87.0000, 85.0000, 92.0000, 'HRB', 'MAJOR',  'Rockwell Hardness',  'A'),
('PN-8813-REV-A','HRC',       87.0000, 85.0000, 92.0000, 'HRB', 'MAJOR',  'Rockwell Hardness',  'A'),
('PN-7743-REV-A','LEN_TOL',   15.0000, 14.9500, 15.0500, 'mm',  'MINOR',  'Caliper',            'A'),
('PN-9902-REV-A','BORE_DIA',  10.0000,  9.9800, 10.0200, 'mm',  'MINOR',  'CMM',                'A'),
('PN-7744-REV-A','SURF_RA',    1.6000,  0.0000,  2.0000, 'µm',  'MINOR',  'Profilometer',       'A')
) AS v (item_id, parameter, nominal, tolerance_min, tolerance_max, unit, spec_class, inspection_method, revision)
WHERE NOT EXISTS (SELECT 1 FROM specifications t WHERE ROW(t.item_id, t.parameter, t.nominal, t.tolerance_min, t.tolerance_max, t.unit, t.spec_class, t.inspection_method, t.revision) IS NOT DISTINCT FROM ROW(v.item_id, v.parameter, v.nominal::NUMERIC(10,4), v.tolerance_min::NUMERIC(10,4), v.tolerance_max::NUMERIC(10,4), v.unit, v.spec_class, v.inspection_method, v.revision));

-- Additional BOM rows to reach 60 rows
INSERT INTO bom_items (parent_item, child_item, find_number, quantity_per, unit, material_spec, approved_vendors, effectivity_date)
SELECT v.parent_item, v.child_item, v.find_number::INTEGER, v.quantity_per::NUMERIC(8,3), v.unit, v.material_spec, v.approved_vendors, v.effectivity_date::DATE
FROM (VALUES
('PN-DRV-001-REV-A','PN-7743-REV-A',100,2.000,'EA','ASTM-A295-52100','AnyCompany Bearings,Example Corp Bearings',              '2024-09-01'),
('PN-CMX441-REV-C','PN-RAW-7075-REV-A',90,0.500,'KG','ASTM-B211-7075-T6','AnyCompany Metals',         '2024-08-01'),
('PN-9901-REV-A','PN-RAW-4140-REV-A',70,0.200,'KG','ASTM-A322-4140','AnyCompany Metals',              '2024-07-01'),
('PN-8810-REV-B','PN-9903-REV-A',    60,2.000,'EA','ASTM-B211-7075-T6','AnyCompany Machining',       '2024-05-20')
) AS v (parent_item, child_item, find_number, quantity_per, unit, material_spec, approved_vendors, effectivity_date)
WHERE NOT EXISTS (SELECT 1 FROM bom_items t WHERE ROW(t.parent_item, t.child_item, t.find_number, t.quantity_per, t.unit, t.material_spec, t.approved_vendors, t.effectivity_date) IS NOT DISTINCT FROM ROW(v.parent_item, v.child_item, v.find_number::INTEGER, v.quantity_per::NUMERIC(8,3), v.unit, v.material_spec, v.approved_vendors, v.effectivity_date::DATE));

-- ============================================================
-- V8 Engine Block family (B1 fix — closes digital thread gap)
-- Detroit AutoParts plant produces this via CNC + heat treat
-- Raw material: FC300 Cast Iron (ITM-7763 in erp_detroit.materials)
-- ============================================================

INSERT INTO products VALUES
-- V8 Engine Block — current released revision
('PN-ENGBLK-V8-B','V8 Engine Block','4.6L V8 engine block, grey cast iron, 8-cylinder','Part','B','Released','Powertrain-Eng','2025-06-01'),
-- Previous revision (superseded after bore diameter change)
('PN-ENGBLK-V8-A','V8 Engine Block','4.6L V8 engine block rev A - superseded','Part','A','Obsolete','Powertrain-Eng','2024-01-15'),
-- Sub-components machined from the block
('PN-ENGBLK-BORE-REV-A','Cylinder Bore Liner','Precision-honed cylinder bore liner, cast iron','Part','A','Released','Powertrain-Eng','2025-05-01'),
('PN-ENGBLK-MAIN-REV-A','Main Bearing Cap','Main bearing cap, forged steel, 5-bolt','Part','A','Released','Powertrain-Eng','2025-05-01'),
('PN-ENGBLK-GASKET-REV-A','Head Gasket Set','Multi-layer steel head gasket set, V8','Part','A','Released','Powertrain-Eng','2025-04-01'),
-- Raw material item for traceability
('PN-RAW-FC300-REV-A','FC300 Cast Iron Billet','Grey cast iron billet, engine block grade','Raw Material','A','Released','Materials-Eng','2024-06-01')
ON CONFLICT (item_id) DO NOTHING;

-- Specifications for V8 Engine Block (Rev B — current)
INSERT INTO specifications (item_id, parameter, nominal, tolerance_min, tolerance_max, unit, spec_class, inspection_method, revision)
SELECT v.item_id, v.parameter, v.nominal::NUMERIC(10,4), v.tolerance_min::NUMERIC(10,4), v.tolerance_max::NUMERIC(10,4), v.unit, v.spec_class, v.inspection_method, v.revision
FROM (VALUES
-- Cylinder bore diameter (critical — affects compression ratio)
('PN-ENGBLK-V8-B','BORE_DIA',   92.0000,  91.9900,  92.0100, 'mm',  'CRITICAL','CMM Bore Gauge',       'B'),
-- Bore roundness
('PN-ENGBLK-V8-B','ROUNDNESS',   0.0000,   0.0000,   0.0050, 'mm',  'CRITICAL','CMM Roundness',        'B'),
-- Bore cylindricity
('PN-ENGBLK-V8-B','CYLINDRIC',   0.0000,   0.0000,   0.0080, 'mm',  'CRITICAL','CMM Cylindricity',     'B'),
-- Deck flatness (head gasket sealing surface)
('PN-ENGBLK-V8-B','DECK_FLAT',   0.0000,   0.0000,   0.0500, 'mm',  'CRITICAL','CMM Flatness',         'B'),
-- Deck surface finish
('PN-ENGBLK-V8-B','SURF_RA',     1.6000,   0.8000,   2.0000, 'µm',  'CRITICAL','Profilometer',         'B'),
-- Main bearing bore diameter
('PN-ENGBLK-V8-B','MAIN_BORE',  63.0000,  62.9900,  63.0100, 'mm',  'CRITICAL','CMM Bore Gauge',       'B'),
-- Main bearing bore alignment (all 5 must be coaxial)
('PN-ENGBLK-V8-B','MAIN_ALIGN',  0.0000,   0.0000,   0.0100, 'mm',  'CRITICAL','CMM Runout',           'B'),
-- Block height (deck-to-pan rail)
('PN-ENGBLK-V8-B','BLK_HEIGHT',230.0000, 229.9500, 230.0500, 'mm',  'MAJOR',  'CMM',                   'B'),
-- Cylinder spacing
('PN-ENGBLK-V8-B','CYL_PITCH',  98.0000,  97.9800,  98.0200, 'mm',  'MAJOR',  'CMM',                   'B'),
-- Wall thickness (minimum — affects structural integrity)
('PN-ENGBLK-V8-B','WALL_THK',    8.0000,   7.5000,  10.0000, 'mm',  'CRITICAL','UT Thickness',         'B'),
-- Hardness (Brinell — cast iron)
('PN-ENGBLK-V8-B','HB',        200.0000, 180.0000, 240.0000, 'HB',  'MAJOR',  'Brinell Hardness',      'B'),
-- Porosity (X-ray inspection — critical for pressure containment)
('PN-ENGBLK-V8-B','POROSITY',    0.0000,   0.0000,   0.5000, 'mm',  'CRITICAL','X-Ray Inspection',     'B'),
-- Rev A specs (looser bore tolerance — reason for rev change)
('PN-ENGBLK-V8-A','BORE_DIA',   92.0000,  91.9800,  92.0200, 'mm',  'CRITICAL','CMM Bore Gauge',       'A'),
('PN-ENGBLK-V8-A','ROUNDNESS',   0.0000,   0.0000,   0.0100, 'mm',  'CRITICAL','CMM Roundness',        'A'),
('PN-ENGBLK-V8-A','DECK_FLAT',   0.0000,   0.0000,   0.0800, 'mm',  'CRITICAL','CMM Flatness',         'A'),
('PN-ENGBLK-V8-A','SURF_RA',     1.6000,   0.8000,   2.5000, 'µm',  'MAJOR',  'Profilometer',          'A'),
('PN-ENGBLK-V8-A','MAIN_BORE',  63.0000,  62.9800,  63.0200, 'mm',  'CRITICAL','CMM Bore Gauge',       'A'),
('PN-ENGBLK-V8-A','HB',        200.0000, 175.0000, 240.0000, 'HB',  'MAJOR',  'Brinell Hardness',      'A'),
-- Cylinder bore liner specs
('PN-ENGBLK-BORE-REV-A','BORE_DIA',  92.0000,  91.9950,  92.0050, 'mm',  'CRITICAL','CMM Bore Gauge',  'A'),
('PN-ENGBLK-BORE-REV-A','WALL_THK',   3.0000,   2.9500,   3.0500, 'mm',  'CRITICAL','Micrometer',      'A'),
('PN-ENGBLK-BORE-REV-A','SURF_RA',    0.4000,   0.0000,   0.6000, 'µm',  'CRITICAL','Profilometer',    'A'),
('PN-ENGBLK-BORE-REV-A','HRC',       58.0000,  55.0000,  62.0000, 'HRC', 'CRITICAL','Rockwell Hardness','A'),
-- Main bearing cap specs
('PN-ENGBLK-MAIN-REV-A','BORE_DIA',  63.0000,  62.9900,  63.0100, 'mm',  'CRITICAL','CMM',             'A'),
('PN-ENGBLK-MAIN-REV-A','SURF_RA',    1.6000,   0.8000,   2.0000, 'µm',  'MAJOR',  'Profilometer',     'A'),
('PN-ENGBLK-MAIN-REV-A','HRC',       32.0000,  28.0000,  36.0000, 'HRC', 'MAJOR',  'Rockwell Hardness','A'),
-- Raw material cert
('PN-RAW-FC300-REV-A','HB',        200.0000, 180.0000, 240.0000, 'HB',  'MAJOR',  'Brinell Hardness',  'A'),
('PN-RAW-FC300-REV-A','TENSILE',   300.0000, 280.0000, 350.0000, 'MPa', 'MAJOR',  'Tensile Test',      'A')
) AS v (item_id, parameter, nominal, tolerance_min, tolerance_max, unit, spec_class, inspection_method, revision)
WHERE NOT EXISTS (SELECT 1 FROM specifications t WHERE ROW(t.item_id, t.parameter, t.nominal, t.tolerance_min, t.tolerance_max, t.unit, t.spec_class, t.inspection_method, t.revision) IS NOT DISTINCT FROM ROW(v.item_id, v.parameter, v.nominal::NUMERIC(10,4), v.tolerance_min::NUMERIC(10,4), v.tolerance_max::NUMERIC(10,4), v.unit, v.spec_class, v.inspection_method, v.revision));

-- BOM for V8 Engine Block assembly
-- An account seeded when the gasket line's second vendor had another name
-- holds that line under the earlier name. The re-run gives the line the
-- seed's vendors in place, so the insert below finds it and writes no second
-- line; a line that differs from the seed's in any other column is left alone.
UPDATE bom_items SET approved_vendors = 'AnyCompany Gaskets,Example Corp Gaskets'
WHERE ROW(parent_item, child_item, find_number, quantity_per, unit, material_spec, effectivity_date)
      IS NOT DISTINCT FROM ROW('PN-ENGBLK-V8-B', 'PN-ENGBLK-GASKET-REV-A', 40, 1.000::NUMERIC(8,3), 'EA', 'MLS-STEEL', '2025-06-01'::DATE)
  AND approved_vendors IS DISTINCT FROM 'AnyCompany Gaskets,Example Corp Gaskets';
INSERT INTO bom_items (parent_item, child_item, find_number, quantity_per, unit, material_spec, approved_vendors, effectivity_date)
SELECT v.parent_item, v.child_item, v.find_number::INTEGER, v.quantity_per::NUMERIC(8,3), v.unit, v.material_spec, v.approved_vendors, v.effectivity_date::DATE
FROM (VALUES
-- Rev B BOM
('PN-ENGBLK-V8-B','PN-RAW-FC300-REV-A',   10, 45.000,'KG','ASTM-A48-CL35',   'AnyCompany Lakefront Castings Corp,AnyCompany Detroit Foundry',  '2025-06-01'),
('PN-ENGBLK-V8-B','PN-ENGBLK-BORE-REV-A', 20,  8.000,'EA','ASTM-A48-CL40',   'AnyCompany Parts Inc,AnyCompany Machining',       '2025-06-01'),
('PN-ENGBLK-V8-B','PN-ENGBLK-MAIN-REV-A', 30,  5.000,'EA','ASTM-A322-4340',  'AnyCompany Parts Inc',                      '2025-06-01'),
('PN-ENGBLK-V8-B','PN-ENGBLK-GASKET-REV-A',40, 1.000,'EA','MLS-STEEL',       'AnyCompany Gaskets,Example Corp Gaskets',                '2025-06-01'),
-- Rev A BOM (superseded)
('PN-ENGBLK-V8-A','PN-RAW-FC300-REV-A',   10, 47.000,'KG','ASTM-A48-CL30',   'AnyCompany Lakefront Castings Corp',                  '2024-01-15'),
('PN-ENGBLK-V8-A','PN-ENGBLK-BORE-REV-A', 20,  8.000,'EA','ASTM-A48-CL35',   'AnyCompany Parts Inc',                      '2024-01-15'),
('PN-ENGBLK-V8-A','PN-ENGBLK-MAIN-REV-A', 30,  5.000,'EA','ASTM-A322-4340',  'AnyCompany Parts Inc',                      '2024-01-15')
) AS v (parent_item, child_item, find_number, quantity_per, unit, material_spec, approved_vendors, effectivity_date)
WHERE NOT EXISTS (SELECT 1 FROM bom_items t WHERE ROW(t.parent_item, t.child_item, t.find_number, t.quantity_per, t.unit, t.material_spec, t.approved_vendors, t.effectivity_date) IS NOT DISTINCT FROM ROW(v.parent_item, v.child_item, v.find_number::INTEGER, v.quantity_per::NUMERIC(8,3), v.unit, v.material_spec, v.approved_vendors, v.effectivity_date::DATE));

-- ============================================================
-- Detroit AutoParts sister parts — ERP cross-reference targets.
-- The live ERP products carry these item_ids in
-- products.engineering_part_number (AUTO-ENGBLK-V6 / AUTO-TRANS-6SP /
-- AUTO-BRAKE-ROTOR — see lambda-db-init seed_plm_part_xref).
-- AUTO-ELEC-ECU is deliberately unlinked: its electronics are managed in a
-- separate ECAD system, a realistic cross-reference gap.
-- ============================================================

INSERT INTO products VALUES
('PN-ENGBLK-V6-A','V6 Engine Block','3.5L V6 engine block, cast aluminum, 6-cylinder','Part','A','Released','Powertrain-Eng','2025-03-01'),
('PN-TRANS-6SP-A','6-Speed Transmission Housing','Die-cast aluminum housing for 6-speed transmission','Part','A','Released','Drivetrain-Eng','2024-11-15'),
('PN-BRKROTOR-A','Brake Rotor','Ventilated disc brake rotor, grey cast iron','Part','A','Released','Chassis-Eng','2024-10-01')
ON CONFLICT (item_id) DO NOTHING;

INSERT INTO specifications (item_id, parameter, nominal, tolerance_min, tolerance_max, unit, spec_class, inspection_method, revision)
SELECT v.item_id, v.parameter, v.nominal::NUMERIC(10,4), v.tolerance_min::NUMERIC(10,4), v.tolerance_max::NUMERIC(10,4), v.unit, v.spec_class, v.inspection_method, v.revision
FROM (VALUES
('PN-ENGBLK-V6-A','BORE_DIA',   89.0000,  88.9900,  89.0100, 'mm',  'CRITICAL','CMM Bore Gauge',    'A'),
('PN-ENGBLK-V6-A','DECK_FLAT',   0.0000,   0.0000,   0.0500, 'mm',  'CRITICAL','CMM Flatness',      'A'),
('PN-TRANS-6SP-A','BORE_DIA',   72.0000,  71.9850,  72.0150, 'mm',  'CRITICAL','CMM',               'A'),
('PN-TRANS-6SP-A','WALL_THK',    6.0000,   5.8000,   6.2000, 'mm',  'MAJOR',  'UT Thickness',       'A'),
('PN-BRKROTOR-A','LEN_TOL',    330.0000, 329.9000, 330.1000, 'mm',  'MAJOR',  'Caliper',            'A'),
('PN-BRKROTOR-A','SURF_RA',      1.6000,   0.8000,   2.0000, 'µm',  'MAJOR',  'Profilometer',       'A')
) AS v (item_id, parameter, nominal, tolerance_min, tolerance_max, unit, spec_class, inspection_method, revision)
WHERE NOT EXISTS (SELECT 1 FROM specifications t WHERE ROW(t.item_id, t.parameter, t.nominal, t.tolerance_min, t.tolerance_max, t.unit, t.spec_class, t.inspection_method, t.revision) IS NOT DISTINCT FROM ROW(v.item_id, v.parameter, v.nominal::NUMERIC(10,4), v.tolerance_min::NUMERIC(10,4), v.tolerance_max::NUMERIC(10,4), v.unit, v.spec_class, v.inspection_method, v.revision));

-- ============================================================
-- Hydraulic Manifold Block family (machined at the machining site:
-- ERP MACH-MNFD-5520 / MES product 5520 on L7-M2). Rev B tightened the
-- port-bore and sealing-face flatness tolerances; rev A is superseded.
-- ============================================================

INSERT INTO products VALUES
('PN-5520-REV-B','Hydraulic Manifold Block','5-axis milled hydraulic manifold block, 7075-T6 aluminum, 4-port','Part','B','Released','Fluid-Power-Eng','2024-10-15'),
('PN-5520-REV-A','Hydraulic Manifold Block','Manifold block rev A - superseded','Part','A','Obsolete','Fluid-Power-Eng','2023-05-02')
ON CONFLICT (item_id) DO NOTHING;

INSERT INTO specifications (item_id, parameter, nominal, tolerance_min, tolerance_max, unit, spec_class, inspection_method, revision)
SELECT v.item_id, v.parameter, v.nominal::NUMERIC(10,4), v.tolerance_min::NUMERIC(10,4), v.tolerance_max::NUMERIC(10,4), v.unit, v.spec_class, v.inspection_method, v.revision
FROM (VALUES
-- PN-5520-REV-B: port bores, sealing-face flatness, finish, hardness, envelope
('PN-5520-REV-B','BORE_DIA',   19.0500,  19.0400,  19.0600, 'mm',  'CRITICAL','CMM Bore Gauge',       'B'),
('PN-5520-REV-B','FLATNESS',    0.0000,   0.0000,   0.0200, 'mm',  'CRITICAL','CMM Flatness',         'B'),
('PN-5520-REV-B','SURF_RA',     0.8000,   0.0000,   1.2000, 'µm',  'CRITICAL','Profilometer',         'B'),
('PN-5520-REV-B','HRC',        87.0000,  85.0000,  92.0000, 'HRB', 'MAJOR',  'Rockwell Hardness',     'B'),
('PN-5520-REV-B','LEN_TOL',   150.0000, 149.9000, 150.1000, 'mm',  'MAJOR',  'CMM',                   'B'),
-- PN-5520-REV-A specs (looser bore and flatness -- reason for rev change)
('PN-5520-REV-A','BORE_DIA',   19.0500,  19.0300,  19.0700, 'mm',  'CRITICAL','CMM Bore Gauge',       'A'),
('PN-5520-REV-A','FLATNESS',    0.0000,   0.0000,   0.0400, 'mm',  'CRITICAL','CMM Flatness',         'A'),
('PN-5520-REV-A','SURF_RA',     1.6000,   0.0000,   2.0000, 'µm',  'MAJOR',  'Profilometer',          'A'),
('PN-5520-REV-A','HRC',        87.0000,  85.0000,  92.0000, 'HRB', 'MAJOR',  'Rockwell Hardness',     'A')
) AS v (item_id, parameter, nominal, tolerance_min, tolerance_max, unit, spec_class, inspection_method, revision)
WHERE NOT EXISTS (SELECT 1 FROM specifications t WHERE ROW(t.item_id, t.parameter, t.nominal, t.tolerance_min, t.tolerance_max, t.unit, t.spec_class, t.inspection_method, t.revision) IS NOT DISTINCT FROM ROW(v.item_id, v.parameter, v.nominal::NUMERIC(10,4), v.tolerance_min::NUMERIC(10,4), v.tolerance_max::NUMERIC(10,4), v.unit, v.spec_class, v.inspection_method, v.revision));

INSERT INTO bom_items (parent_item, child_item, find_number, quantity_per, unit, material_spec, approved_vendors, effectivity_date)
SELECT v.parent_item, v.child_item, v.find_number::INTEGER, v.quantity_per::NUMERIC(8,3), v.unit, v.material_spec, v.approved_vendors, v.effectivity_date::DATE
FROM (VALUES
-- Rev B BOM
('PN-5520-REV-B','PN-RAW-7075-REV-A', 10, 3.600,'KG','ASTM-B211-7075-T6','AnyCompany Metals,Example Corp Metals',          '2024-10-15'),
('PN-5520-REV-B','PN-9903-REV-A',     20, 4.000,'EA','ASTM-B211-7075-T6','AnyCompany Machining',                           '2024-10-15'),
-- Rev A BOM (superseded)
('PN-5520-REV-A','PN-RAW-7075-REV-A', 10, 3.800,'KG','ASTM-B211-7075-T6','AnyCompany Metals',                              '2023-05-02'),
('PN-5520-REV-A','PN-9903-REV-A',     20, 4.000,'EA','ASTM-B211-7075-T6','AnyCompany Machining',                           '2023-05-02')
) AS v (parent_item, child_item, find_number, quantity_per, unit, material_spec, approved_vendors, effectivity_date)
WHERE NOT EXISTS (SELECT 1 FROM bom_items t WHERE ROW(t.parent_item, t.child_item, t.find_number, t.quantity_per, t.unit, t.material_spec, t.approved_vendors, t.effectivity_date) IS NOT DISTINCT FROM ROW(v.parent_item, v.child_item, v.find_number::INTEGER, v.quantity_per::NUMERIC(8,3), v.unit, v.material_spec, v.approved_vendors, v.effectivity_date::DATE));
