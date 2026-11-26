# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Aggregate-over-JOIN statuses on the real ERP and MES view schemas (PG).

Workshop rehearsal, 2026-10-14: a grouped SUM of shipped units joined the
order lines to the shipment lines on the product key both carry, came back as
a few rows (not cut) and was hundreds of times too high. This runs the system
agent's own SQL tools (Mode A ``_execute_direct_query`` and Mode B
``_mode_b_postgres_query``) on the db-init schema plus the live story history
and pins each status on real data:

- the product-key SUM and COUNT(*) are ``repeated`` and really multiplied;
  the same totals on the line key and order totals from their own lines are
  ``verified`` and equal the truth;
- a pass-through CTE carries its base table's key into the check;
- the check keeps HAVING: repeats in a group HAVING excludes do not mark the
  groups that are returned;
- a MES view over a join (no declared key) is checked on its whole row;
- a check that runs past its own statement timeout is ``unchecked`` and the
  user's rows still come back.

Skipped unless ``ERP_SIM_TEST_PG_DSN`` names a throwaway server (same
convention as ``test_story_traceability_pg.py``). Run (from ``agentic_digital_thread/``)::

    ERP_SIM_TEST_PG_DSN=postgresql://postgres:<pw>@127.0.0.1:<port>/postgres \\
      PYTHONPATH=. python -m pytest tests/integration/test_aggregate_guard_pg.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from agents.demo import workshop  # noqa: E402
from tests.integration import story_pg_support as support  # noqa: E402

pytestmark = pytest.mark.integration

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")
_ERP = support.ERP_VIEW_SCHEMA
_MES = workshop.LIVE_MES_SYSTEM["schema"]


@pytest.fixture(scope="module")
def story():
    """(ERP connection, story plan) after db-init and the live story history."""
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    pytest.importorskip("pg8000")
    pytest.importorskip("boto3")          # lambda-db-init imports it at module load
    with support.story_databases(_DSN, "ag") as (conns, plan):
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


@pytest.fixture
def tool(story, monkeypatch):
    import agents.aggregate_guard as ag
    import agents.system_agent as sa
    erp, _plan = story
    monkeypatch.setattr(sa.pgdriver, "connect", lambda **_kw: _SharedConn(erp))
    monkeypatch.setattr(sa, "_pg_readonly_conn", lambda _p: _SharedConn(erp))
    monkeypatch.setattr(sa, "_resolve_pg_conn_params", lambda *_a: {})
    ag.clear_key_cache()
    yield sa
    erp.rollback()
    ag.clear_key_cache()


_JOINS = ("FROM shipment_items si JOIN shipments s ON s.shipment_id = si.shipment_id "
          "JOIN customers c ON c.customer_id = s.customer_id "
          "JOIN order_line_items oli ON oli.{key} = si.{key} ")
_GROUPED = ("SELECT c.company_name, SUM(si.quantity_shipped) AS units, "
            "SUM(si.quantity_shipped * oli.unit_price) AS value " + _JOINS
            + "WHERE si.batch_id = '{run}' GROUP BY c.company_name ORDER BY units DESC")
_COUNTED = ("SELECT c.company_name, COUNT(*) AS lines " + _JOINS
            + "WHERE si.batch_id = '{run}' GROUP BY c.company_name")
_CTE = ("WITH lines AS (SELECT shipment_item_id, line_item_id, product_id, quantity_shipped "
        "FROM shipment_items WHERE batch_id = '{run}') "
        "SELECT SUM(l.quantity_shipped) AS units FROM lines l "
        "JOIN order_line_items oli ON oli.{key} = l.{key}")


def _truth(story):
    erp, plan = story
    run = plan.failed_run.run_number
    with erp.cursor() as cur:
        cur.execute("SELECT count(*), SUM(quantity_shipped) FROM public.shipment_items "
                    "WHERE batch_id = %s", (run,))
        lines, units = cur.fetchone()
    return run, lines, units


def _statuses(result):
    return [e["status"] for e in result["aggregate_check"]["aggregates"]]


