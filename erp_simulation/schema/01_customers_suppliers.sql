-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- ERP Simulation System - Customers and Suppliers Tables
-- Requirements: 1.1, 2.1

-- ============================================================================
-- CUSTOMERS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS customers (
    customer_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_code VARCHAR(50) UNIQUE NOT NULL,
    company_name VARCHAR(200) NOT NULL,
    contact_name VARCHAR(100),
    email VARCHAR(100),
    phone VARCHAR(20),
    billing_address_line1 VARCHAR(200),
    billing_address_line2 VARCHAR(200),
    billing_city VARCHAR(100),
    billing_state VARCHAR(50),
    billing_postal_code VARCHAR(20),
    billing_country VARCHAR(50) DEFAULT 'USA',
    shipping_address_line1 VARCHAR(200),
    shipping_address_line2 VARCHAR(200),
    shipping_city VARCHAR(100),
    shipping_state VARCHAR(50),
    shipping_postal_code VARCHAR(20),
    shipping_country VARCHAR(50) DEFAULT 'USA',
    payment_terms VARCHAR(20) DEFAULT 'NET_30',
    credit_limit DECIMAL(15,2),
    -- Segment -> allowed product categories (BEVERAGE, AUTOMOTIVE, FOOD, MACHINING;
    -- mirrors lambda-db-init/handler.py, which also ALTERs older databases).
    customer_segment VARCHAR(50) DEFAULT 'BEVERAGE',
    is_active BOOLEAN DEFAULT TRUE,
    entity_type VARCHAR(50) DEFAULT 'CUSTOMER',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for customers table
CREATE INDEX IF NOT EXISTS idx_customers_code ON customers(customer_code);
CREATE INDEX IF NOT EXISTS idx_customers_active ON customers(is_active);
CREATE INDEX IF NOT EXISTS idx_customers_company_name ON customers(company_name);

-- ============================================================================
-- SUPPLIERS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS suppliers (
    supplier_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_code VARCHAR(50) UNIQUE NOT NULL,
    company_name VARCHAR(200) NOT NULL,
    contact_name VARCHAR(100),
    email VARCHAR(100),
    phone VARCHAR(20),
    address_line1 VARCHAR(200),
    address_line2 VARCHAR(200),
    city VARCHAR(100),
    state VARCHAR(50),
    postal_code VARCHAR(20),
    country VARCHAR(50) DEFAULT 'USA',
    payment_terms VARCHAR(20) DEFAULT 'NET_30',
    lead_time_days INTEGER DEFAULT 7,
    qualification_status VARCHAR(20) DEFAULT 'APPROVED',
    is_active BOOLEAN DEFAULT TRUE,
    entity_type VARCHAR(50) DEFAULT 'SUPPLIER',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for suppliers table
CREATE INDEX IF NOT EXISTS idx_suppliers_code ON suppliers(supplier_code);
CREATE INDEX IF NOT EXISTS idx_suppliers_active ON suppliers(is_active);
CREATE INDEX IF NOT EXISTS idx_suppliers_company_name ON suppliers(company_name);

-- ============================================================================
-- COMMENTS
-- ============================================================================
COMMENT ON TABLE customers IS 'Customer reference data for sales orders';
COMMENT ON TABLE suppliers IS 'Supplier reference data for purchase orders';

COMMENT ON COLUMN customers.customer_id IS 'UUID primary key for Neptune integration';
COMMENT ON COLUMN customers.customer_code IS 'Unique customer identifier code';
COMMENT ON COLUMN customers.payment_terms IS 'Payment terms (NET_30, NET_60, etc.)';
COMMENT ON COLUMN customers.entity_type IS 'Entity type for Neptune vertex classification';
COMMENT ON COLUMN customers.source_system IS 'Source system identifier for data lineage';

COMMENT ON COLUMN suppliers.supplier_id IS 'UUID primary key for Neptune integration';
COMMENT ON COLUMN suppliers.supplier_code IS 'Unique supplier identifier code';
COMMENT ON COLUMN suppliers.lead_time_days IS 'Standard lead time for deliveries in days';
COMMENT ON COLUMN suppliers.entity_type IS 'Entity type for Neptune vertex classification';
COMMENT ON COLUMN suppliers.source_system IS 'Source system identifier for data lineage';
