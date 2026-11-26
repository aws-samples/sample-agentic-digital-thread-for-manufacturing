# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The identifier-shape guard on the real ERP view schema (PG).

Workshop rehearsal, 2026-10-18 (Quick lot-total lane): the
ERP system agent looked work-order-form batch ids up in
``shipment_items.batch_id`` (which holds run numbers) and joined the two
``batch_id`` columns to each other. Each of those queries ran and could only
find no rows.

Here onboarding's own measurement and schema render run on the real db-init
DDL plus the live-story history, the system agent's PostgreSQL tool is built
from that baked schema against the same database, and these tests pin that:

* the baked schema carries both columns' measured shapes and the tool reads
  them back;
* a wrong-form lookup and a join of the pair are not run, and run directly on
  PostgreSQL each finds no rows (the guard only stops what cannot match);
* a correct-form lookup runs and returns the database's rows;
* a run number edited from a work-order value the agent read runs and
  returns the database's rows, as does the same number given in the request.

Skipped unless ``ERP_SIM_TEST_PG_DSN`` names a throwaway server (same
convention as ``test_erp_join_keys_pg.py``).
"""
from __future__ import annotations

import asyncio
import json
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
_BATCH_ITEMS = [{"concept": "batch", "field": "lot_mes_tracking.batch_id", "role": "identifier"},
                {"concept": "batch", "field": "shipment_items.batch_id", "role": "identifier"}]


@pytest.fixture(scope="module")
def dbs():
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    pytest.importorskip("pg8000")
    pytest.importorskip("boto3")          # lambda-db-init imports it at module load
    with support.story_databases(_DSN, "sg") as (conns, _plan):
        yield conns


def _rows(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {_ERP}")
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.rollback()
    return rows


@pytest.fixture(scope="module")
def provisioned(dbs):
    """The ERP view schema exactly as onboarding bakes it (samples off), the
    provision state and the graph the measured pair was written to
    (``tests/unit/_field_graph_fake.py``; no Neptune)."""
    import agents.db as db
    import agents.onboard as onboard
    from tests.unit._field_graph_fake import FieldGraph
    mp = pytest.MonkeyPatch()
    routed = support.routed_pg(dbs)
    mp.setattr(onboard, "pg", routed)
    mp.setattr(db, "pg", routed)
    mp.setenv("SCHEMA_SAMPLE_VALUES", "off")
    graph = FieldGraph(auto_fields=True).install(mp)
    try:
        views = sorted(r[0] for r in _rows(
            dbs["erp"], "SELECT table_name FROM information_schema.views WHERE table_schema = %s",
            (_ERP,)))
        ctx = onboard._ProvisionState(
            body=None, system_id="erp", system_name="ERP", plant="", schema=_ERP,
            rules=[], mappings=[], connection_type="PostgreSQL", cm_items=list(_BATCH_ITEMS),
            tables_seen=set(views))
        yield onboard._dp_pg_tables_desc(ctx), ctx, graph
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def baked(provisioned):
    return provisioned[0]


@pytest.fixture(scope="module")
def forms(dbs):
    """One stored value of each column and each column's prefix."""
    lot = _rows(dbs["erp"], "SELECT batch_id FROM lot_mes_tracking ORDER BY batch_id LIMIT 1")[0][0]
    ship = _rows(dbs["erp"], "SELECT batch_id FROM shipment_items ORDER BY batch_id LIMIT 1")[0][0]
    return {"lot": lot, "ship": ship,
            "lot_prefix": lot.split("-", 1)[0], "ship_prefix": ship.split("-", 1)[0]}


def _params(dbs):
    from agents.pgdriver import parse_dsn
    params = {**parse_dsn(_DSN), "dbname": dbs["erp"].get_dsn_parameters()["dbname"]}
    return {k: params[k] for k in ("host", "port", "dbname", "user", "password") if k in params}


@pytest.fixture
def tool(dbs, provisioned):
    """The query tool as the provisioned agent builds it: the baked schema
    and the graph records the agent config carries."""
    from agents import system_agent as sa
    desc, ctx, _graph = provisioned
    tools, _desc = sa._system_tools("erp", _ERP, desc, _params(dbs), "PostgreSQL", None,
                                    concept_items=[],
                                    identifier_records=ctx.distinct_identifier_records)
    return tools[0]


