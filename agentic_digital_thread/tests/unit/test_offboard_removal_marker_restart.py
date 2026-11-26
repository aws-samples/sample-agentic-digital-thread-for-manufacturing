# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A removal that did not finish survives a restart as a retryable removal.

A removal keeps the :System node when a graph delete fails, but the node was
no durable "removal incomplete" marker: it still said ``status='onboarded'``.
After a restart the PostgreSQL sweep needs ``System-[:HAS_SCHEMA]->SchemaEntry``
(the SchemaEntry is the second delete), so a PostgreSQL system whose later
delete failed was not restored, and the reset preflight then refused the retry
(the cold catalog had no entry). The direct-source sweep matches every System
of a direct type whatever its status, so a half-removed direct system was
restored, marked onboarded and given an agent, after a full removal over its
pending-deletion secret. Now the removal writes ``s.removal_incomplete = true``
(checked) before anything is removed and stops at the first failed delete;
both restore sweeps never serve a marked system and rebuild its catalog entry
in the ``removal_failed`` state; the reset preflight admits a marked node even
with a cold catalog; and a successful retry clears it all (the node goes,
the entry is removed or back to 'registered'). Offline: the graph is a small
stateful fake, Secrets Manager a fake, the restart a fresh catalog.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

import agents.catalog as catalog_mod
import agents.connection_resolver as cr
import agents.onboard as onboard
import agents.onboard_agent as oa
from tests.unit._chat_removal import removed_by_the_chat  # noqa: E402
import agents.server as server
from agents.catalog import SystemCatalog
from agents.connection_types import DIRECT_SOURCE_CONNECTION_TYPES
from tests.unit.test_connect_new_system_custom_types import (  # noqa: F401 - fixtures
    CONN_ARN, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_credential_source import _register_mcp
from tests.unit.test_connect_offboard_own_secret import _FakeSM

SYSTEM_DELETE = "{id: $sid}) DETACH DELETE s"


class _Neptune:
    """The graph the removal, the restore and the preflight read and write."""

    def __init__(self):
        self.nodes: dict = {}
        self.fail: set = set()

    def add(self, sid, **props):
        self.nodes[sid] = {"id": sid, "status": "onboarded", "has_schema": True, **props}

    def write(self, cypher, params=None):
        params = params or {}
        if any(token in cypher for token in self.fail):
            return False
        sid = params.get("sid")
        if "SET s.removal_incomplete = true" in cypher:
            for key in ([sid] if sid else list(self.nodes)):
                if key in self.nodes:
                    self.nodes[key]["removal_incomplete"] = True
        elif "(se:SchemaEntry) DETACH DELETE se" in cypher and sid in self.nodes:
            self.nodes[sid]["has_schema"] = False
        elif cypher.startswith("MATCH (s:System {id: $sid}) DETACH DELETE s"):
            self.nodes.pop(sid, None)
        elif cypher.startswith("MATCH (n:System) DETACH DELETE n"):
            self.nodes.clear()
        return True

    def _row(self, n):
        return {k: n.get(k) for k in ("id", "name", "plant", "schema", "connection_type",
                                      "system_type", "connection_origin", "status",
                                      "removal_incomplete", "secret_arn")}

    def query(self, cypher, params=None):
        params = params or {}
        if "HAS_SCHEMA]->(se:SchemaEntry)" in cypher and "RETURN DISTINCT" in cypher:
            return [self._row(n) for n in self.nodes.values() if n["has_schema"]]
        if "s.connection_type IN [" in cypher:
            return [self._row(n) for n in self.nodes.values()
                    if n.get("connection_type") in DIRECT_SOURCE_CONNECTION_TYPES
                    or ("removal_incomplete" in cypher and n.get("removal_incomplete"))]
        if cypher.startswith("MATCH (s:System {id: $sid})"):
            node = self.nodes.get(params.get("sid"))
            return [self._row(node)] if node else []
        if cypher.strip() == "MATCH (s:System) RETURN s.id AS id":
            return [{"id": i} for i in self.nodes]
        return []


@pytest.fixture
def env(secrets, monkeypatch):
    graph = _Neptune()
    sm = _FakeSM(secrets)
    monkeypatch.setattr(cr, "_secrets_client", lambda: sm)
    monkeypatch.setattr(cr, "node_secret_arn_checked",
                        lambda sid: (graph.nodes.get(sid) or {}).get("secret_arn") or "")
    for mod in (onboard, oa):
        monkeypatch.setattr(mod, "neptune_write", graph.write)
    for mod in (onboard, server):
        monkeypatch.setattr(mod, "neptune_query", graph.query)
    # The default removal's node read (onboard._keep_registration_connections) is checked.
    monkeypatch.setattr(onboard, "neptune_query_checked", graph.query)
    monkeypatch.setattr(server, "_restore_seed_core_concepts", lambda: None)
    started: list = []
    monkeypatch.setattr(server, "_restore_one", lambda **kw: started.append(kw) or "mcp://x")
    monkeypatch.setattr(cr, "resolve_connection", lambda sid: {"system_id": sid})
    return type("Env", (), {"graph": graph, "sm": sm, "started": started})


def _remove(catalog_obj, *, full: bool):
    cleanup = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                      "wait_timed_out": False})
    with patch.object(onboard, "catalog", catalog_obj), \
            patch.object(onboard, "_agent_manager", MagicMock()), \
            patch.object(onboard, "remove_system_agent", MagicMock()), \
            patch.object(onboard, "_reset_cleanup_agentcore_sync", cleanup), \
            patch.object(onboard, "_retire_or_incomplete", lambda sid: 0), \
            patch.object(onboard, "_reregister_demo_system", lambda sid: False):
        return asyncio.run(onboard._run_reset_steps(SID, full=full))


