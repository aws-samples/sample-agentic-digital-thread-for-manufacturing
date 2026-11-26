# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Order Simulator for ERP Simulation System.

Generates realistic customer orders whose product mix matches each
customer's segment (segment -> categories live in ``segments``).
"""

import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import get_config
from .utils import generate_order_number, calculate_tax
from .logging_config import get_logger
from .base_simulator import BaseSimulator
from . import segments

logger = get_logger(__name__)


class OrderSimulator(BaseSimulator):
    """
    Simulates customer order generation.
    
    Creates realistic customer orders with random product mixes,
    quantities based on demand patterns, and proper pricing.
    """
    
    def __init__(self):
        """Initialize the order simulator."""
        self.config = get_config()
        logger.info("Order simulator initialized")
    
    def generate_orders(self, conn, cycle_id: str) -> Dict[str, Any]:
        """
        Generate random customer orders for this simulation cycle.
        
        Args:
            conn: Database connection.
            cycle_id: Unique identifier for this simulation cycle.
            
        Returns:
            Dictionary with generation statistics.
        """
        cursor = conn.cursor()
        
        try:
            inputs = self._load_order_inputs(cursor)
            if inputs is None:
                return {'orders_created': 0}
            
            orders_created = 0
            for _ in range(inputs['num_orders']):
                orders_created += self._create_order(cursor, inputs)
            
            logger.info(f"Generated {orders_created} customer orders (cycle: {cycle_id})")
            
            return {
                'orders_created': orders_created,
                'cycle_id': cycle_id
            }
        
        except Exception as e:
            logger.error(f"Order generation failed: {e}", exc_info=True)
            raise
        finally:
            cursor.close()
    
    def _load_order_inputs(self, cursor) -> Optional[Dict[str, Any]]:
        """Read the cycle's order count, customers, products, weights and quantity range.
        
        Returns None (after logging why) when this cycle creates no orders.
        """
        # Determine number of orders to create (configurable)
        orders_min = self._load_config_value(cursor, 'orders_per_cycle_min', 4, 'int')
        orders_max = self._load_config_value(cursor, 'orders_per_cycle_max', 8, 'int')
        num_orders = random.randint(orders_min, orders_max)  # nosec B311 - synthetic data generation, not security context
        
        if num_orders == 0:
            logger.info("No orders to generate this cycle")
            return None
        
        customers = self._fetch_active_customers(cursor)
        if not customers:
            logger.warning("No active customers found, cannot generate orders")
            return None
        
        products = self._fetch_active_products(cursor)
        if not products:
            logger.warning("No active products found, cannot generate orders")
            return None
        
        weight_map = self._load_weight_map(cursor)
        
        # Load order quantity range
        order_qty_min = self._load_config_value(cursor, 'order_quantity_min', 50, 'int')
        order_qty_max = self._load_config_value(cursor, 'order_quantity_max', 200, 'int')
        
        return {
            'num_orders': num_orders,
            'customers': customers,
            'products': products,
            'weight_map': weight_map,
            'qty_range': (order_qty_min, order_qty_max),
        }
    
    @staticmethod
    def _fetch_active_customers(cursor) -> List[tuple]:
        """Active customers with their segment (NULL segment reads as BEVERAGE)."""
        cursor.execute("""
            SELECT customer_id, customer_code, company_name, payment_terms,
                   COALESCE(customer_segment, 'BEVERAGE') AS customer_segment
            FROM customers
            WHERE is_active = TRUE
        """)
        return cursor.fetchall()
    
    @staticmethod
    def _fetch_active_products(cursor) -> List[tuple]:
        """Active products with their category, in product_id order."""
        cursor.execute("""
            SELECT product_id, sku, product_name, list_price, category
            FROM products
            WHERE is_active = TRUE
            ORDER BY product_id
        """)
        return cursor.fetchall()
    
    def _load_weight_map(self, cursor) -> Dict[str, float]:
        """Product weights for weighted selection, keyed by SKU flavor (default 1.0)."""
        return {
            'COLA': self._load_config_value(cursor, 'product_cola_weight', 1.0, 'float'),
            'ORANGE': self._load_config_value(cursor, 'product_orange_weight', 1.0, 'float'),
            'LEMON': self._load_config_value(cursor, 'product_lemon_weight', 1.0, 'float'),
            'GRAPE': self._load_config_value(cursor, 'product_grape_weight', 1.0, 'float')
        }
    
    @staticmethod
    def _segment_products(
        products: Sequence[tuple], segment: str, weight_map: Dict[str, float]
    ) -> Tuple[List[tuple], List[float]]:
        """Products a customer of ``segment`` may order, with their selection weights.
        
        Falls back to all products when none match the segment's categories.
        """
        allowed_cats = segments.allowed_categories(segment)
        segment_products = [p for p in products if p[4] in allowed_cats]
        if not segment_products:
            segment_products = list(products)  # fallback to all products
        
        weights = []
        for product in segment_products:
            sku = product[1].upper()
            # Extract flavor from SKU (e.g., "COLA-500ML" -> "COLA")
            flavor = sku.split('-')[0] if '-' in sku else sku
            weights.append(weight_map.get(flavor, 1.0))
        return segment_products, weights
    
    def _create_order(self, cursor, inputs: Dict[str, Any]) -> int:
        """Create one order with its line items; returns 1 when created, else 0."""
        try:
            # Select random customer
            customer = random.choice(inputs['customers'])  # nosec B311 - synthetic data generation, not security context
            customer_id = customer[0]
            customer_segment = customer[4]
            
            segment_products, seg_weights = self._segment_products(
                inputs['products'], customer_segment, inputs['weight_map']
            )
            
            inserted = self._insert_order(cursor, customer_id)
            if inserted is None:
                return 0
            order_id, order_number = inserted
            
            num_line_items, subtotal = self._insert_line_items(
                cursor, order_id, segment_products, seg_weights, inputs['qty_range']
            )
            total_amount = self._finalize_totals(cursor, order_id, subtotal)
            
            logger.debug(
                f"Created order {order_number} for customer {customer_id}: "
                f"{num_line_items} items, total ${total_amount:.2f}"
            )
            return 1
        
        except Exception as e:
            logger.error(f"Failed to create order: {e}", exc_info=True)
            # Continue with next order
            return 0
    
    @staticmethod
    def _insert_order(cursor, customer_id) -> Optional[Tuple[Any, str]]:
        """Insert a PENDING order header; returns (order_id, order_number) or None on a duplicate."""
        order_number = generate_order_number(cursor)
        order_date = datetime.now()
        requested_delivery_date = order_date + timedelta(days=random.randint(7, 21))  # nosec B311 - synthetic data generation, not security context
        
        # Create order record — use savepoint so a duplicate key doesn't abort the whole transaction
        cursor.execute("SAVEPOINT order_insert")
        cursor.execute("""
            INSERT INTO customer_orders (
                order_number, customer_id, order_date,
                requested_delivery_date, status
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (order_number) DO NOTHING
            RETURNING order_id
        """, (
            order_number,
            customer_id,
            order_date,
            requested_delivery_date,
            'PENDING'
        ))
        
        row = cursor.fetchone()
        if row is None:
            # Duplicate order number — skip silently
            cursor.execute("ROLLBACK TO SAVEPOINT order_insert")
            cursor.execute("RELEASE SAVEPOINT order_insert")
            return None
        cursor.execute("RELEASE SAVEPOINT order_insert")
        return row[0], order_number
    
    @staticmethod
    def _insert_line_items(
        cursor, order_id, products: Sequence[tuple], weights: Sequence[float],
        qty_range: Tuple[int, int],
    ) -> Tuple[int, float]:
        """Insert 1-5 weighted-random line items; returns (line count, subtotal)."""
        num_line_items = random.randint(1, 5)  # nosec B311 - synthetic data generation, not security context
        subtotal = 0.0
        
        for line_number in range(1, num_line_items + 1):
            product = random.choices(products, weights=weights, k=1)[0]  # nosec B311 - synthetic data generation, not security context
            product_id = product[0]
            unit_price = float(product[3])
            
            # Quantity by product type (500ml cases, 1L cases, machined parts)
            qty_lo, qty_hi = segments.quantity_range(product[1], *qty_range)
            quantity = random.randint(qty_lo, qty_hi)  # nosec B311 - synthetic data generation, not security context
            subtotal += quantity * unit_price
            
            cursor.execute("""
                INSERT INTO order_line_items (
                    order_id, line_number, product_id,
                    quantity, unit_price
                )
                VALUES (%s, %s, %s, %s, %s)
            """, (
                order_id,
                line_number,
                product_id,
                quantity,
                unit_price
            ))
        return num_line_items, subtotal
    
    @staticmethod
    def _finalize_totals(cursor, order_id, subtotal: float) -> float:
        """Write subtotal, tax, flat shipping and total onto the order; returns the total."""
        tax_amount = calculate_tax(subtotal)
        shipping_amount = 50.00  # Flat shipping rate
        total_amount = subtotal + tax_amount + shipping_amount
        
        cursor.execute("""
            UPDATE customer_orders
            SET subtotal = %s,
                tax_amount = %s,
                shipping_amount = %s,
                total_amount = %s
            WHERE order_id = %s
        """, (
            subtotal,
            tax_amount,
            shipping_amount,
            total_amount,
            order_id
        ))
        return total_amount
