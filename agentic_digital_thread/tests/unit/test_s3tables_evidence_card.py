# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An S3 Tables call whose query carried a table's row exclusion keeps the
applied exclusion under that call's toolUseId for its evidence card, exactly
as the Athena path does (scope 1). A read without the rule, and a call without
an id, keep nothing.

Offline: the engine's ``connection_factory`` seam injects an in-memory DuckDB;
the row-exclusion state is set directly through ``row_exclusion.remember``.
Dataset-neutral fixture.
"""
import json
from types import SimpleNamespace

import duckdb

import agents.row_exclusion as row_exclusion
import agents.state_times as state_times
import agents.storage_bounds as storage_bounds
import agents.system_agent as sa
from agents.tool_result_capture import APPLIED_EXCLUSIONS_STATE_KEY

ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/demo-bucket"
SYS = "s3t-card"
LABEL = "non-record rows"
CONDITIONS = [{"column": "route_name", "op": "not_empty"},
              {"column": "segment_type", "op": "not_in", "values": ["BIND"]}]
PREDICATE = row_exclusion.predicate_sql(row_exclusion.exclusion_from(LABEL, CONDITIONS))


def _factory():
    def factory():
        con = duckdb.connect()
        con.execute("ATTACH ':memory:' AS s3t")
        con.execute("CREATE SCHEMA s3t.flow")
        con.execute("CREATE TABLE s3t.flow.segments(observed_at TIMESTAMP, route_name VARCHAR, segment_type VARCHAR)")
        con.execute("INSERT INTO s3t.flow.segments VALUES (TIMESTAMP '2026-10-06 00:00:00','','WELD')")
        return con
    return factory


def _run_tool():
    from agents.duckdb_tools import make_duckdb_tools
    raw = make_duckdb_tools({"table_bucket_arn": ARN}, source_system=SYS,
                            connection_factory=_factory())
    for t in sa._guard_duckdb_tools(raw, {"table_bucket_arn": ARN}, SYS):
        if getattr(t, "tool_name", "") == "s3tables_run_query":
            return t.__wrapped__
    raise AssertionError("no guarded s3tables_run_query")


def _call(sql, call_id, state, run=None, call=None):
    run = run or _run_tool()
    call = call or sa._ModeACall("exactly how many segment records on 10/06")
    token = sa._MODE_A_CALL.set(call)
    ctx = SimpleNamespace(
        tool_use={"name": "s3tables_run_query", "toolUseId": call_id, "input": {"sql": sql}},
        invocation_state=state)
    try:
        return json.loads(run(sql, tool_context=ctx))
    finally:
        sa._MODE_A_CALL.reset(token)


def setup_function(_):
    for mod in (row_exclusion, state_times, storage_bounds):
        mod.reset()
    row_exclusion.remember(SYS, {"segments": row_exclusion.exclusion_from(LABEL, CONDITIONS)})


def teardown_function(_):
    row_exclusion.reset()


def _present():
    return ("SELECT COUNT(*) AS n FROM s3t.flow.segments "
            f"WHERE observed_at >= TIMESTAMP '2026-10-05' AND {PREDICATE}")


def test_applied_exclusion_is_kept_under_the_call_id():
    state: dict = {}
    out = _call(_present(), "t1", state)
    assert "error" not in out
    kept = state.get(APPLIED_EXCLUSIONS_STATE_KEY, {})
    assert "t1" in kept
    assert kept["t1"]["rules"]                       # one rule per rule-bearing table
    assert kept["t1"]["recorded"].strip().endswith(")") or "COUNT" in kept["t1"]["recorded"]


def test_a_read_without_the_rule_keeps_no_rule():
    # Refuse once, then the identical resend runs WITHOUT the predicate and
    # keeps no applied rule under its id.
    state: dict = {}
    sql = "SELECT COUNT(*) AS n FROM s3t.flow.segments WHERE observed_at >= TIMESTAMP '2026-10-05'"
    run, call = _run_tool(), sa._ModeACall("exactly how many segment records on 10/06")
    _call(sql, "t1", state, run=run, call=call)        # refused
    out = _call(sql, "t1", state, run=run, call=call)  # resend runs, rule NOT applied
    assert "error" not in out
    assert state.get(APPLIED_EXCLUSIONS_STATE_KEY, {}).get("t1") is None


def test_a_call_without_an_id_keeps_nothing():
    state: dict = {}
    out = _call(_present(), "", state)
    assert "error" not in out
    assert not state.get(APPLIED_EXCLUSIONS_STATE_KEY)
