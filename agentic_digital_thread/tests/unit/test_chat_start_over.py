# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The chat's ``reset_system`` can start over a half-done chat onboarding.

The chat's ``discover`` refuses while a system has a durable onboarding
session, and ``reset_system`` used to delete that session. In the onboarding
runtime container ``reset_system`` now hands removal off to the system's card
in the Onboard view (that process cannot run the app's removal). A bundled
(demo) system that is registered but not onboarded has no removal action on
its card, so a chat onboarding of it that stopped part-way (discovered,
mapped, resolved, or failed) was stranded: nothing could clear its session.

Now, when the system is not onboarded (no System node) and its session is in
one of those pre-provision states, or ``onboarded`` (a removal that kept the
registration left it), or a ``discovering`` no discover will finish (stale),
``reset_system`` deletes that session with
a delete conditional on the status it read, so a provisioning claim that wins
in between makes the delete fail and nothing changes. Nothing the app owns
(graph, catalog, secrets, runtimes) is touched. Otherwise the hand-off names
the card action that exists for the system. In the app process the same
start-over applies when the app's removal has nothing to remove. Offline:
the session store and the graph read are fakes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import agents.connection_resolver as cr
import agents.onboard as onboard
import agents.onboard_agent as oa
from agents.catalog import REMOVAL_FAILED, SystemCatalog
from agents.onboarding_session import (DuplicateProvisionError, OutOfOrderError,
                                       SessionExistsError, SessionNotFoundError)

SID = "plant-cmms"


class _Sessions:
    """The durable onboarding session store, in memory, with the store's
    conditional semantics."""

    def __init__(self):
        self.items: dict = {}
        self.fail_reads = False
        self.before_conditional_delete = None
        self.unconditional_deletes: list = []

    def put(self, status: str):
        self.items[SID] = {"system_id": SID, "status": status,
                           "updated_at": datetime.now(timezone.utc).isoformat()}

    def get_session(self, system_id):
        if self.fail_reads:
            raise RuntimeError("ProvisionedThroughputExceededException")
        if system_id not in self.items:
            raise SessionNotFoundError(system_id)
        return dict(self.items[system_id])

    def delete_session_if_status(self, system_id, status):
        if self.before_conditional_delete:
            self.before_conditional_delete()
        item = self.items.get(system_id)
        if not item or item["status"] != status:
            raise OutOfOrderError(system_id)
        del self.items[system_id]

    def delete_session(self, system_id):
        self.unconditional_deletes.append(system_id)
        self.items.pop(system_id, None)

    def create_session(self, system_id, initiated_by, **fields):
        if system_id in self.items:
            raise SessionExistsError(system_id)
        self.items[system_id] = {"system_id": system_id, "status": "discovering", **fields}

    def advance_status(self, system_id, from_status, to_status, **fields):
        item = self.items.get(system_id)
        if not item or item["status"] != from_status:
            raise OutOfOrderError(system_id)
        item.update(fields, status=to_status)

    def claim_provisioning(self, system_id, **fields):
        item = self.items.get(system_id)
        if not item or item["status"] != "resolved":
            raise DuplicateProvisionError(system_id)
        item.update(fields, status="provisioning")


@pytest.fixture
def sessions(monkeypatch):
    store = _Sessions()
    monkeypatch.setattr(oa, "_store", lambda: store)
    return store


@pytest.fixture
def nodes(monkeypatch):
    """The System nodes the checked marker read sees (id -> node)."""
    graph: dict = {}
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: graph.get(sid) or {})
    return graph


@pytest.fixture
def writes(monkeypatch):
    seen: list = []
    monkeypatch.setattr(oa, "neptune_write", lambda q, p=None: seen.append(q) or True)
    return seen


@pytest.fixture
def runtime(monkeypatch):
    """The onboarding runtime container: its own catalog, the reserved id."""
    monkeypatch.setenv("SYSTEM_ID", "onboarding")
    cat = SystemCatalog([])
    monkeypatch.setattr(oa, "catalog", cat)
    monkeypatch.setattr(cat, "_graph_rows", lambda: [])
    return cat


def _demo(cat, status="registered"):
    cat.add({"id": SID, "name": "Plant CMMS", "status": status, "schema": "cmms",
             "connection_type": "PostgreSQL", "connection_origin": "demo"})


# ── the runtime starts over a half-done onboarding ─────────────────────────

