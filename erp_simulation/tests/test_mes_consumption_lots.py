# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""MES material consumption records the ERP lot the production run drew.

A real MES records the lot it scanned at the line. The simulator used to
invent a random ``LOT-YYYYMMDD-XXX`` per consumption row, a value that
existed in no other system, so a lot-to-supplier trace from MES into ERP
could never succeed. Each row now carries the ``material_lots.lot_number``
from the run's ``lot_production_usage`` allocation (split across lots when
the run drew more than one), and a component with no allocation carries no
lot number rather than a made-up one.
"""
import os
import uuid

import pytest

from erp_simulation.mes_simulator import MESSimulator
from erp_simulation.production_simulator import ProductionSimulator


class _Cursor:
    """Scripted cursor: answers the four queries the consumption pass makes."""

    def __init__(self, pending, bom, lots):
        self._pending, self._bom, self._lots = pending, bom, lots
        self._result = []
        self.inserts = []

    def execute(self, sql, params=None):
        if "INSERT INTO mes_material_consumption" in sql:
            self.inserts.append(params)
            self._result = []
        elif "mwo.work_order_id < %s" in sql:    # the wrap-round read: nothing below the start
            self._result = []
        elif "FROM mes_work_orders mwo" in sql:
            self._result = self._pending
        elif "FROM bill_of_materials bom" in sql:
            self._result = self._bom
        elif "FROM lot_production_usage lpu" in sql:
            self._result = self._lots
        else:                                   # simulation_config lookups
            self._result = []

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None

    def close(self):
        pass


def _run(cursor):
    sim = MESSimulator.__new__(MESSimulator)
    conn = type("C", (), {"cursor": lambda self: cursor})()
    return sim.track_material_consumption(conn, "c-1")


_WO = ("wo-1", "WO-20261001-00001", "prod-1", 100, 100, "run-1")


def test_each_row_carries_the_lot_the_run_drew_split_by_quantity():
    cursor = _Cursor(
        pending=[_WO],
        bom=[("item-a", 2, "BOTTLE"), ("item-b", 1, "CAP")],
        lots=[("item-a", "LOT-PO-20260930-00001-01", 150.0),
              ("item-a", "LOT-PO-20260930-00002-01", 50.0),
              ("item-b", "LOT-PO-20260930-00003-01", 100.0)],
    )
    assert _run(cursor)["consumption_records_created"] == 3
    by_lot = {row[5]: row for row in cursor.inserts}
    assert set(by_lot) == {"LOT-PO-20260930-00001-01", "LOT-PO-20260930-00002-01",
                           "LOT-PO-20260930-00003-01"}
    assert by_lot["LOT-PO-20260930-00001-01"][2] == pytest.approx(150.0)   # 3/4 of 200
    assert by_lot["LOT-PO-20260930-00002-01"][2] == pytest.approx(50.0)
    assert by_lot["LOT-PO-20260930-00003-01"][1] == "item-b"
    for row in cursor.inserts:                    # actual = planned x configured variance
        assert 1.02 * row[2] <= row[3] <= 1.05 * row[2] + 1e-9


def test_a_component_with_no_allocated_lot_records_no_lot_number():
    cursor = _Cursor(pending=[_WO], bom=[("item-a", 2, "BOTTLE")], lots=[])
    assert _run(cursor)["consumption_records_created"] == 1
    (row,) = cursor.inserts
    assert row[5] is None                         # never an invented lot
    assert row[2] == pytest.approx(200.0)


def test_nothing_pending_writes_nothing():
    cursor = _Cursor(pending=[], bom=[], lots=[])
    assert _run(cursor) == {"consumption_records_created": 0}
    assert cursor.inserts == []


# ------------------------------------------------------ real PostgreSQL ---

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")


@pytest.fixture(scope="module")
def pg_erp():
    from erp_simulation.tests.test_story_live import _fresh_erp_db
    with _fresh_erp_db() as conn:
        yield conn


def _site_and_product(cur):
    """A (site code, product) whose every BOM component has a lot at the site's raw store."""
    for site_code, location in ProductionSimulator.SITE_RAW_LOCATION.items():
        cur.execute("""
            SELECT bom.product_id FROM bill_of_materials bom
            WHERE bom.is_active
            GROUP BY bom.product_id
            HAVING bool_and(EXISTS (
                SELECT 1 FROM material_lots ml
                JOIN inventory_locations il ON il.location_id = ml.current_location_id
                JOIN suppliers s ON s.supplier_id = ml.supplier_id AND s.qualification_status = 'APPROVED'
                WHERE ml.item_id = bom.component_item_id AND il.location_code = %s
                  AND ml.quality_status = 'APPROVED' AND ml.remaining_quantity > 0))
            ORDER BY bom.product_id LIMIT 1
        """, (location,))
        row = cur.fetchone()
        if row:
            return site_code, row[0]
    pytest.skip("seed has no fully-stocked BOM")


