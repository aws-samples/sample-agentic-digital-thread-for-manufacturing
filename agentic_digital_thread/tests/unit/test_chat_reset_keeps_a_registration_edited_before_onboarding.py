# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A System node that only holds a connection does not make a system onboarded.

An admin Edit of a registration before its first provision, and the per-system
scan budget, write the system's connection onto its System node
(``connection_resolver.write_connection`` / ``write_scan_budget``), creating
the node with no ``status``. Only a provision marks a node onboarded
(``catalog.node_is_onboarded``). So the chat's ``reset_system`` treats a
connection-only node as no node: a user registration edited before onboarding
is started over (only its onboarding session is cleared) and never removed;
its catalog entry, its System node and its own secret stay. The card's full
removal runs only for a node marked onboarded, or one whose removal did not
finish. The onboarding runtime's reset follows the same rule. Offline: the
graph, Secrets Manager and the session table are fakes; the Edit is the real
admin route body.
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
from tests.unit._chat_removal import removed_by_the_chat  # noqa: E402
from agents.catalog import node_is_onboarded  # noqa: E402
from tests.unit.test_chat_reset_removes_only_what_was_onboarded import (  # noqa: E402,F401
    MCP_ARN, MCP_SID, Sessions, _register_mcp, cascade, sessions, sm)
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_env_credential_fill import (  # noqa: E402,F401
    NEW_PW, _admin_update, deployed)

NEW_DSN = (f"postgresql://lab_reader:{NEW_PW}@db2.example.internal:5432/lims"
           "?options=-csearch_path%3Dlims")


@pytest.fixture(autouse=True)
def _app_process(monkeypatch):
    from agents.job_store import JobStore
    monkeypatch.delenv("SYSTEM_ID", raising=False)
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))
    monkeypatch.setattr(onboard, "_RESET_DOMAIN", onboard._ResetDomain())


@pytest.fixture
def budget_writes(graph, monkeypatch):
    """The graph also keeps the scan budget's write (a MERGE that sets only
    the connection config)."""
    real = cr._neptune_write

    def _write(cypher, params):
        if cypher.strip() == "MERGE (s:System {id: $sid}) SET s.connection_config = $cfg_json":
            graph.setdefault(params["sid"], {})["connection_config"] = params["cfg_json"]
            return True
        return real(cypher, params)
    monkeypatch.setattr(cr, "_neptune_write", _write)
    return graph


def _body(res) -> dict:
    return json.loads(res.body) if hasattr(res, "body") else res


def _edited_pg(graph):
    """A PostgreSQL user registration whose connection was edited before its
    first provision (the admin Edit's own body)."""
    assert _register_pg().get("status") == "created"
    res = _admin_update(SID, {"connection_string": NEW_DSN, "host": "db2.example.internal"})
    assert _body(res).get("status") == "ok", _body(res)
    return SID, CONN_ARN


def _edited_mcp(graph):
    """An MCP Server user registration whose token was rotated before its
    first provision."""
    assert _register_mcp().get("status") == "created"
    res = _admin_update(MCP_SID, {"token": "Bearer rotated-example-token"})
    assert _body(res).get("status") == "ok", _body(res)
    return MCP_SID, MCP_ARN


def _budgeted_pg(graph):
    """A PostgreSQL user registration given a scan budget before its first
    provision."""
    assert _register_pg().get("status") == "created"
    cr.write_scan_budget(SID, 10_000_000)
    return SID, CONN_ARN


_REGISTRATIONS = {"pg-edit": _edited_pg, "mcp-edit": _edited_mcp, "pg-budget": _budgeted_pg}


@pytest.fixture(params=sorted(_REGISTRATIONS))
def edited(request, cat, secrets, sm, graph, budget_writes, deployed, monkeypatch):
    # The Edit re-reads the stored credentials uncached (Secrets Manager fake).
    monkeypatch.setattr(cr, "read_secret_uncached", lambda arn: dict(secrets[arn]))
    sid, arn = _REGISTRATIONS[request.param](graph)
    assert sid in graph, "the write did not create the System node"
    node = oa._removal_marker_row(sid)
    assert node and not node_is_onboarded(node), node
    return sid, arn


@pytest.fixture(params=["app_tool", "onboarding_tool"])
def reset(request):
    import agents.onboard_chat_lifecycle as lifecycle
    tool = {"app_tool": lifecycle.reset_system, "onboarding_tool": oa.reset_system}[request.param]
    return lambda sid: tool(sid)


