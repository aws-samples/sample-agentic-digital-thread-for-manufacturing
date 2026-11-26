-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- ============================================================================
-- MES (Manufacturing Execution System) Tracking Tables Schema
-- ============================================================================
-- This schema defines MES tracking tables for production events, quality checks,
-- material consumption, and downtime events during bottling line operations.
--
-- Requirements: 8.3, 8.4, 8.5, 8.6
-- ============================================================================

-- ============================================================================
-- Table: mes_production_events
-- ============================================================================
-- Records real-time events during production execution on the bottling line.
-- Tracks starts, stops, pauses, changeovers, and other significant events.
--
-- Requirement: 8.3
-- ============================================================================

CREATE TABLE IF NOT EXISTS mes_production_events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    work_order_id UUID NOT NULL REFERENCES mes_work_orders(work_order_id),
    
    -- Event classification
    event_type VARCHAR(50) NOT NULL CHECK (event_type IN (
        'START',           -- Production started
        'STOP',            -- Production stopped
        'PAUSE',           -- Production paused
        'RESUME',          -- Production resumed
        'CHANGEOVER',      -- Product changeover
        'QUALITY_CHECK',   -- Quality inspection performed
        'DOWNTIME',        -- Unplanned downtime
        'MATERIAL_CHANGE'  -- Material lot change
    )),
    
    -- Event details
    event_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    duration_seconds INTEGER,  -- Duration of event (for pauses, changeovers, etc.)
    reason_code VARCHAR(50),
    reason_description TEXT,
    operator_id VARCHAR(50),
    quantity_at_event INTEGER,  -- Production quantity at time of event
    notes TEXT,
    
    -- Metadata
    entity_type VARCHAR(50) DEFAULT 'MES_PRODUCTION_EVENT',
    source_system VARCHAR(50) DEFAULT 'MES',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================================
-- Indexes for mes_production_events
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_mes_production_events_work_order 
    ON mes_production_events(work_order_id);

CREATE INDEX IF NOT EXISTS idx_mes_production_events_type 
    ON mes_production_events(event_type);

CREATE INDEX IF NOT EXISTS idx_mes_production_events_time 
    ON mes_production_events(event_time);

CREATE INDEX IF NOT EXISTS idx_mes_production_events_operator 
    ON mes_production_events(operator_id);

-- ============================================================================
-- Table: mes_quality_checks
-- ============================================================================
-- Records quality inspection results during bottling production.
-- Tracks various quality parameters like fill level, cap torque, label placement.
--
-- Requirement: 8.4
-- ============================================================================

