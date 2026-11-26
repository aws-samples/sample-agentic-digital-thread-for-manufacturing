# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Provision's mirrored measure tags on the real ERP schema (PG).

Workshop rehearsals, 2026-10-14: the onboarding mapper tagged
``order_line_items.line_total`` with the money concept (``cost [measure]``)
on some runs and only as ``invoice [measure]`` on others. Provision step b3a
now adds the tag from measured evidence: the measured JoinPath from the order
line to its order, the identical declared type (``numeric(15,2)``) of the
line total and the order total, the word they share, and the line's own
price column already carrying the concept in that type.

These tests run b3 and b3a on the db-init DDL through the ``erp_data``
pass-through views (the schema the ERP agent is onboarded on) with the
measure tags of the failing run (``live1014/p2b/concept_mappings.new.txt``)
and pin every tag the rule adds across the whole schema, so a false positive
anywhere (the lot tables hold quantities in the money type, and an invoice's
amount paid shares only ``amount`` with the order's shipping charge) fails
here.

Skipped unless ``ERP_SIM_TEST_PG_DSN`` names a throwaway server (same
convention as ``test_erp_join_keys_pg.py``). Run (from ``agentic_digital_thread/``)::

    ERP_SIM_TEST_PG_DSN=postgresql://postgres:<pw>@127.0.0.1:<port>/postgres \\
      PYTHONPATH=. python -m pytest tests/integration/test_mirrored_measure_tags_pg.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tests.integration import story_pg_support as support  # noqa: E402

pytestmark = pytest.mark.integration

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")
_ERP = support.ERP_VIEW_SCHEMA

#: Every ``measure`` tag of the failing run's ERP onboarding, as the review
#: confirmed it (``table.column concept``). ``order_line_items.line_total``
#: carries only the invoice entity.
_FAILING_RUN_MEASURES = """
bill_of_materials.quantity_required bill-of-materials
customer_orders.shipping_amount cost
customer_orders.subtotal cost
customer_orders.tax_amount cost
customer_orders.total_amount cost
customer_orders.order_date sales-order
customer_orders.requested_delivery_date schedule
customers.credit_limit cost
inventory_balances.quantity_allocated inventory-level
inventory_balances.quantity_available inventory-level
inventory_balances.quantity_on_hand inventory-level
inventory_balances.last_transaction_date inventory-transaction
inventory_items.unit_cost cost
inventory_items.reorder_point mro-inventory
inventory_items.reorder_quantity mro-inventory
inventory_transactions.unit_cost cost
inventory_transactions.quantity inventory-transaction
inventory_transactions.transaction_date inventory-transaction
inventory_transfers.approved_at event-time
inventory_transfers.completed_at event-time
inventory_transfers.requested_at event-time
inventory_transfers.quantity inventory-transaction
invoice_line_items.line_total cost
invoice_line_items.unit_price cost
invoice_line_items.quantity invoice
invoices.balance_due cost
invoices.subtotal cost
invoices.tax_amount cost
invoices.total_amount cost
invoices.invoice_date invoice
invoices.amount_paid payment
invoices.due_date schedule
item_suppliers.lead_time_days schedule
lot_mes_tracking.consumption_date event-time
lot_mes_tracking.quantity_consumed material-consumption
lot_production_usage.usage_date event-time
lot_production_usage.quantity_used material-consumption
lot_warehouse_movements.movement_date event-time
lot_warehouse_movements.quantity inventory-transaction
material_lots.expiry_date event-time
material_lots.received_date event-time
material_lots.received_quantity inventory-level
material_lots.remaining_quantity inventory-level
order_line_items.unit_price cost
order_line_items.line_total invoice
order_line_items.quantity invoice
payments.amount payment
payments.payment_date payment
po_line_items.line_total cost
po_line_items.unit_price cost
po_line_items.quantity_received inventory-transaction
po_line_items.quantity_ordered purchase-order
po_receipts.receipt_date inventory-transaction
products.list_price cost
products.standard_cost cost
purchase_orders.total_amount cost
purchase_orders.actual_delivery_date purchase-order
purchase_orders.po_date purchase-order
purchase_orders.expected_delivery_date schedule
shipment_items.quantity_shipped shipment
shipments.estimated_delivery_date schedule
shipments.actual_delivery_date shipment
shipments.shipment_date shipment
suppliers.lead_time_days supplier
"""


def _measures(text: str) -> dict:
    out: dict = {}
    for line in text.strip().splitlines():
        field, concept = line.split()
        out.setdefault(field, []).append(concept)
    return out


@pytest.fixture(scope="module")
def dbs():
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    pytest.importorskip("pg8000")
    pytest.importorskip("boto3")          # lambda-db-init imports it at module load
    with support.story_databases(_DSN, "mm", with_history=False) as (conns, _plan):
        yield conns


@pytest.fixture
def onboard_pg(dbs, monkeypatch):
    import agents.db as db
    import agents.onboard as onboard
    routed = support.routed_pg(dbs)
    monkeypatch.setattr(onboard, "pg", routed)
    monkeypatch.setattr(db, "pg", routed)
    monkeypatch.setattr(onboard, "neptune_write", lambda *_a, **_k: True)
    return onboard


def _ctx(onboard, measures: dict):
    """b3 then b3a on the ERP view schema, every column with a reviewed map
    row (C3: the map step returns one for every column), untouched by the
    steward, carrying the run's measure tags."""
    from agents.catalog import CORE_CONCEPTS
    rows = []
    for (table, column) in sorted(onboard._schema_column_types(_ERP)):
        concepts = measures.get(f"{table}.{column}", [])
        rows.append({"table": table, "field": f"{table}.{column}",
                     "mappings": [{"concept": c, "role": "measure", "confidence": 0.9}
                                  for c in concepts],
                     "concepts": [{"concept": c, "confidence": 0.9} for c in concepts],
                     "concept": concepts[0] if concepts else "needs_review"})
    ctx = onboard._ProvisionState(
        body=None, system_id="erp", system_name="ERP", plant="", schema=_ERP,
        rules=[], mappings=rows, connection_type="PostgreSQL", cm_items=[],
        committable=set(CORE_CONCEPTS))
    onboard._dp_internal_joinpaths(ctx)
    onboard._dp_mirrored_measure_tags(ctx)
    return ctx


def _added(ctx):
    return sorted((t["field"], t["concept"], t["from_field"]) for t in ctx.measure_tags_mirrored)


def test_the_views_keep_each_columns_declared_type(onboard_pg):
    """Pass-through views carry the base column's precision and scale (a
    generated base column included), which the rule compares exactly."""
    types_ = onboard_pg._schema_column_types(_ERP)
    for column in (("order_line_items", "unit_price"), ("order_line_items", "line_total"),
                   ("customer_orders", "total_amount"), ("products", "list_price"),
                   ("material_lots", "received_quantity")):
        assert types_[column] == "numeric(15,2)", (column, types_.get(column))
    assert types_[("order_line_items", "quantity")] == "integer"
    assert types_[("bill_of_materials", "quantity_required")] == "numeric(10,4)"


def test_b3_records_the_order_lines_measured_joins(onboard_pg):
    ctx = _ctx(onboard_pg, {})
    for join in (("order_line_items", "order_id", "customer_orders", "order_id"),
                 ("order_line_items", "product_id", "products", "product_id"),
                 ("shipment_items", "line_item_id", "order_line_items", "line_item_id")):
        assert join in ctx.measured_joins, ctx.measured_joins


def test_the_failing_runs_line_total_takes_the_money_concept_and_nothing_else_is_tagged_wrongly(
        onboard_pg):
    """Only the order line total is added. ``invoices.amount_paid`` (a
    payment) shares only the generic ``amount`` with the order's shipping
    charge and has no generation expression, so it takes nothing; the line
    total shares only ``total`` with the order total and is admitted by its
    own expression over the line price."""
    ctx = _ctx(onboard_pg, _measures(_FAILING_RUN_MEASURES))
    assert _added(ctx) == [
        ("order_line_items.line_total", "cost", "customer_orders.total_amount"),
    ]
    line = next(m for m in ctx.mappings if m["field"] == "order_line_items.line_total")
    assert [r["concept"] for r in line["mappings"]] == ["invoice", "cost"]
    paid = next(m for m in ctx.mappings if m["field"] == "invoices.amount_paid")
    assert [r["concept"] for r in paid["mappings"]] == ["payment"]


def test_the_views_carry_their_base_tables_generation_expressions(onboard_pg):
    """The ERP is onboarded through pass-through views, which have no
    generated columns of their own; the read resolves each view column to
    its base column's expression."""
    from agents import measure_equivalence as me
    generated = onboard_pg._schema_generated_columns(_ERP)
    types_ = onboard_pg._schema_column_types(_ERP)

    def named(table, column):
        cols = {c for (t, c) in types_ if t == table}
        return me.expression_columns(generated[(table, column)], cols)
    assert named("order_line_items", "line_total") == {"quantity", "unit_price"}
    assert named("invoices", "balance_due") == {"total_amount", "amount_paid"}
    assert ("order_line_items", "unit_price") not in generated
    assert ("invoices", "amount_paid") not in generated
    assert set(generated) <= set(types_)


def test_every_generated_column_a_pass_through_view_exposes_keeps_its_expression(onboard_pg):
    """Every ``erp_data`` view is ``SELECT * FROM public.<same table>``, so the
    view lineage must give each of them exactly the base table's generated
    columns (the catalog's own ``is_generated`` is the oracle)."""
    res = onboard_pg.pg(_ERP, """
        SELECT c.table_name::text, c.column_name::text
        FROM information_schema.columns c
        JOIN information_schema.views v
            ON v.table_schema = %s AND v.table_name = c.table_name
        WHERE c.table_schema = 'public' AND c.is_generated = 'ALWAYS'
    """, (_ERP,))
    expected = {(t, c) for t, c in res["rows"]}
    assert ("order_line_items", "line_total") in expected
    assert set(onboard_pg._schema_generated_columns(_ERP)) == expected


#: A header and a line table in a base schema, onboarded through a view
#: schema. The base line table has a generated ``line_total`` over its price;
#: ``{select}`` is the line view's query.
_SHADOW_DDL = """
    CREATE SCHEMA {base};
    CREATE SCHEMA {views};
    CREATE TABLE {base}.hdr (hdr_id integer PRIMARY KEY, total_amount numeric(15,2));
    CREATE TABLE {base}.ln (
        ln_id integer PRIMARY KEY,
        hdr_id integer REFERENCES {base}.hdr (hdr_id),
        quantity integer,
        unit_price numeric(15,2),
        paid_amount numeric(15,2),
        line_total numeric(15,2) GENERATED ALWAYS AS (quantity * unit_price) STORED);
    CREATE VIEW {views}.hdr AS SELECT * FROM {base}.hdr;
"""
_SHADOW_LINE_VIEWS = {
    "pass": "CREATE VIEW {views}.ln AS SELECT * FROM {base}.ln",
    # The reviewer's case: a payment column of the same type, renamed to the
    # name of the base table's (unused) generated column.
    "renamed": ("CREATE VIEW {views}.ln AS SELECT ln_id, hdr_id, quantity, unit_price, "
                "paid_amount AS line_total FROM {base}.ln"),
}


@pytest.mark.parametrize("case, added", [
    ("pass", [("ln.line_total", "cost", "hdr.total_amount")]),
    ("renamed", []),
], ids=["pass-through view", "renamed column shadows an unused generated column"])
def test_a_view_column_named_like_a_generated_base_column_inherits_nothing_unless_passed_through(
        dbs, onboard_pg, case, added):
    """The line view's ``line_total`` shares only the generic ``total`` with
    the header total, so only a generation expression of its own can admit
    the money tag. When the view selects ``paid_amount AS line_total``, the
    base table's generated ``line_total`` is not what the view shows, and the
    payment must not be tagged as the line's money. b3's measured join is
    given directly (the views carry no declared key)."""
    from agents.catalog import CORE_CONCEPTS
    from agents.demo import db_routing
    from tests.integration._pg_sql import stmt
    views, base = f"erp_shadow_{case}", f"erp_shadowbase_{case}"
    assert db_routing.is_erp_schema(views) and db_routing.is_erp_schema(base)
    conn = dbs["erp"]
    with conn.cursor() as cur:
        cur.execute(stmt(_SHADOW_DDL, base=base, views=views))
        cur.execute(stmt(_SHADOW_LINE_VIEWS[case], base=base, views=views))
    conn.commit()
    measures = {"hdr.total_amount": ["cost"], "ln.unit_price": ["cost"],
                "ln.line_total": ["payment"]}
    rows = []
    for (table, column) in sorted(onboard_pg._schema_column_types(views)):
        concepts = measures.get(f"{table}.{column}", [])
        rows.append({"table": table, "field": f"{table}.{column}",
                     "mappings": [{"concept": c, "role": "measure", "confidence": 0.9}
                                  for c in concepts],
                     "concepts": [{"concept": c, "confidence": 0.9} for c in concepts],
                     "concept": concepts[0] if concepts else "needs_review"})
    ctx = onboard_pg._ProvisionState(
        body=None, system_id="erp", system_name="ERP", plant="", schema=views,
        rules=[], mappings=rows, connection_type="PostgreSQL", cm_items=[],
        committable=set(CORE_CONCEPTS))
    ctx.measured_joins = [("ln", "hdr_id", "hdr", "hdr_id")]
    onboard_pg._dp_mirrored_measure_tags(ctx)
    assert _added(ctx) == added
    generated = onboard_pg._schema_generated_columns(views)
    assert (("ln", "line_total") in generated) is (case == "pass")


def test_the_line_price_takes_it_when_only_the_line_total_has_it(onboard_pg):
    measures = _measures(_FAILING_RUN_MEASURES)
    measures["order_line_items.line_total"] = ["cost"]
    del measures["order_line_items.unit_price"]
    ctx = _ctx(onboard_pg, measures)
    assert ("order_line_items.unit_price", "cost", "products.list_price") in _added(ctx)


def test_no_lot_quantity_takes_the_lots_stock_concept(onboard_pg):
    """The lot tables keep quantities in ``numeric(15,2)``, the money type,
    and share the word ``quantity`` across their joins to the lot; none of
    their own columns is a stock level, so none is tagged one."""
    ctx = _ctx(onboard_pg, _measures(_FAILING_RUN_MEASURES))
    assert not [t for t in ctx.measure_tags_mirrored if t["field"].startswith("lot_")]
    assert not [t for t in ctx.measure_tags_mirrored if t["concept"] == "inventory-level"]
