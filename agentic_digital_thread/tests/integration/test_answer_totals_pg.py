# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The total shape the SQL system agents are told to use is verified (PG).

EFFICIENCY step 2 (``system_agent._efficiency_prompt``) tells a SQL system
agent to get a total the question asks for from its own aggregate query over
the same FROM, JOIN and WHERE as the per-group query, and never to fold it
into the grouped query (GROUPING SETS, ROLLUP, CUBE or UNION). This runs those
queries through the system agent's own SQL tool on the db-init schema plus
the live story history and pins, on real data, what the aggregate check
(``agents.aggregate_guard``) says about them:

- the per-group query (with or without HAVING), a second grouping and the
  separate overall total, over a join on the line key, are ``verified``, and
  the total equals the per-group figures added up and the truth;
- the same separate total over a join that repeats the summed rows is still
  ``repeated`` (the check is not blind to this shape);
- the folded forms the prompt rules out are not verified: a ROLLUP with
  HAVING reuses each source row at two levels and comes back ``repeated``
  although its figures are right, and a UNION total row is ``unchecked``.
  If the check learns to verify them, this test says so and the prompt's
  ban can be revisited.

Skipped when the aggregate check is not in this tree, and unless
``ERP_SIM_TEST_PG_DSN`` names a throwaway server (same convention as
``test_story_traceability_pg.py``). Run (from ``agentic_digital_thread/``)::

    ERP_SIM_TEST_PG_DSN=postgresql://postgres:<pw>@127.0.0.1:<port>/postgres \\
      PYTHONPATH=. python -m pytest tests/integration/test_answer_totals_pg.py -q
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal
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
    pytest.importorskip("agents.aggregate_guard")
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    pytest.importorskip("pg8000")
    pytest.importorskip("boto3")          # lambda-db-init imports it at module load
    with support.story_databases(_DSN, "at") as (conns, plan):
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
_MEASURES = ("SUM(si.quantity_shipped) AS units, "
             "SUM(si.quantity_shipped * oli.unit_price) AS value ")


def _batches(story) -> tuple[str, list[str], int, Decimal]:
    """The story's failed run and up to four other batches with few shipment
    lines, as a WHERE clause, plus their true line count and units."""
    erp, plan = story
    run = plan.failed_run.run_number
    with erp.cursor() as cur:
        cur.execute("SELECT batch_id FROM public.shipment_items WHERE batch_id <> %s "
                    "GROUP BY batch_id HAVING count(*) <= 3 ORDER BY batch_id LIMIT 4", (run,))
        ids = [run] + [row[0] for row in cur.fetchall()]
        cur.execute("SELECT count(*), SUM(quantity_shipped) FROM public.shipment_items "
                    "WHERE batch_id = ANY(%s)", (ids,))
        lines, units = cur.fetchone()
    where = "WHERE si.batch_id IN (" + ", ".join(f"'{i}'" for i in ids) + ") "
    return where, ids, lines, units


def _check(result):
    return result["aggregate_check"]


def _statuses(result):
    return [e["status"] for e in _check(result)["aggregates"]]


def test_the_recommended_shape_is_verified_and_adds_up(story, tool):
    where, ids, lines, units = _batches(story)
    assert len(ids) >= 2 and lines >= 2
    joins = _JOINS.format(key="line_item_id")
    per_batch_customer = (f"SELECT si.batch_id, c.company_name, {_MEASURES}{joins}{where}"
                          "GROUP BY si.batch_id, c.company_name")
    per_customer = f"SELECT c.company_name, {_MEASURES}{joins}{where}GROUP BY c.company_name"
    total = f"SELECT {_MEASURES}{joins}{where}"

    grouped, _ = tool._execute_direct_query(_ERP, {}, per_batch_customer)
    regrouped, _ = tool._execute_direct_query(_ERP, {}, per_customer)
    overall, _ = tool._execute_direct_query(_ERP, {}, total)
    for result in (grouped, regrouped, overall):
        assert "truncated" not in result
        assert _check(result)["status"] == "verified", _check(result)
        assert _statuses(result) == ["verified", "verified"]
        assert "aggregate_note" not in result

    (total_units, total_value), = overall["rows"]
    assert total_units == units
    for rows, first in ((grouped["rows"], 2), (regrouped["rows"], 1)):
        assert sum(row[first] for row in rows) == total_units
        assert sum(Decimal(str(row[first + 1])) for row in rows) == Decimal(str(total_value))


def test_a_per_group_query_with_having_is_verified(story, tool):
    where, _ids, _lines, _units = _batches(story)
    sql = (f"SELECT c.company_name, {_MEASURES}{_JOINS.format(key='line_item_id')}{where}"
           "GROUP BY c.company_name HAVING SUM(si.quantity_shipped) > 0")
    result, _ = tool._execute_direct_query(_ERP, {}, sql)
    assert result["row_count"] >= 1
    assert _check(result)["status"] == "verified", _check(result)


def test_the_separate_total_still_catches_a_repeating_join(story, tool):
    where, _ids, _lines, units = _batches(story)
    sql = f"SELECT {_MEASURES}{_JOINS.format(key='product_id')}{where}"
    result, _ = tool._execute_direct_query(_ERP, {}, sql)
    assert result["rows"][0][0] > units                   # really multiplied
    assert _check(result)["status"] == "repeated"
    assert "aggregate_note" in result


def test_the_folded_forms_the_prompt_rules_out_are_not_verified(story, tool):
    """Right figures, but the check cannot verify them: the reason the prompt
    asks for a separate total query instead."""
    where, _ids, _lines, units = _batches(story)
    joins = _JOINS.format(key="line_item_id")
    rollup = (f"SELECT c.company_name, {_MEASURES}{joins}{where}"
              "GROUP BY ROLLUP (c.company_name) HAVING SUM(si.quantity_shipped) > 0")
    result, _ = tool._execute_direct_query(_ERP, {}, rollup)
    (grand,) = [row for row in result["rows"] if row[0] is None]
    assert grand[1] == units                              # the figures are right
    assert _check(result)["status"] == "repeated", _check(result)

    union = (f"SELECT c.company_name, {_MEASURES}{joins}{where}GROUP BY c.company_name "
             f"UNION ALL SELECT NULL, {_MEASURES}{joins}{where}")
    result, _ = tool._execute_direct_query(_ERP, {}, union)
    assert _check(result)["status"] == "unchecked", _check(result)
