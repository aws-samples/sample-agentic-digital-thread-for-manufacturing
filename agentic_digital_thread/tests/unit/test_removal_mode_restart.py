# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A removal that did not finish keeps its MODE across a restart.

A full removal of an onboarded user system schedules its own secret for
deletion before the graph deletes. When a delete fails, the :System node keeps
the ``removal_incomplete`` marker, so a restart shows the system as
``removal_failed`` and its card offers "Remove again". The marker did not say
which removal was asked: after the restart the reset job's record was gone,
the dialog opened as a default removal, and the retry finished as one,
putting the system back to ``registered`` while its credentials were pending
deletion.

The same checked marker write now records the mode (``s.removal_full``), the
restore and the reset preflight copy it onto the rebuilt catalog entry
(``removal_mode``), the catalog route returns it, and a retry in the other mode
is refused (409) after a restart too. Offline: the graph is a small stateful
fake, Secrets Manager a fake, the restart a fresh catalog and job store.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

import agents.onboard as onboard
import agents.onboard_agent as oa
from tests.unit._chat_removal import removed_by_the_chat  # noqa: E402
from agents.catalog import REMOVAL_FAILED, SystemCatalog
from tests.unit.test_connect_new_system_custom_types import (  # noqa: F401 - fixtures
    CONN_ARN, SID, _register_pg, cat, no_dns, secrets)
from tests.unit.test_offboard_removal_marker_restart import (  # noqa: F401
    SYSTEM_DELETE, _Neptune, _restart, env)


class _ModeGraph(_Neptune):
    """The marker fake, also keeping the removal's mode as the real marker
    write sets it (an already recorded full mode is kept)."""

    def write(self, cypher, params=None):
        params = params or {}
        ok = super().write(cypher, params)
        if ok and "s.removal_full" in cypher:
            sid = params.get("sid")
            asked = params.get("full", "removal_full = true" in cypher)
            for key in ([sid] if sid else list(self.nodes)):
                if key in self.nodes:
                    node = self.nodes[key]
                    node["removal_full"] = bool(node.get("removal_full")) or bool(asked)
        return ok

    def _row(self, n):
        return {**super()._row(n), "removal_full": n.get("removal_full")}


@pytest.fixture
def graph(env, monkeypatch):
    mode_graph = _ModeGraph()
    for mod in (onboard, oa):
        monkeypatch.setattr(mod, "neptune_write", mode_graph.write)
    import agents.server as server
    for mod in (onboard, server):
        monkeypatch.setattr(mod, "neptune_query", mode_graph.query)
    monkeypatch.setattr(onboard, "neptune_query_checked", mode_graph.query)
    import agents.connection_resolver as cr
    monkeypatch.setattr(cr, "node_secret_arn_checked",
                        lambda sid: (mode_graph.nodes.get(sid) or {}).get("secret_arn") or "")
    env.graph = mode_graph
    return mode_graph


@pytest.fixture(autouse=True)
def _fresh_reset_jobs(monkeypatch):
    from agents.job_store import JobStore
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))


def _cascade_patches(catalog_obj):
    cleanup = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                      "wait_timed_out": False})
    return [patch.object(onboard, "catalog", catalog_obj),
            patch.object(onboard, "_agent_manager", MagicMock()),
            patch.object(onboard, "remove_system_agent", MagicMock()),
            patch.object(onboard, "_reset_cleanup_agentcore_sync", cleanup),
            patch.object(onboard, "_retire_or_incomplete", lambda sid: 0),
            patch.object(onboard, "_reregister_demo_system", lambda sid: False)]


def _route(catalog_obj, *, full: bool):
    """POST /reset/<id>?wait=true&full=<full> against ``catalog_obj``."""
    patches = _cascade_patches(catalog_obj)
    for p in patches:
        p.start()
    try:
        return asyncio.run(onboard.onboard_reset_system(SID, wait=True, full=full))
    finally:
        for p in reversed(patches):
            p.stop()


def _onboarded_user_pg(cat, graph):
    _register_pg()
    cat.update_status(SID, "onboarded")
    graph.add(SID, name="Quality Lab", schema="lims", connection_type="PostgreSQL",
              connection_origin="user", secret_arn=CONN_ARN)


def _failed_then_restarted(cat, env, graph, monkeypatch, *, full: bool) -> SystemCatalog:
    """The removal fails at the final System delete, then the app restarts
    (a fresh catalog and a fresh job store)."""
    graph.fail = {SYSTEM_DELETE}
    with pytest.raises(HTTPException) as err:
        _route(cat, full=full)
    assert err.value.status_code == 503
    graph.fail = set()
    from agents.job_store import JobStore
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))
    return _restart(monkeypatch)


# ── the marker records the mode; the restart rebuilds it ────────────────────

@pytest.mark.parametrize("full", [True, False])
def test_the_marker_records_the_removal_mode(cat, env, graph, full):
    _onboarded_user_pg(cat, graph)
    graph.fail = {SYSTEM_DELETE}
    with pytest.raises(HTTPException):
        _route(cat, full=full)
    assert graph.nodes[SID]["removal_incomplete"] is True
    assert graph.nodes[SID].get("removal_full") is full
    assert cat.get(SID)["status"] == REMOVAL_FAILED
    assert cat.get(SID).get("removal_mode") == ("full" if full else "default")


@pytest.mark.parametrize("full", [True, False])
def test_a_restart_rebuilds_the_entry_with_its_removal_mode(cat, env, graph, monkeypatch, full):
    _onboarded_user_pg(cat, graph)
    cold = _failed_then_restarted(cat, env, graph, monkeypatch, full=full)
    entry = cold.get(SID)
    assert entry["status"] == REMOVAL_FAILED
    assert entry.get("removal_mode") == ("full" if full else "default")
    with patch.object(onboard, "catalog", cold):
        served = asyncio.run(onboard.onboard_catalog())
    assert [e.get("removal_mode") for e in served if e["id"] == SID] == [entry["removal_mode"]]


