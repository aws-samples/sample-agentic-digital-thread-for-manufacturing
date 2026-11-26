# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
State Advancer for ERP Simulation System.

Manages entity lifecycle state transitions with realistic timing.
"""

from typing import Dict, Any
import uuid

from .config import get_config
from .logging_config import get_logger
from . import inventory_draw
from .production_simulator import (
    DEFAULT_FG_LOCATION, FG_LOCATION_CODES, RAW_LOCATION_CODES, SITE_FG_LOCATION,
    record_planned_materials,
)
from .utils import (MAX_ROWS_PER_CYCLE, STORY_ORDER_NUMBER_RE, STORY_SHIPMENT_NUMBER_RE,
                    insert_numbered, savepoint)

logger = get_logger(__name__)

# Per FG line item: (product_id, quantity, available_inventory) summed over every
# FG location. The allowlist is bound (``= ANY(%s)``); a product with NO balance
# rows survives as a zero-stock line via the LEFT JOIN null row.
_LINE_ITEM_AVAILABILITY_SQL = """
    SELECT oli.product_id, oli.quantity,
           COALESCE(SUM(ib.quantity_on_hand), 0) as available_inventory
    FROM order_line_items oli
    JOIN inventory_items ii ON oli.product_id = ii.product_id AND ii.item_type = 'FINISHED_GOOD'
    LEFT JOIN inventory_balances ib ON ii.item_id = ib.item_id
    LEFT JOIN inventory_locations il ON ib.location_id = il.location_id
    WHERE oli.order_id = %s
      AND (il.location_code = ANY(%s) OR ib.item_id IS NULL)
    GROUP BY oli.product_id, oli.quantity
"""

# Every order-status read takes one bounded batch per cycle (``LIMIT``, bound
# to ``MAX_ROWS_PER_CYCLE``) in ``order_id`` order, which the
# ``(status, order_id)`` hot-path index serves without reading the rest of the
# backlog; the remaining orders wait for later cycles. Every CONFIRMED order a
# batch reads leaves CONFIRMED, so that read always moves on.
_CONFIRMED_ORDERS_SQL = """
    SELECT co.order_id, co.order_number
    FROM customer_orders co
    WHERE co.status = 'CONFIRMED'
      AND co.updated_at <= NOW() - INTERVAL '2 minutes'
    ORDER BY co.order_id
    LIMIT %s
"""

# A BACKORDERED order that is still short stays BACKORDERED, so a batch taken
# from the start of the backlog every cycle could keep re-reading the same
# short orders and never reach one that is now in stock. Each read instead
# starts at a fresh random point in ``order_id`` (UUID) order and wraps round
# to the start: every backordered order is read with probability
# min(1, MAX_ROWS_PER_CYCLE / backlog) on every cycle, whatever its neighbours.
_BACKORDERED_FROM_SQL = """
    SELECT co.order_id, co.order_number
    FROM customer_orders co
    WHERE co.status = 'BACKORDERED'
      AND co.order_id >= %s::uuid
    ORDER BY co.order_id
    LIMIT %s
"""

_BACKORDERED_BEFORE_SQL = """
    SELECT co.order_id, co.order_number
    FROM customer_orders co
    WHERE co.status = 'BACKORDERED'
      AND co.order_id < %s::uuid
    ORDER BY co.order_id
    LIMIT %s
