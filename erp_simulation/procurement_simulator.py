# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Procurement Simulator for ERP Simulation System.

Manages inventory replenishment through purchase orders.
"""

import random
from datetime import datetime, timedelta
from typing import Dict, Any, List, Tuple

from .config import get_config
from .utils import insert_numbered, savepoint
from .logging_config import get_logger
from .delivery_timing import (
    DELIVERY_VARIABILITY_KEY, delivery_offset_minutes, spread_minutes)
from .base_simulator import BaseSimulator

logger = get_logger(__name__)


#: Why the PO trigger is lot-aware. Production (``_allocate_lots``) consumes
#: ``material_lots.remaining_quantity`` and never decrements
#: ``inventory_balances``, so a trigger on balances alone never fires once
#: lots run dry — live 2026-09-26: Cedar Rapids had 0 usable lots for most
#: components, balances still above reorder point, no POs since 2026-08-08,
#: so its runs allocated no lots and its lot traceability stopped. The
#: trigger now uses LEAST(balance, usable APPROVED-lot stock) for any item
#: that has lot rows; items with no lots keep the balance-only behaviour.
LOT_AWARE_STOCK_NOTE = "effective_inventory = LEAST(balance, usable approved lots)"

#: Items needing a purchase order (lot-aware; see LOT_AWARE_STOCK_NOTE).
LOW_STOCK_SQL = """
    WITH current_inventory AS (
        SELECT 
            ii.item_id,
            ii.item_code,
            ii.item_name,
            ii.reorder_point,
            ii.reorder_quantity,
            ii.unit_cost,
            COALESCE(SUM(ib.quantity_on_hand), 0) as qty_on_hand
        FROM inventory_items ii
        LEFT JOIN inventory_balances ib ON ii.item_id = ib.item_id
        WHERE ii.item_type IN ('RAW_MATERIAL', 'COMPONENT')
          AND ii.is_active = TRUE
        GROUP BY ii.item_id, ii.item_code, ii.item_name, 
                 ii.reorder_point, ii.reorder_quantity, ii.unit_cost
    ),
    lot_stock AS (
        -- Production allocates from APPROVED lots, not balances,
        -- so a lot-tracked item is only as available as its
        -- usable lots (see LOT_AWARE_STOCK_NOTE).
        SELECT ml.item_id,
               COALESCE(SUM(CASE WHEN ml.quality_status = 'APPROVED'
                                  AND s.qualification_status = 'APPROVED'
                                 THEN ml.remaining_quantity ELSE 0 END), 0)
                   AS usable_qty
        FROM material_lots ml
        JOIN suppliers s ON ml.supplier_id = s.supplier_id
        GROUP BY ml.item_id
    ),
    effective_inventory AS (
        SELECT ci.item_id, ci.item_code, ci.item_name, ci.reorder_point,
               ci.reorder_quantity, ci.unit_cost,
               CASE WHEN ls.item_id IS NULL THEN ci.qty_on_hand
                    ELSE LEAST(ci.qty_on_hand, ls.usable_qty)
               END AS qty_on_hand
        FROM current_inventory ci
        LEFT JOIN lot_stock ls ON ls.item_id = ci.item_id
    ),
    open_po_qty AS (
        SELECT 
            pli.item_id,
            COALESCE(SUM(pli.quantity_ordered - COALESCE(pli.quantity_received, 0)), 0) as qty_on_order
        FROM po_line_items pli
        JOIN purchase_orders po ON pli.po_id = po.po_id
        WHERE po.status IN ('DRAFT', 'SENT', 'ACKNOWLEDGED', 'IN_TRANSIT')
        GROUP BY pli.item_id
    ),
    planned_demand AS (
        -- Calculate demand from PLANNED and IN_PROGRESS production runs
        SELECT 
            bom.component_item_id as item_id,
            COALESCE(SUM(bom.quantity_required * pr.planned_quantity), 0) as planned_qty
        FROM production_runs pr
        JOIN bill_of_materials bom ON pr.product_id = bom.product_id
        WHERE pr.status IN ('PLANNED', 'IN_PROGRESS')
        GROUP BY bom.component_item_id
    )
    SELECT 
        ci.item_id,
        ci.item_code,
        ci.item_name,
        ci.reorder_point,
        ci.reorder_quantity,
        ci.unit_cost,
        ci.qty_on_hand,
        COALESCE(opo.qty_on_order, 0) as qty_on_order,
        COALESCE(pd.planned_qty, 0) as planned_demand,
        -- Calculate needed quantity:
        -- If negative: |deficit| + planned_demand + reorder_point - qty_on_order
        -- If positive but below reorder: planned_demand + reorder_point - qty_on_hand - qty_on_order
        CASE 
            WHEN ci.qty_on_hand < 0 THEN 
                ABS(ci.qty_on_hand) + COALESCE(pd.planned_qty, 0) + ci.reorder_point - COALESCE(opo.qty_on_order, 0)
            ELSE 
                COALESCE(pd.planned_qty, 0) + ci.reorder_point - ci.qty_on_hand - COALESCE(opo.qty_on_order, 0)
        END as calculated_order_qty
    FROM effective_inventory ci
    LEFT JOIN open_po_qty opo ON ci.item_id = opo.item_id
    LEFT JOIN planned_demand pd ON ci.item_id = pd.item_id
    WHERE (
        -- Trigger 1: Below reorder point
        ci.qty_on_hand < ci.reorder_point
        -- Trigger 2: Would go negative with planned production (even if above reorder point)
        OR (ci.qty_on_hand + COALESCE(opo.qty_on_order, 0) < COALESCE(pd.planned_qty, 0))
    )
      -- Only order if we actually need more (calculated qty > 0)
      AND (
          CASE 
              WHEN ci.qty_on_hand < 0 THEN 
                  ABS(ci.qty_on_hand) + COALESCE(pd.planned_qty, 0) + ci.reorder_point - COALESCE(opo.qty_on_order, 0)
              ELSE 
                  COALESCE(pd.planned_qty, 0) + ci.reorder_point - ci.qty_on_hand - COALESCE(opo.qty_on_order, 0)
          END
      ) > 0
