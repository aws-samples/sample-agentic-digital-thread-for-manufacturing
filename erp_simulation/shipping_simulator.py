# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Shipping Simulator for ERP Simulation System.

Manages shipment creation and inventory reduction for order fulfillment.
"""

import random
from datetime import datetime, timedelta
from typing import Dict, Any

from .config import get_config
from .utils import (HISTORY_RUN_NOTE, MAX_ROWS_PER_CYCLE, generate_tracking_number,
                    insert_numbered)
from .logging_config import get_logger
from .base_simulator import BaseSimulator
from .production_simulator import FG_LOCATION_CODES

logger = get_logger(__name__)


def _fg_draw_location(cursor, finished_item_id, quantity):
    """(location_id, location_code, on_hand) to ship from, across every FG location.

    Prefers the fullest location that covers ``quantity``; otherwise the fullest
    one at all (the balance may go negative). None when the item has no FG balance.
    """
    cursor.execute("""
        SELECT ib.location_id, il.location_code, ib.quantity_on_hand
        FROM inventory_balances ib
        JOIN inventory_locations il ON ib.location_id = il.location_id
        WHERE ib.item_id = %s
          AND il.location_code = ANY(%s)
          AND ib.quantity_on_hand >= %s
        ORDER BY ib.quantity_on_hand DESC
        LIMIT 1
    """, (finished_item_id, list(FG_LOCATION_CODES), quantity))
    row = cursor.fetchone()
    if row:
        return row
    cursor.execute("""
        SELECT ib.location_id, il.location_code, ib.quantity_on_hand
        FROM inventory_balances ib
        JOIN inventory_locations il ON ib.location_id = il.location_id
        WHERE ib.item_id = %s
          AND il.location_code = ANY(%s)
        ORDER BY ib.quantity_on_hand DESC
        LIMIT 1
    """, (finished_item_id, list(FG_LOCATION_CODES)))
    return cursor.fetchone()


def _consume_fg(cursor, finished_item_id, fg_location_id, quantity, shipment_id) -> None:
    """Post the shipment CONSUMPTION transaction and reduce the FG balance."""
    cursor.execute("""
        INSERT INTO inventory_transactions (
            item_id, location_id, transaction_type,
            quantity, reference_type, reference_id,
            transaction_date
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (finished_item_id, fg_location_id, 'CONSUMPTION', -quantity, 'SHIPMENT',
          shipment_id, datetime.now()))
    cursor.execute("""
        UPDATE inventory_balances
        SET quantity_on_hand = quantity_on_hand - %s,
            last_transaction_date = %s
        WHERE item_id = %s AND location_id = %s
    """, (quantity, datetime.now(), finished_item_id, fg_location_id))