"""


def _backordered_batch(cursor, start: str) -> list:
    """At most ``MAX_ROWS_PER_CYCLE`` BACKORDERED orders, from ``start`` in
    ``order_id`` order, wrapping round to the lowest ids."""
    cursor.execute(_BACKORDERED_FROM_SQL, (start, MAX_ROWS_PER_CYCLE))
    rows = list(cursor.fetchall())
    if len(rows) < MAX_ROWS_PER_CYCLE:
        cursor.execute(_BACKORDERED_BEFORE_SQL, (start, MAX_ROWS_PER_CYCLE - len(rows)))
        rows.extend(cursor.fetchall())
    return rows

# The pure status steps (PENDING orders, shipments, invoices) move one bounded
# batch per cycle: at most ``MAX_ROWS_PER_CYCLE`` rows per status, the rest wait
# for later cycles. A stalled cycle lets such a backlog grow, and an unbounded
# UPDATE ... RETURNING then reads all of it at once (on a live account, unbounded
# backlog reads timed every cycle out at 236,000 rows and ran the Lambda out of
# memory at 464,000). Every row a batch takes leaves its status, so taking the
# oldest first never starves the rows behind it. The batch is chosen in an
# InitPlan (``= ANY(ARRAY(...))``) that walks a ``(status, <order column>)``
# index (hot_path_indexes.py) and stops at the bound; the outer UPDATE then
# touches only those rows by primary key, and its own status check keeps a row
# another writer already moved.
_PENDING_TO_CONFIRMED_SQL = """
    UPDATE customer_orders
    SET status = 'CONFIRMED',
        updated_at = NOW()
    WHERE status = 'PENDING'
      AND order_id = ANY(ARRAY(
          SELECT co.order_id
          FROM customer_orders co
          WHERE co.status = 'PENDING'
            AND co.order_date <= NOW() - INTERVAL '1 minute'
          ORDER BY co.order_id
          LIMIT %s))
    RETURNING order_id, order_number
"""

_SENT_TO_OVERDUE_SQL = """
    UPDATE invoices
    SET status = 'OVERDUE',
        updated_at = NOW()
    WHERE status = 'SENT'
      AND invoice_id = ANY(ARRAY(
          SELECT i.invoice_id
          FROM invoices i
          WHERE i.status = 'SENT'
            AND i.due_date < NOW()
            AND i.balance_due > 0
          ORDER BY i.due_date
          LIMIT %s))
    RETURNING invoice_id, invoice_number
"""

# (from status, to status, the timestamp the step waits on, the wait, result
# key), in lifecycle order. Times are demo-scaled (packing was 15 minutes,
# transit 1 day, delivery the estimated date).
_SHIPMENT_STEPS = (
    ('PREPARING', 'PICKED', 'shipment_date', '1 minute', 'shipping_preparing_to_picked'),
    ('PICKED', 'PACKED', 'updated_at', '1 minute', 'shipping_picked_to_packed'),
    ('PACKED', 'SHIPPED', 'updated_at', '1 minute', 'shipping_packed_to_shipped'),
    ('SHIPPED', 'IN_TRANSIT', 'updated_at', '1 minute', 'shipping_shipped_to_in_transit'),
    ('IN_TRANSIT', 'DELIVERED', 'updated_at', '2 minutes', 'shipping_in_transit_to_delivered'),
)


def _shipment_step_sql(source: str, target: str, since: str, wait: str) -> str:
    """The bounded UPDATE for one shipment transition, oldest ``since`` first.
    Every interpolated value is a ``_SHIPMENT_STEPS`` constant.

    Story-owned shipments (``shipment_number`` in the story space, bound
    ``%s`` pattern) are excluded in the inner SELECT, BEFORE the LIMIT, so the
    ladder never advances a live-story shipment and the batch still fills with
    eligible rows. The live story writes its shipments terminal (SHIPPED or
    DELIVERED, never through the ladder); leaving its deliberately-SHIPPED
    shipment alone keeps the ADR-0085 "two DELIVERED, one SHIPPED" contract."""
    delivered = " actual_delivery_date = NOW()," if target == 'DELIVERED' else ""
    return f"""
    UPDATE shipments
    SET status = '{target}',{delivered}
        updated_at = NOW()
    WHERE status = '{source}'
      AND shipment_id = ANY(ARRAY(
          SELECT s.shipment_id
          FROM shipments s
          WHERE s.status = '{source}'
            AND s.{since} <= NOW() - INTERVAL '{wait}'
            AND s.shipment_number !~ %s
          ORDER BY s.{since}
          LIMIT %s))
    RETURNING shipment_id, shipment_number
