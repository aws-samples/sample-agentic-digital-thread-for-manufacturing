-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- ============================================================================
-- MES (Manufacturing Execution System) Work Orders Schema
-- ============================================================================
-- This schema defines MES work orders that link ERP production runs to
-- real-time manufacturing execution on bottling lines.
--
-- Requirements: 8.1, 8.2
-- ============================================================================

-- ============================================================================
-- Table: mes_work_orders
-- ============================================================================
-- Links ERP production runs to MES execution on specific bottling lines.
-- Tracks real-time production metrics including OEE (Overall Equipment Effectiveness).
--
-- Sites and Lines:
--   - milwaukee, detroit, cedarrapids (each Line1, Line2, Line3)
-- ============================================================================

CREATE TABLE IF NOT EXISTS mes_work_orders (
    work_order_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    work_order_number VARCHAR(50) UNIQUE NOT NULL,
    production_run_id UUID NOT NULL REFERENCES production_runs(run_id),
    product_id UUID NOT NULL REFERENCES products(product_id),
    
    -- Site and line information
    site VARCHAR(50) NOT NULL DEFAULT 'detroit',
    line VARCHAR(50) NOT NULL DEFAULT 'line1',
    
    -- Quantity tracking
    planned_quantity INTEGER NOT NULL,
    actual_quantity INTEGER DEFAULT 0,
    good_quantity INTEGER DEFAULT 0,
    reject_quantity INTEGER DEFAULT 0,
    
    -- Status workflow
    status VARCHAR(20) DEFAULT 'SCHEDULED' CHECK (status IN (
        'SCHEDULED',    -- Work order created, waiting to start
        'READY',        -- Ready to begin production
        'RUNNING',      -- Currently in production
        'PAUSED',       -- Temporarily paused
        'COMPLETED',    -- Production finished
        'CANCELLED'     -- Work order cancelled
    )),
    
    -- Timing
    scheduled_start_time TIMESTAMP,
    actual_start_time TIMESTAMP,
    actual_end_time TIMESTAMP,
    
    -- Performance metrics
    target_rate_per_minute INTEGER,  -- Target production rate (bottles/minute)
    actual_rate_per_minute DECIMAL(10,2),  -- Actual production rate achieved
    
    -- OEE (Overall Equipment Effectiveness) calculation
    -- OEE = Availability × Performance × Quality
    availability DECIMAL(5,2),  -- OEE Availability = run time / planned time × 100 (0-100%)
    performance DECIMAL(5,2),   -- OEE Performance = actual rate / target rate × 100 (0-100%)
    oee_percentage DECIMAL(5,2),  -- Overall Equipment Effectiveness (0-100%)
    
    -- Metadata
    entity_type VARCHAR(50) DEFAULT 'MES_WORK_ORDER',
    source_system VARCHAR(50) DEFAULT 'MES',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================================
-- Indexes for mes_work_orders
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_mes_work_orders_number 
    ON mes_work_orders(work_order_number);

CREATE INDEX IF NOT EXISTS idx_mes_work_orders_production_run 
    ON mes_work_orders(production_run_id);

CREATE INDEX IF NOT EXISTS idx_mes_work_orders_product 
    ON mes_work_orders(product_id);

CREATE INDEX IF NOT EXISTS idx_mes_work_orders_status 
    ON mes_work_orders(status);

CREATE INDEX IF NOT EXISTS idx_mes_work_orders_site_line 
    ON mes_work_orders(site, line);

CREATE INDEX IF NOT EXISTS idx_mes_work_orders_scheduled_time 
    ON mes_work_orders(scheduled_start_time);

-- ============================================================================
-- Comments
-- ============================================================================

COMMENT ON TABLE mes_work_orders IS 
    'MES work orders linking ERP production runs to real-time manufacturing execution';

COMMENT ON COLUMN mes_work_orders.site IS 
    'Manufacturing site (milwaukee, detroit, cedarrapids)';

COMMENT ON COLUMN mes_work_orders.line IS 
    'Production line identifier (line1, line2, line3)';

COMMENT ON COLUMN mes_work_orders.oee_percentage IS 
    'Overall Equipment Effectiveness: Availability × Performance × Quality';

COMMENT ON COLUMN mes_work_orders.availability IS 
    'OEE Availability: run time / planned time × 100 (0-100%)';

COMMENT ON COLUMN mes_work_orders.performance IS 
    'OEE Performance: actual rate / target rate × 100 (0-100%)';

COMMENT ON COLUMN mes_work_orders.target_rate_per_minute IS 
    'Target production rate in bottles per minute';

COMMENT ON COLUMN mes_work_orders.actual_rate_per_minute IS 
    'Actual production rate achieved in bottles per minute';
