-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- ERP Simulation System - Order Management Tables
-- Requirements: 1.1, 3.1

-- ============================================================================
-- CUSTOMER ORDERS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS customer_orders (
    order_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_number VARCHAR(50) UNIQUE NOT NULL,
    customer_id UUID NOT NULL REFERENCES customers(customer_id),
    order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    requested_delivery_date TIMESTAMP,
    status VARCHAR(20) DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'CONFIRMED', 'BACKORDERED', 'IN_PRODUCTION', 'READY_TO_SHIP', 'SHIPPED', 'DELIVERED', 'CANCELLED')),
    subtotal DECIMAL(15,2),
    tax_amount DECIMAL(15,2),
    shipping_amount DECIMAL(15,2),
    total_amount DECIMAL(15,2),
    notes TEXT,
    entity_type VARCHAR(50) DEFAULT 'CUSTOMER_ORDER',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for customer_orders table
CREATE INDEX IF NOT EXISTS idx_customer_orders_number ON customer_orders(order_number);
CREATE INDEX IF NOT EXISTS idx_customer_orders_customer ON customer_orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_customer_orders_status ON customer_orders(status);
CREATE INDEX IF NOT EXISTS idx_customer_orders_date ON customer_orders(order_date);
CREATE INDEX IF NOT EXISTS idx_customer_orders_delivery_date ON customer_orders(requested_delivery_date);
-- The bounded READY_TO_SHIP batch read (erp_simulation/hot_path_indexes.py)
CREATE INDEX IF NOT EXISTS idx_customer_orders_status_order ON customer_orders (status, order_id);
-- The delivered-order catch-up, oldest order first (erp_simulation/state_advancer.py)
CREATE INDEX IF NOT EXISTS idx_customer_orders_status_date ON customer_orders (status, order_date NULLS FIRST, order_id);

-- ============================================================================
-- ORDER LINE ITEMS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS order_line_items (
    line_item_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id UUID NOT NULL REFERENCES customer_orders(order_id) ON DELETE CASCADE,
    line_number INTEGER NOT NULL,
    product_id UUID NOT NULL REFERENCES products(product_id),
    quantity INTEGER NOT NULL,
    unit_price DECIMAL(15,2) NOT NULL,
    line_total DECIMAL(15,2) GENERATED ALWAYS AS (quantity * unit_price) STORED,
    entity_type VARCHAR(50) DEFAULT 'ORDER_LINE_ITEM',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(order_id, line_number)
);

-- Indexes for order_line_items table
CREATE INDEX IF NOT EXISTS idx_order_line_items_order ON order_line_items(order_id);
CREATE INDEX IF NOT EXISTS idx_order_line_items_product ON order_line_items(product_id);

-- ============================================================================
-- COMMENTS
-- ============================================================================
COMMENT ON TABLE customer_orders IS 'Sales orders from customers';
COMMENT ON TABLE order_line_items IS 'Individual line items for customer orders';

COMMENT ON COLUMN customer_orders.order_number IS 'Unique order identifier';
COMMENT ON COLUMN customer_orders.status IS 'Order lifecycle status';
COMMENT ON COLUMN customer_orders.subtotal IS 'Sum of all line item totals';
COMMENT ON COLUMN customer_orders.tax_amount IS 'Sales tax amount';
COMMENT ON COLUMN customer_orders.shipping_amount IS 'Shipping and handling charges';
COMMENT ON COLUMN customer_orders.total_amount IS 'Total order amount (subtotal + tax + shipping)';

COMMENT ON COLUMN order_line_items.line_number IS 'Sequential line number within order';
COMMENT ON COLUMN order_line_items.quantity IS 'Quantity ordered';
COMMENT ON COLUMN order_line_items.unit_price IS 'Price per unit at time of order';
COMMENT ON COLUMN order_line_items.line_total IS 'Calculated total (quantity × unit_price)';