"""  # nosec B608 - code constants only; the pattern and row limit are bound parameters


_SHIPMENT_STEP_SQL = {step[0]: _shipment_step_sql(*step[:4]) for step in _SHIPMENT_STEPS}

# Catch up every SHIPPED, NON-story order whose shipments are all delivered to
# DELIVERED, one bounded batch per cycle, oldest ORDER first: by ``order_date``
# (an order with no date counts as oldest, so it can never be passed over), then
# ``order_id`` to break ties. Not by ``order_id`` alone: ids are random UUIDs, so
# while the batch is full, newly delivered orders with low ids would keep taking
# the places of older ones. ``idx_customer_orders_status_date (status,
# order_date NULLS FIRST, order_id)`` from hot_path_indexes.py hands the SHIPPED
# subset over in exactly this order, so the read never sorts and never touches
# other statuses. This covers both the orders a cycle
# just delivered AND any pre-existing SHIPPED-but-fully-delivered backlog (e.g.
# orders left SHIPPED before this step existed), so the backlog drains instead
# of stranding. Eligibility is computed in the InitPlan BEFORE the LIMIT
# (``= ANY(ARRAY(... LIMIT))``, the same bounded shape the shipment/invoice
# steps use), so an in-flight order can never starve the batch, and every row
# taken leaves SHIPPED so nothing is re-read. A story-owned order
# (``order_number`` in the story space, bound ``%s`` pattern) is excluded by
# number, not by status: the live story keeps its deliberately-SHIPPED order
# SHIPPED even though the ladder no longer touches its shipment, AND an account
# whose story shipment was already walked to DELIVERED by the pre-fix ladder is
# left SHIPPED (the story re-plan / reset is the only thing that moves story
# rows). An order needs at least one shipment (``EXISTS``) so a SHIPPED order
# with none is never delivered. ``updated_at = NOW()`` like the other order
# transitions.
_SHIPPED_ORDERS_TO_DELIVERED_SQL = """
    UPDATE customer_orders
    SET status = 'DELIVERED',
        updated_at = NOW()
    WHERE order_id = ANY(ARRAY(
        SELECT co.order_id
        FROM customer_orders co
        WHERE co.status = 'SHIPPED'
          AND co.order_number !~ %s
          AND EXISTS (SELECT 1 FROM shipments s WHERE s.order_id = co.order_id)
          AND NOT EXISTS (
              SELECT 1 FROM shipments s2
              WHERE s2.order_id = co.order_id
                AND s2.status <> 'DELIVERED')
        ORDER BY co.order_date NULLS FIRST, co.order_id
        LIMIT %s))
    RETURNING order_id
"""

_MARK_READY_SQL = """
    UPDATE customer_orders
    SET status = 'READY_TO_SHIP', updated_at = NOW()
    WHERE order_id = %s
"""

_MARK_BACKORDERED_SQL = """
    UPDATE customer_orders
    SET status = 'BACKORDERED', updated_at = NOW()
    WHERE order_id = %s