class ShippingSimulator(BaseSimulator):
    """
    Simulates shipping and order fulfillment.
    
    Creates shipments for ready orders, generates shipment items,
    reduces inventory, and updates order status.
    """
    
    def __init__(self):
        """Initialize the shipping simulator."""
        self.config = get_config()
        # Carrier list with configurable weights
        self.carriers = ['AnyCompany Freight', 'AnyCompany Logistics', 'AnyCompany Parcel']
        logger.info("Shipping simulator initialized")
    
    def create_shipments(self, conn, cycle_id: str) -> Dict[str, Any]:
        """
        Create shipments for orders that are ready to ship.
        
        Args:
            conn: Database connection.
            cycle_id: Unique identifier for this simulation cycle.
            
        Returns:
            Dictionary with shipment creation statistics.
        """
        cursor = conn.cursor()
        self._batch_id_memo = {}  # traceable run per product, this batch only

        try:
            # Oldest ready orders first, at most MAX_ROWS_PER_CYCLE: a backlog
            # drains over cycles (ADR-0090); SKIP LOCKED gives an overlapping
            # invocation a disjoint batch.
            cursor.execute("""
                SELECT co.order_id, co.order_number, co.customer_id
                FROM customer_orders co
                WHERE co.status = 'READY_TO_SHIP'
                ORDER BY co.order_id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            """, (MAX_ROWS_PER_CYCLE,))
            
            orders_to_ship = cursor.fetchall()
            
            if not orders_to_ship:
                logger.debug("No orders ready to ship")
                return {'shipments_created': 0}
            
            logger.info(f"Found {len(orders_to_ship)} orders ready to ship (batch limit {MAX_ROWS_PER_CYCLE})")
            shipments_created = sum(
                1 for order_id, order_number, customer_id in orders_to_ship
                if self._ship_order(cursor, order_id, order_number, customer_id)
            )
            logger.info(f"Created {shipments_created} shipments (cycle: {cycle_id})")
            
            return {
                'shipments_created': shipments_created,
                'cycle_id': cycle_id
            }
        
        except Exception as e:
            logger.error(f"Shipment creation failed: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def _ship_order(self, cursor, order_id, order_number, customer_id) -> bool:
        """Ship one order under a savepoint; False (rolled back, logged) on any error."""
        try:
            cursor.execute("SAVEPOINT ship_order")
            inserted = self._insert_shipment(cursor, order_id, customer_id)
            if inserted is None:  # the number was skipped (logged): retry next cycle
                cursor.execute("RELEASE SAVEPOINT ship_order")
                return False
            shipment_number, shipment_id, carrier = inserted
            cursor.execute("""
                SELECT oli.line_item_id, oli.product_id, oli.quantity, p.sku
                FROM order_line_items oli
                JOIN products p ON oli.product_id = p.product_id
                WHERE oli.order_id = %s
            """, (order_id,))
            line_items = cursor.fetchall()
            # Create shipment items and reduce inventory
            for line_item_id, product_id, quantity, sku in line_items:
                self._ship_line_item(cursor, shipment_id, line_item_id, product_id, quantity, sku)
            cursor.execute("""
                UPDATE customer_orders
                SET status = 'SHIPPED'
                WHERE order_id = %s
            """, (order_id,))
            logger.debug(
                f"Created shipment {shipment_number} for order {order_number}: "
                f"{len(line_items)} items, carrier {carrier}"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to create shipment for order {order_number}: {e}", exc_info=True)
            # Roll back to savepoint so transaction stays alive for next order
            try:
                cursor.execute("ROLLBACK TO SAVEPOINT ship_order")
                cursor.execute("RELEASE SAVEPOINT ship_order")
            except Exception:  # nosec B110 - intentional fallback, no recovery action needed
                pass
            return False

    def _insert_shipment(self, cursor, order_id, customer_id):
        """Insert a PREPARING shipment (weighted carrier, configurable delay); returns
        (number, id, carrier), or None when the number was skipped (ADR-0090)."""
        tracking_number = generate_tracking_number()
        carrier_weights = [
            self._load_config_value(cursor, 'shipping_carrier_freight_weight', 1.0, 'float'),
            self._load_config_value(cursor, 'shipping_carrier_logistics_weight', 1.0, 'float'),
            self._load_config_value(cursor, 'shipping_carrier_parcel_weight', 1.0, 'float')
        ]
        carrier = random.choices(self.carriers, weights=carrier_weights, k=1)[0]  # nosec B311 - synthetic data generation, not security context
        shipment_date = datetime.now()
        shipping_min = self._load_config_value(cursor, 'shipping_delay_min', 1, 'int')
        shipping_max = self._load_config_value(cursor, 'shipping_delay_max', 3, 'int')
        shipping_days = random.randint(shipping_min, shipping_max)  # nosec B311 - synthetic data generation, not security context
        estimated_delivery_date = shipment_date + timedelta(days=shipping_days)
        inserted = insert_numbered(cursor, 'SHIP', """
            INSERT INTO shipments (
                shipment_number, order_id, customer_id,
                shipment_date, carrier, tracking_number,
                estimated_delivery_date, status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING shipment_id
        """, lambda number: (number, order_id, customer_id, shipment_date, carrier,
                             tracking_number, estimated_delivery_date, 'PREPARING'),
            what=f"shipment for order {order_id}")
        if inserted is None:
            return None
        shipment_number, (shipment_id,) = inserted
        return shipment_number, shipment_id, carrier

    def _ship_line_item(self, cursor, shipment_id, line_item_id, product_id, quantity, sku) -> None:
        """Record the shipment item (with traceable batch) and draw its FG stock down."""
        batch_id = self._batch_id_for(cursor, product_id, sku)
        cursor.execute("""
            INSERT INTO shipment_items (
                shipment_id, line_item_id, product_id, quantity_shipped, batch_id
            )
            VALUES (%s, %s, %s, %s, %s)
        """, (shipment_id, line_item_id, product_id, quantity, batch_id))
        cursor.execute("""
            SELECT item_id
            FROM inventory_items
            WHERE product_id = %s
              AND item_type = 'FINISHED_GOOD'
            LIMIT 1
        """, (product_id,))
        finished_item_row = cursor.fetchone()
        if not finished_item_row:
            return
        finished_item_id = finished_item_row[0]
        fg_location_row = _fg_draw_location(cursor, finished_item_id, quantity)
        if not fg_location_row:
            logger.warning(
                f"No FG inventory location found for item {finished_item_id} (SKU: {sku}), skipping inventory reduction"
            )
            return
        _consume_fg(cursor, finished_item_id, fg_location_row[0], quantity, shipment_id)
        logger.debug(f"Reduced inventory for {sku} at {fg_location_row[1]} by {quantity}")

    def _batch_id_for(self, cursor, product_id, sku):
        """:meth:`_traceable_batch_id` once per PRODUCT per batch, not per line
        item (the batch is one transaction: no run completes inside it)."""
        memo = self.__dict__.setdefault('_batch_id_memo', {})
        if product_id not in memo:
            memo[product_id] = self._traceable_batch_id(cursor, product_id, sku)
        return memo[product_id]

    def _traceable_batch_id(self, cursor, product_id, sku):
        """Run number of the latest COMPLETED run with lot usage (traceable), else None.

        A live-story history run (``notes`` = :data:`utils.HISTORY_RUN_NOTE`)
        is never chosen: the history plan records where each of those batches
        went, and on a fresh account the newest completed run of a story part
        IS a history run until the first live run finishes, so the first live
        shipments used to land on a story batch and grow its lot's customer
        list past the plan (a rehearsal account, 2026-10-02: module 2's lot total
        read $38,760 instead of $37,400).

        Served by ``idx_production_runs_traceable`` (hot_path_indexes.py): the
        ORDER BY matches it, so this walks newest-first and stops at the first
        traceable run instead of reading and sorting every run of the product."""
        cursor.execute("""
            SELECT pr.run_number
            FROM production_runs pr
            WHERE pr.product_id = %s
              AND pr.status = 'COMPLETED'
              AND pr.notes IS DISTINCT FROM %s
              AND EXISTS (
                  SELECT 1 FROM lot_production_usage lpu
                  WHERE lpu.production_run_id = pr.run_id
              )
            ORDER BY pr.actual_end_time DESC NULLS LAST,
                     pr.updated_at DESC NULLS LAST
            LIMIT 1
        """, (product_id, HISTORY_RUN_NOTE))
        batch_id_row = cursor.fetchone()
        if batch_id_row:
            return batch_id_row[0]
        logger.debug(
            f"No traceable production run found for product {sku} "
            f"(product_id={product_id}); batch_id will be NULL on shipment item"
        )
        return None