def _call(tool, sql, invocation_state=None):
    """Call ``tool`` the way a Strands agent does: ``stream`` with the
    invocation's state (a context is injected only if the tool asks for one)."""
    state = invocation_state if invocation_state is not None else {}
    state.setdefault("agent", None)
    use = {"toolUseId": "t1", "name": tool.tool_name, "input": {"sql": sql}}

    async def run():
        last = None
        async for event in tool.stream(use, state):
            last = event
        return last

    result = asyncio.run(run()).tool_result
    assert result["status"] == "success", result
    return json.loads(result["content"][0]["text"])


class _Stop(Exception):
    pass


def _in_one_invocation(monkeypatch, tool, question, *sqls):
    """Run ``sqls`` through ``tool`` inside one Mode A invocation of
    ``question``, with the state the runtime builds for it."""
    from agents import system_agent as sa
    outs = []

    def agent(_prompt, invocation_state=None, **_kw):
        for sql in sqls:
            outs.append(_call(tool, sql, invocation_state))
        raise _Stop

    monkeypatch.setattr("agents.config.response_integrity_mode", lambda: "report-only")
    with pytest.raises(_Stop):
        sa._invoke_mode_a_agent_with_state(agent, question)
    return outs


def test_the_baked_schema_carries_both_measured_shapes(baked, forms):
    from agents import identifier_shape_guard as g
    from agents.schema_format import distinct_identifier_pairs
    from agents.system_agent import _shape_signature, _signatures_match
    (pair,) = distinct_identifier_pairs(baked)
    assert (pair["table_a"], pair["table_b"], pair["column"]) == (
        "lot_mes_tracking", "shipment_items", "batch_id")
    assert _signatures_match(_shape_signature(pair["shape_a"]), _shape_signature(forms["lot"]))
    assert _signatures_match(_shape_signature(pair["shape_b"]), _shape_signature(forms["ship"]))
    assert g.context_from(baked) is not None


def test_the_pair_is_stored_in_the_graph_and_the_agent_gets_that_record(provisioned, forms):
    from agents.system_agent import _shape_signature
    _desc, ctx, graph = provisioned
    (row,) = graph.records("erp")
    assert (row["table_a"], row["table_b"], row["column"]) == (
        "lot_mes_tracking", "shipment_items", "batch_id")
    assert row["basis"] == "measured"
    # The stored signature IS the IDENTIFIER-MISS signature of the values stored there.
    assert json.loads(row["signature_a"]) == json.loads(json.dumps(_shape_signature(forms["lot"])))
    assert json.loads(row["signature_b"]) == json.loads(json.dumps(_shape_signature(forms["ship"])))
    (record,) = ctx.distinct_identifier_records
    assert {k: record[k] for k in ("table_a", "table_b", "column", "signature_a",
                                   "signature_b", "read_a", "read_b")} == {
        k: row[k] for k in ("table_a", "table_b", "column", "signature_a", "signature_b",
                            "read_a", "read_b")}
    assert "shape_a" in record and "shape_b" in record     # both print in the note


def test_the_record_alone_guards_a_schema_with_no_note(dbs, provisioned, forms):
    """The guard is built from the graph record, not from the note: with the
    note cut from the schema the wrong-form lookup is still not run, and the
    same lookup on PostgreSQL finds no rows."""
    from agents import system_agent as sa
    from agents.schema_format import DISTINCT_IDENTIFIERS_PREFIX
    desc, ctx, _graph = provisioned
    bare = desc.split(" " + DISTINCT_IDENTIFIERS_PREFIX, 1)[0]
    assert DISTINCT_IDENTIFIERS_PREFIX not in bare
    tools, _d = sa._system_tools("erp", _ERP, bare, _params(dbs), "PostgreSQL", None,
                                 concept_items=[],
                                 identifier_records=ctx.distinct_identifier_records)
    sql = "SELECT si.batch_id FROM shipment_items si WHERE si.batch_id = %s"
    assert _rows(dbs["erp"], sql, (forms["lot"],)) == []
    out = _call(tools[0], sql.replace("%s", f"'{forms['lot']}'"), {})
    assert out["identifier_check"]["kind"] == "wrong_form"