"""


def _line_item_availability(cursor, order_id):
    """FG availability rows for ``order_id`` across ``FG_LOCATION_CODES``."""
    cursor.execute(_LINE_ITEM_AVAILABILITY_SQL, (order_id, list(FG_LOCATION_CODES)))
    return cursor.fetchall()


def _all_available(line_items) -> bool:
    """True when every (product_id, quantity, available) row is fully in stock (vacuously for none)."""
    return all(available >= quantity for _, quantity, available in line_items)



class StateAdvancer:
    """
    Advances entity states through their lifecycle.
    
    Manages state transitions for orders, shipments, and invoices
    with realistic timing delays.
    """
    
    def __init__(self):
        """Initialize the state advancer."""
        self.config = get_config()
        logger.info("State advancer initialized")

    def _backorder_scan_start(self) -> str:
        """A fresh random point in ``order_id`` (UUID) order where this cycle's
        backorder read starts (``_backordered_batch``)."""
        return str(uuid.uuid4())
    
    def advance_order_states(self, conn, cycle_id: str) -> Dict[str, Any]:
        """
        Advance orders through their lifecycle states.
        
        PENDING → CONFIRMED (when materials available)

        At most ``MAX_ROWS_PER_CYCLE`` orders per cycle, in ``order_id`` order
        like the other order-status reads (``_PENDING_TO_CONFIRMED_SQL``).
        
        Args:
            conn: Database connection.
            cycle_id: Unique identifier for this simulation cycle.
            
        Returns:
            Dictionary with state transition statistics.
        """
        cursor = conn.cursor()
        transitions = 0
        
        try:
            # PENDING → CONFIRMED
            # Confirm orders that are at least 1 minute old (simulating order review)
            cursor.execute(_PENDING_TO_CONFIRMED_SQL, (MAX_ROWS_PER_CYCLE,))
            
            confirmed_orders = cursor.fetchall()
            transitions += len(confirmed_orders)
            
            if confirmed_orders:
                logger.debug(f"Confirmed {len(confirmed_orders)} orders")
            
            return {
                'transitions': transitions,
                'orders_pending_to_confirmed': len(confirmed_orders),
                'cycle_id': cycle_id
            }
        
        except Exception as e:
            logger.error(f"Order state advancement failed: {e}", exc_info=True)
            raise
        finally:
            cursor.close()
    
    def advance_production_states(self, conn, cycle_id: str) -> Dict[str, Any]:
        """
        Advance orders through production-related states.
        
        Orders ship from FINISHED GOODS INVENTORY - not tied to production runs.
        Production runs replenish inventory independently.
        
        CONFIRMED → READY_TO_SHIP (when sufficient FG inventory exists)
        CONFIRMED → BACKORDERED (when insufficient FG inventory)
        BACKORDERED → READY_TO_SHIP (when FG inventory becomes available)
        
        Args:
            conn: Database connection.
            cycle_id: Unique identifier for this simulation cycle.
            
        Returns:
            Dictionary with state transition statistics.
        """
        cursor = conn.cursor()
        try:
            confirmed_to_ready, confirmed_to_backordered = self._resolve_confirmed_orders(cursor)
            backordered_to_ready = self._release_available_backorders(
                cursor, _backordered_batch(cursor, self._backorder_scan_start()), require_lines=False)
            return {
                'transitions': confirmed_to_ready + confirmed_to_backordered + backordered_to_ready,
                'orders_confirmed_to_ready': confirmed_to_ready,
                'orders_confirmed_to_backordered': confirmed_to_backordered,
                'orders_backordered_to_ready': backordered_to_ready,
                'cycle_id': cycle_id
            }
        
        except Exception as e:
            logger.error(f"Production state advancement failed: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def _resolve_confirmed_orders(self, cursor):
        """CONFIRMED → READY_TO_SHIP / BACKORDERED by FG stock; returns (ready, backordered).

        Waits 2 minutes after confirmation so CONFIRMED stays visible for 2 cycles.
        A backorder triggers order-driven production for its short products.
        """
        cursor.execute(_CONFIRMED_ORDERS_SQL, (MAX_ROWS_PER_CYCLE,))
        ready = backordered = 0
        for order_id, order_number in cursor.fetchall():
            line_items = _line_item_availability(cursor, order_id)
            if _all_available(line_items):
                cursor.execute(_MARK_READY_SQL, (order_id,))
                ready += 1
                logger.debug(f"Order {order_number} → READY_TO_SHIP (inventory available)")
            else:
                cursor.execute(_MARK_BACKORDERED_SQL, (order_id,))
                backordered += 1
                logger.debug(f"Order {order_number} → BACKORDERED (insufficient inventory)")
                self._create_production_for_backorder(cursor, order_id, order_number, line_items)
        return ready, backordered

    def _release_available_backorders(self, cursor, orders, require_lines: bool) -> int:
        """BACKORDERED → READY_TO_SHIP for ``orders`` whose line items are now all in stock.

        ``require_lines`` additionally refuses an order with no FG line items.
        Returns the number of orders released.
        """
        released = 0
        for order_id, order_number in orders:
            line_items = _line_item_availability(cursor, order_id)
            if _all_available(line_items) and (line_items or not require_lines):
                cursor.execute(_MARK_READY_SQL, (order_id,))
                released += 1
                logger.debug(f"Backordered order {order_number} → READY_TO_SHIP (inventory now available)")
        return released
    
    def _create_production_for_backorder(self, cursor, order_id: str, order_number: str, line_items: list):
        """
        Create production runs for products that caused a backorder.
        
        This is order-driven production - triggered when an order can't be filled from inventory.
        Any error aborts the remaining line items and is logged, never raised.
        
        Args:
            cursor: Database cursor
            order_id: Order that was backordered
            order_number: Order number for logging
            line_items: List of (product_id, quantity_needed, available_inventory) tuples
        """
        try:
            # For each line item with insufficient inventory, create a production run
            for product_id, quantity_needed, available_inventory in line_items:
                shortage = quantity_needed - available_inventory
                if shortage > 0:
                    with savepoint(cursor, 'backorder_run'):  # a failure never aborts the step
                        self._backorder_run_for_product(cursor, order_id, order_number, product_id, shortage)
        except Exception as e:
            logger.error(f"Failed to create production for backorder: {e}", exc_info=True)

    def _backorder_run_for_product(self, cursor, order_id, order_number, product_id, shortage) -> None:
        """Plan one order-driven run covering ``shortage`` unless one is already open."""
        if self._has_open_production_run(cursor, product_id):
            # There's already a run planned/in progress - don't create another
            logger.debug(f"Existing production run found for product {product_id}, skipping order-specific run")
            return
        cursor.execute("""
            SELECT sku, product_name
            FROM products
            WHERE product_id = %s
        """, (product_id,))
        product_info = cursor.fetchone()
        if not product_info:
            return
        sku = product_info[0]
        # Use a batch size that covers the shortage (minimum batch of 100)
        batch_size = max(shortage, 100)
        site_code = self._routed_site(cursor, product_id, sku)
        if site_code is None:
            return
        location_id = self._backorder_fg_location_id(cursor, site_code, sku)
        if location_id is None:
            return
        inserted = self._insert_backorder_run(
            cursor, product_id, order_id, batch_size, location_id, site_code,
            f"Order-driven production for {order_number} - {sku} (shortage: {shortage})"
        )
        if inserted is None:
            return
        run_number, run_id = inserted
        logger.info(f"Created production run {run_number} for backordered order {order_number}: {sku} qty={batch_size}")
        record_planned_materials(cursor, run_id, product_id, batch_size)

    def _has_open_production_run(self, cursor, product_id) -> bool:
        """True when the product already has a PLANNED/RELEASED/IN_PROGRESS run."""
        cursor.execute("""
            SELECT run_id, planned_quantity
            FROM production_runs
            WHERE product_id = %s
              AND status IN ('PLANNED', 'RELEASED', 'IN_PROGRESS')
            LIMIT 1
        """, (product_id,))
        return cursor.fetchone() is not None

    def _routed_site(self, cursor, product_id, sku):
        """Highest-priority active site routed for the product's category; None (logged) when unrouted."""
        cursor.execute("""
            SELECT s.site_code
            FROM products p
            JOIN product_site_routing psr ON p.category = psr.product_category
            JOIN sites s ON psr.site_id = s.site_id
            WHERE p.product_id = %s
              AND psr.is_active = TRUE
              AND s.is_active = TRUE
            ORDER BY psr.priority ASC
            LIMIT 1
        """, (product_id,))
        site_row = cursor.fetchone()
        if not site_row:
            logger.warning(
                f"No site routing found for product {sku} - skipping backorder production. "
                f"Add routing in product_site_routing table."
            )
            return None
        return site_row[0]

    def _backorder_fg_location_id(self, cursor, site_code, sku):
        """Resolve the site-specific FG location id (shared map); None (logged) when absent."""
        fg_location_code = SITE_FG_LOCATION.get(site_code, DEFAULT_FG_LOCATION)
        cursor.execute("""
            SELECT location_id
            FROM inventory_locations
            WHERE location_code = %s
            LIMIT 1
        """, (fg_location_code,))
        location_row = cursor.fetchone()
        if not location_row:
            logger.warning(f"No FG location found for {fg_location_code} - skipping backorder production for {sku}")
            return None
        return location_row[0]

    def _insert_backorder_run(self, cursor, product_id, order_id, batch_size, location_id, site_code, notes):
        """Insert a PLANNED order-driven run; returns (run_number, run_id), or None
        when the number was skipped (ADR-0090)."""
        inserted = insert_numbered(cursor, 'RUN', """
            INSERT INTO production_runs (
                run_number, product_id, order_id,
                planned_quantity, status,
                location_id, site, notes
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING run_id
        """, lambda number: (number, product_id, order_id, batch_size, 'PLANNED', location_id, site_code, notes),
            what=f"backorder run for order {order_id}")
        return None if inserted is None else (inserted[0], inserted[1][0])
    
    def release_production_runs(self, conn, cycle_id: str) -> Dict[str, Any]:
        """
        Release production runs when materials are available.
        
        PLANNED → RELEASED (when all raw materials available)
        
        This is the critical gate that prevents negative inventory.
        Production runs stay in PLANNED until materials are confirmed available.
        
        Args:
            conn: Database connection.
            cycle_id: Unique identifier for this simulation cycle.
            
        Returns:
            Dictionary with release statistics.
        """
        cursor = conn.cursor()
        transitions = 0
        
        try:
            from .material_checker import MaterialChecker
            material_checker = MaterialChecker()
            
            # Find PLANNED production runs that need material checking
            cursor.execute("""
                SELECT pr.run_id, pr.run_number, pr.product_id, p.sku
                FROM production_runs pr
                JOIN products p ON pr.product_id = p.product_id
                WHERE pr.status = 'PLANNED'
                ORDER BY pr.created_at ASC
            """)
            
            planned_runs = cursor.fetchall()
            
            if not planned_runs:
                logger.debug("No PLANNED production runs to release")
                return {'transitions': 0, 'cycle_id': cycle_id}
            
            logger.info(f"Checking materials for {len(planned_runs)} PLANNED production runs")
            
            for run_id, run_number, product_id, sku in planned_runs:
                try:
                    # Check if all materials are available
                    materials_available, shortages = material_checker.check_material_availability(
                        conn, run_id
                    )
                    
                    if materials_available:
                        # Release the production run
                        cursor.execute("""
                            UPDATE production_runs
                            SET status = 'RELEASED',
                                updated_at = NOW()
                            WHERE run_id = %s
                        """, (run_id,))
                        
                        transitions += 1
                        logger.info(f"Released production run {run_number} for {sku} (materials available)")
                    else:
                        # Log shortages but don't fail - procurement will replenish
                        if shortages:
                            shortage_summary = ", ".join([
                                f"{s['item_code']}: short {s['shortage']:.2f}"
                                for s in shortages[:3]  # Show first 3
                            ])
                            logger.debug(
                                f"Production run {run_number} waiting for materials: {shortage_summary}"
                            )
                
                except Exception as e:
                    logger.error(f"Failed to check materials for run {run_number}: {e}", exc_info=True)
            
            logger.info(f"Released {transitions} production runs (cycle: {cycle_id})")
            
            return {
                'transitions': transitions,
                'production_planned_to_released': transitions,
                'cycle_id': cycle_id
            }
        
        except Exception as e:
            logger.error(f"Production release failed: {e}", exc_info=True)
            raise
        finally:
            cursor.close()
    
    def advance_shipment_states(self, conn, cycle_id: str) -> Dict[str, Any]:
        """
        Advance shipments through their lifecycle states.

        PREPARING → PICKED → PACKED → SHIPPED → IN_TRANSIT → DELIVERED

        Each transition moves at most ``MAX_ROWS_PER_CYCLE`` shipments per
        cycle, oldest first by the timestamp it waits on (``_SHIPMENT_STEPS``).
        The steps run in lifecycle order in one transaction; a shipment moved
        this cycle has ``updated_at`` = now, so it moves on next cycle at the
        earliest.

        After the shipment steps, every SHIPPED customer order whose shipments
        are all delivered moves to DELIVERED, one bounded batch per cycle
        (``_deliver_orders_of``): this drains the current cycle's deliveries
        AND any pre-existing SHIPPED-but-fully-delivered backlog. Story-owned
        orders and shipments are excluded by number, so the live story's rows
        are never advanced here. The count is reported under
        ``shipping_orders_delivered``.

        Args:
            conn: Database connection.
            cycle_id: Unique identifier for this simulation cycle.

        Returns:
            Dictionary with state transition statistics.
        """
        cursor = conn.cursor()
        result: Dict[str, Any] = {'transitions': 0}
        try:
            for source, target, _since, _wait, key in _SHIPMENT_STEPS:
                cursor.execute(_SHIPMENT_STEP_SQL[source],
                               (STORY_SHIPMENT_NUMBER_RE, MAX_ROWS_PER_CYCLE))
                moved = len(cursor.fetchall())
                result[key] = moved
                result['transitions'] += moved
                if moved:
                    logger.debug(f"Moved {moved} shipments to {target}")
            result['shipping_orders_delivered'] = self._deliver_orders_of(cursor)
            result['transitions'] += result['shipping_orders_delivered']
            result['cycle_id'] = cycle_id
            return result

        except Exception as e:
            logger.error(f"Shipment state advancement failed: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def _deliver_orders_of(self, cursor) -> int:
        """Move one bounded batch of SHIPPED, non-story orders whose shipments
        are all delivered to DELIVERED (``_SHIPPED_ORDERS_TO_DELIVERED_SQL``;
        at most ``MAX_ROWS_PER_CYCLE``, oldest first). Covers both the current
        cycle's deliveries and any pre-existing backlog.

        Runs in the step's own transaction, so a failure aborts the cycle the
        same way the shipment steps do. Returns the number of orders moved."""
        cursor.execute(_SHIPPED_ORDERS_TO_DELIVERED_SQL,
                       (STORY_ORDER_NUMBER_RE, MAX_ROWS_PER_CYCLE))
        moved = len(cursor.fetchall())
        if moved:
            logger.debug(f"Moved {moved} orders to DELIVERED")
        return moved

    def advance_invoice_states(self, conn, cycle_id: str) -> Dict[str, Any]:
        """
        Advance invoices through their lifecycle states.
        
        SENT → OVERDUE (when past due date without payment)

        At most ``MAX_ROWS_PER_CYCLE`` invoices per cycle, earliest due first
        (``_SENT_TO_OVERDUE_SQL``).
        
        Args:
            conn: Database connection.
            cycle_id: Unique identifier for this simulation cycle.
            
        Returns:
            Dictionary with state transition statistics.
        """
        cursor = conn.cursor()
        transitions = 0
        
        try:
            # SENT → OVERDUE
            # When past due date and not paid
            cursor.execute(_SENT_TO_OVERDUE_SQL, (MAX_ROWS_PER_CYCLE,))
            
            overdue_invoices = cursor.fetchall()
            transitions += len(overdue_invoices)
            
            if overdue_invoices:
                logger.debug(f"Moved {len(overdue_invoices)} invoices to OVERDUE")
            
            return {
                'transitions': transitions,
                'finance_sent_to_overdue': len(overdue_invoices),
                'cycle_id': cycle_id
            }
        
        except Exception as e:
            logger.error(f"Invoice state advancement failed: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def net_split_inventory(self, conn, cycle_id: str) -> Dict[str, Any]:
        """Clear negative FG and raw-material balances that sit beside positive
        ones for the same item (``inventory_draw.net_split_balances``; totals
        unchanged, bounded per cycle)."""
        cursor = conn.cursor()
        try:
            netted = (inventory_draw.net_split_balances(cursor, FG_LOCATION_CODES)
                      + inventory_draw.net_split_balances(cursor, RAW_LOCATION_CODES))
            if netted:
                logger.info(f"Netted {netted} split inventory balances (cycle: {cycle_id})")
            return {'balances_netted': netted, 'cycle_id': cycle_id}
        finally:
            cursor.close()

    def advance_backorder_states(self, conn, cycle_id: str) -> Dict[str, Any]:
        """
        Advance backordered orders when inventory becomes available.
        BACKORDERED → READY_TO_SHIP (when ALL line items have sufficient inventory)
        """
        cursor = conn.cursor()
        try:
            transitions = self._release_available_backorders(
                cursor, _backordered_batch(cursor, self._backorder_scan_start()), require_lines=True)
            return {'transitions': transitions, 'cycle_id': cycle_id}
        
        except Exception as e:
            logger.error(f"Backorder state advancement failed: {e}", exc_info=True)
            raise
        finally:
            cursor.close()