def _restart(monkeypatch) -> SystemCatalog:
    """A new process: an empty catalog, then the startup restore."""
    cold = SystemCatalog([])
    for mod in (catalog_mod, onboard, oa, server):
        monkeypatch.setattr(mod, "catalog", cold)
    server._restore_base()
    return cold


def _fail_then_restart(env, monkeypatch, cat, token: str, *, full: bool) -> SystemCatalog:
    env.graph.fail = {token}
    with pytest.raises(onboard.ResetIncompleteError):
        _remove(cat, full=full)
    assert cat.get(SID)["status"] == catalog_mod.REMOVAL_FAILED   # this process too
    assert env.graph.nodes[SID]["removal_incomplete"] is True
    env.graph.fail = set()
    return _restart(monkeypatch)


def _shows_only_a_retryable_removal(env, cold):
    assert env.started == [], "a half-removed system was served after the restart"
    entry = cold.get(SID)
    assert entry is not None and entry["status"] == catalog_mod.REMOVAL_FAILED, entry
    assert SID not in {e["id"] for e in cold.get_onboarded()}
    assert onboard._reset_preflight(SID) == (None, None), "the retry is refused"


# ── (a) PostgreSQL: a later delete fails after the SchemaEntry went ─────────

def test_a_postgres_removal_failing_after_its_schema_entry_is_retryable_after_restart(
        cat, env, monkeypatch):
    _register_pg()
    cat.update_status(SID, "onboarded")
    env.graph.add(SID, name="Quality Lab", schema="lims", connection_type="PostgreSQL",
                  connection_origin="user", secret_arn=CONN_ARN)
    cold = _fail_then_restart(env, monkeypatch, cat, "IdentityRule", full=True)
    assert env.graph.nodes[SID]["has_schema"] is False     # the SQL sweep cannot see it
    _shows_only_a_retryable_removal(env, cold)
    assert cold.get(SID)["connection_origin"] == "user"
    result = _remove(cold, full=True)
    assert result["removed"] is True and cold.get(SID) is None
    assert SID not in env.graph.nodes


def test_the_first_failed_delete_stops_the_removal(cat, env):
    _register_pg()
    cat.update_status(SID, "onboarded")
    env.graph.add(SID, schema="lims", connection_type="PostgreSQL", secret_arn=CONN_ARN)
    writes: list = []
    real = env.graph.write

    def _spy(cypher, params=None):
        writes.append(cypher)
        return real(cypher, params)
    with patch.object(onboard, "neptune_write", _spy):
        env.graph.fail = {"Property {system"}
        with pytest.raises(onboard.ResetIncompleteError) as err:
            _remove(cat, full=False)
    assert "Property" in str(err.value)
    assert writes[-1].startswith("MATCH (p:Property"), writes      # nothing after it
    assert not any("IdentityRule" in w or "JoinPath" in w for w in writes), writes


# ── (b) a direct source: the final System delete fails ──────────────────────

def test_a_direct_source_removal_failing_at_the_system_delete_is_not_served_after_restart(
        cat, env, monkeypatch):
    assert _register_mcp().get("status") == "created"
    cat.update_status(SID, "onboarded")
    env.graph.add(SID, name="Quality Lab", connection_type="MCP Server",
                  connection_origin="user")
    cold = _fail_then_restart(env, monkeypatch, cat, SYSTEM_DELETE, full=False)
    assert env.graph.nodes[SID]["connection_type"] == "MCP Server"   # the direct sweep sees it
    _shows_only_a_retryable_removal(env, cold)
    result = _remove(cold, full=False)
    assert result["kept_registration"] is True
    assert cold.get(SID)["status"] == "registered" and SID not in env.graph.nodes


def test_a_full_direct_removal_never_restarts_over_its_pending_deletion_secret(
        cat, env, monkeypatch):
    assert _register_mcp().get("status") == "created"
    cat.update_status(SID, "onboarded")
    env.graph.add(SID, name="Quality Lab", connection_type="MCP Server",
                  connection_origin="user", secret_arn=CONN_ARN)
    cold = _fail_then_restart(env, monkeypatch, cat, SYSTEM_DELETE, full=True)
    assert [kw["SecretId"] for kw in env.sm.deleted] == [CONN_ARN]
    _shows_only_a_retryable_removal(env, cold)


# ── the marker itself ──────────────────────────────────────────────────────