def test_the_product_key_aggregate_is_repeated(story, tool):
    run, lines, units = _truth(story)
    assert lines >= 2
    result, _ = tool._execute_direct_query(_ERP, {}, _GROUPED.format(key="product_id", run=run))
    assert "truncated" not in result                      # a few rows, nothing cut
    assert sum(row[1] for row in result["rows"]) > units  # really multiplied
    assert result["aggregate_check"]["status"] == "repeated"
    assert _statuses(result) == ["repeated", "repeated"]
    facts = result["aggregate_check"]["aggregates"][0]["repeats"]
    (fact,) = [f for f in facts if f["table"] == "shipment_items"]
    assert fact["key"] == "shipment_item_id" and fact["distinct"] == lines
    assert "aggregate_note" in result


def test_the_line_key_aggregate_is_verified(story, tool):
    run, _lines, units = _truth(story)
    result, _ = tool._execute_direct_query(_ERP, {}, _GROUPED.format(key="line_item_id", run=run))
    assert _statuses(result) == ["verified", "verified"]
    assert "aggregate_note" not in result
    assert sum(row[1] for row in result["rows"]) == units


def test_count_star_is_checked_at_the_root_grain(story, tool):
    run, lines, _units = _truth(story)
    bad, _ = tool._execute_direct_query(_ERP, {}, _COUNTED.format(key="product_id", run=run))
    assert sum(row[1] for row in bad["rows"]) > lines
    assert _statuses(bad) == ["repeated"]
    good, _ = tool._execute_direct_query(_ERP, {}, _COUNTED.format(key="line_item_id", run=run))
    assert sum(row[1] for row in good["rows"]) == lines
    assert _statuses(good) == ["verified"]


def test_a_pass_through_cte_carries_its_key_into_the_check(story, tool):
    run, _lines, units = _truth(story)
    bad, _ = tool._execute_direct_query(_ERP, {}, _CTE.format(key="product_id", run=run))
    assert bad["rows"][0][0] > units and _statuses(bad) == ["repeated"]
    good, _ = tool._execute_direct_query(_ERP, {}, _CTE.format(key="line_item_id", run=run))
    assert good["rows"][0][0] == units and _statuses(good) == ["verified"]


def test_order_totals_from_their_own_lines_are_verified(story, tool):
    sql = ("SELECT o.order_id, SUM(ol.quantity * ol.unit_price) AS order_value, "
           "COUNT(ol.line_item_id) AS lines FROM customer_orders o "
           "JOIN order_line_items ol ON ol.order_id = o.order_id GROUP BY o.order_id")
    result, _ = tool._execute_direct_query(_ERP, {}, sql)
    assert result["row_count"] >= 1
    assert _statuses(result) == ["verified", "verified"]


def test_the_check_keeps_having_scope(story, tool):
    """One order gets two more lines, so its order row repeats three times in
    the join. HAVING keeps only the orders with one line: in every returned
    group the order row appears once, so the order-side SUM is verified. The
    same query without the HAVING returns that order too and is repeated."""
    erp, _plan = story
    with erp.cursor() as cur:
        cur.execute("""INSERT INTO public.order_line_items
                           (order_id, line_number, product_id, quantity, unit_price)
                       SELECT o.order_id, 3000 + g, p.product_id, 1, 1.00
                       FROM (SELECT order_id FROM public.customer_orders LIMIT 1) o,
                            (SELECT product_id FROM public.products LIMIT 1) p,
                            generate_series(1, 2) g""")
    base = ("SELECT o.order_id, SUM(o.total_amount) AS order_total FROM customer_orders o "
            "JOIN order_line_items ol ON ol.order_id = o.order_id GROUP BY o.order_id")
    kept, _ = tool._execute_direct_query(_ERP, {}, base + " HAVING COUNT(ol.line_item_id) = 1")
    # the projected SUM and the HAVING COUNT both read only the kept groups
    assert kept["row_count"] >= 1 and _statuses(kept) == ["verified", "verified"]
    every, _ = tool._execute_direct_query(_ERP, {}, base)
    assert _statuses(every) == ["repeated"]


