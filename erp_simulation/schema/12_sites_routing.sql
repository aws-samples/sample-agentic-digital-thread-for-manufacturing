-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
-- ============================================================================
-- SITES AND PRODUCT ROUTING TABLES
-- ============================================================================
-- Sites table for production locations
-- Product site routing for mapping product categories to production sites

-- ============================================================================
-- SITES TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS sites (
    site_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_code VARCHAR(50) UNIQUE NOT NULL,
    site_name VARCHAR(255) NOT NULL,
    site_type VARCHAR(50) NOT NULL,
    location VARCHAR(255),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sites_site_code ON sites(site_code);
CREATE INDEX IF NOT EXISTS idx_sites_is_active ON sites(is_active);

-- ============================================================================
-- PRODUCT SITE ROUTING TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS product_site_routing (
    routing_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_category VARCHAR(100) NOT NULL,
    site_id UUID NOT NULL REFERENCES sites(site_id),
    priority INTEGER DEFAULT 1,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(product_category, site_id)
);

CREATE INDEX IF NOT EXISTS idx_product_site_routing_category ON product_site_routing(product_category);
CREATE INDEX IF NOT EXISTS idx_product_site_routing_site_id ON product_site_routing(site_id);
CREATE INDEX IF NOT EXISTS idx_product_site_routing_active ON product_site_routing(is_active);

-- ============================================================================
-- SEED DEFAULT SITE
-- ============================================================================
INSERT INTO sites (site_code, site_name, site_type, location, is_active)
VALUES ('BOTTLING_PLANT', 'Main Bottling Plant', 'BOTTLING_PLANT', 'Main Facility', TRUE)
ON CONFLICT (site_code) DO NOTHING;

-- ============================================================================
-- SEED PRODUCT CATEGORY ROUTING
-- ============================================================================
-- Route all product categories to BOTTLING_PLANT
INSERT INTO product_site_routing (product_category, site_id, priority, is_active)
SELECT 
    'Carbonated Soft Drinks',
    site_id,
    1,
    TRUE
FROM sites
WHERE site_code = 'BOTTLING_PLANT'
ON CONFLICT (product_category, site_id) DO NOTHING;

INSERT INTO product_site_routing (product_category, site_id, priority, is_active)
SELECT 
    'Juices',
    site_id,
    1,
    TRUE
FROM sites
WHERE site_code = 'BOTTLING_PLANT'
ON CONFLICT (product_category, site_id) DO NOTHING;

INSERT INTO product_site_routing (product_category, site_id, priority, is_active)
SELECT 
    'Water',
    site_id,
    1,
    TRUE
FROM sites
WHERE site_code = 'BOTTLING_PLANT'
ON CONFLICT (product_category, site_id) DO NOTHING;
