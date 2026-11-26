# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Seed data generation for ERP Simulation System.

Generates realistic reference data for customers, suppliers, products, materials,
bill of materials, and initial inventory balances.
"""

import logging
from typing import List, Dict, Any, Optional
import random

from .database import DatabaseManager, get_database
from .segments import SEGMENT_CATEGORIES as _ORDER_SEGMENT_CATEGORIES

logger = logging.getLogger(__name__)

# Deterministic "anchor" suppliers always seeded into the corporate ERP, in
# addition to the randomly generated ones. Their company_name values are FIXED
# so a second system (the corporate supplier master / AP system,
# lambda-adt-seed/012_supplier_master.sql) can carry the SAME companies under
# DIFFERENT vendor ids — giving the ValueMapping crosswalk miner a real,
# repeatable "same supplier, different id across systems" match to propose
# (ADR-0087: the join is DERIVED from measured name agreement, never seeded as a
# pre-built join). Codes/names are fictitious; no demo literal leaks into the
# engine (these live in the demo seam / seed only).
ANCHOR_SUPPLIERS: tuple[dict, ...] = (
    {"supplier_code": "SUPP-VM-01", "company_name": "AnyCompany Precision Alloys Inc",
     "payment_terms": "NET_30", "lead_time_days": 5},
    {"supplier_code": "SUPP-VM-02", "company_name": "AnyCompany Industrial Fasteners LLC",
     "payment_terms": "NET_45", "lead_time_days": 7},
    {"supplier_code": "SUPP-VM-03", "company_name": "Example Corp Packaging Systems",
     "payment_terms": "NET_60", "lead_time_days": 4},
)

# Supplier generation literal tables (extracted from generate_suppliers).
_SUPPLIER_TYPES = [
    ('Bottle Supplier', ['PET Bottles', 'Glass Bottles', 'Plastic Containers'], 2, 5000),
    ('Cap Supplier', ['Plastic Caps', 'Metal Caps', 'Closure Systems'], 1, 10000),
    ('Label Printer', ['Pressure Sensitive Labels', 'Shrink Sleeves', 'Direct Print'], 2, 2000),
    ('Concentrate Manufacturer', ['Beverage Concentrates', 'Flavor Systems', 'Syrups'], 3, 500),
    ('Ingredient Supplier', ['Water Treatment', 'CO2 Supply', 'Sweeteners'], 1, 1000),
]
_SUPPLIER_ADDRESSES = [
    ('100 Main Street', 'Anytown', 'WA', '00000'),
    ('123 Any Street', 'Any Town', 'CA', '00000'),
]
_SUPPLIER_PAYMENT_TERMS = ['NET_30', 'NET_45', 'NET_60']

# The legacy customer segments and their categories in the explicit
# pre-refactor order (never set iteration or sorted()). The categories
# themselves come from segments.py; an unlisted one raises at import.
_CUSTOMER_SEGMENTS = ('BEVERAGE', 'AUTOMOTIVE', 'FOOD')
_CATEGORY_ORDER = (
    'Carbonated Soft Drinks', 'Juices', 'Water',
    'Engine Components', 'Transmission Components', 'Brake Components', 'Electrical Systems',
    'Cereals', 'Snack Foods', 'Grain Products',
)


def _ordered_segment_categories() -> Dict[str, List[str]]:
    unlisted = set().union(*(_ORDER_SEGMENT_CATEGORIES[s] for s in _CUSTOMER_SEGMENTS))
    unlisted -= set(_CATEGORY_ORDER)
    if unlisted:
        raise ValueError(f"categories missing from _CATEGORY_ORDER: {sorted(unlisted)}")
    return {seg: [c for c in _CATEGORY_ORDER if c in _ORDER_SEGMENT_CATEGORIES[seg]]
            for seg in _CUSTOMER_SEGMENTS}


class SeedDataGenerator:
    """Generates and loads seed data into the database."""
    
    def __init__(self, db: Optional[DatabaseManager] = None):
        """
        Initialize the seed data generator.
        
        Args:
            db: Database instance. If None, uses global database.
        """
        self.db = db or get_database()
    
    # Product categories by segment, derived from the ONE map in segments.py.
    # MACHINING is excluded: its customers are seeded by db-init with the
    # machining site, so generate_customers splits across the legacy three.
    SEGMENT_CATEGORIES = _ordered_segment_categories()

    def generate_customers(self, count: int = 30) -> List[Dict[str, Any]]:
        """
        Generate realistic customer seed data aligned to product segments.

        Customers are split evenly across BEVERAGE, AUTOMOTIVE, and FOOD segments
        so the order simulator only assigns products that match the customer's business.
        """
        # Segment-specific business types and name templates
        segment_businesses = {
            'BEVERAGE': [
                ('Retail', ['Beverage Outlet', 'Convenience Mart', 'Liquor Store', 'Gas Station']),
                ('Distribution', ['Beverage Distributor', 'Wholesale Drinks', 'Regional Beverage Supply']),
                ('Food Service', ['Restaurant', 'Cafe', 'Fast Food Chain', 'Hotel Bar']),
            ],
            'AUTOMOTIVE': [
                ('Retail', ['Auto Parts Dealer', 'Tire & Service Center', 'Auto Body Shop']),
                ('Distribution', ['Automotive Distributor', 'Regional Auto Parts', 'OEM Supply Chain']),
                ('Industrial', ['Fleet Service Center', 'OEM Assembly Plant', 'Heavy Equipment Depot']),
            ],
            'FOOD': [
                ('Retail', ['Grocery Chain', 'Supermarket', 'Food Co-op', 'Health Food Store']),
                ('Distribution', ['Food Distributor', 'Wholesale Grocery', 'Regional Food Supply']),
                ('Food Service', ['Restaurant Group', 'Cafeteria Services', 'School District Food']),
            ],
        }

        addresses = [
            ('100 Main Street', 'Anytown', 'WA', '00000'),
            ('123 Any Street', 'Any Town', 'CA', '00000'),
        ]

        payment_terms = ['NET_3', 'NET_5', 'NET_7']
        segments = ['BEVERAGE', 'AUTOMOTIVE', 'FOOD']
        per_segment = count // len(segments)

        customers = []
        for i in range(count):
            segment = segments[i // per_segment] if i // per_segment < len(segments) else segments[-1]
            biz_types = segment_businesses[segment]
            category, subtypes = random.choice(biz_types)  # nosec B311 - synthetic data generation, not security context
            subtype = random.choice(subtypes)  # nosec B311 - synthetic data generation, not security context
            street_address, city, state, base_zip = random.choice(addresses)  # nosec B311 - synthetic data generation, not security context

            company_prefix = random.choice(['AnyCompany', 'Example Corp'])  # nosec B311 - synthetic data generation, not security context
            company_name = f"{company_prefix} {subtype} {i + 1:02d}"

            customer_code = f"CUST{i+1:04d}"
            contact_name = random.choice([  # nosec B311 - synthetic data generation, not security context
                'John Stiles', 'Jane Doe', 'Mary Major', 'Carlos Salazar',
                'Diego Ramirez', 'Saanvi Sarkar', 'Mateo Jackson', 'Shirley Rodriguez',
            ])

            credit_limit = random.randint(100000, 500000) if category == 'Distribution' else random.randint(25000, 150000)  # nosec B311 - synthetic data generation, not security context

            customers.append({
                'customer_code': customer_code,
                'company_name': company_name,
                'contact_name': contact_name,
                'email': f"{contact_name.lower().replace(' ', '.')}@example.com",
                'phone': f"(202) 555-{random.randint(100,199):04d}",  # nosec B311 - synthetic data generation, not security context
                'billing_address_line1': street_address,
                'billing_address_line2': f"Suite {random.randint(100,999)}" if random.random() > 0.5 else None,  # nosec B311 - synthetic data generation, not security context
                'billing_city': city, 'billing_state': state,
                'billing_postal_code': base_zip, 'billing_country': 'USA',
                'shipping_address_line1': street_address,
                'shipping_address_line2': f"Dock {random.randint(1,20)}" if random.random() > 0.3 else None,  # nosec B311 - synthetic data generation, not security context
                'shipping_city': city, 'shipping_state': state,
                'shipping_postal_code': base_zip, 'shipping_country': 'USA',
                'payment_terms': random.choice(payment_terms),  # nosec B311 - synthetic data generation, not security context
                'credit_limit': credit_limit,
                'is_active': True,
                'customer_segment': segment,
            })

        logger.info(f"Generated {len(customers)} customer records ({per_segment} per segment)")
        return customers
    
    def load_customers(self, customers: List[Dict[str, Any]]) -> int:
        """
        Load customer data into the database.
        
        Args:
            customers: List of customer dictionaries
            
        Returns:
            Number of customers inserted
        """
        query = """
            INSERT INTO customers (
                customer_code, company_name, contact_name, email, phone,
                billing_address_line1, billing_address_line2, billing_city, 
                billing_state, billing_postal_code, billing_country,
                shipping_address_line1, shipping_address_line2, shipping_city,
                shipping_state, shipping_postal_code, shipping_country,
                payment_terms, credit_limit, is_active
            ) VALUES (
                %(customer_code)s, %(company_name)s, %(contact_name)s, %(email)s, %(phone)s,
                %(billing_address_line1)s, %(billing_address_line2)s, %(billing_city)s,
                %(billing_state)s, %(billing_postal_code)s, %(billing_country)s,
                %(shipping_address_line1)s, %(shipping_address_line2)s, %(shipping_city)s,
                %(shipping_state)s, %(shipping_postal_code)s, %(shipping_country)s,
                %(payment_terms)s, %(credit_limit)s, %(is_active)s
            )
            ON CONFLICT (customer_code) DO NOTHING
        """
        
        with self.db.connection() as conn:
            cursor = conn.cursor()
            try:
                for customer in customers:
                    cursor.execute(query, customer)
                inserted = cursor.rowcount
                logger.info(f"Loaded {inserted} customers into database")
                return inserted
            finally:
                cursor.close()
    
    def seed_customers(self, count: int = 30) -> int:
        """
        Generate and load customer seed data.
        
        Args:
            count: Number of customers to generate
            
        Returns:
            Number of customers inserted
        """
        logger.info(f"Seeding {count} customers...")
        customers = self.generate_customers(count)
        return self.load_customers(customers)

    @staticmethod
    def _build_supplier(supplier_idx, specialties, lead_time, addresses, payment_terms) -> Dict[str, Any]:
        """Build one random supplier dict (one row of the generated bulk)."""
        specialty = random.choice(specialties)  # nosec B311 - synthetic data generation
        street_address, city, state, base_zip = random.choice(addresses)  # nosec B311
        company_prefix = random.choice(['AnyCompany', 'Example Corp'])  # nosec B311
        contact_name = random.choice([  # nosec B311 - synthetic data generation
            'Akua Mansa', 'Mary Major', 'Martha Rivera', 'Li Juan',
            'Efua Owusu', 'Ana Carolina Silva', 'Saanvi Sarkar', 'Wang Xiulan',
        ])
        actual_lead_time = max(1, lead_time + random.randint(-2, 3))  # nosec B311
        return {
            'supplier_code': f"SUPP{supplier_idx:04d}",
            'company_name': f"{company_prefix} {specialty} {supplier_idx:02d}",
            'contact_name': contact_name,
            'email': f"{contact_name.lower().replace(' ', '.')}@example.com",
            'phone': f"(202) 555-{random.randint(100, 199):04d}",  # nosec B311
            'address_line1': street_address,
            'address_line2': (f"Building {random.choice(['A', 'B', 'C'])}"  # nosec B311
                              if random.random() > 0.6 else None),
            'city': city, 'state': state,
            'postal_code': base_zip, 'country': 'USA',
            'payment_terms': random.choice(payment_terms),  # nosec B311
            'lead_time_days': actual_lead_time,
            'is_active': True,
        }

    def generate_suppliers(self, count: int = 12) -> List[Dict[str, Any]]:
        """Generate realistic supplier seed data.

        ``count`` is the EXACT number of suppliers returned (for ``count`` >= 0):
        first ``min(count, len(ANCHOR_SUPPLIERS))`` deterministic anchor
        suppliers (which the supplier-master crosswalks to), then exactly the
        remaining ``count`` randomly generated ones. The returned length is
        always ``count``, preserving the pre-anchor contract for any downstream
        consumer regardless of how many supplier types exist.
        """
        addresses = _SUPPLIER_ADDRESSES
        payment_terms = _SUPPLIER_PAYMENT_TERMS

        n_anchors = min(max(0, count), len(ANCHOR_SUPPLIERS))
        random_budget = max(0, count - n_anchors)
        suppliers = []
        supplier_idx = 1

        # Cycle the supplier types in stable order until the budget is met, so a
        # count larger than one pass still returns exactly random_budget rows.
        while supplier_idx <= random_budget:
            for supplier_type, specialties, lead_time, min_order in _SUPPLIER_TYPES:
                if supplier_idx > random_budget:
                    break
                suppliers.append(self._build_supplier(
                    supplier_idx, specialties, lead_time, addresses, payment_terms))
                supplier_idx += 1

        logger.info(f"Generated {len(suppliers)} random + {n_anchors} anchor supplier records")
        return self._with_anchor_suppliers(suppliers, n_anchors)

    @staticmethod
    def _with_anchor_suppliers(suppliers: List[Dict[str, Any]],
                               n_anchors: int) -> List[Dict[str, Any]]:
        """Prepend the first ``n_anchors`` deterministic ANCHOR_SUPPLIERS (fixed
        code/name) the supplier-master crosswalks to, filling the remaining
        columns with neutral demo defaults. The crosswalk keys on
        company_name/supplier_code, so the anchor ``contact_name`` is just an
        approved name drawn the same inline way the generated bulk draws it.
        Prepending keeps the random bulk's relative order intact."""
        anchors = []
        for a in ANCHOR_SUPPLIERS[:n_anchors]:
            contact_name = random.choice([  # nosec B311 - synthetic data generation
                'Akua Mansa', 'Mary Major', 'Martha Rivera', 'Li Juan',
                'Efua Owusu', 'Ana Carolina Silva', 'Saanvi Sarkar', 'Wang Xiulan',
            ])
            anchors.append({
                'supplier_code': a['supplier_code'],
                'company_name': a['company_name'],
                'contact_name': contact_name,
                'email': f"{contact_name.lower().replace(' ', '.')}@example.com",
                'phone': '(202) 555-0100',
                'address_line1': '100 Main Street',
                'address_line2': None,
                'city': 'Anytown', 'state': 'WA',
                'postal_code': '00000', 'country': 'USA',
                'payment_terms': a['payment_terms'],
                'lead_time_days': a['lead_time_days'],
                'is_active': True,
            })
        return anchors + suppliers
    
    def load_suppliers(self, suppliers: List[Dict[str, Any]]) -> int:
        """
        Load supplier data into the database.
        
        Args:
            suppliers: List of supplier dictionaries
            
        Returns:
            Number of suppliers inserted
        """
        query = """
            INSERT INTO suppliers (
                supplier_code, company_name, contact_name, email, phone,
                address_line1, address_line2, city, state, postal_code, country,
                payment_terms, lead_time_days, is_active
            ) VALUES (
                %(supplier_code)s, %(company_name)s, %(contact_name)s, %(email)s, %(phone)s,
                %(address_line1)s, %(address_line2)s, %(city)s, %(state)s, 
                %(postal_code)s, %(country)s, %(payment_terms)s, %(lead_time_days)s, %(is_active)s
            )
            ON CONFLICT (supplier_code) DO NOTHING
        """
        
        with self.db.connection() as conn:
            cursor = conn.cursor()
            try:
                for supplier in suppliers:
                    cursor.execute(query, supplier)
                inserted = cursor.rowcount
                logger.info(f"Loaded {inserted} suppliers into database")
                return inserted
            finally:
                cursor.close()
    
    def seed_suppliers(self, count: int = 12) -> int:
        """
        Generate and load supplier seed data.
        
        Args:
            count: Number of suppliers to generate
            
        Returns:
            Number of suppliers inserted
        """
        logger.info(f"Seeding {count} suppliers...")
        suppliers = self.generate_suppliers(count)
        return self.load_suppliers(suppliers)
    
    def generate_products(self) -> List[Dict[str, Any]]:
        """
        Generate product seed data for bottled beverages.
        
        Creates 8 products as specified in the design:
        - Cola 500ml, Cola 1L
        - Lemon-Lime 500ml
        - Orange 500ml
        - Apple Juice 1L
        - Orange Juice 1L
        - Water 500ml, Water 1L
        
        Returns:
            List of product dictionaries
        """
        products = [
            {
                'sku': 'BEV-COLA-500',
                'product_name': 'Cola 500ml',
                'description': 'Carbonated cola beverage in 500ml PET bottle',
                'category': 'Carbonated Soft Drinks',
                'unit_of_measure': 'EA',
                'standard_cost': 0.45,
                'list_price': 1.29,
                'weight_kg': 0.52
            },
            {
                'sku': 'BEV-COLA-1000',
                'product_name': 'Cola 1L',
                'description': 'Carbonated cola beverage in 1L PET bottle',
                'category': 'Carbonated Soft Drinks',
                'unit_of_measure': 'EA',
                'standard_cost': 0.75,
                'list_price': 2.19,
                'weight_kg': 1.04
            },
            {
                'sku': 'BEV-LEMON-500',
                'product_name': 'Lemon-Lime 500ml',
                'description': 'Carbonated lemon-lime beverage in 500ml PET bottle',
                'category': 'Carbonated Soft Drinks',
                'unit_of_measure': 'EA',
                'standard_cost': 0.42,
                'list_price': 1.29,
                'weight_kg': 0.52
            },
            {
                'sku': 'BEV-ORANGE-500',
                'product_name': 'Orange Soda 500ml',
                'description': 'Carbonated orange beverage in 500ml PET bottle',
                'category': 'Carbonated Soft Drinks',
                'unit_of_measure': 'EA',
                'standard_cost': 0.43,
                'list_price': 1.29,
                'weight_kg': 0.52
            },
            {
                'sku': 'JUC-APPLE-1000',
                'product_name': 'Apple Juice 1L',
                'description': '100% apple juice in 1L PET bottle',
                'category': 'Juices',
                'unit_of_measure': 'EA',
                'standard_cost': 1.20,
                'list_price': 3.49,
                'weight_kg': 1.04
            },
            {
                'sku': 'JUC-ORANGE-1000',
                'product_name': 'Orange Juice 1L',
                'description': '100% orange juice in 1L PET bottle',
                'category': 'Juices',
                'unit_of_measure': 'EA',
                'standard_cost': 1.35,
                'list_price': 3.99,
                'weight_kg': 1.04
            },
            {
                'sku': 'WAT-PURE-500',
                'product_name': 'Purified Water 500ml',
                'description': 'Purified drinking water in 500ml PET bottle',
                'category': 'Water',
                'unit_of_measure': 'EA',
                'standard_cost': 0.25,
                'list_price': 0.99,
                'weight_kg': 0.52
            },
            {
                'sku': 'WAT-PURE-1000',
                'product_name': 'Purified Water 1L',
                'description': 'Purified drinking water in 1L PET bottle',
                'category': 'Water',
                'unit_of_measure': 'EA',
                'standard_cost': 0.40,
                'list_price': 1.49,
                'weight_kg': 1.04
            }
        ]
        
        # Add is_active flag
        for product in products:
            product['is_active'] = True
        
        logger.info(f"Generated {len(products)} product records")
        return products
    
    def load_products(self, products: List[Dict[str, Any]]) -> int:
        """
        Load product data into the database.
        
        Args:
            products: List of product dictionaries
            
        Returns:
            Number of products inserted
        """
        query = """
            INSERT INTO products (
                sku, product_name, description, category, unit_of_measure,
                standard_cost, list_price, weight_kg, is_active
            ) VALUES (
                %(sku)s, %(product_name)s, %(description)s, %(category)s, %(unit_of_measure)s,
                %(standard_cost)s, %(list_price)s, %(weight_kg)s, %(is_active)s
            )
            ON CONFLICT (sku) DO NOTHING
        """
        
        with self.db.connection() as conn:
            cursor = conn.cursor()
            try:
                for product in products:
                    cursor.execute(query, product)
                inserted = cursor.rowcount
                logger.info(f"Loaded {inserted} products into database")
                return inserted
            finally:
                cursor.close()
    
    def seed_products(self) -> int:
        """
        Generate and load product seed data.
        
        Returns:
            Number of products inserted
        """
        logger.info("Seeding products...")
        products = self.generate_products()
        return self.load_products(products)
    
    def generate_materials(self) -> List[Dict[str, Any]]:
        """
        Generate material (inventory items) seed data.
        
        Creates materials needed for bottling operations:
        - Bottles (500ml PET, 1L PET)
        - Caps (28mm for 500ml, 38mm for 1L)
        - Labels (one per product)
        - Concentrates (Cola, Lemon-Lime, Orange, Apple, Orange Juice)
        - Water and CO2
        
        Returns:
            List of material dictionaries
        """
        materials = [
            # Bottles
            {
                'item_code': 'BTL-PET-500',
                'item_name': '500ml PET Bottle',
                'description': 'Clear PET bottle, 500ml capacity, 28mm neck finish',
                'item_type': 'RAW_MATERIAL',
                'unit_of_measure': 'EA',
                'unit_cost': 0.08,
                'reorder_point': 500,
                'reorder_quantity': 2000
            },
            {
                'item_code': 'BTL-PET-1000',
                'item_name': '1L PET Bottle',
                'description': 'Clear PET bottle, 1L capacity, 38mm neck finish',
                'item_type': 'RAW_MATERIAL',
                'unit_of_measure': 'EA',
                'unit_cost': 0.12,
                'reorder_point': 300,
                'reorder_quantity': 1500
            },
            # Caps
            {
                'item_code': 'CAP-28MM',
                'item_name': '28mm Plastic Cap',
                'description': 'Tamper-evident plastic cap, 28mm diameter',
                'item_type': 'RAW_MATERIAL',
                'unit_of_measure': 'EA',
                'unit_cost': 0.02,
                'reorder_point': 500,
                'reorder_quantity': 2500
            },
            {
                'item_code': 'CAP-38MM',
                'item_name': '38mm Plastic Cap',
                'description': 'Tamper-evident plastic cap, 38mm diameter',
                'item_type': 'RAW_MATERIAL',
                'unit_of_measure': 'EA',
                'unit_cost': 0.03,
                'reorder_point': 300,
                'reorder_quantity': 1500
            },
            # Labels
            {
                'item_code': 'LBL-COLA',
                'item_name': 'Cola Label',
                'description': 'Pressure-sensitive label for cola products',
                'item_type': 'RAW_MATERIAL',
                'unit_of_measure': 'EA',
                'unit_cost': 0.04,
                'reorder_point': 300,
                'reorder_quantity': 1500
            },
            {
                'item_code': 'LBL-LEMON',
                'item_name': 'Lemon-Lime Label',
                'description': 'Pressure-sensitive label for lemon-lime products',
                'item_type': 'RAW_MATERIAL',
                'unit_of_measure': 'EA',
                'unit_cost': 0.04,
                'reorder_point': 200,
                'reorder_quantity': 1000
            },
            {
                'item_code': 'LBL-ORANGE',
                'item_name': 'Orange Soda Label',
                'description': 'Pressure-sensitive label for orange soda products',
                'item_type': 'RAW_MATERIAL',
                'unit_of_measure': 'EA',
                'unit_cost': 0.04,
                'reorder_point': 200,
                'reorder_quantity': 1000
            },
            {
                'item_code': 'LBL-APPLE',
                'item_name': 'Apple Juice Label',
                'description': 'Pressure-sensitive label for apple juice products',
                'item_type': 'RAW_MATERIAL',
                'unit_of_measure': 'EA',
                'unit_cost': 0.05,
                'reorder_point': 150,
                'reorder_quantity': 800
            },
            {
                'item_code': 'LBL-OJ',
                'item_name': 'Orange Juice Label',
                'description': 'Pressure-sensitive label for orange juice products',
                'item_type': 'RAW_MATERIAL',
                'unit_of_measure': 'EA',
                'unit_cost': 0.05,
                'reorder_point': 150,
                'reorder_quantity': 800
            },
            {
                'item_code': 'LBL-WATER',
                'item_name': 'Water Label',
                'description': 'Pressure-sensitive label for water products',
                'item_type': 'RAW_MATERIAL',
                'unit_of_measure': 'EA',
                'unit_cost': 0.03,
                'reorder_point': 300,
                'reorder_quantity': 1500
            },
            # Concentrates
            {
                'item_code': 'CONC-COLA',
                'item_name': 'Cola Concentrate',
                'description': 'Cola flavoring concentrate, 5x strength',
                'item_type': 'RAW_MATERIAL',
                'unit_of_measure': 'L',
                'unit_cost': 8.50,
                'reorder_point': 50,
                'reorder_quantity': 200
            },
            {
                'item_code': 'CONC-LEMON',
                'item_name': 'Lemon-Lime Concentrate',
                'description': 'Lemon-lime flavoring concentrate, 5x strength',
                'item_type': 'RAW_MATERIAL',
                'unit_of_measure': 'L',
                'unit_cost': 7.80,
                'reorder_point': 30,
                'reorder_quantity': 150
            },
            {
                'item_code': 'CONC-ORANGE',
                'item_name': 'Orange Concentrate',
                'description': 'Orange flavoring concentrate, 5x strength',
                'item_type': 'RAW_MATERIAL',
                'unit_of_measure': 'L',
                'unit_cost': 8.20,
                'reorder_point': 30,
                'reorder_quantity': 150
            },
            {
                'item_code': 'CONC-APPLE',
                'item_name': 'Apple Juice Concentrate',
                'description': '100% apple juice concentrate, 6x strength',
                'item_type': 'RAW_MATERIAL',
                'unit_of_measure': 'L',
                'unit_cost': 12.50,
                'reorder_point': 20,
                'reorder_quantity': 100
            },
            {
                'item_code': 'CONC-OJ',
                'item_name': 'Orange Juice Concentrate',
                'description': '100% orange juice concentrate, 6x strength',
                'item_type': 'RAW_MATERIAL',
                'unit_of_measure': 'L',
                'unit_cost': 15.00,
                'reorder_point': 20,
                'reorder_quantity': 100
            },
            # Water and CO2
            {
                'item_code': 'WATER-PURIFIED',
                'item_name': 'Purified Water',
                'description': 'Purified water for beverage production',
                'item_type': 'RAW_MATERIAL',
                'unit_of_measure': 'L',
                'unit_cost': 0.001,
                'reorder_point': 1000,
                'reorder_quantity': 5000
            },
            {
                'item_code': 'CO2-FOOD',
                'item_name': 'Food Grade CO2',
                'description': 'Food grade carbon dioxide for carbonation',
                'item_type': 'RAW_MATERIAL',
                'unit_of_measure': 'KG',
                'unit_cost': 0.50,
                'reorder_point': 50,
                'reorder_quantity': 200
            }
        ]
        
        # Add is_active flag and product_id (null for raw materials)
        for material in materials:
            material['is_active'] = True
            material['product_id'] = None
        
        logger.info(f"Generated {len(materials)} material records")
        return materials
    
    def load_materials(self, materials: List[Dict[str, Any]]) -> int:
        """
        Load material data into the database.
        
        Args:
            materials: List of material dictionaries
            
        Returns:
            Number of materials inserted
        """
        query = """
            INSERT INTO inventory_items (
                item_code, item_name, description, item_type, unit_of_measure,
                unit_cost, reorder_point, reorder_quantity, product_id, is_active
            ) VALUES (
                %(item_code)s, %(item_name)s, %(description)s, %(item_type)s, %(unit_of_measure)s,
                %(unit_cost)s, %(reorder_point)s, %(reorder_quantity)s, %(product_id)s, %(is_active)s
            )
            ON CONFLICT (item_code) DO NOTHING
        """
        
        with self.db.connection() as conn:
            cursor = conn.cursor()
            try:
                for material in materials:
                    cursor.execute(query, material)
                inserted = cursor.rowcount
                logger.info(f"Loaded {inserted} materials into database")
                return inserted
            finally:
                cursor.close()
    
    def seed_materials(self) -> int:
        """
        Generate and load material seed data.
        
        Returns:
            Number of materials inserted
        """
        logger.info("Seeding materials...")
        materials = self.generate_materials()
        return self.load_materials(materials)
    
    def generate_finished_goods(self) -> List[Dict[str, Any]]:
        """
        Generate finished goods inventory items.
        
        Creates one inventory item per product with item_type='FINISHED_GOOD'.
        These are needed for inventory balances of finished products.
        
        Returns:
            List of finished goods dictionaries
        """
        finished_goods = [
            {
                'item_code': 'FG-COLA-500',
                'item_name': 'Finished Cola 500ml',
                'description': 'Finished good: Cola 500ml ready for sale',
                'item_type': 'FINISHED_GOOD',
                'unit_of_measure': 'EA',
                'unit_cost': 0.45,
                'reorder_point': 50,
                'reorder_quantity': 500,
                'product_sku': 'BEV-COLA-500'
            },
            {
                'item_code': 'FG-COLA-1000',
                'item_name': 'Finished Cola 1L',
                'description': 'Finished good: Cola 1L ready for sale',
                'item_type': 'FINISHED_GOOD',
                'unit_of_measure': 'EA',
                'unit_cost': 0.75,
                'reorder_point': 30,
                'reorder_quantity': 300,
                'product_sku': 'BEV-COLA-1000'
            },
            {
                'item_code': 'FG-LEMON-500',
                'item_name': 'Finished Lemon-Lime 500ml',
                'description': 'Finished good: Lemon-Lime 500ml ready for sale',
                'item_type': 'FINISHED_GOOD',
                'unit_of_measure': 'EA',
                'unit_cost': 0.42,
                'reorder_point': 40,
                'reorder_quantity': 400,
                'product_sku': 'BEV-LEMON-500'
            },
            {
                'item_code': 'FG-ORANGE-500',
                'item_name': 'Finished Orange Soda 500ml',
                'description': 'Finished good: Orange Soda 500ml ready for sale',
                'item_type': 'FINISHED_GOOD',
                'unit_of_measure': 'EA',
                'unit_cost': 0.43,
                'reorder_point': 40,
                'reorder_quantity': 400,
                'product_sku': 'BEV-ORANGE-500'
            },
            {
                'item_code': 'FG-APPLE-1000',
                'item_name': 'Finished Apple Juice 1L',
                'description': 'Finished good: Apple Juice 1L ready for sale',
                'item_type': 'FINISHED_GOOD',
                'unit_of_measure': 'EA',
                'unit_cost': 1.20,
                'reorder_point': 20,
                'reorder_quantity': 200,
                'product_sku': 'JUC-APPLE-1000'
            },
            {
                'item_code': 'FG-OJ-1000',
                'item_name': 'Finished Orange Juice 1L',
                'description': 'Finished good: Orange Juice 1L ready for sale',
                'item_type': 'FINISHED_GOOD',
                'unit_of_measure': 'EA',
                'unit_cost': 1.35,
                'reorder_point': 20,
                'reorder_quantity': 200,
                'product_sku': 'JUC-ORANGE-1000'
            },
            {
                'item_code': 'FG-WATER-500',
                'item_name': 'Finished Water 500ml',
                'description': 'Finished good: Water 500ml ready for sale',
                'item_type': 'FINISHED_GOOD',
                'unit_of_measure': 'EA',
                'unit_cost': 0.25,
                'reorder_point': 50,
                'reorder_quantity': 500,
                'product_sku': 'WAT-PURE-500'
            },
            {
                'item_code': 'FG-WATER-1000',
                'item_name': 'Finished Water 1L',
                'description': 'Finished good: Water 1L ready for sale',
                'item_type': 'FINISHED_GOOD',
                'unit_of_measure': 'EA',
                'unit_cost': 0.40,
                'reorder_point': 30,
                'reorder_quantity': 300,
                'product_sku': 'WAT-PURE-1000'
            }
        ]
        
        # Add is_active flag
        for fg in finished_goods:
            fg['is_active'] = True
        
        logger.info(f"Generated {len(finished_goods)} finished goods records")
        return finished_goods
    
    def load_finished_goods(self, finished_goods: List[Dict[str, Any]]) -> int:
        """
        Load finished goods inventory items into the database.
        
        Args:
            finished_goods: List of finished goods dictionaries
            
        Returns:
            Number of finished goods inserted
        """
        query = """
            INSERT INTO inventory_items (
                item_code, item_name, description, item_type, unit_of_measure,
                unit_cost, reorder_point, reorder_quantity, product_id, is_active
            )
            SELECT 
                %(item_code)s, %(item_name)s, %(description)s, %(item_type)s, %(unit_of_measure)s,
                %(unit_cost)s, %(reorder_point)s, %(reorder_quantity)s, p.product_id, %(is_active)s
            FROM products p
            WHERE p.sku = %(product_sku)s
            ON CONFLICT (item_code) DO NOTHING
        """
        
        with self.db.connection() as conn:
            cursor = conn.cursor()
            try:
                inserted = 0
                for fg in finished_goods:
                    cursor.execute(query, fg)
                    inserted += cursor.rowcount
                logger.info(f"Loaded {inserted} finished goods into database")
                return inserted
            finally:
                cursor.close()
    
    def seed_finished_goods(self) -> int:
        """
        Generate and load finished goods inventory items.
        
        Returns:
            Number of finished goods inserted
        """
        logger.info("Seeding finished goods...")
        finished_goods = self.generate_finished_goods()
        return self.load_finished_goods(finished_goods)
    
    def generate_bom(self) -> List[Dict[str, Any]]:
        """
        Generate bill of materials seed data.
        
        Defines material requirements for each product:
        - 1 bottle (appropriate size)
        - 1 cap (appropriate size)
        - 1 label (product-specific)
        - Concentrate (varies by product)
        - Water (varies by product)
        - CO2 (for carbonated drinks only)
        
        Returns:
            List of BOM dictionaries with product SKU and material codes
        """
        # BOM structure: (product_sku, material_code, quantity, uom)
        bom_data = [
            # Cola 500ml
            ('BEV-COLA-500', 'BTL-PET-500', 1.0, 'EA'),
            ('BEV-COLA-500', 'CAP-28MM', 1.0, 'EA'),
            ('BEV-COLA-500', 'LBL-COLA', 1.0, 'EA'),
            ('BEV-COLA-500', 'CONC-COLA', 0.08, 'L'),  # 80ml concentrate
            ('BEV-COLA-500', 'WATER-PURIFIED', 0.42, 'L'),  # 420ml water
            ('BEV-COLA-500', 'CO2-FOOD', 0.004, 'KG'),  # 4g CO2
            
            # Cola 1L
            ('BEV-COLA-1000', 'BTL-PET-1000', 1.0, 'EA'),
            ('BEV-COLA-1000', 'CAP-38MM', 1.0, 'EA'),
            ('BEV-COLA-1000', 'LBL-COLA', 1.0, 'EA'),
            ('BEV-COLA-1000', 'CONC-COLA', 0.16, 'L'),  # 160ml concentrate
            ('BEV-COLA-1000', 'WATER-PURIFIED', 0.84, 'L'),  # 840ml water
            ('BEV-COLA-1000', 'CO2-FOOD', 0.008, 'KG'),  # 8g CO2
            
            # Lemon-Lime 500ml
            ('BEV-LEMON-500', 'BTL-PET-500', 1.0, 'EA'),
            ('BEV-LEMON-500', 'CAP-28MM', 1.0, 'EA'),
            ('BEV-LEMON-500', 'LBL-LEMON', 1.0, 'EA'),
            ('BEV-LEMON-500', 'CONC-LEMON', 0.08, 'L'),
            ('BEV-LEMON-500', 'WATER-PURIFIED', 0.42, 'L'),
            ('BEV-LEMON-500', 'CO2-FOOD', 0.004, 'KG'),
            
            # Orange Soda 500ml
            ('BEV-ORANGE-500', 'BTL-PET-500', 1.0, 'EA'),
            ('BEV-ORANGE-500', 'CAP-28MM', 1.0, 'EA'),
            ('BEV-ORANGE-500', 'LBL-ORANGE', 1.0, 'EA'),
            ('BEV-ORANGE-500', 'CONC-ORANGE', 0.08, 'L'),
            ('BEV-ORANGE-500', 'WATER-PURIFIED', 0.42, 'L'),
            ('BEV-ORANGE-500', 'CO2-FOOD', 0.004, 'KG'),
            
            # Apple Juice 1L (no CO2)
            ('JUC-APPLE-1000', 'BTL-PET-1000', 1.0, 'EA'),
            ('JUC-APPLE-1000', 'CAP-38MM', 1.0, 'EA'),
            ('JUC-APPLE-1000', 'LBL-APPLE', 1.0, 'EA'),
            ('JUC-APPLE-1000', 'CONC-APPLE', 0.167, 'L'),  # 167ml concentrate (6x)
            ('JUC-APPLE-1000', 'WATER-PURIFIED', 0.833, 'L'),  # 833ml water
            
            # Orange Juice 1L (no CO2)
            ('JUC-ORANGE-1000', 'BTL-PET-1000', 1.0, 'EA'),
            ('JUC-ORANGE-1000', 'CAP-38MM', 1.0, 'EA'),
            ('JUC-ORANGE-1000', 'LBL-OJ', 1.0, 'EA'),
            ('JUC-ORANGE-1000', 'CONC-OJ', 0.167, 'L'),
            ('JUC-ORANGE-1000', 'WATER-PURIFIED', 0.833, 'L'),
            
            # Water 500ml (no concentrate or CO2)
            ('WAT-PURE-500', 'BTL-PET-500', 1.0, 'EA'),
            ('WAT-PURE-500', 'CAP-28MM', 1.0, 'EA'),
            ('WAT-PURE-500', 'LBL-WATER', 1.0, 'EA'),
            ('WAT-PURE-500', 'WATER-PURIFIED', 0.5, 'L'),
            
            # Water 1L (no concentrate or CO2)
            ('WAT-PURE-1000', 'BTL-PET-1000', 1.0, 'EA'),
            ('WAT-PURE-1000', 'CAP-38MM', 1.0, 'EA'),
            ('WAT-PURE-1000', 'LBL-WATER', 1.0, 'EA'),
            ('WAT-PURE-1000', 'WATER-PURIFIED', 1.0, 'L'),
        ]
        
        # Convert to dictionary format with product_sku and component_item_code
        bom_records = []
        for product_sku, material_code, quantity, uom in bom_data:
            bom_records.append({
                'product_sku': product_sku,
                'component_item_code': material_code,
                'quantity_required': quantity,
                'unit_of_measure': uom,
                'scrap_factor': 0.05,  # 5% scrap
                'is_active': True
            })
        
        logger.info(f"Generated {len(bom_records)} BOM records")
        return bom_records
    
    def load_bom(self, bom_records: List[Dict[str, Any]]) -> int:
        """
        Load bill of materials data into the database.
        
        Args:
            bom_records: List of BOM dictionaries
            
        Returns:
            Number of BOM records inserted
        """
        # Need to look up product_id and component_item_id from SKU/codes
        query = """
            INSERT INTO bill_of_materials (
                product_id, component_item_id, quantity_required, 
                unit_of_measure, scrap_factor, is_active
            )
            SELECT 
                p.product_id,
                i.item_id,
                %(quantity_required)s,
                %(unit_of_measure)s,
                %(scrap_factor)s,
                %(is_active)s
            FROM products p
            CROSS JOIN inventory_items i
            WHERE p.sku = %(product_sku)s
              AND i.item_code = %(component_item_code)s
            ON CONFLICT (product_id, component_item_id) DO NOTHING
        """
        
        with self.db.connection() as conn:
            cursor = conn.cursor()
            try:
                inserted = 0
                for bom in bom_records:
                    cursor.execute(query, bom)
                    inserted += cursor.rowcount
                logger.info(f"Loaded {inserted} BOM records into database")
                return inserted
            finally:
                cursor.close()
    
    def seed_bom(self) -> int:
        """
        Generate and load bill of materials seed data.
        
        Returns:
            Number of BOM records inserted
        """
        logger.info("Seeding bill of materials...")
        bom_records = self.generate_bom()
        return self.load_bom(bom_records)
    
    def generate_inventory_locations(self) -> List[Dict[str, Any]]:
        """
        Generate inventory location seed data.
        
        Creates warehouse locations for inventory tracking.
        
        Returns:
            List of location dictionaries
        """
        locations = [
            {
                'location_code': 'WH-MAIN',
                'location_name': 'Main Warehouse',
                'location_type': 'WAREHOUSE',
                'is_active': True
            },
            {
                'location_code': 'WH-RAW',
                'location_name': 'Raw Materials Storage',
                'location_type': 'WAREHOUSE',
                'is_active': True
            },
            {
                'location_code': 'PROD-FLOOR',
                'location_name': 'Production Floor',
                'location_type': 'PRODUCTION',
                'is_active': True
            },
            {
                'location_code': 'FG-WAREHOUSE',
                'location_name': 'Finished Goods Warehouse',
                'location_type': 'WAREHOUSE',
                'is_active': True
            },
            {
                'location_code': 'SHIPPING-DOCK',
                'location_name': 'Shipping Dock',
                'location_type': 'SHIPPING',
                'is_active': True
            }
        ]
        
        logger.info(f"Generated {len(locations)} inventory location records")
        return locations
    
    def load_inventory_locations(self, locations: List[Dict[str, Any]]) -> int:
        """
        Load inventory location data into the database.
        
        Args:
            locations: List of location dictionaries
            
        Returns:
            Number of locations inserted
        """
        query = """
            INSERT INTO inventory_locations (
                location_code, location_name, location_type, is_active
            ) VALUES (
                %(location_code)s, %(location_name)s, %(location_type)s, %(is_active)s
            )
            ON CONFLICT (location_code) DO NOTHING
        """
        
        with self.db.connection() as conn:
            cursor = conn.cursor()
            try:
                for location in locations:
                    cursor.execute(query, location)
                inserted = cursor.rowcount
                logger.info(f"Loaded {inserted} inventory locations into database")
                return inserted
            finally:
                cursor.close()
    
    def seed_inventory_locations(self) -> int:
        """
        Generate and load inventory location seed data.
        
        Returns:
            Number of locations inserted
        """
        logger.info("Seeding inventory locations...")
        locations = self.generate_inventory_locations()
        return self.load_inventory_locations(locations)
    
    def generate_inventory_balances(self) -> List[Dict[str, Any]]:
        """
        Generate initial inventory balance seed data.
        
        Sets starting inventory levels for all materials at appropriate locations.
        Raw materials go to WH-RAW, finished goods to FG-WAREHOUSE.
        
        Returns:
            List of balance dictionaries with item codes and location codes
        """
        # Material codes and their initial quantities (reduced 10x for demo)
        material_balances = [
            # Bottles - high volume
            ('BTL-PET-500', 'WH-RAW', 1500),
            ('BTL-PET-1000', 'WH-RAW', 1000),
            
            # Caps - high volume
            ('CAP-28MM', 'WH-RAW', 1500),
            ('CAP-38MM', 'WH-RAW', 1000),
            
            # Labels - moderate volume
            ('LBL-COLA', 'WH-RAW', 800),
            ('LBL-LEMON', 'WH-RAW', 500),
            ('LBL-ORANGE', 'WH-RAW', 500),
            ('LBL-APPLE', 'WH-RAW', 400),
            ('LBL-OJ', 'WH-RAW', 400),
            ('LBL-WATER', 'WH-RAW', 800),
            
            # Concentrates - measured in liters
            ('CONC-COLA', 'WH-RAW', 150),
            ('CONC-LEMON', 'WH-RAW', 100),
            ('CONC-ORANGE', 'WH-RAW', 100),
            ('CONC-APPLE', 'WH-RAW', 80),
            ('CONC-OJ', 'WH-RAW', 80),
            
            # Water and CO2 - very high volume
            ('WATER-PURIFIED', 'WH-RAW', 5000),
            ('CO2-FOOD', 'WH-RAW', 150),
            
            # Finished goods - small starting inventory (100 units each)
            ('FG-COLA-500', 'FG-WAREHOUSE', 100),
            ('FG-COLA-1000', 'FG-WAREHOUSE', 100),
            ('FG-LEMON-500', 'FG-WAREHOUSE', 100),
            ('FG-ORANGE-500', 'FG-WAREHOUSE', 100),
            ('FG-APPLE-1000', 'FG-WAREHOUSE', 100),
            ('FG-OJ-1000', 'FG-WAREHOUSE', 100),
            ('FG-WATER-500', 'FG-WAREHOUSE', 100),
            ('FG-WATER-1000', 'FG-WAREHOUSE', 100),
        ]
        
        balances = []
        for item_code, location_code, quantity in material_balances:
            balances.append({
                'item_code': item_code,
                'location_code': location_code,
                'quantity_on_hand': quantity,
                'quantity_allocated': 0
            })
        
        logger.info(f"Generated {len(balances)} inventory balance records")
        return balances
    
    def load_inventory_balances(self, balances: List[Dict[str, Any]]) -> int:
        """
        Load inventory balance data into the database.
        
        Args:
            balances: List of balance dictionaries
            
        Returns:
            Number of balances inserted
        """
        # Need to look up item_id and location_id from codes
        query = """
            INSERT INTO inventory_balances (
                item_id, location_id, quantity_on_hand, quantity_allocated, last_transaction_date
            )
            SELECT 
                i.item_id,
                l.location_id,
                %(quantity_on_hand)s,
                %(quantity_allocated)s,
                CURRENT_TIMESTAMP
            FROM inventory_items i
            CROSS JOIN inventory_locations l
            WHERE i.item_code = %(item_code)s
              AND l.location_code = %(location_code)s
            ON CONFLICT (item_id, location_id) DO NOTHING
        """
        
        with self.db.connection() as conn:
            cursor = conn.cursor()
            try:
                inserted = 0
                for balance in balances:
                    cursor.execute(query, balance)
                    inserted += cursor.rowcount
                logger.info(f"Loaded {inserted} inventory balances into database")
                return inserted
            finally:
                cursor.close()
    
    def seed_inventory_balances(self) -> int:
        """
        Generate and load inventory balance seed data.
        
        Returns:
            Number of balances inserted
        """
        logger.info("Seeding inventory balances...")
        balances = self.generate_inventory_balances()
        return self.load_inventory_balances(balances)