def test_a_cold_catalog_preflight_rebuilds_the_mode_from_the_node(cat, env, graph, monkeypatch):
    _onboarded_user_pg(cat, graph)
    graph.fail = {SYSTEM_DELETE}
    with pytest.raises(HTTPException):
        _route(cat, full=True)
    graph.fail = set()
    cold = SystemCatalog([])
    monkeypatch.setattr(onboard, "catalog", cold)
    assert onboard._reset_preflight(SID) == (None, None)
    assert cold.get(SID).get("removal_mode") == "full"


# ── after the restart the retry keeps the mode ──────────────────────────────

def test_after_a_restart_a_default_retry_of_a_failed_full_removal_is_refused(
        cat, env, graph, monkeypatch):
    _onboarded_user_pg(cat, graph)
    cold = _failed_then_restarted(cat, env, graph, monkeypatch, full=True)
    assert [d["SecretId"] for d in env.sm.deleted] == [CONN_ARN]
    with pytest.raises(HTTPException) as err:
        _route(cold, full=False)
    assert err.value.status_code == 409
    assert "full=true" in err.value.detail
    assert cold.get(SID)["status"] == REMOVAL_FAILED, "the retry kept the registration"
    assert SID in graph.nodes


def test_after_a_restart_the_full_retry_finishes_the_removal(cat, env, graph, monkeypatch):
    _onboarded_user_pg(cat, graph)
    cold = _failed_then_restarted(cat, env, graph, monkeypatch, full=True)
    result = _route(cold, full=True)
    assert result["removed"] is True and cold.get(SID) is None
    assert SID not in graph.nodes


def test_after_a_restart_a_default_retry_of_a_failed_default_removal_finishes(
        cat, env, graph, monkeypatch):
    _onboarded_user_pg(cat, graph)
    cold = _failed_then_restarted(cat, env, graph, monkeypatch, full=False)
    result = _route(cold, full=False)
    assert result["kept_registration"] is True
    assert cold.get(SID)["status"] == "registered"
    assert "removal_mode" not in cold.get(SID), "a finished removal left its mode behind"


# ── the global reset and the chat removal record the mode too ──────────────

def test_a_failed_full_global_reset_records_full_on_every_node(cat, env, graph):
    fresh = SystemCatalog([{"id": SID, "name": "Q", "status": "onboarded",
                            "connection_type": "PostgreSQL", "schema": "lims"}])
    graph.add(SID, schema="lims", connection_type="PostgreSQL")
    graph.fail = {"MATCH (n:IdentityRule)"}
    with patch.object(onboard, "catalog", fresh), \
            patch.object(onboard, "core_concept_merges", return_value=[]), \
            patch.object(onboard, "reset_agents", MagicMock()), \
            patch.object(onboard, "_agent_manager", MagicMock()), \
            patch.object(onboard, "_graph_system_ids", lambda: [SID]), \
            patch.object(onboard, "_retire_many_or_incomplete", lambda ids: 0), \
            patch.object(onboard, "_delete_user_registration_secrets", lambda: 0), \
            patch.object(onboard, "_global_reset_cleanup_agentcore_sync",
                         return_value={"runtimes_awaited": [], "targets_awaited": [],
                                       "wait_timed_out": False}), \
            pytest.raises(onboard.ResetIncompleteError):
        asyncio.run(onboard._global_reset_steps(True))
    assert graph.nodes[SID].get("removal_full") is True
    assert fresh.get(SID)["status"] == REMOVAL_FAILED
    assert fresh.get(SID)["removal_mode"] == "full"


def test_a_full_global_reset_keeps_an_earlier_full_mode_full(cat, env, graph):
    """A default Reset All that fails never turns an earlier full removal's
    mode back to default (its secret may already be pending deletion)."""
    graph.add(SID, schema="lims", connection_type="PostgreSQL",
              removal_incomplete=True, removal_full=True)
    fresh = SystemCatalog([])
    graph.fail = {"MATCH (n:IdentityRule)"}
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
    assert graph.nodes[SID].get("removal_full") is True


def test_the_chat_removal_records_full(cat, env, graph, monkeypatch):
    _onboarded_user_pg(cat, graph)
    import agents.connection_resolver as cr
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: graph.nodes.get(sid))
    monkeypatch.setattr(oa, "_store", lambda: MagicMock())
    graph.fail = {SYSTEM_DELETE}
    patches = _cascade_patches(cat)
    for p in patches:
        p.start()
    try:
        assert "error" in removed_by_the_chat(oa.reset_system, SID)
    finally:
        for p in reversed(patches):
            p.stop()
    assert graph.nodes[SID].get("removal_full") is True
    assert cat.get(SID)["removal_mode"] == "full"
    cold = _restart(monkeypatch)
    assert cold.get(SID)["removal_mode"] == "full"


def test_a_marker_without_a_mode_restores_without_one(cat, env, graph, monkeypatch):
    """A marker written before the mode was recorded: the retry is offered
    with the choice open, and either mode is admitted."""
    graph.add(SID, name="Quality Lab", schema="lims", connection_type="PostgreSQL",
              connection_origin="user", secret_arn=CONN_ARN, removal_incomplete=True)
    cold = _restart(monkeypatch)
    assert cold.get(SID)["status"] == REMOVAL_FAILED
    assert "removal_mode" not in cold.get(SID)