def test_a_work_order_form_lookup_in_shipments_is_not_run_and_could_find_nothing(
        dbs, tool, forms):
    sql = "SELECT si.batch_id FROM shipment_items si WHERE si.batch_id = %s"
    assert _rows(dbs["erp"], sql, (forms["lot"],)) == []
    out = _call(tool, sql.replace("%s", f"'{forms['lot']}'"), {})
    assert out["status"] == "not_run"
    assert out["identifier_check"]["kind"] == "wrong_form"
    assert out["identifier_check"]["column"] == "shipment_items.batch_id"
    assert out["identifier_check"]["paired_column"] == "lot_mes_tracking.batch_id"


def test_a_run_form_lookup_in_shipments_runs_and_returns_the_rows(dbs, tool, forms):
    expected = _rows(dbs["erp"], "SELECT COUNT(*) FROM shipment_items WHERE batch_id = %s",
                     (forms["ship"],))[0][0]
    assert expected > 0
    out = _call(tool, "SELECT COUNT(*) AS n FROM shipment_items "
                      f"WHERE batch_id = '{forms['ship']}'", {})
    assert out["rows"] == [[expected]]


def test_a_join_of_the_two_columns_is_not_run_and_could_find_nothing(dbs, tool):
    sql = ("SELECT COUNT(*) FROM shipment_items si "
           "JOIN lot_mes_tracking lmt ON si.batch_id = lmt.batch_id")
    assert _rows(dbs["erp"], sql) == [(0,)]
    out = _call(tool, sql, {})
    assert out["identifier_check"]["kind"] == "paired_join"


def test_a_run_number_edited_from_a_work_order_value_read_runs_and_returns_the_rows(
        dbs, tool, forms, monkeypatch):
    lot = forms["lot"]
    edited = forms["ship_prefix"] + lot[len(forms["lot_prefix"]):]
    expected = _rows(dbs["erp"], "SELECT COUNT(*) FROM shipment_items WHERE batch_id = %s",
                     (edited,))[0][0]
    read, out = _in_one_invocation(
        monkeypatch, tool, f"Which customers received parts from the batches in {lot}?",
        f"SELECT batch_id FROM lot_mes_tracking WHERE batch_id = '{lot}' LIMIT 1",
        f"SELECT COUNT(*) AS n FROM shipment_items WHERE batch_id = '{edited}'")
    assert read["rows"] == [[lot]]
    assert out.get("status") != "not_run", out
    assert out["rows"] == [[expected]]


def test_the_same_run_number_given_in_the_request_runs(dbs, tool, forms, monkeypatch):
    lot = forms["lot"]
    edited = forms["ship_prefix"] + lot[len(forms["lot_prefix"]):]
    expected = _rows(dbs["erp"], "SELECT COUNT(*) FROM shipment_items WHERE batch_id = %s",
                     (edited,))[0][0]
    _read, out = _in_one_invocation(
        monkeypatch, tool, f"Which customers received parts from batch {edited}?",
        f"SELECT batch_id FROM lot_mes_tracking WHERE batch_id = '{lot}' LIMIT 1",
        f"SELECT COUNT(*) AS n FROM shipment_items WHERE batch_id = '{edited}'")
    assert out["rows"] == [[expected]]


def test_a_dead_lookup_in_an_or_branch_runs_and_returns_the_other_branch(dbs, tool, forms):
    sql = ("SELECT COUNT(*) AS n FROM shipment_items "
           "WHERE batch_id = %s OR batch_id = %s")
    expected = _rows(dbs["erp"], sql, (forms["lot"], forms["ship"]))[0][0]
    assert expected > 0
    out = _call(tool, sql % (f"'{forms['lot']}'", f"'{forms['ship']}'"), {})
    assert out["rows"] == [[expected]]


def test_a_left_join_of_the_pair_runs_and_keeps_every_left_row(dbs, tool):
    sql = ("SELECT COUNT(*) AS n FROM lot_mes_tracking lmt "
           "LEFT JOIN shipment_items si ON si.batch_id = lmt.batch_id")
    expected = _rows(dbs["erp"], sql)[0][0]
    assert expected > 0
    assert _call(tool, sql, {})["rows"] == [[expected]]