def _kept(cat, secrets, sm, graph, cascade, sid, arn):
    entry = cat.get(sid)
    assert entry is not None, "the registration was removed"
    assert (entry.get("connection_origin"), entry.get("status")) == ("user", "registered"), entry
    assert arn in secrets, "the registration's own secret was removed"
    assert sm.deleted == [], f"the secret was scheduled for deletion: {sm.deleted}"
    assert sid in graph, "the System node was removed"
    cascade.assert_not_called()
    assert cascade.graph_deletes == []
    assert onboard._RESET_JOBS.get(sid) is None, "a removal was claimed"


# ── a connection-only node: started over, never removed ─────────────────────

@pytest.mark.parametrize("status", ["discovered", "mapped", "resolved", "failed", "onboarded"])
def test_an_edited_registration_is_started_over_and_kept(edited, cat, secrets, sm, graph,
                                                         sessions, cascade, reset, status):
    sid, arn = edited
    sessions.put(sid, status)
    res = reset(sid)
    assert res.get("status") == "session_cleared", res
    assert sessions.status(sid) is None
    _kept(cat, secrets, sm, graph, cascade, sid, arn)


def test_a_lost_provision_of_an_edited_registration_is_started_over(edited, cat, secrets, sm,
                                                                    graph, sessions, cascade):
    sid, arn = edited
    sessions.put(sid, "provisioning")
    res = oa.reset_system(sid)
    assert res.get("status") == "session_cleared", res
    assert res.get("previous_status") == "provisioning"
    _kept(cat, secrets, sm, graph, cascade, sid, arn)


def test_without_a_session_an_edited_registration_is_pointed_to_its_card(
        edited, cat, secrets, sm, graph, sessions, cascade):
    sid, arn = edited
    res = oa.reset_system(sid)
    assert res.get("reason") == "remove_in_app", res
    assert "Remove registration" in res["error"] and "Nothing was changed" in res["error"]
    _kept(cat, secrets, sm, graph, cascade, sid, arn)


def test_the_onboarding_runtime_starts_an_edited_registration_over(edited, cat, secrets, sm,
                                                                   graph, sessions, cascade,
                                                                   monkeypatch):
    sid, arn = edited
    monkeypatch.setenv("SYSTEM_ID", "onboarding")
    sessions.put(sid, "mapped")
    res = oa.reset_system(sid)
    assert res.get("status") == "session_cleared", res
    _kept(cat, secrets, sm, graph, cascade, sid, arn)


def test_the_onboarding_runtime_never_calls_a_connection_only_node_onboarded(
        edited, cat, secrets, sm, graph, sessions, cascade, monkeypatch):
    sid, arn = edited
    monkeypatch.setenv("SYSTEM_ID", "onboarding")
    res = oa.reset_system(sid)
    assert res.get("reason") == "remove_in_app", res
    assert "is onboarded" not in res["error"] and "Nothing was changed" in res["error"]
    _kept(cat, secrets, sm, graph, cascade, sid, arn)


# ── the node read carries what decides it ───────────────────────────────────

@pytest.mark.parametrize("node", [
    {"status": "onboarded"},
    {"status": "onboarded", "removal_incomplete": True},
    {"connection_config": "{}", "secret_arn": CONN_ARN},
], ids=["onboarded", "half-removed", "connection-only"])
def test_the_node_read_returns_the_status_and_the_marker(monkeypatch, node):
    """A graph that answers only the properties the query names."""
    import re

    import agents.db as db

    def _answer(cypher, parameters=None):
        aliases = re.findall(r"s\.(\w+) AS (\w+)", cypher)
        return [{alias: node.get(prop) for prop, alias in aliases}]
    monkeypatch.setattr(db, "neptune_query_checked", _answer)
    row = oa._removal_marker_row(SID)
    assert node_is_onboarded(row) is (node.get("status") == "onboarded")
    assert (row.get("removal_incomplete") is True) is bool(node.get("removal_incomplete"))


# ── a node marked onboarded is removed as before ────────────────────────────

def test_an_onboarded_edited_registration_is_removed_completely(cat, secrets, sm, graph,
                                                                sessions, cascade, deployed):
    sid, arn = _edited_pg(graph)
    graph[sid]["status"] = "onboarded"
    cat.update_status(sid, "onboarded")
    sessions.put(sid, "onboarded")
    res = removed_by_the_chat(oa.reset_system, sid)
    assert res.get("status") == "reset", res
    assert cat.get(sid) is None
    assert sm.deleted == [{"SecretId": arn, "RecoveryWindowInDays": 7}]
    assert cascade.graph_deletes == [sid]
    assert sessions.status(sid) is None
