# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The onboarding runtime never offers a half-removed system for onboarding.

The app honours a :System node's ``removal_incomplete`` marker (restore and
the reset preflight build its catalog entry as ``removal_failed``; the
discover / map / provision routes refuse it). The onboarding runtime is a
separate, stateless process: its catalog is hydrated from the graph
(``SystemCatalog.hydrate``), which did not read the marker, so its chat
catalog listed a marked system as onboarded and the chat tools would
discover, map and provision it again. Hydration now reads the marker and
builds that entry through the same ``ensure_removal_failed`` path; the chat
``discover`` / ``map_fields`` / ``provision_system`` refuse a
``removal_failed`` system (also one this container hydrated earlier, before
the marker was written), with the app's text. Removing it again happens from
its card in the Onboard view: the runtime's ``reset_system`` cannot run the
app's removal, so it changes nothing and names Remove again. Offline:
the graph read is faked.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import agents.catalog as catalog_mod
import agents.db as db
import agents.onboard_agent as oa
from agents.catalog import REMOVAL_FAILED, SystemCatalog

SID = "half-removed"
REFUSAL = f"The removal of '{SID}' did not finish. Remove it again, then onboard it."
ROWS = [
    {"id": SID, "name": "Half Removed", "schema": "lims", "plant": "",
     "connection_type": "PostgreSQL", "system_type": "LIMS", "removal_incomplete": True,
     "status": "onboarded"},
    {"id": "healthy", "name": "Healthy", "schema": "mes", "plant": "",
     "connection_type": "PostgreSQL", "system_type": "MES", "removal_incomplete": None,
     "status": "onboarded"},
]


@pytest.fixture
def runtime_catalog(monkeypatch):
    """The onboarding runtime's own module-level catalog, cold."""
    cold = SystemCatalog([])
    for mod in (catalog_mod, oa):
        monkeypatch.setattr(mod, "catalog", cold)
    return cold


@pytest.fixture
def graph(monkeypatch):
    """The graph the runtime reads: hydration rows, and the per-system node row."""
    state = {"rows": [dict(r) for r in ROWS], "queries": []}

    def _query(cypher, params=None):
        state["queries"].append(cypher)
        if "MATCH (s:System {id: $sid})" in cypher:
            sid = (params or {}).get("sid")
            return [dict(r) for r in state["rows"] if r["id"] == sid]
        if cypher.startswith("MATCH (s:System)"):
            return [dict(r) for r in state["rows"]]
        return []
    monkeypatch.setattr(db, "neptune_query", _query)
    monkeypatch.setattr(oa, "neptune_query", _query)
    return state


def _not_reached(*_a, **_k):
    raise AssertionError("a half-removed system reached onboarding work")


# ── hydration ──────────────────────────────────────────────────────────────

def test_hydration_reads_the_marker_and_builds_a_removal_failed_entry(runtime_catalog, graph):
    assert runtime_catalog.hydrate() == 2
    assert any("removal_incomplete" in q for q in graph["queries"]), graph["queries"]
    assert runtime_catalog.get(SID)["status"] == REMOVAL_FAILED
    assert runtime_catalog.get("healthy")["status"] == "onboarded"
    assert runtime_catalog.get_onboarded_ids() == {"healthy"}


def test_the_chat_catalog_lists_it_as_removal_failed(runtime_catalog, graph):
    systems = {s["id"]: s["status"] for s in oa.get_catalog()["systems"]}
    assert systems == {SID: REMOVAL_FAILED, "healthy": "onboarded"}


def test_hydration_marks_an_entry_this_container_hydrated_before_the_marker(
        runtime_catalog, graph):
    graph["rows"][0]["removal_incomplete"] = None
    runtime_catalog.hydrate()
    assert runtime_catalog.get(SID)["status"] == "onboarded"
    graph["rows"][0]["removal_incomplete"] = True          # the app's removal then failed
    runtime_catalog.hydrate()
    assert runtime_catalog.get(SID)["status"] == REMOVAL_FAILED


# ── the chat tools refuse onboarding work for it ──────────────────────────

def test_discover_refuses_it_without_touching_the_source(runtime_catalog, graph, monkeypatch):
    runtime_catalog.hydrate()
    monkeypatch.setattr(oa, "_discover_backend_result", _not_reached)
    monkeypatch.setattr("agents.connection_resolver.resolve_connection", _not_reached)
    res = oa.discover(SID)
    assert res == {"error": REFUSAL, "reason": "removal_failed", "system_id": SID}, res


def test_map_fields_refuses_it_before_reading_the_session(runtime_catalog, graph, monkeypatch):
    runtime_catalog.hydrate()
    store = MagicMock()
    store.get_session.side_effect = _not_reached
    monkeypatch.setattr(oa, "_store", lambda: store)
    assert oa.map_fields(SID)["reason"] == "removal_failed"


def test_provision_refuses_it_and_starts_no_provisioner(runtime_catalog, graph, monkeypatch):
    runtime_catalog.hydrate()
    store = MagicMock()
    store.get_session.side_effect = _not_reached
    monkeypatch.setattr(oa, "_store", lambda: store)
    import boto3
    monkeypatch.setattr(boto3, "client", _not_reached)
    assert oa.provision_system(SID)["error"] == REFUSAL


def test_a_stale_onboarded_entry_is_refused_once_the_node_is_marked(
        runtime_catalog, graph, monkeypatch):
    """This container hydrated the system as onboarded before the app's removal
    failed and marked the node: the tool reads the node, marks the entry and
    refuses."""
    graph["rows"][0]["removal_incomplete"] = None
    runtime_catalog.hydrate()
    graph["rows"][0]["removal_incomplete"] = True
    monkeypatch.setattr(oa, "_discover_backend_result", _not_reached)
    monkeypatch.setattr("agents.connection_resolver.resolve_connection", _not_reached)
    assert oa.discover(SID)["reason"] == "removal_failed"
    assert runtime_catalog.get(SID)["status"] == REMOVAL_FAILED


def test_reset_system_points_to_the_card_and_changes_nothing(runtime_catalog, graph,
                                                             monkeypatch):
    runtime_catalog.hydrate()
    monkeypatch.setenv("SYSTEM_ID", "onboarding")
    writes: list = []
    monkeypatch.setattr(oa, "neptune_write", lambda q, p=None: writes.append(q) or True)
    store = MagicMock()
    monkeypatch.setattr(oa, "_store", lambda: store)
    res = oa.reset_system(SID)
    assert res.get("reason") == "remove_in_app", res
    assert "Remove again" in res["error"]
    assert runtime_catalog.get(SID)["status"] == REMOVAL_FAILED
    assert writes == []
    store.delete_session.assert_not_called()


def test_guard_a_healthy_system_still_reaches_discover(runtime_catalog, graph, monkeypatch):
    runtime_catalog.hydrate()
    reached = {}

    def _backend(system_id, entry, connection_type, resolved):
        reached["sid"] = system_id
        return {"error": "stop here"}
    store = MagicMock()
    store.get_session.side_effect = oa.SessionNotFoundError("healthy")  # no session yet
    monkeypatch.setattr(oa, "_store", lambda: store)
    monkeypatch.setattr(oa, "_discover_backend_result", _backend)
    monkeypatch.setattr("agents.connection_resolver.resolve_connection",
                        lambda sid: {"system_id": sid})
    assert oa.discover("healthy") == {"error": "stop here"}
    assert reached == {"sid": "healthy"}
