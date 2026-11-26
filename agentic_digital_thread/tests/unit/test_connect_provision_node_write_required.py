# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A USER registration's provision needs its System-node write.

The node's pointer to the registration's own ``adt/conn/<id>`` secret is the
durable origin marker: after a restart ``server._restore`` re-marks the catalog
entry as user-registered only from it. Provision's node write used to be
fail-soft for every system (``connection_resolver._persist_node_connection_config``
ignored the graph client's ``False``, ``onboard._persist_node_connection`` logged
and carried on), so a failed write let a user system finish "provisioned" with a
bare node. Now, for a user registration, a failed node write is a retryable
provision error: the PostgreSQL agent is never started, an agent a connection
branch already started is unregistered in this process, and the catalog status
goes back. Demo / workshop systems stay fail-soft. Offline: no AWS, no graph.
"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
from tests.unit import test_onboard_provision_node_write as h  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, SID, _registered_entry, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_credential_source import (  # noqa: E402
    MCP_URL, _register_mcp)
from tests.unit.test_connect_new_system_reonboard_source import _node_row  # noqa: E402
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401 - fixture

PG_NON_SECRET = {"connection_type": "PostgreSQL", "host": "db.example.internal",
                 "port": "5432", "dbname": "lims", "schema": "lims"}


@pytest.fixture
def failing_graph(monkeypatch):
    """The graph client's fail-soft answer to a write it could not do."""
    writes: list = []
    monkeypatch.setattr(cr, "_neptune_write", lambda cypher, params: writes.append(params) or False)
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: None)
    return writes


def _spy_agents(monkeypatch) -> dict:
    seen = {"started": [], "stopped": [], "status": []}
    monkeypatch.setattr(h._FakeAgentManager, "start_agent",
                        lambda self, **kw: seen["started"].append(kw) or "mcp://test-agent",
                        raising=False)
    monkeypatch.setattr(h._FakeAgentManager, "stop_agent",
                        lambda self, sid: seen["stopped"].append(sid), raising=False)
    monkeypatch.setattr(h._FakeCatalog, "update_status",
                        lambda self, sid, status: seen["status"].append((sid, status)))
    return seen


# ── write_connection(require_node=True) ────────────────────────────────────

def _raises_node_write_error(fn) -> None:
    """``fn()`` raises ``ConnectionNodeWriteError`` (by name, so a run on code
    without it reports the behaviour, not a missing attribute)."""
    with pytest.raises(Exception) as caught:
        fn()
    assert type(caught.value).__name__ == "ConnectionNodeWriteError", repr(caught.value)


def test_a_required_node_write_that_fails_raises(secrets, failing_graph):
    _raises_node_write_error(lambda: cr.write_connection(
        SID, non_secret=PG_NON_SECRET, secret_arn=CONN_ARN, require_node=True))
    assert len(failing_graph) == 1


def test_a_required_node_write_that_raises_raises(secrets, monkeypatch):
    def _boom(cypher, params):
        raise RuntimeError("graph unavailable")
    monkeypatch.setattr(cr, "_neptune_write", _boom)
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: None)
    _raises_node_write_error(lambda: cr.write_connection(
        SID, non_secret=PG_NON_SECRET, secret_arn=CONN_ARN, require_node=True))


def test_guard_a_node_write_is_still_fail_soft_by_default(secrets, failing_graph):
    assert cr.write_connection(SID, non_secret=PG_NON_SECRET, secret_arn=CONN_ARN) == CONN_ARN


# ── provision: user PostgreSQL ─────────────────────────────────────────────

def test_a_user_pg_provision_whose_node_write_fails_is_a_retryable_error(
        cat, secrets, monkeypatch):
    entry = _registered_entry(cat, secrets)
    seen = _spy_agents(monkeypatch)
    result, calls = h._run_provision(entry, write_connection_raises=True)
    assert result.get("status") != "provisioned", result
    assert result.get("error") == onboard._NODE_WRITE_ERROR, result
    assert len(calls) == 1 and calls[0]["secret_arn"] == CONN_ARN, calls
    # The PostgreSQL agent starts after the connection phase: it never started.
    assert seen["started"] == [], seen


