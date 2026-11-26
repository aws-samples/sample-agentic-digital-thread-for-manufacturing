-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- ERP Simulation System - Shipping and Financial Tables
-- Requirements: 1.1, 6.1, 7.1

-- ============================================================================
-- SHIPMENTS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS shipments (
    shipment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    shipment_number VARCHAR(50) UNIQUE NOT NULL,
    order_id UUID NOT NULL REFERENCES customer_orders(order_id),
    customer_id UUID NOT NULL REFERENCES customers(customer_id),
    shipment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    carrier VARCHAR(100),
    tracking_number VARCHAR(100),
    estimated_delivery_date TIMESTAMP,
    actual_delivery_date TIMESTAMP,
    status VARCHAR(20) DEFAULT 'PREPARING' CHECK (status IN ('PREPARING', 'PICKED', 'PACKED', 'SHIPPED', 'IN_TRANSIT', 'DELIVERED')),
    notes TEXT,
    entity_type VARCHAR(50) DEFAULT 'SHIPMENT',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for shipments table
CREATE INDEX IF NOT EXISTS idx_shipments_number ON shipments(shipment_number);
CREATE INDEX IF NOT EXISTS idx_shipments_order ON shipments(order_id);
CREATE INDEX IF NOT EXISTS idx_shipments_customer ON shipments(customer_id);
CREATE INDEX IF NOT EXISTS idx_shipments_status ON shipments(status);
CREATE INDEX IF NOT EXISTS idx_shipments_date ON shipments(shipment_date);
CREATE INDEX IF NOT EXISTS idx_shipments_tracking ON shipments(tracking_number);
-- The bounded shipment status steps, oldest first (erp_simulation/hot_path_indexes.py)
CREATE INDEX IF NOT EXISTS idx_shipments_status_shipment_date ON shipments (status, shipment_date);
CREATE INDEX IF NOT EXISTS idx_shipments_status_updated ON shipments (status, updated_at);

-- ============================================================================
-- SHIPMENT ITEMS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS shipment_items (
    shipment_item_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    shipment_id UUID NOT NULL REFERENCES shipments(shipment_id) ON DELETE CASCADE,
    line_item_id UUID NOT NULL REFERENCES order_line_items(line_item_id),
    product_id UUID NOT NULL REFERENCES products(product_id),
    quantity_shipped INTEGER NOT NULL,
    entity_type VARCHAR(50) DEFAULT 'SHIPMENT_ITEM',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for shipment_items table
CREATE INDEX IF NOT EXISTS idx_shipment_items_shipment ON shipment_items(shipment_id);
CREATE INDEX IF NOT EXISTS idx_shipment_items_line_item ON shipment_items(line_item_id);
CREATE INDEX IF NOT EXISTS idx_shipment_items_product ON shipment_items(product_id);

-- ============================================================================
-- INVOICES TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_number VARCHAR(50) UNIQUE NOT NULL,
    customer_id UUID NOT NULL REFERENCES customers(customer_id),
    order_id UUID REFERENCES customer_orders(order_id),
    shipment_id UUID REFERENCES shipments(shipment_id),
    invoice_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    due_date TIMESTAMP,
    status VARCHAR(20) DEFAULT 'DRAFT' CHECK (status IN ('DRAFT', 'SENT', 'PAID', 'OVERDUE', 'CANCELLED')),
    subtotal DECIMAL(15,2),
    tax_amount DECIMAL(15,2),
    total_amount DECIMAL(15,2),
    amount_paid DECIMAL(15,2) DEFAULT 0,
    balance_due DECIMAL(15,2) GENERATED ALWAYS AS (total_amount - amount_paid) STORED,
    notes TEXT,
    entity_type VARCHAR(50) DEFAULT 'INVOICE',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for invoices table
CREATE INDEX IF NOT EXISTS idx_invoices_number ON invoices(invoice_number);
CREATE INDEX IF NOT EXISTS idx_invoices_customer ON invoices(customer_id);
CREATE INDEX IF NOT EXISTS idx_invoices_order ON invoices(order_id);
CREATE INDEX IF NOT EXISTS idx_invoices_shipment ON invoices(shipment_id);
CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);
CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(invoice_date);
CREATE INDEX IF NOT EXISTS idx_invoices_due_date ON invoices(due_date);
-- The bounded SENT -> OVERDUE step, earliest due first (erp_simulation/hot_path_indexes.py)
CREATE INDEX IF NOT EXISTS idx_invoices_status_due ON invoices (status, due_date);

-- ============================================================================
-- INVOICE LINE ITEMS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS invoice_line_items (
    line_item_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id UUID NOT NULL REFERENCES invoices(invoice_id) ON DELETE CASCADE,
    line_number INTEGER NOT NULL,
    description VARCHAR(200) NOT NULL,
    product_id UUID REFERENCES products(product_id),
    quantity INTEGER NOT NULL,
    unit_price DECIMAL(15,2) NOT NULL,
    line_total DECIMAL(15,2) GENERATED ALWAYS AS (quantity * unit_price) STORED,
    entity_type VARCHAR(50) DEFAULT 'INVOICE_LINE_ITEM',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(invoice_id, line_number)
);

-- Indexes for invoice_line_items table
CREATE INDEX IF NOT EXISTS idx_invoice_line_items_invoice ON invoice_line_items(invoice_id);
CREATE INDEX IF NOT EXISTS idx_invoice_line_items_product ON invoice_line_items(product_id);

-- ============================================================================
-- PAYMENTS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS payments (
    payment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_number VARCHAR(50) UNIQUE NOT NULL,
    customer_id UUID NOT NULL REFERENCES customers(customer_id),
    invoice_id UUID REFERENCES invoices(invoice_id),
    payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    payment_method VARCHAR(50) DEFAULT 'WIRE_TRANSFER',
    amount DECIMAL(15,2) NOT NULL,
    reference_number VARCHAR(100),
    notes TEXT,
    entity_type VARCHAR(50) DEFAULT 'PAYMENT',
    source_system VARCHAR(50) DEFAULT 'ERP_SIMULATION',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for payments table
CREATE INDEX IF NOT EXISTS idx_payments_number ON payments(payment_number);
CREATE INDEX IF NOT EXISTS idx_payments_customer ON payments(customer_id);
CREATE INDEX IF NOT EXISTS idx_payments_invoice ON payments(invoice_id);
CREATE INDEX IF NOT EXISTS idx_payments_date ON payments(payment_date);

-- ============================================================================
-- COMMENTS
-- ============================================================================
COMMENT ON TABLE shipments IS 'Shipments to customers';
COMMENT ON TABLE shipment_items IS 'Items included in shipments';
COMMENT ON TABLE invoices IS 'Invoices sent to customers';
COMMENT ON TABLE invoice_line_items IS 'Individual line items for invoices';
COMMENT ON TABLE payments IS 'Payments received from customers';

COMMENT ON COLUMN shipments.shipment_number IS 'Unique shipment identifier';
COMMENT ON COLUMN shipments.carrier IS 'Shipping carrier (e.g., AnyCompany Freight, AnyCompany Logistics)';
COMMENT ON COLUMN shipments.tracking_number IS 'Carrier tracking number';
COMMENT ON COLUMN shipments.status IS 'Shipment lifecycle status';

COMMENT ON COLUMN shipment_items.quantity_shipped IS 'Quantity of product shipped';

COMMENT ON COLUMN invoices.invoice_number IS 'Unique invoice identifier';
COMMENT ON COLUMN invoices.due_date IS 'Payment due date';
COMMENT ON COLUMN invoices.status IS 'Invoice lifecycle status';
COMMENT ON COLUMN invoices.subtotal IS 'Sum of all line item totals';
COMMENT ON COLUMN invoices.tax_amount IS 'Sales tax amount';
COMMENT ON COLUMN invoices.total_amount IS 'Total invoice amount (subtotal + tax)';
COMMENT ON COLUMN invoices.amount_paid IS 'Total amount paid so far';
COMMENT ON COLUMN invoices.balance_due IS 'Calculated remaining balance (total_amount - amount_paid)';

COMMENT ON COLUMN invoice_line_items.line_number IS 'Sequential line number within invoice';
COMMENT ON COLUMN invoice_line_items.description IS 'Line item description';
COMMENT ON COLUMN invoice_line_items.line_total IS 'Calculated total (quantity × unit_price)';

COMMENT ON COLUMN payments.payment_number IS 'Unique payment identifier';
COMMENT ON COLUMN payments.payment_method IS 'Payment method (WIRE_TRANSFER, CHECK, ACH, etc.)';
COMMENT ON COLUMN payments.amount IS 'Payment amount';
COMMENT ON COLUMN payments.reference_number IS 'External payment reference number';