@pytest.mark.parametrize("status", ["discovered", "mapped", "resolved", "failed"])
def test_a_pre_provision_session_of_a_registered_demo_system_is_cleared(
        runtime, sessions, nodes, writes, status):
    _demo(runtime)
    sessions.put(status)
    res = oa.reset_system(SID)
    assert res.get("status") == "session_cleared", res
    assert res.get("previous_status") == status
    assert "discover" in res["message"].lower()
    assert SID not in sessions.items
    assert sessions.unconditional_deletes == []
    assert writes == [], "the start-over wrote to the graph"
    assert runtime.get(SID)["status"] == "registered"


def test_after_the_start_over_discover_works_again(runtime, sessions, nodes, monkeypatch):
    _demo(runtime)
    sessions.put("mapped")
    monkeypatch.setattr(cr, "resolve_connection", lambda sid: {"system_id": sid})
    monkeypatch.setattr(oa, "_discover_backend_result", lambda *a, **k: {
        "tables": [{"name": "assets", "row_count": 3, "columns": [{"name": "asset_num"}]}]})
    blocked = oa.discover(SID)
    assert "already has an onboarding session" in blocked.get("error", ""), blocked

    assert oa.reset_system(SID).get("status") == "session_cleared"
    res = oa.discover(SID)
    assert res.get("total_tables") == 1, res
    assert sessions.items[SID]["status"] == "discovered"


def test_a_provisioning_claim_in_between_makes_the_clear_fail_and_changes_nothing(
        runtime, sessions, nodes, writes):
    _demo(runtime)
    sessions.put("resolved")
    sessions.before_conditional_delete = lambda: sessions.claim_provisioning(SID)
    res = oa.reset_system(SID)
    assert res.get("reason") == "session_changed", res
    assert "nothing was changed" in res["error"].lower()
    assert sessions.items[SID]["status"] == "provisioning"
    assert sessions.unconditional_deletes == [] and writes == []
    assert runtime.get(SID)["status"] == "registered"


@pytest.mark.parametrize("status", ["provisioning", "discovering"])
def test_a_session_no_provisioner_has_finished_with_is_not_cleared(
        runtime, sessions, nodes, writes, status):
    _demo(runtime)
    sessions.put(status)
    res = oa.reset_system(SID)
    assert res.get("status") != "session_cleared", res
    assert "error" in res
    assert sessions.items[SID]["status"] == status
    assert writes == []


def test_an_onboarded_session_of_a_system_no_longer_onboarded_is_cleared(
        runtime, sessions, nodes, writes):
    """A removal that kept the registration deleted the System node and left
    the chat's ``onboarded`` session: the runtime clears it, as the app does,
    so discover can run again."""
    _demo(runtime)
    sessions.put("onboarded")
    res = oa.reset_system(SID)
    assert res.get("status") == "session_cleared", res
    assert res.get("previous_status") == "onboarded"
    assert SID not in sessions.items and writes == []
    assert runtime.get(SID)["status"] == "registered"


def test_a_provisioning_session_says_so(runtime, sessions, nodes):
    _demo(runtime)
    sessions.put("provisioning")
    res = oa.reset_system(SID)
    assert res.get("reason") == "provisioning", res
    assert "being provisioned" in res["error"] and "Remove system" in res["error"]


def test_a_session_read_error_changes_nothing(runtime, sessions, nodes, writes):
    _demo(runtime)
    sessions.put("mapped")
    sessions.fail_reads = True
    res = oa.reset_system(SID)
    assert res.get("reason") == "session_unavailable", res
    assert sessions.items[SID]["status"] == "mapped" and writes == []


def test_a_graph_read_error_changes_nothing(runtime, sessions, writes, monkeypatch):
    _demo(runtime)
    sessions.put("mapped")
    import agents.db as db

    def _down(*_a, **_k):
        raise db.GraphQueryError("neptune unavailable")
    monkeypatch.setattr(db, "neptune_query_checked", _down)
    res = oa.reset_system(SID)
    assert res.get("reason") == "graph_unavailable", res
    assert sessions.items[SID]["status"] == "mapped" and writes == []


# ── otherwise: the hand-off names the action that exists on the card ──────

def test_an_onboarded_system_is_handed_to_remove_system(runtime, sessions, nodes, writes):
    _demo(runtime, status="onboarded")
    nodes[SID] = {"id": SID, "schema": "cmms", "status": "onboarded"}
    sessions.put("mapped")
    res = oa.reset_system(SID)
    assert res.get("reason") == "remove_in_app", res
    assert "Remove system" in res["error"]
    assert "Remove registration" not in res["error"]
    assert sessions.items[SID]["status"] == "mapped", "an onboarded system's session was cleared"
    assert writes == []


def test_a_half_removed_system_is_handed_to_remove_again(runtime, sessions, nodes):
    _demo(runtime, status=REMOVAL_FAILED)
    nodes[SID] = {"id": SID, "removal_incomplete": True}
    res = oa.reset_system(SID)
    assert res.get("reason") == "remove_in_app", res
    assert "Remove again" in res["error"]