def deploy_base_schema(db: Optional[DatabaseManager] = None) -> None:
    """
    Deploy base ERP schema from SQL files in erp_simulation/schema/ directory.
    
    Reads and executes all SQL files in order to create the complete database schema.
    
    Args:
        db: Database instance. If None, uses global database.
    """
    import os
    
    database = db or get_database()
    
    # Get the directory containing schema files
    schema_dir = os.path.join(os.path.dirname(__file__), 'schema')
    
    if not os.path.exists(schema_dir):
        logger.warning(f"Schema directory not found: {schema_dir}")
        return
    
    # Get all SQL files in order
    sql_files = sorted([f for f in os.listdir(schema_dir) if f.endswith('.sql')])
    
    if not sql_files:
        logger.warning(f"No SQL files found in {schema_dir}")
        return
    
    logger.info(f"Deploying base schema from {len(sql_files)} SQL files...")
    
    with database.connection() as conn:
        cursor = conn.cursor()
        try:
            for sql_file in sql_files:
                file_path = os.path.join(schema_dir, sql_file)
                logger.info(f"Executing {sql_file}...")
                
                with open(file_path, 'r', encoding="utf-8") as f:
                    sql_content = f.read()
                
                # Execute the SQL file
                cursor.execute(sql_content)
                logger.info(f"✅ {sql_file} executed successfully")
            
            logger.info("Base schema deployment completed successfully")
        except Exception as e:
            logger.error(f"Base schema deployment failed: {e}")
            raise
        finally:
            cursor.close()


