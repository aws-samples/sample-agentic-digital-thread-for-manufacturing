# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The PO trigger sees lot depletion (lot traceability keeps flowing).

Production consumes material_lots and never decrements inventory_balances, so
a balance-only trigger stopped ordering once lots ran dry (live 2026-09-26:
Cedar Rapids had no usable lots and no POs since 2026-08-08).

Runs LOW_STOCK_SQL against a real PostgreSQL when ERP_SIM_TEST_PG_DSN is set
(e.g. a throwaway container); skipped otherwise.

    ERP_SIM_TEST_PG_DSN=postgresql://postgres:pw@localhost:55432/postgres \\
        python -m pytest erp_simulation/tests -q
"""
from __future__ import annotations

import os
import uuid

import pytest

from erp_simulation.procurement_simulator import LOW_STOCK_SQL
from erp_simulation.tests._pg_sql import stmt

DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")

_SCHEMA = """
CREATE TABLE inventory_items (item_id uuid PRIMARY KEY, item_code text, item_name text,
  item_type text, is_active boolean, reorder_point numeric, reorder_quantity numeric,
  unit_cost numeric);
CREATE TABLE inventory_balances (item_id uuid, location_id uuid, quantity_on_hand numeric);
CREATE TABLE purchase_orders (po_id uuid PRIMARY KEY, status text);
CREATE TABLE po_line_items (po_id uuid, item_id uuid, quantity_ordered numeric,
  quantity_received numeric);
CREATE TABLE production_runs (run_id uuid, product_id uuid, planned_quantity numeric, status text);
CREATE TABLE bill_of_materials (product_id uuid, component_item_id uuid, quantity_required numeric);
CREATE TABLE suppliers (supplier_id uuid PRIMARY KEY, qualification_status text);
CREATE TABLE material_lots (lot_id uuid, item_id uuid, supplier_id uuid,
  remaining_quantity numeric, quality_status text);
"""


@pytest.fixture
def cur():
    from erp_simulation import pgdriver
    conn = pgdriver.connect(DSN)
    conn.autocommit = False
    c = conn.cursor()
    schema = "t_" + uuid.uuid4().hex[:8]
    c.execute(stmt("CREATE SCHEMA {schema}; SET search_path TO {schema};", schema=schema))
    c.execute(_SCHEMA)
    yield c
    conn.rollback()
    conn.close()


def _item(c, code, balance, lots=(), supplier_status="APPROVED"):
    item = uuid.uuid4()
    sup = uuid.uuid4()
    c.execute("INSERT INTO inventory_items VALUES (%s,%s,%s,'RAW_MATERIAL',TRUE,100,500,1)",
              (str(item), code, code))
    c.execute("INSERT INTO inventory_balances VALUES (%s,%s,%s)", (str(item), str(uuid.uuid4()), balance))
    c.execute("INSERT INTO suppliers VALUES (%s,%s)", (str(sup), supplier_status))
    for qty, status in lots:
        c.execute("INSERT INTO material_lots VALUES (%s,%s,%s,%s,%s)",
                  (str(uuid.uuid4()), str(item), str(sup), qty, status))
    return code


def _ordered(c):
    c.execute(LOW_STOCK_SQL)
    return {row[1] for row in c.fetchall()}


def test_depleted_lots_trigger_a_po_even_when_the_balance_looks_healthy(cur):
    _item(cur, "DRY", balance=10_000, lots=[(0, "APPROVED")])
    assert "DRY" in _ordered(cur)


def test_only_approved_lots_from_approved_suppliers_count_as_stock(cur):
    _item(cur, "QUAR", balance=10_000, lots=[(10_000, "QUARANTINE")])
    _item(cur, "DISQ", balance=10_000, lots=[(10_000, "APPROVED")], supplier_status="DISQUALIFIED")
    assert {"QUAR", "DISQ"} <= _ordered(cur)


def test_healthy_lots_and_balance_do_not_trigger(cur):
    _item(cur, "OK", balance=10_000, lots=[(10_000, "APPROVED")])
    assert "OK" not in _ordered(cur)


def test_items_without_lots_keep_balance_only_behaviour(cur):
    _item(cur, "NOLOT-HIGH", balance=10_000)
    _item(cur, "NOLOT-LOW", balance=5)
    got = _ordered(cur)
    assert "NOLOT-HIGH" not in got and "NOLOT-LOW" in got