CREATE TABLE IF NOT EXISTS mes_quality_checks (
    check_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    work_order_id UUID NOT NULL REFERENCES mes_work_orders(work_order_id),
    
    -- Check details
    check_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    check_type VARCHAR(50) NOT NULL CHECK (check_type IN (
        'VISUAL',           -- Visual inspection
        'WEIGHT',           -- Weight check
        'FILL_LEVEL',       -- Fill level measurement
        'CAP_TORQUE',       -- Cap tightness check
        'LABEL_PLACEMENT',  -- Label position check
        'LEAK_TEST',        -- Leak detection test
        'DIMENSIONAL'       -- Machined-part dimensional (SPC) check
    )),
    
    -- Sample and results
    sample_size INTEGER,
    pass_count INTEGER,
    fail_count INTEGER,
    
    -- Calculated pass rate (generated column)
    pass_rate DECIMAL(5,2) GENERATED ALWAYS AS (
        CASE WHEN sample_size > 0 
        THEN (pass_count::DECIMAL / sample_size) * 100 
        ELSE 0 END
    ) STORED,
    
    -- Inspector information
    inspector_id VARCHAR(50),
    notes TEXT,
    
    -- Metadata
    entity_type VARCHAR(50) DEFAULT 'MES_QUALITY_CHECK',
    source_system VARCHAR(50) DEFAULT 'MES',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================================
-- Indexes for mes_quality_checks
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_mes_quality_checks_work_order 
    ON mes_quality_checks(work_order_id);

CREATE INDEX IF NOT EXISTS idx_mes_quality_checks_type 
    ON mes_quality_checks(check_type);

CREATE INDEX IF NOT EXISTS idx_mes_quality_checks_time 
    ON mes_quality_checks(check_time);

CREATE INDEX IF NOT EXISTS idx_mes_quality_checks_inspector 
    ON mes_quality_checks(inspector_id);

-- ============================================================================
-- Table: mes_material_consumption
-- ============================================================================
-- Tracks actual material usage during production with lot number traceability.
-- Records bottles, caps, labels, concentrates consumed during bottling.
--
-- Requirement: 8.5
-- ============================================================================

CREATE TABLE IF NOT EXISTS mes_material_consumption (
    consumption_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    work_order_id UUID NOT NULL REFERENCES mes_work_orders(work_order_id),
    item_id UUID NOT NULL REFERENCES inventory_items(item_id),
    
    -- Quantity tracking
    planned_quantity DECIMAL(10,4) NOT NULL,
    actual_quantity DECIMAL(10,4) NOT NULL,
    
    -- Variance calculations (generated columns)
    variance_quantity DECIMAL(10,4) GENERATED ALWAYS AS (
        actual_quantity - planned_quantity
    ) STORED,
    
    variance_percentage DECIMAL(5,2) GENERATED ALWAYS AS (
        CASE WHEN planned_quantity > 0 
        THEN ((actual_quantity - planned_quantity) / planned_quantity) * 100 
        ELSE 0 END
    ) STORED,
    
    -- Traceability
    consumption_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    lot_number VARCHAR(50),  -- Material lot number for traceability
    
    -- Metadata
    entity_type VARCHAR(50) DEFAULT 'MES_MATERIAL_CONSUMPTION',
    source_system VARCHAR(50) DEFAULT 'MES',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================================
-- Indexes for mes_material_consumption
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_mes_material_consumption_work_order 
    ON mes_material_consumption(work_order_id);

CREATE INDEX IF NOT EXISTS idx_mes_material_consumption_item 
    ON mes_material_consumption(item_id);

CREATE INDEX IF NOT EXISTS idx_mes_material_consumption_time 
    ON mes_material_consumption(consumption_time);

CREATE INDEX IF NOT EXISTS idx_mes_material_consumption_lot 
    ON mes_material_consumption(lot_number);

-- ============================================================================
-- Table: mes_downtime_events
-- ============================================================================
-- Records unplanned stops and equipment failures during production.
-- Tracks downtime categories, durations, and resolutions for OEE calculation.
--
-- Requirement: 8.6
-- ============================================================================

CREATE TABLE IF NOT EXISTS mes_downtime_events (
    downtime_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    work_order_id UUID REFERENCES mes_work_orders(work_order_id),
    line_id VARCHAR(50) NOT NULL,  -- Can track downtime even without active work order
    
    -- Downtime classification
    downtime_category VARCHAR(50) NOT NULL CHECK (downtime_category IN (
        'MECHANICAL',        -- Mechanical failure
        'ELECTRICAL',        -- Electrical issue
        'MATERIAL_SHORTAGE', -- Ran out of materials
        'QUALITY_ISSUE',     -- Quality problem requiring stop
        'CHANGEOVER',        -- Product changeover time
        'OPERATOR',          -- Operator-related delay
        'OTHER'              -- Other unplanned stop
    )),
    
    downtime_reason VARCHAR(200),
    
    -- Timing
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP,
    
    -- Duration calculation (generated column): minutes to two decimal
    -- places, so stops recorded to the second keep distinct lengths.
    -- Mirrors lambda-db-init/handler.py (DOWNTIME_DURATION_TYPE).
    duration_minutes NUMERIC(8,2) GENERATED ALWAYS AS (
        CASE WHEN end_time IS NOT NULL 
        THEN EXTRACT(EPOCH FROM (end_time - start_time)) / 60 
        ELSE NULL END
    ) STORED,
    
    -- Resolution
    resolved_by VARCHAR(50),
    resolution_notes TEXT,
    
    -- Metadata
    entity_type VARCHAR(50) DEFAULT 'MES_DOWNTIME_EVENT',
    source_system VARCHAR(50) DEFAULT 'MES',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================================
-- Indexes for mes_downtime_events
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_mes_downtime_events_work_order 
    ON mes_downtime_events(work_order_id);

CREATE INDEX IF NOT EXISTS idx_mes_downtime_events_line 
    ON mes_downtime_events(line_id);

CREATE INDEX IF NOT EXISTS idx_mes_downtime_events_category 
    ON mes_downtime_events(downtime_category);

CREATE INDEX IF NOT EXISTS idx_mes_downtime_events_start_time 
    ON mes_downtime_events(start_time);

CREATE INDEX IF NOT EXISTS idx_mes_downtime_events_resolved_by 
    ON mes_downtime_events(resolved_by);

-- ============================================================================
-- Comments
-- ============================================================================

COMMENT ON TABLE mes_production_events IS 
    'Real-time production events during bottling line operations';

COMMENT ON TABLE mes_quality_checks IS 
    'Quality inspection results for bottled products';

COMMENT ON TABLE mes_material_consumption IS 
    'Actual material usage with lot traceability during production';

COMMENT ON TABLE mes_downtime_events IS 
    'Unplanned downtime events and equipment failures';

COMMENT ON COLUMN mes_quality_checks.pass_rate IS 
    'Calculated pass rate: (pass_count / sample_size) × 100';

COMMENT ON COLUMN mes_material_consumption.variance_quantity IS 
    'Material variance: actual_quantity - planned_quantity';

COMMENT ON COLUMN mes_material_consumption.variance_percentage IS 
    'Material variance percentage: ((actual - planned) / planned) × 100';

COMMENT ON COLUMN mes_downtime_events.duration_minutes IS 
    'Downtime duration in minutes, calculated from start_time and end_time';

-- ============================================================================
-- Table: mes_line_status
-- ============================================================================
-- Current real-time status per production line, as a real MES tracks it.
-- Provides the OT-native concepts (machine state, cumulative runtime hours,
-- line speed, throughput) used by the dashboard OEE / Line State cards.
-- One row per (site, line); upserted by the simulator each cycle.
-- ============================================================================

CREATE TABLE IF NOT EXISTS mes_line_status (
    line_status_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site VARCHAR(50) NOT NULL,
    line VARCHAR(50) NOT NULL,

    -- Current machine/line state
    machine_state VARCHAR(20) NOT NULL DEFAULT 'IDLE' CHECK (machine_state IN (
        'RUNNING',      -- Actively producing
        'IDLE',         -- Powered but not producing
        'DOWN',         -- Unplanned stop / failure
        'CHANGEOVER'    -- Product changeover
    )),

    -- Real-time telemetry
    runtime_hours DECIMAL(10,2) NOT NULL DEFAULT 0,  -- Cumulative operating hours
    line_speed DECIMAL(10,2),   -- Current running speed (units/minute)
    throughput DECIMAL(10,2),   -- Current produced-output rate (units/interval)
    state_since TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Metadata
    entity_type VARCHAR(50) DEFAULT 'MES_LINE_STATUS',
    source_system VARCHAR(50) DEFAULT 'MES',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_mes_line_status_site_line UNIQUE (site, line)
);

-- ============================================================================
-- Indexes for mes_line_status
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_mes_line_status_site_line 
    ON mes_line_status(site, line);

CREATE INDEX IF NOT EXISTS idx_mes_line_status_machine_state 
    ON mes_line_status(machine_state);

COMMENT ON TABLE mes_line_status IS 
    'Current real-time status per production line (machine state, runtime hours, line speed, throughput)';

COMMENT ON COLUMN mes_line_status.machine_state IS 
    'Current line state: RUNNING, IDLE, DOWN, or CHANGEOVER';

COMMENT ON COLUMN mes_line_status.runtime_hours IS 
    'Cumulative operating (RUNNING) hours for the line';

COMMENT ON COLUMN mes_line_status.line_speed IS 
    'Current running speed in units per minute';

COMMENT ON COLUMN mes_line_status.throughput IS 
    'Current produced-output rate (units per interval)';