def run_schema_migration(db: Optional[DatabaseManager] = None) -> None:
    """
    Run schema migration for v2.2.0: Sites and Product Routing.
    
    This migration:
    - Updates production_runs CHECK constraint to include RELEASED status
    - Creates sites table
    - Creates product_site_routing table
    - Seeds BOTTLING_PLANT site
    - Routes all product categories to BOTTLING_PLANT
    - Updates existing production runs to use BOTTLING_PLANT
    
    Args:
        db: Database instance. If None, uses global database.
    """
    database = db or get_database()
    
    migration_sql = """
    -- Schema Migration: Sites and Product Routing
    -- Version: 2.2.0
    
    -- Update production_runs CHECK constraint to include RELEASED status
    -- Note: Base schema uses VARCHAR with CHECK constraint, not enum
    DO $$ 
    BEGIN
        -- Drop the old constraint if it exists
        ALTER TABLE production_runs DROP CONSTRAINT IF EXISTS production_runs_status_check;
        
        -- Add new constraint with RELEASED status
        ALTER TABLE production_runs 
        ADD CONSTRAINT production_runs_status_check 
        CHECK (status IN ('PLANNED', 'RELEASED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED'));
        
        RAISE NOTICE 'Updated production_runs status constraint to include RELEASED';
    EXCEPTION
        WHEN duplicate_object THEN
            RAISE NOTICE 'Constraint already exists with RELEASED status';
    END $$;
    
    -- Create sites table
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
    
    -- Create product_site_routing table
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
    
    -- Insert BOTTLING_PLANT site
    INSERT INTO sites (site_code, site_name, site_type, location, is_active)
    VALUES ('BOTTLING_PLANT', 'Main Bottling Plant', 'BOTTLING_PLANT', 'Main Facility', TRUE)
    ON CONFLICT (site_code) DO NOTHING;
    
    -- Route all existing product categories to BOTTLING_PLANT
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
    
    -- Update existing production runs to use BOTTLING_PLANT
    UPDATE production_runs
    SET site = 'BOTTLING_PLANT'
    WHERE site = 'MAIN_PLANT' OR site IS NULL;
    """
    
    with database.connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(migration_sql)
            logger.info("Schema migration v2.2.0 completed successfully")
        except Exception as e:
            logger.error(f"Schema migration failed: {e}")
            raise
        finally:
            cursor.close()


