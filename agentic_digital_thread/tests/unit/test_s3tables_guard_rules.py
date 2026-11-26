# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An S3 Tables system agent enforces and discloses the same data rules the
Athena agent does: the row-exclusion rule refuses a count that omits the
source owner's predicate and runs/discloses it when the predicate is present
(scope 1), using the shared guard plumbing rather than a forked copy.

Reproduces the live finding (parity brief): a row-exclusion-bearing system on
S3 Tables answered a count without the predicate (too many rows); the same
DuckDB SQL with the predicate gives the record count. These tests prove the
guard now refuses the bare count and discloses the applied exclusion.

Offline: the engine's ``connection_factory`` seam injects an in-memory DuckDB;
the row-exclusion state is set directly through ``row_exclusion.remember``.
Dataset-neutral: a namespace/table the shipped seed does not use.
"""
import json

import duckdb
import pytest

import agents.row_exclusion as row_exclusion
import agents.state_times as state_times
import agents.storage_bounds as storage_bounds
import agents.system_agent as sa

ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/demo-bucket"
SYS = "s3t-neutral"
LABEL = "non-record rows"
# A record (a row that SATISFIES this predicate) is one whose route_name is
# empty OR whose segment_type is a buffer phase. Shaped like the live rule but
# on a neutral schema: NOT (route_name non-empty AND segment_type not a buffer).
CONDITIONS = [{"column": "route_name", "op": "not_empty"},
              {"column": "segment_type", "op": "not_in", "values": ["BIND", "TRIM_BUFFER"]}]


def _factory():
    def factory():
        con = duckdb.connect()
        con.execute("ATTACH ':memory:' AS s3t")
        con.execute("CREATE SCHEMA s3t.flow")
        con.execute(
            "CREATE TABLE s3t.flow.segments("
            "observed_at TIMESTAMP, route_name VARCHAR, segment_type VARCHAR)")
        # Records satisfy NOT (route_name <> '' AND segment_type NOT IN buffers):
        con.execute(
            "INSERT INTO s3t.flow.segments VALUES "
            "(TIMESTAMP '2026-10-06 00:00:00','','WELD'),"          # record (route empty)
            "(TIMESTAMP '2026-10-06 00:00:00','r1','WELD'),"        # not a record
            "(TIMESTAMP '2026-10-06 00:00:00','r2','WELD')")        # not a record
        return con
    return factory


def _guarded_tools():
    # Build the raw DuckDB tools with the connection_factory test seam, then
    # apply the same guard the S3 Tables branch of _direct_system_tools does.
    from agents.duckdb_tools import make_duckdb_tools
    raw = make_duckdb_tools({"table_bucket_arn": ARN}, source_system=SYS,
                            connection_factory=_factory())
    return sa._guard_duckdb_tools(raw, {"table_bucket_arn": ARN}, SYS)


def _run_query_tool():
    for t in _guarded_tools():
        if getattr(t, "tool_name", "") == "s3tables_run_query":
            return t.__wrapped__
    raise AssertionError("no s3tables_run_query tool")


def _ask(sql, question):
    run = _run_query_tool()
    call = sa._ModeACall(question)
    token = sa._MODE_A_CALL.set(call)
    try:
        return json.loads(run(sql))
    finally:
        sa._MODE_A_CALL.reset(token)


def setup_function(_):
    for mod in (row_exclusion, state_times, storage_bounds):
        mod.reset()
    row_exclusion.remember(SYS, {"segments": row_exclusion.exclusion_from(LABEL, CONDITIONS)})


def teardown_function(_):
    row_exclusion.reset()


_PREDICATE = row_exclusion.predicate_sql(row_exclusion.exclusion_from(LABEL, CONDITIONS))


def test_bare_count_is_refused_with_the_predicate_to_add():
    sql = "SELECT COUNT(*) AS n FROM s3t.flow.segments WHERE observed_at >= TIMESTAMP '2026-10-05'"
    out = _ask(sql, "exactly how many segment records on 10/06")
    assert "error" in out
    blob = json.dumps(out)
    assert "route_name" in blob and "segment_type" in blob  # the predicate columns


def test_count_with_the_predicate_runs_and_discloses_the_exclusion():
    sql = ("SELECT COUNT(*) AS n FROM s3t.flow.segments "
           f"WHERE observed_at >= TIMESTAMP '2026-10-05' AND {_PREDICATE}")
    out = _ask(sql, "exactly how many segment records on 10/06")
    assert "error" not in out
    assert out["rows"] == [[1]]                       # only the one record row
    disclosed = out.get("row_exclusion")
    disclosed = disclosed[0] if isinstance(disclosed, list) else disclosed
    assert isinstance(disclosed, dict) and disclosed.get("applied") is True


def test_refused_bare_count_runs_on_identical_resend_with_a_may_include_note():
    sql = "SELECT COUNT(*) AS n FROM s3t.flow.segments WHERE observed_at >= TIMESTAMP '2026-10-05'"
    q = "exactly how many segment records on 10/06"
    run = _run_query_tool()
    call = sa._ModeACall(q)
    token = sa._MODE_A_CALL.set(call)
    try:
        first = json.loads(run(sql))
        second = json.loads(run(sql))
    finally:
        sa._MODE_A_CALL.reset(token)
    # Same per-rule gate as Athena: refused once naming the predicate, then the
    # identical resend runs and discloses that its figures may include the
    # non-record rows (the rule was NOT applied).
    assert "error" in first
    assert "error" not in second and second["rows"] == [[3]]
    disclosed = second.get("row_exclusion")
    disclosed = disclosed[0] if isinstance(disclosed, list) else disclosed
    assert isinstance(disclosed, dict) and disclosed.get("applied") is not True
