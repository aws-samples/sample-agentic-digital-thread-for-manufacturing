# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Production Simulator for ERP Simulation System.

Manages production runs based on INVENTORY REPLENISHMENT, not individual orders.
This is a beverage bottling plant - we build to stock, not build to order.
Production runs are triggered when finished goods inventory falls below reorder points.
"""

import random
import re
from datetime import datetime
from typing import Dict, Any

from .config import get_config
from .utils import MAX_ROWS_PER_CYCLE, SqlText, insert_numbered, savepoint, sql_text
from .logging_config import get_logger
from .base_simulator import BaseSimulator
from .external_mes import claims_table_present, unclaimed

logger = get_logger(__name__)

# Site code → FG location code. The ONE map for replenishment planning,
# completion deposits and backorder production (state_advancer imports it).
SITE_FG_LOCATION = {
    'MILWAUKEE_BOTTLING': 'MKE-FG',
    'DETROIT_AUTOPARTS': 'DET-FG',
    'CEDARRAPIDS_FOOD': 'CR-FG',
    'INDIANAPOLIS_MACHINING': 'IND-FG',  # ADR-0072
    'BOTTLING_PLANT': 'FG-WAREHOUSE',
}
DEFAULT_FG_LOCATION = 'FG-WAREHOUSE'
# Every FG location code — the ONE allowlist for order availability checks and
# shipping draw-downs (bind as ``il.location_code = ANY(%s)``, never inline).
FG_LOCATION_CODES = tuple(sorted({*SITE_FG_LOCATION.values(), DEFAULT_FG_LOCATION}))

# Site code → raw-material warehouse. Lots live here (FIFO allocation reads it)
# and MES consumption draws here first (inventory_draw.py).
SITE_RAW_LOCATION = {
    'MILWAUKEE_BOTTLING': 'MKE-WH-RAW',
    'DETROIT_AUTOPARTS': 'DET-WH-RAW',
    'CEDARRAPIDS_FOOD': 'CR-WH-RAW',
    'INDIANAPOLIS_MACHINING': 'IND-WH-RAW',  # ADR-0083
    'BOTTLING_PLANT': 'RAW-WAREHOUSE',
}
# The legacy central raw store: PO receipts still land here.
LEGACY_RAW_LOCATION = 'WH-RAW'
# Every raw-material location code a draw or a netting pass may touch.
RAW_LOCATION_CODES = tuple(sorted({*SITE_RAW_LOCATION.values(), LEGACY_RAW_LOCATION}))


_SQL_LITERAL_CODE = re.compile(r'[A-Z0-9_-]+')  # a code that cannot end the quoted literal it sits in


def fg_location_case_sql(column: str) -> SqlText:
    """Render ``SITE_FG_LOCATION`` as a SQL CASE over ``column`` (ELSE the default).

    ``column`` is a plain identifier (``alias.column``; :func:`sql_text`
    checks it) and every branch value is a module constant code, checked to be
    ``_SQL_LITERAL_CODE``; no caller-supplied data reaches the SQL text.
    """
    codes = [*SITE_FG_LOCATION.keys(), *SITE_FG_LOCATION.values(), DEFAULT_FG_LOCATION]
    if not all(_SQL_LITERAL_CODE.fullmatch(code) for code in codes):
        raise ValueError(f"fg_location_case_sql: a site or location code is not {_SQL_LITERAL_CODE.pattern}")
    branches = ' '.join(f"WHEN '{site}' THEN '{loc}'" for site, loc in SITE_FG_LOCATION.items())
    return sql_text("CASE {column} " + branches + f" ELSE '{DEFAULT_FG_LOCATION}' END", column=column)


# Completed runs still waiting for their FG receipt: a bounded batch, oldest
# completion first, each with the FG location (site map, CASE from module
# constants) and the product's FG item already resolved. A run with no active
# FG location or no FG item cannot be received, so it is never selected: it
# cannot fill the batch and starve the runs behind it. ``FOR UPDATE OF pr
# SKIP LOCKED`` keeps any concurrent receiver off the same rows; the only
# bound parameter is the row limit.
_RECEIPT_BACKLOG_SQL = """
    SELECT pr.run_id, pr.run_number, pr.actual_quantity, p.sku, pr.site,
           fg.location_code, fg.location_id, fi.item_id
    FROM production_runs pr
    JOIN products p ON p.product_id = pr.product_id
    CROSS JOIN LATERAL (
        SELECT il.location_id, il.location_code
        FROM inventory_locations il
        WHERE il.location_code = ({fg_case}) AND il.is_active = TRUE
        LIMIT 1
    ) fg
    CROSS JOIN LATERAL (
        SELECT ii.item_id
        FROM inventory_items ii
        WHERE ii.product_id = pr.product_id AND ii.item_type = 'FINISHED_GOOD'
        LIMIT 1
    ) fi
    WHERE pr.status = 'COMPLETED'
      AND pr.actual_quantity IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM inventory_transactions it
          WHERE it.reference_type = 'PRODUCTION_RUN'
            AND it.reference_id = pr.run_id
            AND it.transaction_type = 'RECEIPT'
      )
    ORDER BY pr.actual_end_time NULLS FIRST, pr.run_number
    LIMIT %s
    FOR UPDATE OF pr SKIP LOCKED