def test_a_registered_user_system_is_handed_to_remove_registration(runtime, sessions, nodes):
    runtime.add({"id": SID, "name": "Plant CMMS", "status": "registered", "schema": "cmms",
                 "connection_type": "PostgreSQL", "connection_origin": "user"})
    res = oa.reset_system(SID)
    assert res.get("reason") == "remove_in_app", res
    assert "Remove registration" in res["error"]
    assert "Remove system" not in res["error"]


def test_a_registered_demo_system_without_a_session_has_nothing_to_do(runtime, sessions,
                                                                      nodes):
    _demo(runtime)
    res = oa.reset_system(SID)
    assert res.get("reason") == "nothing_to_remove", res
    assert "Remove" not in res["error"]


# ── in the app process: the same start-over when nothing can be removed ───

@pytest.fixture
def app_catalog(monkeypatch):
    from agents.job_store import JobStore
    monkeypatch.delenv("SYSTEM_ID", raising=False)
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))
    monkeypatch.setattr(onboard, "_RESET_DOMAIN", onboard._ResetDomain())
    cat = SystemCatalog([])
    for mod in (onboard, oa):
        monkeypatch.setattr(mod, "catalog", cat)
    monkeypatch.setattr(onboard, "neptune_query", MagicMock(return_value=[{}]))
    return cat


def test_in_the_app_a_registered_demo_systems_pre_provision_session_is_cleared(
        app_catalog, sessions, nodes):
    _demo(app_catalog)
    sessions.put("resolved")
    res = oa.reset_system(SID)
    assert res.get("status") == "session_cleared", res
    assert SID not in sessions.items
    assert app_catalog.get(SID)["status"] == "registered"


def test_in_the_app_without_a_session_the_apps_refusal_is_returned(app_catalog, sessions,
                                                                    nodes):
    _demo(app_catalog)
    res = oa.reset_system(SID)
    assert res == {"error": f"System '{SID}' is not onboarded"}
    assert app_catalog.get(SID) is not None


@pytest.mark.parametrize("node", [{"id": SID, "removal_incomplete": True},
                                  {"id": SID, "schema": "cmms", "status": "onboarded"}],
                         ids=["marked", "onboarded"])
def test_in_the_app_a_session_is_kept_when_a_system_node_exists(app_catalog, sessions, nodes,
                                                                 node):
    """The checked read finds a System node marked onboarded, or marked by a
    removal that did not finish: the session is not the only state, so it is
    not cleared."""
    _demo(app_catalog)
    nodes[SID] = node
    sessions.put("failed")
    res = oa.reset_system(SID)
    assert res.get("status") != "session_cleared", res
    assert sessions.items[SID]["status"] == "failed"


def test_in_the_app_a_node_holding_only_a_connection_is_started_over(app_catalog, sessions,
                                                                      nodes):
    """A node an Edit or a scan budget wrote before the first provision has
    no onboarding status: it does not count as onboarded."""
    _demo(app_catalog)
    nodes[SID] = {"id": SID, "connection_config": "{}"}
    sessions.put("failed")
    res = oa.reset_system(SID)
    assert res.get("status") == "session_cleared", res
    assert SID not in sessions.items
    assert app_catalog.get(SID)["status"] == "registered"


def test_in_the_app_a_graph_read_error_clears_nothing(app_catalog, sessions, monkeypatch):
    _demo(app_catalog)
    sessions.put("resolved")
    import agents.db as db

    def _down(*_a, **_k):
        raise db.GraphQueryError("neptune unavailable")
    monkeypatch.setattr(db, "neptune_query_checked", _down)
    res = oa.reset_system(SID)
    assert res.get("reason") == "graph_unavailable", res
    assert sessions.items[SID]["status"] == "resolved"


# ── a cold runtime does not claim what only the app knows ─────────────────

def test_a_cold_runtime_with_nothing_found_points_at_the_card_without_guessing(
        runtime, sessions, nodes, writes):
    """A registration the app owns has no System node until it is
    provisioned, so this container's catalog cannot see it. With no node, no
    session and no entry, the answer says nothing was found to reset from the
    chat and names both card actions, without saying which one applies."""
    assert runtime.lookup(SID) is None
    res = oa.reset_system(SID)
    assert res.get("reason") == "remove_in_app", res
    text = res["error"]
    assert "nothing to reset from the chat" in text
    assert "Remove registration" in text and "Remove system" in text
    assert "is registered" not in text and "is onboarded" not in text
    assert "Nothing was changed" in text
    assert writes == []
