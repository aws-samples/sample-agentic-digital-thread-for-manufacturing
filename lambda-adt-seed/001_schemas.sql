-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- 001: Create schemas for each simulated source system
-- Indianapolis plant systems (orders, customers and lots live in the one
-- corporate ERP owned by the simulator, not in this seed)
CREATE SCHEMA IF NOT EXISTS mes_line3;
CREATE SCHEMA IF NOT EXISTS cmms_maintenance;
CREATE SCHEMA IF NOT EXISTS plm_engineering;

-- Detroit plant systems
CREATE SCHEMA IF NOT EXISTS erp_detroit;
CREATE SCHEMA IF NOT EXISTS mes_detroit;
CREATE SCHEMA IF NOT EXISTS cmms_detroit;

-- Corporate supplier master / AP system (demo-only). A second system that
-- names the SAME suppliers as the corporate ERP under DIFFERENT vendor ids, so
-- the ValueMapping crosswalk miner has a real "same supplier, different id"
-- match to propose (derived from measured name agreement, never a seeded join).
CREATE SCHEMA IF NOT EXISTS supplier_master;