def seed_all_data(db: Optional[DatabaseManager] = None) -> Dict[str, int]:
    """
    Generate and load all seed data.
    
    Args:
        db: Database instance. If None, uses global database.
        
    Returns:
        Dictionary with counts of inserted records by entity type
    """
    # Deploy base schema first (if needed)
    logger.info("Deploying base ERP schema (if needed)...")
    deploy_base_schema(db)
    
    # Run schema migration (v2.2.0)
    logger.info("Running schema migration v2.2.0...")
    run_schema_migration(db)
    
    generator = SeedDataGenerator(db)
    results = {}
    
    # Seed customers
    results['customers'] = generator.seed_customers(30)
    
    # Seed suppliers
    results['suppliers'] = generator.seed_suppliers(12)
    
    # Seed products
    results['products'] = generator.seed_products()
    
    # Seed materials
    results['materials'] = generator.seed_materials()
    
    # Seed finished goods (requires products to exist)
    results['finished_goods'] = generator.seed_finished_goods()
    
    # Seed bill of materials (requires products and materials to exist)
    results['bom'] = generator.seed_bom()
    
    # Seed inventory locations
    results['locations'] = generator.seed_inventory_locations()
    
    # Seed inventory balances (requires materials, finished goods, and locations to exist)
    results['balances'] = generator.seed_inventory_balances()
    
    # Run multi-site seed data (consolidated — inline SQL, no external imports)
    logger.info("Running consolidated multi-site seed data...")
    results['multisite'] = _run_multisite_seed(db)

    logger.info(f"Seed data loading complete: {results}")
    return results


