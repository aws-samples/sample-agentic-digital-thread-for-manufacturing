-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- ============================================================================
-- SIMULATION CONFIG TABLE
-- Runtime configuration for simulation parameters
-- Allows dashboard control without Lambda redeployment
-- ============================================================================

CREATE TABLE IF NOT EXISTS simulation_config (
    config_id SERIAL PRIMARY KEY,
    config_key VARCHAR(100) UNIQUE NOT NULL,
    config_value TEXT NOT NULL,
    data_type VARCHAR(20) NOT NULL,  -- 'int', 'float', 'string', 'boolean'
    category VARCHAR(50) NOT NULL,    -- 'orders', 'production', 'mes', 'procurement', 'shipping', 'finance', 'dashboard'
    description TEXT,
    min_value NUMERIC,
    max_value NUMERIC,
    default_value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT NOW(),
    updated_by VARCHAR(100)
);

-- Index for fast lookups during simulation cycles
CREATE INDEX IF NOT EXISTS idx_simulation_config_key ON simulation_config(config_key);
CREATE INDEX IF NOT EXISTS idx_simulation_config_category ON simulation_config(category);

COMMENT ON TABLE simulation_config IS 'Runtime configuration for simulation and dashboard parameters - allows control without redeployment';
