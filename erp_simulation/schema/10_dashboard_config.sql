-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- ============================================================================
-- DASHBOARD CONFIG SEED DATA
-- Configuration parameters for dashboard sparklines and refresh
-- ============================================================================

INSERT INTO simulation_config (config_key, config_value, data_type, category, description, min_value, max_value, default_value) VALUES
('dashboard_sparkline_events', '60', 'int', 'dashboard', 'Number of events to show in sparklines', 10, 200, '60'),
('dashboard_refresh_seconds', '60', 'int', 'dashboard', 'Dashboard auto-refresh interval (seconds)', 10, 300, '60')
ON CONFLICT (config_key) DO NOTHING;
