-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- 012: Corporate Supplier Master / AP system (demo-only, SAP-MDG / Ariba style).
--
-- A SECOND system that lists the SAME suppliers as the corporate ERP but under
-- DIFFERENT vendor ids and a different ID shape (VM-1000## vs the ERP's
-- SUPP####). company_name values match the ERP's ANCHOR_SUPPLIERS exactly
-- (erp_simulation/seed_data.py), so the ValueMapping crosswalk miner can
-- DERIVE the "same supplier, different id across systems" match from measured
-- name agreement and a steward confirms it into a ValueMapping (ADR-0087 — the
-- join is never pre-seeded).
--
-- Demo-only: registered via the demo system manifest, NOT the workshop seed or
-- any bulk-onboard default, so the workshop content is untouched. Routes to the
-- SEED database by the default rule (no erp_/mes_ prefix).
-- Third-party product names below describe naming-convention style only
-- (nominative use); not affiliated/endorsed. See repo NOTICE.
SET search_path TO supplier_master;

CREATE TABLE IF NOT EXISTS vendors (
    vendor_number VARCHAR(16) PRIMARY KEY,   -- distinct ID shape from the ERP's SUPP####
    legal_name    VARCHAR(200) NOT NULL,     -- matches the ERP supplier company_name
    vendor_group  VARCHAR(30),
    payment_terms VARCHAR(20),
    tax_id        VARCHAR(20),
    status        VARCHAR(12) DEFAULT 'ACTIVE',
    created_on    DATE DEFAULT CURRENT_DATE
);

CREATE TABLE IF NOT EXISTS vendor_spend_summary (
    vendor_number VARCHAR(16) REFERENCES vendors(vendor_number),
    fiscal_year   INTEGER,
    total_spend   NUMERIC(14,2),
    po_count      INTEGER,
    PRIMARY KEY (vendor_number, fiscal_year)
);

-- The three anchor vendors: SAME legal_name as the ERP ANCHOR_SUPPLIERS,
-- DIFFERENT vendor_number — the crosswalk targets.
INSERT INTO vendors (vendor_number, legal_name, vendor_group, payment_terms, tax_id) VALUES
    ('VM-1000017', 'AnyCompany Precision Alloys Inc',      'RAW_MATERIAL', 'NET30', '00-0000017'),
    ('VM-1000042', 'AnyCompany Industrial Fasteners LLC',  'COMPONENT',    'NET45', '00-0000042'),
    ('VM-1000088', 'Example Corp Packaging Systems',       'PACKAGING',    'NET60', '00-0000088'),
    -- a couple of supplier-master-only vendors (no ERP match) so the crosswalk
    -- is not trivially total — the miner must find the overlap, not assume it.
    ('VM-1000091', 'AnyCompany Logistics Partners',        'SERVICE',      'NET30', '00-0000091'),
    ('VM-1000104', 'Example Corp Calibration Services',    'SERVICE',      'NET30', '00-0000104')
ON CONFLICT (vendor_number) DO NOTHING;

INSERT INTO vendor_spend_summary (vendor_number, fiscal_year, total_spend, po_count) VALUES
    ('VM-1000017', 2026, 482150.00, 37),
    ('VM-1000042', 2026, 215400.00, 61),
    ('VM-1000088', 2026, 128900.00, 24),
    ('VM-1000091', 2026,  54300.00, 12),
    ('VM-1000104', 2026,  21750.00,  8)
ON CONFLICT (vendor_number, fiscal_year) DO NOTHING;