"""


# Replenishment candidates; ``{fg_case}`` is filled by fg_location_case_sql()
# (module constants only) and the reorder point is a bound parameter.
_REPLENISHMENT_CANDIDATES_SQL = """
    WITH pipeline_production AS (
        SELECT product_id, site,
               COALESCE(SUM(planned_quantity), 0) as total_pipeline
        FROM production_runs
        WHERE status IN ('PLANNED', 'RELEASED', 'IN_PROGRESS')
        GROUP BY product_id, site
    )
    SELECT p.product_id, p.sku, p.product_name,
           COALESCE(ib.quantity_on_hand, 0) as current_inventory,
           COALESCE(pp.total_pipeline, 0) as pipeline_production,
           s.site_code
    FROM products p
    JOIN inventory_items ii ON p.product_id = ii.product_id AND ii.item_type = 'FINISHED_GOOD'
    JOIN product_site_routing psr ON p.category = psr.product_category AND psr.is_active = TRUE
    JOIN sites s ON psr.site_id = s.site_id AND s.is_active = TRUE AND s.site_code != 'BOTTLING_PLANT'
    LEFT JOIN inventory_locations il ON il.location_code = (
        {fg_case}
    ) AND il.is_active = TRUE
    LEFT JOIN inventory_balances ib ON ii.item_id = ib.item_id AND ib.location_id = il.location_id
    LEFT JOIN pipeline_production pp ON p.product_id = pp.product_id AND pp.site = s.site_code
    WHERE p.is_active = TRUE
      AND (COALESCE(ib.quantity_on_hand, 0) + COALESCE(pp.total_pipeline, 0)) < %s
    ORDER BY (COALESCE(ib.quantity_on_hand, 0) + COALESCE(pp.total_pipeline, 0)) ASC