def test_the_provision_node_write_of_a_user_registration_is_required(
        cat, secrets, failing_graph):
    _registered_entry(cat, secrets)
    with pytest.raises(getattr(onboard, "_UserNodeWriteFailed", RuntimeError)):
        onboard._persist_node_connection(SID, non_secret=PG_NON_SECRET, secret_arn=CONN_ARN)


def test_guard_the_provision_node_write_of_a_demo_system_stays_fail_soft(
        cat, secrets, failing_graph):
    cat.add({"id": "cmms", "name": "CMMS", "connection_type": "PostgreSQL",
             "schema": "cmms_maintenance", "status": "registered", "connection_origin": "demo"})
    onboard._persist_node_connection("cmms", non_secret=PG_NON_SECRET, secret_arn=CONN_ARN)
    assert len(failing_graph) == 1


def test_a_reprovision_rewrites_a_user_pg_node_that_already_points_at_its_secret(
        cat, secrets, graph):
    """Found while making the write required: a provision whose node already
    held the registration's own pointer (any re-provision, or one after a
    restart) passed that pointer with no connection string, write_connection
    took it for a credentials edit and refused it, and the fail-soft caller
    hid that, so the node was never rewritten."""
    _registered_entry(cat, secrets)
    graph[SID] = _node_row({"host": "old.example.internal"}, CONN_ARN, "PostgreSQL")
    onboard._persist_node_connection(SID, non_secret=PG_NON_SECRET, secret_arn=CONN_ARN)
    node = graph[SID]
    assert json.loads(node["connection_config"])["host"] == "db.example.internal", node
    assert node["secret_arn"] == CONN_ARN, node


# ── provision: a branch that starts its agent first (user MCP) ─────────────

def _user_mcp_entry(cat) -> dict:
    _register_mcp()
    entry = dict(cat.get(SID))
    entry["_provision"] = {"system_id": SID, "system_name": "Quality Lab", "plant": "",
                           "connection_type": "MCP Server", "connection_string": MCP_URL}
    return entry


def test_a_user_mcp_provision_whose_node_write_fails_unregisters_its_agent(
        cat, secrets, monkeypatch):
    entry = _user_mcp_entry(cat)
    seen = _spy_agents(monkeypatch)
    removed: list = []
    monkeypatch.setattr(onboard, "remove_system_agent", lambda sid: removed.append(sid))
    result, calls = h._run_provision(entry, write_connection_raises=True)
    assert result.get("status") != "provisioned", result
    assert result.get("error") == onboard._NODE_WRITE_ERROR, result
    assert len(seen["started"]) == 1, seen            # the branch had started it
    assert seen["stopped"] == [SID] and removed == [SID], (seen, removed)
    # The branch flipped the status to onboarded; it is put back.
    assert seen["status"][-1] == (SID, "registered"), seen["status"]


# ── guards: demo / workshop systems stay fail-soft ─────────────────────────

def test_guard_a_demo_pg_provision_with_a_failed_node_write_still_provisions(monkeypatch):
    _spy_agents(monkeypatch)
    result, calls = h._run_provision(h._pg_entry(), write_connection_raises=True)
    assert result["status"] == "provisioned", result


def test_guard_a_demo_mcp_provision_with_a_failed_node_write_still_provisions(monkeypatch):
    seen = _spy_agents(monkeypatch)
    entry = {"id": "highbyte", "name": "HighByte", "connection_type": "MCP Server",
             "schema": "", "status": "registered",
             "connection_config": {"url": "https://hb.example.com/mcp", "token": "t"},
             "_provision": {"system_id": "highbyte", "system_name": "HighByte", "plant": "",
                            "connection_type": "MCP Server",
                            "connection_string": "https://hb.example.com/mcp"}}
    result, calls = h._run_provision(entry, write_connection_raises=True)
    assert result["status"] == "provisioned", result
    assert seen["stopped"] == []