def test_mes_lots_join_erp_lots_and_suppliers_on_real_postgres(pg_erp):
    tag = uuid.uuid4().hex[:8].upper()
    with pg_erp.cursor() as cur:
        site_code, product_id = _site_and_product(cur)
        cur.execute("""
            INSERT INTO production_runs (run_number, product_id, planned_quantity, actual_quantity,
                                         status, actual_start_time, actual_end_time)
            VALUES (%s, %s, 10, 10, 'COMPLETED', NOW() - INTERVAL '1 hour', NOW())
            RETURNING run_id
        """, (f"RUN-T-{tag}", product_id))
        run_id = cur.fetchone()[0]
        prod = ProductionSimulator.__new__(ProductionSimulator)
        assert prod._allocate_lots_fifo(cur, run_id, product_id, 10, site_code) > 0
        cur.execute("""
            INSERT INTO mes_work_orders (work_order_number, production_run_id, product_id,
                                         planned_quantity, status)
            VALUES (%s, %s, %s, 10, 'COMPLETED')
        """, (f"WO-T-{tag}", run_id, product_id))
    pg_erp.commit()

    assert MESSimulator.__new__(MESSimulator).track_material_consumption(pg_erp, "c-pg")[
        "consumption_records_created"] > 0
    pg_erp.commit()

    with pg_erp.cursor() as cur:
        cur.execute("""
            SELECT mmc.lot_number, ml.lot_number, s.company_name
            FROM mes_material_consumption mmc
            JOIN mes_work_orders mwo ON mwo.work_order_id = mmc.work_order_id
            LEFT JOIN material_lots ml ON ml.lot_number = mmc.lot_number
            LEFT JOIN suppliers s ON s.supplier_id = ml.supplier_id
            WHERE mwo.work_order_number = %s
        """, (f"WO-T-{tag}",))
        rows = cur.fetchall()
        cur.execute("""
            SELECT DISTINCT ml.lot_number FROM lot_production_usage lpu
            JOIN material_lots ml ON ml.lot_id = lpu.lot_id WHERE lpu.production_run_id = %s
        """, (run_id,))
        drawn = {r[0] for r in cur.fetchall()}
    assert rows
    assert {r[0] for r in rows} == drawn          # MES records exactly the lots ERP allocated
    assert all(r[1] == r[0] and r[2] for r in rows)   # each joins to an ERP lot with a supplier


def test_an_order_driven_run_gets_lots_when_its_work_order_is_created(pg_erp):
    """A backorder run (StateAdvancer) is planned with no lots; creating its
    MES work order allocates them FIFO, so lot_mes_tracking and the later
    consumption rows carry real ERP lots. A second pass allocates nothing."""
    tag = uuid.uuid4().hex[:8].upper()
    sim = MESSimulator.__new__(MESSimulator)
    with pg_erp.cursor() as cur:
        site_code, product_id = _site_and_product(cur)
        cur.execute("""
            INSERT INTO production_runs (run_number, product_id, planned_quantity, status, site)
            VALUES (%s, %s, 10, 'RELEASED', %s) RETURNING run_id
        """, (f"RUN-B-{tag}", product_id, site_code))
        run_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO mes_work_orders (work_order_number, production_run_id, product_id,
                                         planned_quantity, status)
            VALUES (%s, %s, %s, 10, 'SCHEDULED') RETURNING work_order_id
        """, (f"WO-B-{tag}", run_id, product_id))
        wo_id = cur.fetchone()[0]
        sim._log_lot_tracking(cur, wo_id, f"WO-B-{tag}", run_id, f"RUN-B-{tag}", site_code, "LINE1")
        assert MESSimulator._ensure_run_lots(cur, run_id, site_code) == 0      # already allocated
        cur.execute("SELECT count(*) FROM lot_production_usage WHERE production_run_id = %s", (run_id,))
        assert cur.fetchone()[0] > 0
        cur.execute("SELECT count(*) FROM lot_mes_tracking WHERE work_order_id = %s", (wo_id,))
        assert cur.fetchone()[0] > 0
        cur.execute("UPDATE mes_work_orders SET status = 'COMPLETED' WHERE work_order_id = %s", (wo_id,))
    pg_erp.commit()
    assert sim.track_material_consumption(pg_erp, "c-b")["consumption_records_created"] > 0
    pg_erp.commit()
    with pg_erp.cursor() as cur:
        cur.execute("""
            SELECT count(*) FILTER (WHERE ml.lot_id IS NULL), count(*)
            FROM mes_material_consumption mmc
            LEFT JOIN material_lots ml ON ml.lot_number = mmc.lot_number
            WHERE mmc.work_order_id = %s
        """, (wo_id,))
        unresolved, total = cur.fetchone()
    assert total > 0 and unresolved == 0