"""


def create_receipt_lot(cursor, *, po_id, po_number, supplier_id, item_id,
                       quantity, line_no) -> bool:
    """Create the material lot a PO receipt delivers (lot traceability).

    Only the seed ever created ``material_lots``, so each site's lots were
    consumed to zero within weeks; after that production runs allocated NO
    lots, ``lot_production_usage`` / ``lot_mes_tracking`` stopped growing, and
    no current MES work order could be traced to an ERP supplier (live-found
    2026-09-24: ERP lot tracking ended ~2026-08-08). A receipt now lands as an
    APPROVED lot at the raw-material location that item's lots already live
    at (items are site-specific), so FIFO allocation keeps finding stock.

    Returns True when a lot was inserted; False when the item has no known
    lot location (nothing to infer — skip rather than guess a site) or the lot
    number already exists (idempotent re-receipt).
    """
    cursor.execute("""
        SELECT current_location_id, unit_of_measure
        FROM material_lots
        WHERE item_id = %s AND current_location_id IS NOT NULL
        ORDER BY received_date DESC
        LIMIT 1
    """, (item_id,))
    row = cursor.fetchone()
    if not row:
        return False
    location_id, uom = row
    lot_number = f"LOT-{po_number}-{int(line_no):02d}"
    cursor.execute("""
        INSERT INTO material_lots (
            lot_number, item_id, supplier_id, po_id, received_date,
            received_quantity, remaining_quantity, unit_of_measure,
            quality_status, current_location_id
        )
        VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s, 'APPROVED', %s)
        ON CONFLICT (lot_number) DO NOTHING
    """, (lot_number, item_id, supplier_id, po_id, quantity, quantity, uom, location_id))
    return bool(cursor.rowcount)


class ProcurementSimulator(BaseSimulator):
    """
    Simulates procurement and inventory replenishment.
    
    Monitors inventory levels, creates purchase orders for low-stock items,
    and simulates PO receipt with realistic lead times.
    """
    
    def __init__(self):
        """Initialize the procurement simulator."""
        self.config = get_config()
        logger.info("Procurement simulator initialized")
    
    def check_and_create_pos(self, conn, cycle_id: str) -> Dict[str, Any]:
        """
        Check inventory levels and create purchase orders for low-stock items.
        
        Calculates dynamic order quantity based on:
        - |negative_inventory| (deficit to cover)
        - planned_production_demand (upcoming needs)
        - reorder_point (safety buffer)
        
        Args:
            conn: Database connection.
            cycle_id: Unique identifier for this simulation cycle.
            
        Returns:
            Dictionary with PO creation statistics.
        """
        cursor = conn.cursor()
        
        try:
            return self._create_pos_for_low_stock(cursor, cycle_id)
        
        except Exception as e:
            logger.error(f"PO creation failed: {e}", exc_info=True)
            raise
        finally:
            cursor.close()
    
    def _create_pos_for_low_stock(self, cursor, cycle_id: str) -> Dict[str, Any]:
        """Body of check_and_create_pos: one PO per approved supplier of low-stock items."""
        # Find items below reorder point
        # Calculate dynamic order quantity = |deficit| + planned_demand + reorder_point
        # Also get quantity already on order (open POs) to avoid over-ordering
        cursor.execute(LOW_STOCK_SQL)
        low_stock_items = cursor.fetchall()
        if not low_stock_items:
            logger.debug("No items below reorder point needing orders")
            return {'pos_created': 0}
        self._log_low_stock_items(low_stock_items)
        suppliers = self._fetch_active_suppliers(cursor)
        if not suppliers:
            logger.warning("No active suppliers found, cannot create POs")
            return {'pos_created': 0}
        # Group items by approved supplier for realistic traceability
        items_by_supplier = self._group_items_by_supplier(low_stock_items, suppliers, cursor)
        pos_created = 0
        for supplier_info, items in items_by_supplier.items():
            if self._create_supplier_po_isolated(cursor, supplier_info, items):
                pos_created += 1
        logger.info(f"Created {pos_created} purchase orders (cycle: {cycle_id})")
        return {
            'pos_created': pos_created,
            'cycle_id': cycle_id
        }

    @staticmethod
    def _log_low_stock_items(low_stock_items) -> None:
        """Log details about what we're ordering, then the item count."""
        for item in low_stock_items:
            item_code = item[1]
            qty_on_hand = float(item[6])
            qty_on_order = float(item[7])
            planned_demand = float(item[8])
            calc_order_qty = float(item[9])
            logger.debug(
                f"Item {item_code}: on_hand={qty_on_hand:.0f}, on_order={qty_on_order:.0f}, "
                f"planned_demand={planned_demand:.0f}, ordering={calc_order_qty:.0f}"
            )
        logger.info(f"Found {len(low_stock_items)} items needing procurement")

    @staticmethod
    def _fetch_active_suppliers(cursor):
        """Get active suppliers."""
        cursor.execute("""
            SELECT supplier_id, supplier_code, company_name, lead_time_days
            FROM suppliers
            WHERE is_active = TRUE
        """)
        return cursor.fetchall()

    def _create_supplier_po_isolated(self, cursor, supplier_info, items) -> bool:
        """One supplier's PO under a savepoint; False (rolled back, logged) on failure."""
        _supplier_id, supplier_code, _lead_time_days = supplier_info
        try:
            with savepoint(cursor, 'supplier_po'):
                return self._create_supplier_po(cursor, supplier_info, items)
        except Exception as e:
            logger.error(f"Failed to create PO for supplier {supplier_code}: {e}", exc_info=True)
            return False

    @staticmethod
    def _create_supplier_po(cursor, supplier_info, items) -> bool:
        """Insert a SENT PO with one line per item; False when the number was skipped."""
        supplier_id, supplier_code, _lead_time_days = supplier_info
        po_date = datetime.now()
        # For demo purposes, use 2 minutes instead of days for faster turnaround
        # In production, use: expected_delivery_date = po_date + timedelta(days=lead_time_days)
        expected_delivery_date = po_date + timedelta(minutes=2)
        inserted = insert_numbered(cursor, 'PO', """
            INSERT INTO purchase_orders (
                po_number, supplier_id, po_date,
                expected_delivery_date, status
            )
            VALUES (%s, %s, %s, %s, %s)
            RETURNING po_id
        """, lambda number: (number, supplier_id, po_date, expected_delivery_date, 'SENT'),
            what=f"PO for supplier {supplier_code}")
        if inserted is None:
            return False
        po_number, (po_id,) = inserted
        total_amount = 0.0
        for line_number, item in enumerate(items, 1):
            item_id, item_code, unit_price = item[0], item[1], float(item[5])
            # Use calculated order quantity (index 9) instead of static reorder_quantity
            calculated_order_qty = max(1, int(item[9]))  # Ensure at least 1
            total_amount += calculated_order_qty * unit_price
            cursor.execute("""
                INSERT INTO po_line_items (
                    po_id, line_number, item_id,
                    quantity_ordered, unit_price
                )
                VALUES (%s, %s, %s, %s, %s)
            """, (po_id, line_number, item_id, calculated_order_qty, unit_price))
            logger.debug(f"PO line: {item_code} qty={calculated_order_qty}")
        cursor.execute("""
            UPDATE purchase_orders
            SET total_amount = %s
            WHERE po_id = %s
        """, (total_amount, po_id))
        logger.debug(
            f"Created PO {po_number} for supplier {supplier_code}: "
            f"{len(items)} items, total ${total_amount:.2f}"
        )
        return True

    def receive_pos(self, conn, cycle_id: str) -> Dict[str, Any]:
        """Receive POs past their expected delivery date; also advance SENT POs
        to IN_TRANSIT. Returns receipt statistics."""
        cursor = conn.cursor()
        try:
            pos_in_transit_count = self._advance_to_in_transit(cursor)
            pos_to_receive = self._pos_ready_to_receive(cursor)
            if not pos_to_receive:
                logger.debug("No purchase orders ready to receive")
                return {'pos_received': 0}
            logger.info(f"Found {len(pos_to_receive)} purchase orders ready to receive")
            location_id = self._raw_materials_location(cursor)
            if location_id is None:
                logger.warning("No raw materials warehouse location found, cannot receive inventory")
                return {'pos_received': 0}
            reliability = self._load_config_value(cursor, 'procurement_delivery_reliability', 1.0, 'float')
            # Delivery-time spread (OTD); default 0.0 keeps receipts at NOW(), the workshop's behavior.
            variability_minutes = self._load_config_value(
                cursor, DELIVERY_VARIABILITY_KEY, 0.0, 'float')
            pos_received = 0
            for po_id, po_number, supplier_id, supplier_code in pos_to_receive:
                try:
                    if self._skip_this_cycle(
                            cursor, po_id=po_id, po_number=po_number, supplier_code=supplier_code,
                            reliability=reliability, variability_minutes=variability_minutes):
                        continue  # missed this cycle (reliability) or a late PO not yet arrived
                    line_items = self._receive_one_po(
                        cursor, po_id=po_id, po_number=po_number,
                        supplier_id=supplier_id, supplier_code=supplier_code,
                        location_id=location_id,
                        variability_minutes=variability_minutes,
                    )
                    pos_received += 1
                    logger.debug(f"Received PO {po_number}: {line_items} items")
                except Exception as e:
                    logger.error(f"Failed to receive PO {po_number}: {e}", exc_info=True)
                    # Continue with next PO
            logger.info(f"Received {pos_received} purchase orders (cycle: {cycle_id})")
            return {
                'pos_received': pos_received,
                'pos_in_transit': pos_in_transit_count,
                'cycle_id': cycle_id
            }
        
        except Exception as e:
            logger.error(f"PO receipt failed: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    @staticmethod
    def _advance_to_in_transit(cursor) -> int:
        """Advance SENT POs to IN_TRANSIT (after 1 minute, demo speed). Returns count."""
        cursor.execute("""
            UPDATE purchase_orders
            SET status = 'IN_TRANSIT', updated_at = NOW()
            WHERE status = 'SENT' AND po_date <= NOW() - INTERVAL '1 minute'
            RETURNING po_id, po_number
        """)
        rows = cursor.fetchall()
        if rows:
            logger.info(f"Moved {len(rows)} POs to IN_TRANSIT")
        return len(rows)

    @staticmethod
    def _pos_ready_to_receive(cursor):
        """IN_TRANSIT POs past their expected delivery date, with each one's
        supplier_code (stable across a full reset, unlike the supplier_id UUID),
        which keys that supplier's on-time share."""
        cursor.execute("""
            SELECT po.po_id, po.po_number, po.supplier_id, s.supplier_code
            FROM purchase_orders po
            LEFT JOIN suppliers s ON s.supplier_id = po.supplier_id
            WHERE po.status = 'IN_TRANSIT' AND po.expected_delivery_date <= NOW()
        """)
        return cursor.fetchall()

    @staticmethod
    def _raw_materials_location(cursor):
        """The raw-materials warehouse location_id, or None if absent."""
        cursor.execute("""
            SELECT location_id FROM inventory_locations
            WHERE location_code = 'WH-RAW' AND is_active = TRUE
            LIMIT 1
        """)
        row = cursor.fetchone()
        return row[0] if row else None

    @staticmethod
    def _skip_this_cycle(cursor, *, po_id, po_number, supplier_code, reliability,
                         variability_minutes) -> bool:
        """True when a due PO is not received this cycle.

        With a delivery spread configured, a late PO whose delivery time
        (``expected + offset``) has not passed yet waits: it stays IN_TRANSIT,
        and so shows as overdue, until then, so a receipt never records a
        delivery time in the future. The reliability draw runs only once the PO
        has arrived (so a waiting PO is not re-drawn every cycle, which would
        compound the delay). With no spread (the default, the workshop's) there
        is no arrival check: only the draw, byte-identical to before."""
        if ProcurementSimulator._late_po_not_arrived(
                cursor, po_id, po_number, supplier_code, variability_minutes):
            return True
        return ProcurementSimulator._maybe_delay_delivery(cursor, po_id, po_number, reliability)

    @staticmethod
    def _late_po_not_arrived(cursor, po_id, po_number, supplier_code, variability_minutes) -> bool:
        """True when a spread is configured, this PO is late, and its delivery
        time has not passed yet."""
        if spread_minutes(variability_minutes) == 0.0:
            return False
        offset_min = delivery_offset_minutes(
            po_number, variability_minutes, supplier_key=supplier_code)
        if offset_min <= 0:
            return False
        cursor.execute("""
            SELECT expected_delivery_date + make_interval(secs => %s) > NOW()
            FROM purchase_orders WHERE po_id = %s
        """, (offset_min * 60.0, po_id))
        row = cursor.fetchone()
        return bool(row and row[0])

    @staticmethod
    def _maybe_delay_delivery(cursor, po_id, po_number, reliability) -> bool:
        """With probability (1 - reliability), delay the PO 1-3 days and return
        True (skip receipt this cycle); otherwise return False (receive now)."""
        if random.random() <= reliability:  # nosec B311 - synthetic data generation
            return False
        delay_days = random.randint(1, 3)  # nosec B311 - synthetic data generation
        cursor.execute("""
            UPDATE purchase_orders
            SET expected_delivery_date = expected_delivery_date + make_interval(days => %s)
            WHERE po_id = %s
        """, (delay_days, po_id))
        logger.info(f"PO {po_number} delivery delayed by {delay_days} days "
                    f"(reliability: {reliability*100:.0f}%)")
        return True

    @staticmethod
    def _receive_one_po(cursor, *, po_id, po_number, supplier_id, location_id,
                        variability_minutes, supplier_code=None) -> int:
        """Receive one PO's line items, then mark it RECEIVED.

        When no delivery spread is configured (``variability_minutes <= 0``, the
        default everywhere including the workshop), ``actual_delivery_date`` is
        the real receipt timestamp ``NOW()`` — byte-identical to the behaviour
        before this feature. Only when a positive spread is configured is the
        delivery date set to ``expected_delivery_date`` plus a deterministic
        offset (each supplier's own on-time share, each PO placed by its PO
        number — no global RNG) so an OTD view shows realistic variation. A late
        PO reaches here only once that delivery time has passed
        (``_skip_this_cycle``), so it is never in the future. Returns the
        line-item count."""
        cursor.execute("""
            SELECT line_item_id, item_id, quantity_ordered, unit_price
            FROM po_line_items WHERE po_id = %s
        """, (po_id,))
        line_items = cursor.fetchall()
        for line_no, row in enumerate(line_items, start=1):
            ProcurementSimulator._receive_po_line(
                cursor, row, line_no=line_no, po_id=po_id, po_number=po_number,
                supplier_id=supplier_id, location_id=location_id)
        if spread_minutes(variability_minutes) == 0.0:
            # Default / no spread: preserve the pre-feature behaviour exactly —
            # the delivery date is the real receipt time.
            cursor.execute("""
                UPDATE purchase_orders
                SET status = 'RECEIVED',
                    actual_delivery_date = %s,
                    updated_at = NOW()
                WHERE po_id = %s
            """, (datetime.now(), po_id))
        else:
            # Demo OTD spread: label the delivery relative to the plan, with
            # each supplier's own on-time share (keyed by its stable code).
            offset_min = delivery_offset_minutes(
                po_number, variability_minutes, supplier_key=supplier_code)
            cursor.execute("""
                UPDATE purchase_orders
                SET status = 'RECEIVED',
                    actual_delivery_date = expected_delivery_date + make_interval(secs => %s),
                    updated_at = NOW()
                WHERE po_id = %s
            """, (offset_min * 60.0, po_id))
        return len(line_items)

    @staticmethod
    def _receive_po_line(cursor, row, *, line_no, po_id, po_number,
                         supplier_id, location_id) -> None:
        """Receive one PO line: inventory txn + balance + receipt lot + received qty."""
        line_item_id, item_id, quantity_ordered, unit_price = row
        cursor.execute("""
            INSERT INTO inventory_transactions (
                item_id, location_id, transaction_type, quantity, unit_cost,
                reference_type, reference_id, transaction_date
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (item_id, location_id, 'RECEIPT', quantity_ordered, unit_price,
              'PURCHASE_ORDER', po_id, datetime.now()))
        cursor.execute("""
            INSERT INTO inventory_balances (
                item_id, location_id, quantity_on_hand, last_transaction_date
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (item_id, location_id)
            DO UPDATE SET
                quantity_on_hand = inventory_balances.quantity_on_hand + EXCLUDED.quantity_on_hand,
                last_transaction_date = EXCLUDED.last_transaction_date
        """, (item_id, location_id, quantity_ordered, datetime.now()))
        # The receipt is a physical lot — keep lot traceability
        # (production FIFO allocation) fed after the seed lots run out.
        create_receipt_lot(cursor, po_id=po_id, po_number=po_number,
                           supplier_id=supplier_id, item_id=item_id,
                           quantity=quantity_ordered, line_no=line_no)
        cursor.execute("""
            UPDATE po_line_items SET quantity_received = %s WHERE line_item_id = %s
        """, (quantity_ordered, line_item_id))

    def _group_items_by_supplier(
        self,
        items: List[Tuple],
        suppliers: List[Tuple],
        cursor=None,
    ) -> Dict[Tuple, List[Tuple]]:
        """
        Group items by approved supplier. Uses item_suppliers table if available,
        falls back to random assignment for items without approved suppliers.
        """
        items_by_supplier = {}

        # Build a lookup of item_id -> approved supplier_ids from item_suppliers table
        approved = {}
        if cursor:
            try:
                cursor.execute("""
                    SELECT item_id, supplier_id
                    FROM item_suppliers
                    ORDER BY is_preferred DESC
                """)
                for item_id, supplier_id in cursor.fetchall():
                    approved.setdefault(str(item_id), []).append(str(supplier_id))
            except Exception:  # nosec B110 - intentional fallback, no recovery action needed
                pass  # table may not exist yet, fall back to random

        supplier_map = {str(s[0]): s for s in suppliers}

        for item in items:
            item_id = str(item[0])
            approved_ids = approved.get(item_id, [])
            # Filter to suppliers that are in the active supplier list
            valid_ids = [sid for sid in approved_ids if sid in supplier_map]

            if valid_ids:
                # Pick randomly from approved suppliers for variability
                chosen_id = random.choice(valid_ids)  # nosec B311 - synthetic data generation, not security context
                supplier = supplier_map[chosen_id]
            else:
                # No approved supplier defined — use random (fallback)
                supplier = random.choice(suppliers)  # nosec B311 - synthetic data generation, not security context

            supplier_key = (supplier[0], supplier[1], supplier[3])
            if supplier_key not in items_by_supplier:
                items_by_supplier[supplier_key] = []
            items_by_supplier[supplier_key].append(item)

        return items_by_supplier
