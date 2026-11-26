-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- ============================================================================
-- SIMULATION EVENTS TABLE
-- Stores one row per Lambda execution cycle with counts for all metrics
-- Eliminates browser cache issues and ensures perfect alignment across sparklines
-- ============================================================================

CREATE TABLE IF NOT EXISTS simulation_events (
    event_id SERIAL PRIMARY KEY,
    event_timestamp TIMESTAMP NOT NULL DEFAULT NOW(),
    cycle_number INTEGER NOT NULL,
    
    -- Orders metrics (including new orders count)
    orders_new INTEGER DEFAULT 0,
    orders_pending INTEGER DEFAULT 0,
    orders_confirmed INTEGER DEFAULT 0,
    orders_backordered INTEGER DEFAULT 0,
    orders_shipped INTEGER DEFAULT 0,
    orders_in_production INTEGER DEFAULT 0,
    
    -- Production metrics (including new production runs)
    production_new INTEGER DEFAULT 0,
    production_planned INTEGER DEFAULT 0,
    production_released INTEGER DEFAULT 0,
    production_in_progress INTEGER DEFAULT 0,
    production_completed INTEGER DEFAULT 0,
    
    -- Procurement metrics
    procurement_sent INTEGER DEFAULT 0,
    procurement_in_transit INTEGER DEFAULT 0,
    procurement_received INTEGER DEFAULT 0,
    
    -- Shipping metrics
    shipping_preparing INTEGER DEFAULT 0,
    shipping_picked INTEGER DEFAULT 0,
    shipping_packed INTEGER DEFAULT 0,
    shipping_shipped INTEGER DEFAULT 0,
    shipping_in_transit INTEGER DEFAULT 0,
    shipping_delivered INTEGER DEFAULT 0,
    
    -- Finance metrics
    finance_invoices INTEGER DEFAULT 0,
    finance_paid INTEGER DEFAULT 0,
    finance_revenue DECIMAL(12,2) DEFAULT 0,
    finance_collected DECIMAL(12,2) DEFAULT 0,
    
    -- MES metrics
    mes_scheduled INTEGER DEFAULT 0,
    mes_running INTEGER DEFAULT 0,
    mes_completed INTEGER DEFAULT 0,

    -- Per-site MES metrics (mirrors lambda-db-init/handler.py)
    mes_mke_scheduled INTEGER DEFAULT 0,
    mes_mke_running INTEGER DEFAULT 0,
    mes_mke_completed INTEGER DEFAULT 0,
    mes_det_scheduled INTEGER DEFAULT 0,
    mes_det_running INTEGER DEFAULT 0,
    mes_det_completed INTEGER DEFAULT 0,
    mes_cr_scheduled INTEGER DEFAULT 0,
    mes_cr_running INTEGER DEFAULT 0,
    mes_cr_completed INTEGER DEFAULT 0,
    mes_ind_scheduled INTEGER DEFAULT 0,
    mes_ind_running INTEGER DEFAULT 0,
    mes_ind_completed INTEGER DEFAULT 0,
    
    -- Inventory metrics
    inventory_transactions INTEGER DEFAULT 0,
    
    created_at TIMESTAMP DEFAULT NOW()
);


-- Index for fast queries (most recent events first)
CREATE INDEX IF NOT EXISTS idx_simulation_events_timestamp ON simulation_events(event_timestamp DESC);

-- Index for cleanup queries
CREATE INDEX IF NOT EXISTS idx_simulation_events_created_at ON simulation_events(created_at);

COMMENT ON TABLE simulation_events IS 'Stores simulation event counts per Lambda cycle for sparkline visualization';
COMMENT ON COLUMN simulation_events.cycle_number IS 'Sequential Lambda execution cycle number';
COMMENT ON COLUMN simulation_events.event_timestamp IS 'When this Lambda cycle executed';
COMMENT ON COLUMN simulation_events.orders_new IS 'Count of new orders created since last event';
COMMENT ON COLUMN simulation_events.production_new IS 'Count of new production runs created since last event';
