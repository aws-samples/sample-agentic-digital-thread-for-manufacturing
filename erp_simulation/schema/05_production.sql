-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- ERP Simulation System - Production Tables
-- Requirements: 1.1, 5.1

-- ============================================================================
-- PRODUCTION RUNS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS production_runs (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_number VARCHAR(50) UNIQUE NOT NULL,
    product_id UUID NOT NULL REFERENCES products(product_id),
    order_id UUID REFERENCES customer_orders(order_id),
    planned_quantity INTEGER NOT NULL,
    actual_quantity INTEGER,
    status VARCHAR(20) DEFAULT 'PLANNED' CHECK (status IN ('PLANNED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED')),
    planned_start_time TIMESTAMP,
    actual_start_time TIMESTAMP,
    planned_end_time TIMESTAMP,
    actual_end_time TIMESTAMP,
    location_id UUID REFERENCES inventory_locations(location_id),
    site VARCHAR(50),
    line VARCHAR(50),
    notes TEXT,
    entity_type VARCHAR(50) DEFAULT 'PRODUCTION_RUN',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for production_runs table
CREATE INDEX IF NOT EXISTS idx_production_runs_number ON production_runs(run_number);
CREATE INDEX IF NOT EXISTS idx_production_runs_product ON production_runs(product_id);
CREATE INDEX IF NOT EXISTS idx_production_runs_order ON production_runs(order_id);
CREATE INDEX IF NOT EXISTS idx_production_runs_status ON production_runs(status);
CREATE INDEX IF NOT EXISTS idx_production_runs_location ON production_runs(location_id);
CREATE INDEX IF NOT EXISTS idx_production_runs_site_line ON production_runs(site, line);
CREATE INDEX IF NOT EXISTS idx_production_runs_start_time ON production_runs(planned_start_time);
-- Shipping's traceable-run lookup (erp_simulation/hot_path_indexes.py)
CREATE INDEX IF NOT EXISTS idx_production_runs_traceable ON production_runs (product_id, status, actual_end_time DESC NULLS LAST, updated_at DESC NULLS LAST);

-- ============================================================================
-- PRODUCTION MATERIALS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS production_materials (
    material_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES production_runs(run_id) ON DELETE CASCADE,
    item_id UUID NOT NULL REFERENCES inventory_items(item_id),
    planned_quantity DECIMAL(10,4) NOT NULL,
    actual_quantity DECIMAL(10,4),
    entity_type VARCHAR(50) DEFAULT 'PRODUCTION_MATERIAL',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for production_materials table
CREATE INDEX IF NOT EXISTS idx_production_materials_run ON production_materials(run_id);
CREATE INDEX IF NOT EXISTS idx_production_materials_item ON production_materials(item_id);

-- ============================================================================
-- COMMENTS
-- ============================================================================
COMMENT ON TABLE production_runs IS 'Manufacturing execution records';
COMMENT ON TABLE production_materials IS 'Materials consumed in production runs';

COMMENT ON COLUMN production_runs.run_number IS 'Unique production run identifier';
COMMENT ON COLUMN production_runs.order_id IS 'Optional link to customer order driving this production';
COMMENT ON COLUMN production_runs.planned_quantity IS 'Planned quantity to produce';
COMMENT ON COLUMN production_runs.actual_quantity IS 'Actual quantity produced';
COMMENT ON COLUMN production_runs.status IS 'Production run lifecycle status';
COMMENT ON COLUMN production_runs.site IS 'Production site (e.g., milwaukee, detroit, cedarrapids)';
COMMENT ON COLUMN production_runs.line IS 'Production line (e.g., line1, line2, line3)';
COMMENT ON COLUMN production_runs.location_id IS 'Inventory location for finished goods';

COMMENT ON COLUMN production_materials.planned_quantity IS 'Planned material consumption based on BOM';
COMMENT ON COLUMN production_materials.actual_quantity IS 'Actual material consumed (may differ due to waste)';