def test_nothing_is_removed_when_the_marker_cannot_be_written(cat, env):
    _register_pg()
    cat.update_status(SID, "onboarded")
    env.graph.add(SID, schema="lims", connection_type="PostgreSQL", secret_arn=CONN_ARN)
    env.graph.fail = {"SET s.removal_incomplete"}
    agentcore = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                        "wait_timed_out": False})
    with patch.object(onboard, "_reset_cleanup_agentcore_sync", agentcore), \
            patch.object(onboard, "catalog", cat), \
            patch.object(onboard, "_agent_manager", MagicMock()), \
            patch.object(onboard, "remove_system_agent", MagicMock()), \
            patch.object(onboard, "_retire_or_incomplete", lambda sid: 0), \
            pytest.raises(onboard.ResetIncompleteError) as err:
        asyncio.run(onboard._run_reset_steps(SID, full=True))
    assert "Nothing was removed" in str(err.value)
    agentcore.assert_not_called()
    assert cat.get(SID)["status"] == "onboarded" and env.graph.nodes[SID]["has_schema"]
    assert env.sm.deleted == []


def test_guard_a_cold_catalog_still_refuses_an_unmarked_unknown_system(cat, env, monkeypatch):
    env.graph.add(SID, schema="lims", connection_type="PostgreSQL")
    cold = SystemCatalog([])
    monkeypatch.setattr(onboard, "catalog", cold)
    err, _other = onboard._reset_preflight(SID)
    assert err and "Unknown system_id" in err["error"]


def test_guard_an_unmarked_system_is_still_restored_and_served(cat, env, monkeypatch):
    env.graph.add("plain-mcp", name="Plain", connection_type="MCP Server")
    monkeypatch.setattr(cr, "resolve_connection",
                        lambda sid: {"system_id": sid, "url": "https://mcp.example.com/mcp"})
    cold = _restart(monkeypatch)
    assert cold.get("plain-mcp")["status"] == "onboarded"
    assert [kw["system_id"] for kw in env.started] == ["plain-mcp"]


# ── onboarding writers refuse a system whose removal did not finish ────────

def test_a_removal_failed_system_cannot_be_onboarded_again_before_it_is_removed(cat, env):
    _register_pg()
    cat.update_status(SID, catalog_mod.REMOVAL_FAILED)
    with patch.object(onboard, "catalog", cat):
        conflict = onboard._reset_conflict_or_none(SID)
    assert conflict and conflict["reason"] == "removal_failed", conflict
    assert "Remove it again" in conflict["error"]


# ── the chat reset_system and the global reset write the marker too ───────

def test_the_chat_reset_marks_the_node_before_the_secret_goes(cat, env, monkeypatch):
    """The chat tool runs the same removal, so it writes the same marker."""
    from agents.job_store import JobStore
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))
    _register_pg()
    cat.update_status(SID, "onboarded")
    env.graph.add(SID, schema="lims", connection_type="PostgreSQL", connection_origin="user",
                  secret_arn=CONN_ARN)
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: env.graph.nodes.get(sid))
    monkeypatch.setattr(oa, "_store", lambda: MagicMock())
    env.graph.fail = {SYSTEM_DELETE}
    cleanup = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                      "wait_timed_out": False})
    with patch.object(onboard, "_agent_manager", MagicMock()), \
            patch.object(onboard, "remove_system_agent", MagicMock()), \
            patch.object(onboard, "_reset_cleanup_agentcore_sync", cleanup), \
            patch.object(onboard, "_retire_or_incomplete", lambda sid: 0), \
            patch.object(onboard, "_reregister_demo_system", lambda sid: False):
        assert "error" in removed_by_the_chat(oa.reset_system, SID)
    assert env.graph.nodes[SID]["removal_incomplete"] is True
    assert cat.get(SID)["status"] == catalog_mod.REMOVAL_FAILED
    cold = _restart(monkeypatch)
    _shows_only_a_retryable_removal(env, cold)


def test_a_failed_global_wipe_leaves_marked_systems_and_wipes_the_system_label_last(
        cat, env, secrets):
    fresh = SystemCatalog([{"id": SID, "name": "Q", "status": "onboarded",
                            "connection_type": "PostgreSQL", "schema": "lims"}])
    env.graph.add(SID, schema="lims", connection_type="PostgreSQL")
    env.graph.fail = {"MATCH (n:IdentityRule)"}
    with patch.object(onboard, "catalog", fresh), \
            patch.object(onboard, "core_concept_merges", return_value=[]), \
            patch.object(onboard, "reset_agents", MagicMock()), \
            patch.object(onboard, "_agent_manager", MagicMock()), \
            patch.object(onboard, "_graph_system_ids", lambda: [SID]), \
            patch.object(onboard, "_retire_many_or_incomplete", lambda ids: 0), \
            patch.object(onboard, "_global_reset_cleanup_agentcore_sync",
                         return_value={"runtimes_awaited": [], "targets_awaited": [],
                                       "wait_timed_out": False}), \
            pytest.raises(onboard.ResetIncompleteError):
        asyncio.run(onboard._global_reset_steps(False))
    assert env.graph.nodes[SID]["removal_incomplete"] is True
    assert fresh.get(SID)["status"] == catalog_mod.REMOVAL_FAILED
