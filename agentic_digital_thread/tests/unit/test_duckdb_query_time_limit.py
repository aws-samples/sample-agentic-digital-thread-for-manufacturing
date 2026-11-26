# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An S3 Tables query stops at the connection's time limit with an honest,
actionable error, instead of running until the gateway drops the call.

Measured on a live deployment: ``query_timeout_s`` was validated but never
enforced, so an unbounded whole-table aggregate kept running until the
AgentCore gateway closed the MCP session (about 300 s). The orchestrator then
asked again, and one schema question took over 20 minutes. Athena stops a
data query after 60 s; S3 Tables now stops one after ``query_timeout_s``
(default 120 s) and tells the agent to narrow the query.
"""
import json
import time

import duckdb

from agents import duckdb_tools as d
from agents.duckdb_tools import make_duckdb_tools

ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/demo"


def _factory():
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS s3t")
    con.execute("CREATE SCHEMA s3t.flow")
    con.execute("CREATE TABLE s3t.flow.t AS SELECT range AS x FROM range(200000)")
    return con


_SLOW = ("SELECT COUNT(*) AS n FROM s3t.flow.t AS a, s3t.flow.t AS b "
         "WHERE (a.x * 7 + b.x * 13) % 1000003 = 5")


def _run(cfg, sql):
    tools = make_duckdb_tools({"table_bucket_arn": ARN, **cfg}, connection_factory=_factory)
    return json.loads(tools[2].__wrapped__(sql))


def test_a_query_past_the_time_limit_is_stopped_with_narrowing_guidance():
    t0 = time.monotonic()
    out = _run({"query_timeout_s": 0.5}, _SLOW)
    assert time.monotonic() - t0 < 20
    assert out.get("error_code") == "query_time_limit", out
    text = json.dumps(out).lower()
    assert "narrow" in text
    # Never worded as a transient gateway fault, which the orchestrator would
    # retry blindly (its markers include "timeout" and "timed out").
    from agents.orchestrator import _TRANSIENT_TOOL_ERROR_MARKERS
    assert not any(marker in text for marker in _TRANSIENT_TOOL_ERROR_MARKERS)


def test_a_fast_query_is_unaffected_and_the_connection_stays_usable():
    tools = make_duckdb_tools({"table_bucket_arn": ARN, "query_timeout_s": 0.5},
                              connection_factory=_factory)
    run = tools[2].__wrapped__
    assert json.loads(run(_SLOW)).get("error_code") == "query_time_limit"
    out = json.loads(run("SELECT COUNT(*) AS n FROM s3t.flow.t"))
    assert "error" not in out, out
    assert out["rows"] == [[200000]]


def test_the_default_limit_is_applied_when_none_is_configured():
    assert d._DEFAULT_QUERY_TIMEOUT_S == 120.0
    reader = d._S3TablesReader({"table_bucket_arn": ARN}, connection_factory=_factory)
    assert reader._query_time_limit() == 120.0
    reader = d._S3TablesReader({"table_bucket_arn": ARN, "query_timeout_s": 5.0},
                               connection_factory=_factory)
    assert reader._query_time_limit() == 5.0


def test_a_configured_limit_never_outlives_the_gateway_tool_call():
    cfg = d._resolve_config({"table_bucket_arn": ARN, "query_timeout_s": 900})
    reader = d._S3TablesReader(cfg, connection_factory=_factory)
    assert reader._query_time_limit() == d._GATEWAY_SAFE_QUERY_TIMEOUT_S
    assert d._GATEWAY_SAFE_QUERY_TIMEOUT_S < 300


class _FakeCursor:
    def __init__(self):
        self.closed = False
        self.interrupts = 0

    def interrupt(self):
        if self.closed:
            raise RuntimeError("Connection already closed")
        self.interrupts += 1


def test_a_stop_that_started_before_finish_completes_before_the_cursor_closes():
    cur = _FakeCursor()
    stopper = d._QueryStopper(cur, 3600)
    stopper._stop()                      # the timer fired while the query ran
    assert stopper.finish() is True      # the caller sees the stop
    cur.closed = True
    stopper._stop()                      # a late callback after close does nothing
    assert cur.interrupts == 1


def test_a_stop_after_finish_never_touches_the_closed_cursor():
    cur = _FakeCursor()
    stopper = d._QueryStopper(cur, 3600)
    assert stopper.finish() is False
    assert stopper.finish() is False     # idempotent
    cur.closed = True
    stopper._stop()
    assert cur.interrupts == 0


def test_a_metadata_read_stopped_at_the_limit_gets_catalog_guidance():
    reader = d._S3TablesReader({"table_bucket_arn": ARN, "query_timeout_s": 0.5},
                               connection_factory=_factory)
    try:
        reader.fetch(_SLOW, [], max_rows=10)
    except d.QueryError as exc:
        assert exc.code == "metadata_time_limit"
        assert "narrow" not in exc.safe_message.lower()
        from agents.orchestrator import _TRANSIENT_TOOL_ERROR_MARKERS
        assert not any(m in exc.safe_message.lower() for m in _TRANSIENT_TOOL_ERROR_MARKERS)
    else:
        raise AssertionError("the slow read was not stopped")