def test_a_mes_view_over_a_join_is_checked_on_its_whole_row(story, tool):
    """The site MES views read two base tables, so they carry no declared key;
    each view row is still one base row, which the whole-row check sees."""
    sql = ("SELECT w.work_order_number, COUNT(*) AS checks FROM quality_checks q "
           "JOIN work_orders w ON w.work_order_id = q.work_order_id GROUP BY w.work_order_number")
    result, _ = tool._execute_direct_query(_MES, {}, sql)
    assert result["row_count"] >= 1
    assert _statuses(result) == ["verified"], result["aggregate_check"]


def test_a_check_past_its_timeout_is_unchecked(story, tool, monkeypatch):
    """The check gets its own statement timeout (here 1 ms against a join that
    takes far longer); the user's rows still come back, marked unchecked."""
    import agents.aggregate_guard as ag
    run, _lines, units = _truth(story)
    monkeypatch.setattr(ag, "CHECK_TIMEOUT_MS", 1)
    sql = ("SELECT SUM(si.quantity_shipped) AS units FROM shipment_items si "
           "JOIN generate_series(1, 400000) g ON g = 1 "
           f"JOIN order_line_items oli ON oli.line_item_id = si.line_item_id "
           f"WHERE si.batch_id = '{run}'")
    result, _ = tool._execute_direct_query(_ERP, {}, sql)
    assert result["rows"][0][0] == units
    check = result["aggregate_check"]
    assert check["status"] == "unchecked" and check["reason"] == "timeout", check


def test_mode_b_marks_the_product_key_aggregate(story, tool):
    run, _lines, _units = _truth(story)
    out = tool._mode_b_postgres_query(_GROUPED.format(key="product_id", run=run), _ERP, {}, None)
    assert out["aggregate_check"]["status"] == "repeated"
    assert "aggregate_note" in out


def test_an_order_by_aggregate_that_picks_the_top_customer_is_repeated(story, tool):
    """No total is projected, but the fan-out SUM in ORDER BY picks the top
    customer; the result is marked repeated so the ranking is not reported."""
    run, _lines, _units = _truth(story)
    sql = ("SELECT c.company_name " + _JOINS.format(key="product_id")
           + f"WHERE si.batch_id = '{run}' GROUP BY c.company_name "
           "ORDER BY SUM(si.quantity_shipped) DESC LIMIT 1")
    result, _ = tool._execute_direct_query(_ERP, {}, sql)
    assert result["row_count"] == 1
    (entry,) = result["aggregate_check"]["aggregates"]
    assert (entry["clause"], entry["status"]) == ("order by", "repeated")
    fixed, _ = tool._execute_direct_query(_ERP, {}, sql.replace(
        "oli.product_id = si.product_id", "oli.line_item_id = si.line_item_id"))
    assert _statuses(fixed) == ["verified"]


@pytest.mark.parametrize("from_clause", [
    "material_lots ml JOIN lot_production_usage u ON u.lot_id = ml.lot_id",
    "lot_production_usage u JOIN material_lots ml ON u.lot_id = ml.lot_id",
])
def test_count_star_of_usage_rows_per_lot_is_verified_in_either_from_order(story, tool,
                                                                          from_clause):
    """How many runs used one lot: COUNT(*) counts the usage rows, the one lot
    repeats on each. Verified whichever table comes first in FROM."""
    erp, _plan = story
    with erp.cursor() as cur:
        cur.execute("SELECT lot_id, lot_number FROM public.material_lots LIMIT 1")
        lot_id, lot_number = cur.fetchone()
        cur.execute("""INSERT INTO public.lot_production_usage
                           (lot_id, production_run_id, quantity_used, unit_of_measure)
                       SELECT %s, r.run_id, 1, 'KG' FROM public.production_runs r LIMIT 5""",
                    (lot_id,))
        cur.execute("SELECT count(*) FROM public.lot_production_usage WHERE lot_id = %s",
                    (lot_id,))
        (expected,) = cur.fetchone()
    assert expected >= 2
    result, _ = tool._execute_direct_query(
        _ERP, {}, f"SELECT COUNT(*) FROM {from_clause} WHERE ml.lot_number = '{lot_number}'")
    assert result["rows"][0][0] == expected
    assert _statuses(result) == ["verified"], result["aggregate_check"]