def _temporary_handler_modules() -> Dict[str, Optional[Any]]:
    """Install local stand-ins for optional handler dependencies."""
    import sys
    import types

    originals = {}
    for module_name in ('boto3',):
        if module_name not in sys.modules:
            sys.modules[module_name] = types.ModuleType(module_name)
            originals[module_name] = None
        else:
            originals[module_name] = sys.modules[module_name]
    return originals


def _restore_handler_modules(originals: Dict[str, Optional[Any]]) -> None:
    """Restore optional handler dependencies to their pre-load state."""
    import sys

    for module_name, original in originals.items():
        if original is None:
            del sys.modules[module_name]
        else:
            sys.modules[module_name] = original


def _load_multisite_handler(handler_path: str) -> Any:
    """Load the static sibling handler with temporary local dependencies."""
    import importlib.util

    originals = _temporary_handler_modules()
    try:
        spec = importlib.util.spec_from_file_location('db_handler', handler_path)
        if spec is None or spec.loader is None:
            raise ImportError(
                f"Unable to load db_handler from {handler_path}: "
                "module spec or loader unavailable")
        handler_module = importlib.util.module_from_spec(spec)
        setattr(handler_module, 'logger', logger)
        setattr(handler_module, 'random', random)
        spec.loader.exec_module(handler_module)
        return handler_module
    finally:
        _restore_handler_modules(originals)


