-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- ERP Simulation System - Procurement Tables
-- Requirements: 1.1, 4.1

-- ============================================================================
-- PURCHASE ORDERS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS purchase_orders (
    po_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    po_number VARCHAR(50) UNIQUE NOT NULL,
    supplier_id UUID NOT NULL REFERENCES suppliers(supplier_id),
    po_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expected_delivery_date TIMESTAMP,
    actual_delivery_date TIMESTAMP,
    status VARCHAR(20) DEFAULT 'DRAFT' CHECK (status IN ('DRAFT', 'SENT', 'ACKNOWLEDGED', 'IN_TRANSIT', 'RECEIVED', 'CANCELLED')),
    total_amount DECIMAL(15,2),
    notes TEXT,
    entity_type VARCHAR(50) DEFAULT 'PURCHASE_ORDER',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for purchase_orders table
CREATE INDEX IF NOT EXISTS idx_purchase_orders_number ON purchase_orders(po_number);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_supplier ON purchase_orders(supplier_id);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_status ON purchase_orders(status);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_date ON purchase_orders(po_date);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_expected_delivery ON purchase_orders(expected_delivery_date);

-- ============================================================================
-- PO LINE ITEMS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS po_line_items (
    line_item_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    po_id UUID NOT NULL REFERENCES purchase_orders(po_id) ON DELETE CASCADE,
    line_number INTEGER NOT NULL,
    item_id UUID NOT NULL REFERENCES inventory_items(item_id),
    quantity_ordered INTEGER NOT NULL,
    quantity_received INTEGER DEFAULT 0,
    unit_price DECIMAL(15,2) NOT NULL,
    line_total DECIMAL(15,2) GENERATED ALWAYS AS (quantity_ordered * unit_price) STORED,
    entity_type VARCHAR(50) DEFAULT 'PO_LINE_ITEM',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(po_id, line_number)
);

-- Indexes for po_line_items table
CREATE INDEX IF NOT EXISTS idx_po_line_items_po ON po_line_items(po_id);
CREATE INDEX IF NOT EXISTS idx_po_line_items_item ON po_line_items(item_id);

-- ============================================================================
-- PO RECEIPTS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS po_receipts (
    receipt_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    po_id UUID NOT NULL REFERENCES purchase_orders(po_id),
    receipt_number VARCHAR(50) UNIQUE NOT NULL,
    receipt_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    received_by VARCHAR(100),
    notes TEXT,
    entity_type VARCHAR(50) DEFAULT 'PO_RECEIPT',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for po_receipts table
CREATE INDEX IF NOT EXISTS idx_po_receipts_po ON po_receipts(po_id);
CREATE INDEX IF NOT EXISTS idx_po_receipts_number ON po_receipts(receipt_number);
CREATE INDEX IF NOT EXISTS idx_po_receipts_date ON po_receipts(receipt_date);

-- ============================================================================
-- COMMENTS
-- ============================================================================
COMMENT ON TABLE purchase_orders IS 'Purchase orders to suppliers for materials';
COMMENT ON TABLE po_line_items IS 'Individual line items for purchase orders';
COMMENT ON TABLE po_receipts IS 'Records of inventory receipts from purchase orders';

COMMENT ON COLUMN purchase_orders.po_number IS 'Unique purchase order identifier';
COMMENT ON COLUMN purchase_orders.status IS 'PO lifecycle status';
COMMENT ON COLUMN purchase_orders.expected_delivery_date IS 'Expected date of delivery';
COMMENT ON COLUMN purchase_orders.actual_delivery_date IS 'Actual date materials were received';
COMMENT ON COLUMN purchase_orders.total_amount IS 'Total PO amount';

COMMENT ON COLUMN po_line_items.line_number IS 'Sequential line number within PO';
COMMENT ON COLUMN po_line_items.quantity_ordered IS 'Quantity ordered from supplier';
COMMENT ON COLUMN po_line_items.quantity_received IS 'Quantity actually received (supports partial receipts)';
COMMENT ON COLUMN po_line_items.unit_price IS 'Price per unit from supplier';
COMMENT ON COLUMN po_line_items.line_total IS 'Calculated total (quantity_ordered × unit_price)';

COMMENT ON COLUMN po_receipts.receipt_number IS 'Unique receipt identifier';
COMMENT ON COLUMN po_receipts.receipt_date IS 'Date materials were received';
COMMENT ON COLUMN po_receipts.received_by IS 'Person who received the materials';
