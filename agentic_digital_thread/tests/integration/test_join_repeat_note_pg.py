# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A multiplying JOIN on the real ERP schema comes back with the repeat note (PG).

Workshop rehearsal, 2026-10-14: the ERP system agent joined an order-line
table to the shipment lines on the ``product_id`` both carry, so every
shipment line repeated once per order line of that product; the detail query
came back cut, and the grouped re-run kept the same JOIN, so its totals came
back hundreds of times too high. This runs the system agent's own SQL tool
(``_execute_direct_query``) on the db-init schema plus the live story
history and pins that the product-key join comes back cut WITH the repeat
note, its total is multiplied, and the line-key join returns exactly the
shipment lines with no note. Offline unit coverage:
tests/unit/test_join_repeat_note.py.

Skipped unless ``ERP_SIM_TEST_PG_DSN`` names a throwaway server (same
convention as ``test_story_traceability_pg.py``). Run (from ``agentic_digital_thread/``)::

    ERP_SIM_TEST_PG_DSN=postgresql://postgres:<pw>@127.0.0.1:<port>/postgres \\
      PYTHONPATH=. python -m pytest tests/integration/test_join_repeat_note_pg.py -q
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


@pytest.fixture(scope="module")
def story():
    """(ERP connection, story plan) after db-init and the live story history."""
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    pytest.importorskip("pg8000")
    pytest.importorskip("boto3")          # lambda-db-init imports it at module load
    with support.story_databases(_DSN, "rn") as (conns, plan):
        yield conns["erp"], plan


class _SharedConn:
    """The test connection handed to the SQL tool as its own: the tool's
    session settings and close are no-ops, so its statements run inside this
    test's transaction, which the test rolls back."""

    def __init__(self, conn):
        self._conn = conn

    def set_session(self, **_kwargs):
        return None

    def cursor(self):
        return self._conn.cursor()

    def close(self):
        return None


_SHIPPED_SQL = ("SELECT si.shipment_id, si.product_id, si.quantity_shipped, ol.unit_price "
                "FROM shipment_items si JOIN order_line_items ol ON ol.{key} = si.{key} "
                "WHERE si.batch_id = {run} ORDER BY si.shipment_id")
_TOTAL_SQL = ("SELECT SUM(si.quantity_shipped) FROM shipment_items si "
              "JOIN order_line_items ol ON ol.{key} = si.{key} WHERE si.batch_id = {run}")


def _sql(conn, template: str, key: str, run: str) -> str:
    from agents.pgdriver import sql
    return sql.SQL(template).format(key=sql.Identifier(key), run=sql.Literal(run)).as_string(conn)


def test_a_multiplying_join_comes_back_with_the_repeat_note(story, monkeypatch):
    """The measured fan-out on the real schema, through the system agent's own
    SQL tool: the product-key join of the failed run's shipment lines comes
    back cut, its kept rows are exact copies (nothing from the order line tells
    them apart) and carry the repeat note, its total is multiplied, and the
    line-key join returns exactly the shipment lines with no note."""
    import agents.system_agent as sa
    from agents.response_integrity_query import REPEAT_NOTE_KEY, REPEATED_ROWS_KEY
    erp, plan = story
    run = plan.failed_run.run_number
    monkeypatch.setattr(sa.pgdriver, "connect", lambda **_kw: _SharedConn(erp))
    try:
        with erp.cursor() as cur:
            # A live simulator keeps adding order lines (2,004 for one product on
            # a rehearsal account); add enough for the product join to pass the cap.
            cur.execute("""INSERT INTO public.order_line_items
                               (order_id, line_number, product_id, quantity, unit_price)
                           SELECT o.order_id, 1000 + g, p.product_id, 1, 1.00
                           FROM (SELECT order_id FROM public.customer_orders LIMIT 1) o,
                                (SELECT DISTINCT product_id FROM public.shipment_items
                                 WHERE batch_id = %s) p,
                                generate_series(1, 200) g""", (run,))
            cur.execute("SELECT count(*), SUM(quantity_shipped) FROM public.shipment_items "
                        "WHERE batch_id = %s", (run,))
            lines, units = cur.fetchone()
        assert lines >= 2
        wrong, _ = sa._execute_direct_query(_ERP, {}, _sql(erp, _SHIPPED_SQL, "product_id", run))
        right, _ = sa._execute_direct_query(_ERP, {}, _sql(erp, _SHIPPED_SQL, "line_item_id", run))
        assert wrong["truncated"] is True and wrong["row_count"] == sa._MAX_ROWS
        fact = wrong[REPEATED_ROWS_KEY]
        assert fact["rows"] == sa._MAX_ROWS and 1 <= fact["distinct_rows"] <= 10, fact
        assert "exact copies of another row kept" in wrong[REPEAT_NOTE_KEY]
        assert right["row_count"] == lines and "truncated" not in right
        assert REPEAT_NOTE_KEY not in right
        with erp.cursor() as cur:
            cur.execute("SET search_path TO " + _ERP)
            cur.execute(_sql(erp, _TOTAL_SQL, "product_id", run))
            multiplied = cur.fetchone()[0]
            cur.execute(_sql(erp, _TOTAL_SQL, "line_item_id", run))
            exact = cur.fetchone()[0]
        assert exact == units and multiplied > 100 * units
    finally:
        erp.rollback()


def test_a_correct_one_to_many_listing_gets_no_note(story, monkeypatch):
    """Regression on the real schema: orders joined to their
    own lines, cut at the cap. Each row is a distinct line, so there is no
    repeat note even though every order id repeats."""
    import agents.system_agent as sa
    from agents.response_integrity_query import REPEAT_NOTE_KEY, REPEATED_ROWS_KEY
    erp, _plan = story
    monkeypatch.setattr(sa.pgdriver, "connect", lambda **_kw: _SharedConn(erp))
    try:
        with erp.cursor() as cur:
            cur.execute("""INSERT INTO public.order_line_items
                               (order_id, line_number, product_id, quantity, unit_price)
                           SELECT o.order_id, 2000 + g, p.product_id, 1 + g % 3, 1.00
                           FROM public.customer_orders o,
                                (SELECT product_id FROM public.products LIMIT 1) p,
                                generate_series(1, 40) g""")
        sql = ("SELECT o.order_id, ol.line_item_id, ol.quantity FROM customer_orders o "
               "JOIN order_line_items ol ON ol.order_id = o.order_id ORDER BY o.order_id")
        result, _ = sa._execute_direct_query(_ERP, {}, sql)
        assert result["truncated"] is True and result["row_count"] == sa._MAX_ROWS
        order_ids = {row[0] for row in result["rows"]}
        assert len(order_ids) < sa._MAX_ROWS                # order ids repeat
        assert REPEAT_NOTE_KEY not in result and REPEATED_ROWS_KEY not in result
    finally:
        erp.rollback()