def _apply_multisite_seed(database: DatabaseManager, handler_module: Any) -> int:
    """Apply schema and seed operations exported by the loaded handler."""
    with database.connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(handler_module.get_schema_sql_part5())
            logger.info("✅ Multi-site schema tables created")
            handler_module.seed_suppliers(cursor)
            handler_module.seed_products(cursor)
            handler_module.seed_inventory_locations(cursor)
            handler_module.seed_multisite_materials(cursor)
            handler_module.seed_multisite_finished_goods(cursor)
            handler_module.seed_sites(cursor)
            handler_module.seed_product_site_routing(cursor)
            handler_module.seed_production_lines(cursor)
            handler_module.seed_site_product_config(cursor)
            handler_module.seed_multisite_bom(cursor)
            handler_module.seed_multisite_raw_inventory(cursor)
            handler_module.seed_multisite_fg_inventory(cursor)
            handler_module.seed_simulation_config(cursor)
            handler_module.seed_item_suppliers(cursor)
            handler_module.seed_material_lots_all_sites(cursor)
            handler_module.seed_additional_products(cursor)
            handler_module.seed_machining_site(cursor)
            handler_module.seed_mes_equipment(cursor)
            cursor.execute(handler_module.get_views_sql())
            cursor.execute(handler_module.get_machining_views_sql())
            logger.info("✅ All multi-site seed data applied")
            conn.commit()
            return 1
        except Exception as e:
            logger.error(f"Multi-site seed failed: {e}")
            conn.rollback()
            raise
        finally:
            cursor.close()


def _run_multisite_seed(db: Optional[DatabaseManager] = None) -> int:
    """Run consolidated multi-site seed data from the static sibling handler."""
    import os

    database = db or get_database()
    handler_path = os.path.join(
        os.path.dirname(__file__), '..', 'lambda-db-init', 'handler.py')
    if not os.path.exists(handler_path):
        logger.warning(
            f"handler.py not found at {handler_path}, skipping multi-site seed")
        return 0

    handler_module = _load_multisite_handler(handler_path)
    return _apply_multisite_seed(database, handler_module)


if __name__ == '__main__':
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Load seed data
    results = seed_all_data()
    print(f"Seed data loaded: {results}")