"""


def record_planned_materials(cursor, run_id, product_id, batch_size) -> None:
    """Insert one planned ``production_materials`` row per active BOM component."""
    cursor.execute("""
        SELECT component_item_id, quantity_required
        FROM bill_of_materials
        WHERE product_id = %s AND is_active = TRUE
    """, (product_id,))
    for component_item_id, quantity_required in cursor.fetchall():
        cursor.execute("""
            INSERT INTO production_materials (
                run_id, item_id, planned_quantity
            )
            VALUES (%s, %s, %s)
        """, (run_id, component_item_id, float(quantity_required) * batch_size))


# IN_PROGRESS runs with no RUNNING MES work order (orphans of a crashed
# cycle): completed with their planned quantity. The claims form adds only the
# external-MES ownership rule; the predicate for unclaimed runs is the same.
_STALE_SQL = """
            UPDATE production_runs
            SET status = 'COMPLETED',
                actual_end_time = COALESCE(actual_end_time, NOW()),
                actual_quantity = COALESCE(actual_quantity, planned_quantity),
                updated_at = NOW()
            WHERE status = 'IN_PROGRESS'
              AND NOT EXISTS (
                  SELECT 1 FROM mes_work_orders mwo
                  WHERE mwo.production_run_id = production_runs.run_id
                    AND mwo.status = 'RUNNING'
              )
        """
_STALE_CLAIMS_SQL = (_STALE_SQL.rstrip()
                     + f"\n              AND {unclaimed('production_runs.run_id')}\n        ")


class ProductionSimulator(BaseSimulator):
    """
    Simulates production execution for a beverage bottling plant.
    
    Creates BATCH production runs to replenish finished goods inventory.
    Orders ship from existing FG inventory - production is NOT tied to individual orders.
    Production runs are large batches (500-2000 units) triggered by low inventory.
    """
    
    # Batch sizes for production runs (realistic for bottling line)
    # Larger batches = more inventory per run = fewer runs needed
    BATCH_SIZE_MIN = 500
    BATCH_SIZE_MAX = 600
    
    # Reorder point for finished goods (trigger production when below this)
    FG_REORDER_POINT = 200
    
    # Site code → FG location code mapping (the module-level map, not a copy)
    SITE_FG_LOCATION = SITE_FG_LOCATION

    # Site code → raw material warehouse location code mapping (the module-level map)
    SITE_RAW_LOCATION = SITE_RAW_LOCATION
    
    def __init__(self):
        """Initialize the production simulator."""
        self.config = get_config()
        self.production_lines = ['LINE1', 'LINE2', 'LINE3']
        logger.info("Production simulator initialized (inventory-based batch production)")

    @classmethod
    def allocate_run_lots(cls, cursor, run_id, product_id, batch_size: int, site_code: str) -> int:
        """FIFO lot allocation for a run planned on another path (an order-driven
        backorder run from ``StateAdvancer``), the same rule a replenishment
        run gets at planning. Returns the lot_production_usage rows inserted."""
        return cls._allocate_lots_fifo(cls.__new__(cls), cursor, run_id, product_id, batch_size, site_code)

    def _allocate_lots_fifo(
        self,
        cursor,
        run_id,
        product_id,
        batch_size: int,
        site_code: str
    ) -> int:
        """
        Allocate material lots to a production run using FIFO order.

        Queries the BOM for the product, then for each component finds APPROVED
        lots at the site's raw material location ordered by received_date ASC.
        Inserts lot_production_usage rows and deducts remaining_quantity.

        Returns the number of lot_production_usage rows inserted.
        Logs a warning (does not raise) if no APPROVED lot is available for a
        component.
        """
        raw_location_code = self.SITE_RAW_LOCATION.get(site_code, 'RAW-WAREHOUSE')

        # Get BOM components for this product
        cursor.execute("""
            SELECT bom.component_item_id, bom.quantity_required, ii.item_code, ii.unit_of_measure
            FROM bill_of_materials bom
            JOIN inventory_items ii ON bom.component_item_id = ii.item_id
            WHERE bom.product_id = %s AND bom.is_active = TRUE
        """, (product_id,))
        bom_items = cursor.fetchall()

        if not bom_items:
            logger.debug(f"No BOM found for product {product_id} at {site_code}")
            return 0

        rows_inserted = 0

        for component_item_id, quantity_required, item_code, uom in bom_items:
            needed = float(quantity_required) * batch_size

            # Find APPROVED lots at the site's raw material location, FIFO order
            # Only use lots from APPROVED suppliers (not DISQUALIFIED/UNDER_EVALUATION)
            cursor.execute("""
                SELECT ml.lot_id, ml.remaining_quantity
                FROM material_lots ml
                JOIN suppliers s ON ml.supplier_id = s.supplier_id
                WHERE ml.item_id = %s
                  AND ml.quality_status = 'APPROVED'
                  AND s.qualification_status = 'APPROVED'
                  AND ml.remaining_quantity > 0
                  AND ml.current_location_id IN (
                      SELECT location_id FROM inventory_locations
                      WHERE location_code = %s AND is_active = TRUE
                  )
                ORDER BY ml.received_date ASC
                FOR UPDATE SKIP LOCKED
            """, (component_item_id, raw_location_code))

            lots = cursor.fetchall()

            if not lots:
                logger.warning(
                    f"No APPROVED lot for {item_code} at {site_code} "
                    f"(location: {raw_location_code})"
                )
                continue

            for lot_id, remaining_quantity in lots:
                if needed <= 0:
                    break

                consume = min(float(remaining_quantity), needed)

                cursor.execute("""
                    INSERT INTO lot_production_usage (
                        lot_id, production_run_id, quantity_used, unit_of_measure, usage_date
                    )
                    VALUES (%s, %s, %s, %s, NOW())
                """, (lot_id, run_id, consume, uom))

                cursor.execute("""
                    UPDATE material_lots
                    SET remaining_quantity = remaining_quantity - %s,
                        updated_at = NOW()
                    WHERE lot_id = %s
                """, (consume, lot_id))

                rows_inserted += 1
                needed -= consume

            if needed > 0:
                logger.warning(
                    f"Insufficient lot quantity for {item_code} at {site_code}: "
                    f"{needed:.2f} units unallocated"
                )

        return rows_inserted
    
    def create_production_runs(self, conn, cycle_id: str) -> Dict[str, Any]:
        """
        Create batch production runs based on finished goods inventory levels.
        
        This is inventory replenishment - NOT order-based production.
        When FG inventory for a product falls below reorder point, create a large batch run.
        
        Args:
            conn: Database connection.
            cycle_id: Unique identifier for this simulation cycle.
            
        Returns:
            Dictionary with production run creation statistics.
        """
        cursor = conn.cursor()
        
        try:
            self._cleanup_stale_in_progress(cursor)
            batch_size_min, batch_size_max, fg_reorder_point = self._load_replenishment_params(cursor)
            candidates = self._replenishment_candidates(cursor, fg_reorder_point)
            
            if not candidates:
                logger.debug("No products need inventory replenishment")
                return {'runs_created': 0}
            
            logger.info(f"Found {len(candidates)} products needing inventory replenishment")
            runs_created = self._plan_replenishment_runs(cursor, candidates, batch_size_min, batch_size_max)
            logger.info(f"Created {runs_created} batch production runs (cycle: {cycle_id})")
            
            return {
                'runs_created': runs_created,
                'cycle_id': cycle_id
            }
        
        except Exception as e:
            logger.error(f"Production run creation failed: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def _cleanup_stale_in_progress(self, cursor) -> int:
        """Complete IN_PROGRESS runs that have no RUNNING MES work order.

        These are orphaned from simulator crashes and block the pipeline calculation.
        A run an external MES claimed (erp_simulation/external_mes.py) is not
        orphaned: it has no built-in work order by design and stays with its
        integrator. Without the claims table the statement is the one it
        always was (one catalog probe per cycle decides).
        """
        cursor.execute(_STALE_CLAIMS_SQL if claims_table_present(cursor) else _STALE_SQL)
        stale_cleaned = cursor.rowcount
        if stale_cleaned > 0:
            logger.info(f"Cleaned up {stale_cleaned} stale IN_PROGRESS production runs")
        return stale_cleaned

    def _load_replenishment_params(self, cursor):
        """Return ``(batch_size_min, batch_size_max, fg_reorder_point)`` from simulation_config."""
        return (
            self._load_config_value(cursor, 'production_batch_size_min', 500, 'int'),
            self._load_config_value(cursor, 'production_batch_size_max', 600, 'int'),
            self._load_config_value(cursor, 'fg_reorder_point', 1000, 'int'),
        )

    def _replenishment_candidates(self, cursor, reorder_point) -> list:
        """Products whose site FG stock plus site pipeline is below ``reorder_point``.

        FG stock is read at the routed site's FG location (``SITE_FG_LOCATION``
        rendered as a CASE); pipeline production is scoped per site to avoid
        overproduction. Rows: (product_id, sku, product_name, current_inventory,
        pipeline_production, site_code).
        """
        cursor.execute(sql_text(_REPLENISHMENT_CANDIDATES_SQL, fg_case=fg_location_case_sql('s.site_code')),
                       (reorder_point,))
        return cursor.fetchall()

    def _plan_replenishment_runs(self, cursor, candidates, batch_size_min, batch_size_max) -> int:
        """Plan one run per candidate, capped at one queued run per line per site."""
        runs_created = 0
        runs_per_site = {}
        for candidate in candidates:
            sku, site_code = candidate[1], candidate[5]
            # Don't create more runs than there are lines at this site
            if runs_per_site.get(site_code, 0) >= len(self.production_lines):
                logger.debug(f"All lines at {site_code} already have queued runs, skipping")
                continue
            try:
                with savepoint(cursor, 'plan_run'):  # one failed run never aborts the step
                    planned = self._plan_run_for_candidate(cursor, candidate, batch_size_min, batch_size_max)
                if planned:
                    runs_created += 1
                    runs_per_site[site_code] = runs_per_site.get(site_code, 0) + 1
            except Exception as e:
                logger.error(f"Failed to create production run for {sku}: {e}", exc_info=True)
        return runs_created

    def _plan_run_for_candidate(self, cursor, candidate, batch_size_min, batch_size_max) -> bool:
        """Create one PLANNED replenishment run; False when skipped."""
        product_id, sku, _product_name, current_inventory, pipeline_production, site_code = candidate
        batch_size = random.randint(batch_size_min, batch_size_max)  # nosec B311 - synthetic data generation, not security context
        batch_size = self._material_capped_batch(cursor, product_id, sku, site_code, batch_size)
        if batch_size is None:
            return False
        logger.info(
            f"Creating batch production run for {sku} at {site_code}: "
            f"current inventory={current_inventory}, pipeline={pipeline_production}, batch size={batch_size}"
        )
        location_id = self._active_fg_location_id(cursor, site_code, sku)
        if location_id is None:
            return False
        inserted = self._insert_replenishment_run(
            cursor, product_id, batch_size, location_id, site_code,
            f"Inventory replenishment batch for {sku} (current: {current_inventory})"
        )
        if inserted is None:
            return False
        run_number, run_id = inserted
        lots_allocated = self._allocate_lots_fifo(cursor, run_id, product_id, batch_size, site_code)
        if lots_allocated == 0:
            logger.warning(
                f"Production run {run_number} created with no lot allocation "
                f"(no APPROVED lots available at {site_code})"
            )
        record_planned_materials(cursor, run_id, product_id, batch_size)
        logger.debug(
            f"Created batch production run {run_number}: "
            f"product {sku}, batch size {batch_size} units"
        )
        return True

    def _material_capped_batch(self, cursor, product_id, sku, site_code, batch_size):
        """Cap ``batch_size`` to what raw materials support; None when under 10 units.

        This prevents runs from being permanently stuck at PLANNED.
        """
        cursor.execute("""
            SELECT bom.component_item_id, bom.quantity_required,
                   COALESCE(SUM(ib.quantity_on_hand), 0) as available,
                   ii.item_code
            FROM bill_of_materials bom
            JOIN inventory_items ii ON bom.component_item_id = ii.item_id
            LEFT JOIN inventory_balances ib ON ib.item_id = bom.component_item_id
            WHERE bom.product_id = %s AND bom.is_active = TRUE
            GROUP BY bom.component_item_id, bom.quantity_required, ii.item_code
        """, (product_id,))
        bom_availability = cursor.fetchall()
        if not bom_availability:
            return batch_size
        max_producible = batch_size
        for _, qty_required, available, _item_code in bom_availability:
            if float(qty_required) > 0:
                max_producible = min(max_producible, int(float(available) / float(qty_required)))
        if max_producible < 10:
            logger.info(f"Skipping {sku} at {site_code}: only {max_producible} units producible from materials")
            return None
        if max_producible < batch_size:
            logger.info(f"Capping batch for {sku} from {batch_size} to {max_producible} (material limited)")
        return max_producible

    def _active_fg_location_id(self, cursor, site_code, sku):
        """Resolve the site's active FG location id; None (logged) when absent."""
        fg_location_code = SITE_FG_LOCATION.get(site_code, DEFAULT_FG_LOCATION)
        cursor.execute("""
            SELECT location_id
            FROM inventory_locations
            WHERE location_code = %s
              AND is_active = TRUE
            LIMIT 1
        """, (fg_location_code,))
        location_row = cursor.fetchone()
        if not location_row:
            logger.warning(f"No FG location found for {fg_location_code} - skipping {sku}")
            return None
        return location_row[0]

    def _insert_replenishment_run(self, cursor, product_id, batch_size, location_id, site_code, notes):
        """Insert a PLANNED run with no order_id (inventory replenishment); returns
        (run_number, run_id), or None when the number was skipped (ADR-0090)."""
        inserted = insert_numbered(cursor, 'RUN', """
            INSERT INTO production_runs (
                run_number, product_id, order_id,
                planned_quantity, status,
                location_id, site, notes
            )
            VALUES (%s, %s, NULL, %s, %s, %s, %s, %s)
            RETURNING run_id
        """, lambda number: (number, product_id, batch_size, 'PLANNED', location_id, site_code, notes),
            what=f"replenishment run for product {product_id}")
        return None if inserted is None else (inserted[0], inserted[1][0])
    
    def complete_production(self, conn, cycle_id: str) -> Dict[str, Any]:
        """
        Produce finished goods inventory for completed production runs.

        MES marks runs as COMPLETED; this posts each one's FG receipt into its
        site's FG warehouse. One bounded batch per cycle (``MAX_ROWS_PER_CYCLE``,
        oldest completion first, ``_RECEIPT_BACKLOG_SQL``), so a backlog drains
        over cycles instead of one unbounded pass outliving the Lambda. Each
        receipt runs under its own savepoint: a failed row rolls back alone and
        is retried next cycle while the rest of the batch commits, and the
        failures are logged once per cycle (the first with its traceback).
        """
        cursor = conn.cursor()
        try:
            cursor.execute(sql_text(_RECEIPT_BACKLOG_SQL, fg_case=fg_location_case_sql('pr.site')),
                           (MAX_ROWS_PER_CYCLE,))
            runs = cursor.fetchall()
            if not runs:
                logger.debug("No production runs need inventory production")
                return {'runs_completed': 0}
            logger.info(f"Producing inventory for {len(runs)} completed production runs "
                        f"(at most {MAX_ROWS_PER_CYCLE} per cycle, oldest first)")
            self._warn_unmapped_sites(runs)
            runs_completed, failures = self._post_receipts(cursor, runs)
            if failures:
                logger.warning(f"{failures} of {len(runs)} production receipts failed this cycle "
                               f"and were rolled back alone; they are retried next cycle")
            logger.info(f"Produced inventory for {runs_completed} production runs (cycle: {cycle_id})")
            return {'runs_completed': runs_completed, 'receipt_failures': failures, 'cycle_id': cycle_id}
        except Exception as e:
            logger.error(f"Production inventory creation failed: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def _post_receipts(self, cursor, runs) -> tuple:
        """Post one FG receipt per run, each under its own savepoint; returns
        ``(posted, failed)``. Only the first failure carries a traceback."""
        posted = failed = 0
        for run_id, run_number, quantity, sku, _site, location_code, location_id, item_id in runs:
            try:
                with savepoint(cursor, 'adt_receipt'):
                    self._post_receipt(cursor, run_id, item_id, location_id, quantity)
            except Exception as e:
                failed += 1
                if failed == 1:
                    logger.error(f"Failed to produce inventory for run {run_number}: {e}", exc_info=True)
                continue
            posted += 1
            logger.info(f"Produced inventory for batch run {run_number}: "
                        f"product {sku}, produced {quantity} units to {location_code}")
        return posted, failed

    @staticmethod
    def _post_receipt(cursor, run_id, item_id, location_id, quantity) -> None:
        """The receipt transaction and the FG balance it adds to."""
        now = datetime.now()
        cursor.execute("""
            INSERT INTO inventory_transactions (
                item_id, location_id, transaction_type,
                quantity, reference_type, reference_id,
                transaction_date
            )
            VALUES (%s, %s, 'RECEIPT', %s, 'PRODUCTION_RUN', %s, %s)
        """, (item_id, location_id, quantity, run_id, now))
        cursor.execute("""
            INSERT INTO inventory_balances (
                item_id, location_id, quantity_on_hand, last_transaction_date
            )
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (item_id, location_id)
            DO UPDATE SET
                quantity_on_hand = inventory_balances.quantity_on_hand + EXCLUDED.quantity_on_hand,
                last_transaction_date = EXCLUDED.last_transaction_date
        """, (item_id, location_id, quantity, now))

    def _warn_unmapped_sites(self, runs) -> None:
        """One warning per batch for sites the FG map does not name (their
        receipts go to the default FG location)."""
        unmapped = sorted({row[4] for row in runs if row[4] and row[4] not in self.SITE_FG_LOCATION})
        if unmapped:
            logger.warning(f"Unknown site(s) {', '.join(unmapped)} - receipts fall back to "
                           f"{DEFAULT_FG_LOCATION}")
