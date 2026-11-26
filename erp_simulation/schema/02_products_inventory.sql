-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- ERP Simulation System - Products and Inventory Tables
-- Requirements: 1.1, 2.2, 2.3

-- ============================================================================
-- PRODUCTS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS products (
    product_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sku VARCHAR(50) UNIQUE NOT NULL,
    product_name VARCHAR(200) NOT NULL,
    description TEXT,
    category VARCHAR(100),
    unit_of_measure VARCHAR(20) DEFAULT 'EA',
    standard_cost DECIMAL(15,2) NOT NULL,
    list_price DECIMAL(15,2) NOT NULL,
    weight_kg DECIMAL(10,2),
    -- PLM engineering part number (e.g. PN-ENGBLK-V8-B) for products with a
    -- released engineering item; NULL when the product is not PLM-managed.
    engineering_part_number VARCHAR(50),
    is_active BOOLEAN DEFAULT TRUE,
    entity_type VARCHAR(50) DEFAULT 'PRODUCT',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Idempotent migration for databases created before the column existed
ALTER TABLE products ADD COLUMN IF NOT EXISTS engineering_part_number VARCHAR(50);

-- Indexes for products table
CREATE INDEX IF NOT EXISTS idx_products_sku ON products(sku);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
CREATE INDEX IF NOT EXISTS idx_products_active ON products(is_active);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(product_name);

-- ============================================================================
-- INVENTORY ITEMS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS inventory_items (
    item_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    item_code VARCHAR(50) UNIQUE NOT NULL,
    item_name VARCHAR(200) NOT NULL,
    description TEXT,
    item_type VARCHAR(20) NOT NULL CHECK (item_type IN ('RAW_MATERIAL', 'COMPONENT', 'WIP', 'FINISHED_GOOD')),
    unit_of_measure VARCHAR(20) DEFAULT 'EA',
    unit_cost DECIMAL(15,2) NOT NULL,
    reorder_point INTEGER DEFAULT 100,
    reorder_quantity INTEGER DEFAULT 500,
    product_id UUID REFERENCES products(product_id),
    is_active BOOLEAN DEFAULT TRUE,
    entity_type VARCHAR(50) DEFAULT 'INVENTORY_ITEM',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for inventory_items table
CREATE INDEX IF NOT EXISTS idx_inventory_items_code ON inventory_items(item_code);
CREATE INDEX IF NOT EXISTS idx_inventory_items_type ON inventory_items(item_type);
CREATE INDEX IF NOT EXISTS idx_inventory_items_product ON inventory_items(product_id);
CREATE INDEX IF NOT EXISTS idx_inventory_items_name ON inventory_items(item_name);

-- ============================================================================
-- INVENTORY LOCATIONS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS inventory_locations (
    location_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    location_code VARCHAR(50) UNIQUE NOT NULL,
    location_name VARCHAR(200) NOT NULL,
    location_type VARCHAR(50) DEFAULT 'WAREHOUSE',
    is_active BOOLEAN DEFAULT TRUE,
    entity_type VARCHAR(50) DEFAULT 'INVENTORY_LOCATION',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for inventory_locations table
CREATE INDEX IF NOT EXISTS idx_inventory_locations_code ON inventory_locations(location_code);
CREATE INDEX IF NOT EXISTS idx_inventory_locations_type ON inventory_locations(location_type);

-- ============================================================================
-- INVENTORY BALANCES TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS inventory_balances (
    balance_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id UUID NOT NULL REFERENCES inventory_items(item_id),
    location_id UUID NOT NULL REFERENCES inventory_locations(location_id),
    quantity_on_hand INTEGER DEFAULT 0,
    quantity_allocated INTEGER DEFAULT 0,
    quantity_available INTEGER GENERATED ALWAYS AS (quantity_on_hand - quantity_allocated) STORED,
    last_transaction_date TIMESTAMP,
    entity_type VARCHAR(50) DEFAULT 'INVENTORY_BALANCE',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(item_id, location_id)
);

-- Indexes for inventory_balances table
CREATE INDEX IF NOT EXISTS idx_inventory_balances_item ON inventory_balances(item_id);
CREATE INDEX IF NOT EXISTS idx_inventory_balances_location ON inventory_balances(location_id);
CREATE INDEX IF NOT EXISTS idx_inventory_balances_available ON inventory_balances(quantity_available);

-- ============================================================================
-- INVENTORY TRANSACTIONS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS inventory_transactions (
    transaction_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id UUID NOT NULL REFERENCES inventory_items(item_id),
    location_id UUID NOT NULL REFERENCES inventory_locations(location_id),
    transaction_type VARCHAR(20) NOT NULL CHECK (transaction_type IN ('RECEIPT', 'CONSUMPTION', 'ADJUSTMENT', 'TRANSFER')),
    quantity INTEGER NOT NULL,
    unit_cost DECIMAL(15,2),
    reference_type VARCHAR(50),
    reference_id UUID,
    transaction_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT,
    entity_type VARCHAR(50) DEFAULT 'INVENTORY_TRANSACTION',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for inventory_transactions table
CREATE INDEX IF NOT EXISTS idx_inventory_transactions_item ON inventory_transactions(item_id);
CREATE INDEX IF NOT EXISTS idx_inventory_transactions_location ON inventory_transactions(location_id);
CREATE INDEX IF NOT EXISTS idx_inventory_transactions_date ON inventory_transactions(transaction_date);
CREATE INDEX IF NOT EXISTS idx_inventory_transactions_reference ON inventory_transactions(reference_type, reference_id);
CREATE INDEX IF NOT EXISTS idx_inventory_transactions_type ON inventory_transactions(transaction_type);

-- ============================================================================
-- BILL OF MATERIALS (BOM) TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS bill_of_materials (
    bom_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id UUID NOT NULL REFERENCES products(product_id),
    component_item_id UUID NOT NULL REFERENCES inventory_items(item_id),
    quantity_required DECIMAL(10,4) NOT NULL,
    unit_of_measure VARCHAR(20) DEFAULT 'EA',
    scrap_factor DECIMAL(5,4) DEFAULT 0.05,
    is_active BOOLEAN DEFAULT TRUE,
    entity_type VARCHAR(50) DEFAULT 'BOM',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(product_id, component_item_id)
);

-- Indexes for bill_of_materials table
CREATE INDEX IF NOT EXISTS idx_bom_product ON bill_of_materials(product_id);
CREATE INDEX IF NOT EXISTS idx_bom_component ON bill_of_materials(component_item_id);
CREATE INDEX IF NOT EXISTS idx_bom_active ON bill_of_materials(is_active);

-- ============================================================================
-- COMMENTS
-- ============================================================================
COMMENT ON TABLE products IS 'Product reference data for finished goods';
COMMENT ON TABLE inventory_items IS 'All inventory items (raw materials, components, finished goods)';
COMMENT ON TABLE inventory_locations IS 'Warehouse locations for inventory tracking';
COMMENT ON TABLE inventory_balances IS 'Current inventory quantities by item and location';
COMMENT ON TABLE inventory_transactions IS 'All inventory movements and transactions';
COMMENT ON TABLE bill_of_materials IS 'Material requirements for manufacturing products';

COMMENT ON COLUMN products.sku IS 'Stock Keeping Unit - unique product identifier';
COMMENT ON COLUMN products.standard_cost IS 'Standard manufacturing cost';
COMMENT ON COLUMN products.list_price IS 'Selling price to customers';

COMMENT ON COLUMN inventory_items.item_type IS 'Type: RAW_MATERIAL, COMPONENT, WIP, or FINISHED_GOOD';
COMMENT ON COLUMN inventory_items.reorder_point IS 'Minimum quantity before triggering reorder';
COMMENT ON COLUMN inventory_items.reorder_quantity IS 'Quantity to order when below reorder point';
COMMENT ON COLUMN inventory_items.product_id IS 'Link to product if this is a finished good';

COMMENT ON COLUMN inventory_balances.quantity_on_hand IS 'Physical quantity in location';
COMMENT ON COLUMN inventory_balances.quantity_allocated IS 'Quantity reserved for orders';
COMMENT ON COLUMN inventory_balances.quantity_available IS 'Available quantity (on_hand - allocated)';

COMMENT ON COLUMN inventory_transactions.transaction_type IS 'Type: RECEIPT, CONSUMPTION, ADJUSTMENT, or TRANSFER';
COMMENT ON COLUMN inventory_transactions.reference_type IS 'Type of related entity (e.g., PRODUCTION_RUN, SHIPMENT)';
COMMENT ON COLUMN inventory_transactions.reference_id IS 'UUID of related entity';

COMMENT ON COLUMN bill_of_materials.quantity_required IS 'Quantity of component needed per unit of product';
COMMENT ON COLUMN bill_of_materials.scrap_factor IS 'Expected waste/scrap percentage (default 5%)';
