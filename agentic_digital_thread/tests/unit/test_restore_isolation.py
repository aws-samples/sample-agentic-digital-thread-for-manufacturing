# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""One system's restore failure never stops the others.

Both restore loops in ``server._restore`` used to call ``start_agent`` with
no per-system guard, so the first system whose provisioning raised (now
including the fail-closed gateway-target errors) aborted the whole pass and
every system after it stayed unregistered. These tests pin
``server._restore_one``:

* a first-system failure still registers the second, in the SQL loop and in
  the direct-source loop;
* a failure in the SQL loop does not stop the direct-source loop;
* the failed system is left unregistered and NOT retried (fail-closed until
  the next restart or a re-onboard);
* the failure log carries the exception TYPE, and the message only for the
  package's own gateway-target errors (an arbitrary exception's text is
  never logged).

All seams are mocked; no AWS or database call is made.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402  (the measured-records read a PG restore makes)
import agents.server as server  # noqa: E402
from agents.runtime_provision_helpers import GatewayTargetUpdateError  # noqa: E402

_URL = "https://gateway.example.com/mcp"


def _direct(*ids):
    return [{"id": i, "name": i.upper(), "plant": "P1", "connection_type": "MCP Server",
             "status": "onboarded"} for i in ids]


def _sql(*ids):
    return [{"id": i, "name": i.upper(), "schema": f"{i}_data", "plant": "P1",
             "status": "onboarded"} for i in ids]


def _fake_pg(*args, **kwargs):
    sql = args[1] if len(args) > 1 else ""
    if "information_schema.tables" in sql:
        return {"rows": [["orders"]]}
    if "information_schema.columns" in sql:
        return {"rows": [["id", "integer"]]}
    return {"rows": []}


def _resolved(sid):
    return {"system_id": sid, "connection_type": "MCP Server",
            "url": f"https://{sid}.example.com/mcp", "host": "db-host"}


class _Starts:
    """start_agent stand-in: the systems in ``failing`` raise ``error`` on
    every call; the rest return the gateway URL. Records every call."""

    def __init__(self, failing, error: Exception):
        self.failing = set(failing)
        self.error = error
        self.calls: list[str] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs["system_id"])
        if kwargs["system_id"] in self.failing:
            raise self.error
        return _URL


def _restore(sql_systems, direct_systems, starts: _Starts):
    """Run the real server._restore with every connection/IO seam mocked;
    returns the (system_id, url) pairs that were registered."""
    mgr = MagicMock()
    mgr.start_agent = starts
    cat = MagicMock()
    cat.get_all.return_value = [{"id": s["id"]} for s in sql_systems + direct_systems]
    add_system_agent = MagicMock()
    with patch.object(server, "_agent_manager", mgr), \
            patch.object(server, "_sql_tables_desc_from_graph", return_value=""), \
            patch.object(server, "neptune_query", MagicMock(side_effect=[sql_systems,
                                                                         direct_systems])), \
            patch.object(server, "pg", _fake_pg), \
            patch.object(server, "catalog", cat), \
            patch.object(server, "add_system_agent", add_system_agent), \
            patch.object(cr, "resolve_connection", MagicMock(side_effect=_resolved)), \
            patch.object(cr, "pg_conn_params", MagicMock(return_value={"host": "db-host"})), \
            patch.object(onboard, "neptune_query_checked", MagicMock(return_value=[])), \
            patch.object(server, "_reconcile_restored_sql_provenance", MagicMock()):
        server._restore()
    return [c.args for c in add_system_agent.call_args_list]


def test_a_first_direct_system_failure_still_registers_the_second():
    starts = _Starts({"first"}, GatewayTargetUpdateError("target did not take"))
    assert _restore([], _direct("first", "second"), starts) == [("second", _URL)]


def test_a_first_sql_system_failure_still_registers_the_second():
    starts = _Starts({"alpha"}, GatewayTargetUpdateError("target did not take"))
    assert _restore(_sql("alpha", "beta"), [], starts) == [("beta", _URL)]


def test_a_failure_in_the_sql_loop_does_not_stop_the_direct_loop():
    starts = _Starts({"alpha"}, RuntimeError("boom"))
    assert _restore(_sql("alpha"), _direct("gamma"), starts) == [("gamma", _URL)]


def test_the_failed_system_is_left_unregistered_and_not_retried():
    starts = _Starts({"first"}, GatewayTargetUpdateError("never lands"))
    registered = _restore([], _direct("first", "second"), starts)
    assert ("first", _URL) not in registered
    assert starts.calls.count("first") == 1   # attempted once; no retry machinery


def test_the_failure_log_never_carries_an_arbitrary_exception_text(caplog):
    starts = _Starts({"first"}, RuntimeError("password=SECRET_VALUE"))
    with caplog.at_level(logging.WARNING):
        _restore([], _direct("first"), starts)
    assert "RuntimeError" in caplog.text and "SECRET_VALUE" not in caplog.text


def test_the_failure_log_carries_a_gateway_target_error_message(caplog):
    starts = _Starts({"first"}, GatewayTargetUpdateError("target tid-1 is UPDATE_UNSUCCESSFUL"))
    with caplog.at_level(logging.WARNING):
        _restore([], _direct("first"), starts)
    assert "UPDATE_UNSUCCESSFUL" in caplog.text
