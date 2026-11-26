# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The chat finishes a removal that did not finish only when it was a full
removal of an onboarded system.

A card removal and a Reset All write ``removal_incomplete`` on the System node
before they remove anything, with the mode they were asked in
(``removal_full``). Reset All marks EVERY System node, including a node that
holds only a connection (an Edit before the first provision), so after a
failed Reset All a system that was never onboarded can carry the marker while
its registration is still kept. What a failure leaves:

* a card removal of an onboarded system: the node keeps ``status='onboarded'``
  and gets the marker with its mode; the catalog entry is ``removal_failed``
  with that mode;
* a Reset All: every node gets the marker with its mode; onboarded entries
  become ``removal_failed``; a registered entry stays ``registered`` (a full
  one may already have its own secret scheduled).

The chat's ``reset_system`` runs the card's full removal only for a node
marked onboarded with no marker, or with a marker that recorded a full
removal. Any other marked node is handed to the card's Remove again, which
repeats the removal the way it was asked (its catalog entry is shown
``removal_failed`` with the recorded mode, as a restart would show it):
nothing is removed from the chat. Built from real writes: the admin Edit
before onboarding, Reset All and the card removal through their own code,
with the AgentCore or graph step failing. Offline.
"""
from __future__ import annotations

import asyncio
import os
import re
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402
from tests.unit._chat_removal import removed_by_the_chat  # noqa: E402
from agents.catalog import REMOVAL_FAILED, REMOVAL_MODE  # noqa: E402
from tests.unit.test_chat_reset_keeps_a_registration_edited_before_onboarding import (  # noqa: E402
    _edited_pg)
from tests.unit.test_chat_reset_removes_only_what_was_onboarded import (  # noqa: E402,F401
    sessions, sm)
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, SID, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_env_credential_fill import deployed  # noqa: E402,F401


class Removals:
    """What the card removal and Reset All reach outside the graph and the
    catalog, faked, with a failure that can be set: ``agentcore`` (the
    runtime and target cleanup) or ``graph`` (the graph delete or wipe)."""

    def __init__(self, graph):
        self.graph = graph
        self.fail = ""
        self.single_cleanups: list = []
        self.graph_deletes: list = []

    def nodes(self) -> dict:
        return {k: v for k, v in self.graph.items() if isinstance(v, dict)}

    # graph writes and reads through onboard.neptune_write / neptune_query
    def write(self, cypher, params=None):
        params = params or {}
        if onboard._REMOVAL_MARKER_SET in cypher:
            targets = [params["sid"]] if "$sid" in cypher else list(self.nodes())
            for sid in targets:
                node = self.graph.get(sid)
                if isinstance(node, dict):
                    node["removal_incomplete"] = True
                    node["removal_full"] = bool(node.get("removal_full")) or bool(params["full"])
        return True

    def query(self, cypher, params=None):
        params = params or {}
        aliases = re.findall(r"s\.(\w+) AS (\w+)", cypher)
        rows = ([(params["sid"], self.graph.get(params["sid"]))] if "$sid" in cypher
                else list(self.nodes().items()))
        return [{alias: (sid if prop == "id" else node.get(prop)) for prop, alias in aliases}
                for sid, node in rows if isinstance(node, dict)]

    # the card removal
    def single_cleanup(self, sid, *_a, **_k):
        self.single_cleanups.append(sid)
        if self.fail == "agentcore":
            raise onboard.ResetIncompleteError(f"removal of '{sid}' is incomplete: AgentCore")
        return {"runtimes_awaited": [], "targets_awaited": [], "wait_timed_out": False}

    def delete_graph(self, sid):
        if self.fail == "graph":
            raise onboard.ResetIncompleteError(f"removal of '{sid}' is incomplete: graph")
        self.graph_deletes.append(sid)
        self.graph.pop(sid, None)

    # Reset All
    def all_cleanup(self, *_a, **_k):
        if self.fail == "agentcore":
            raise onboard.ResetIncompleteError("reset of all systems is incomplete: AgentCore")
        return {"runtimes_awaited": [], "targets_awaited": [], "wait_timed_out": False}

    def wipe(self):
        if self.fail == "graph":
            raise onboard.ResetIncompleteError("reset of all systems is incomplete: graph")
        for sid in list(self.nodes()):
            self.graph.pop(sid, None)
        return 0


@pytest.fixture
def removals(graph, cat, secrets, sm, deployed, monkeypatch):
    from agents.job_store import JobStore
    r = Removals(graph)
    monkeypatch.delenv("SYSTEM_ID", raising=False)
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))
    monkeypatch.setattr(onboard, "_RESET_DOMAIN", onboard._ResetDomain())
    monkeypatch.setattr(cr, "read_secret_uncached", lambda arn: dict(secrets[arn]))
    for target, value in (("neptune_write", r.write), ("neptune_query", r.query),
                          ("_agent_manager", MagicMock()), ("remove_system_agent", MagicMock()),
                          ("reset_agents", MagicMock()),
                          ("_reset_cleanup_agentcore_sync", r.single_cleanup),
                          ("_reset_delete_graph_artifacts_invalidating", r.delete_graph),
                          ("_global_reset_cleanup_agentcore_sync", r.all_cleanup),
                          ("_global_reset_wipe_graph", r.wipe),
                          ("_retire_or_incomplete", lambda sid: 0),
                          ("_retire_many_or_incomplete", lambda ids, *a: 0),
                          ("_reregister_demo_system", lambda sid: False)):
        monkeypatch.setattr(onboard, target, value)
    monkeypatch.setattr(oa, "neptune_write", r.write)
    return r


def _failed_reset_all(removals, *, full: bool, at: str) -> None:
    removals.fail = at
    with pytest.raises(Exception):
        asyncio.run(onboard.onboard_reset(None, full=full))
    removals.fail = ""


def _failed_card_removal(removals, sid, *, full: bool, at: str) -> None:
    removals.fail = at
    with pytest.raises(Exception):
        asyncio.run(onboard.onboard_reset_system(sid, wait=True, full=full))
    removals.fail = ""


def _onboarded(cat, graph, sid):
    graph[sid]["status"] = "onboarded"
    cat.update_status(sid, "onboarded")


def _chat(sid):
    import agents.onboard_chat_lifecycle as lifecycle
    return lifecycle.reset_system(sid)


def _handed_to_remove_again(res, sid):
    assert res.get("reason") == "remove_in_app", res
    assert "Remove again" in res["error"] and "Nothing was removed" in res["error"], res
    assert res.get("system_id") == sid


def _kept(cat, secrets, sm, graph, removals, sid, *, deleted_before: list, mode: str):
    entry = cat.get(sid)
    assert entry is not None, "the registration was removed"
    assert entry.get("connection_origin") == "user"
    assert (entry.get("status"), entry.get(REMOVAL_MODE)) == (REMOVAL_FAILED, mode), entry
    assert CONN_ARN in secrets, "the registration's own secret was removed"
    assert sm.deleted == deleted_before, f"the chat scheduled the secret: {sm.deleted}"
    assert sid in graph and graph[sid].get("removal_incomplete") is True, "the node was removed"
    assert removals.graph_deletes == [], "the chat ran a removal"


# ── never onboarded, marked by a Reset All that did not finish ──────────────

@pytest.mark.parametrize("at", ["agentcore", "graph"])
@pytest.mark.parametrize("full", [False, True], ids=["default", "full"])
def test_a_registration_marked_by_a_failed_reset_all_is_handed_to_its_card(
        cat, secrets, sm, graph, removals, sessions, full, at):
    sid, _arn = _edited_pg(graph)
    _failed_reset_all(removals, full=full, at=at)
    assert graph[sid]["removal_incomplete"] is True and "status" not in graph[sid]
    assert graph[sid]["removal_full"] is full
    assert cat.get(sid)["status"] == "registered"      # Reset All left the registration
    deleted_before = list(sm.deleted)
    sessions.put(sid, "mapped")
    res = _chat(sid)
    _handed_to_remove_again(res, sid)
    _kept(cat, secrets, sm, graph, removals, sid, deleted_before=deleted_before,
          mode="full" if full else "default")
    assert sessions.status(sid) == "mapped"


def test_the_onboarding_tool_hands_it_to_the_card_too(cat, secrets, sm, graph, removals, sessions):
    sid, _arn = _edited_pg(graph)
    _failed_reset_all(removals, full=False, at="agentcore")
    res = oa.reset_system(sid)
    _handed_to_remove_again(res, sid)
    _kept(cat, secrets, sm, graph, removals, sid, deleted_before=[], mode="default")


def test_after_the_hand_off_remove_again_keeps_the_registration(cat, secrets, sm, graph, removals,
                                                                sessions):
    """The card's Remove again repeats the default removal Reset All was asked
    for: the node goes, the registration and its secret stay."""
    sid, _arn = _edited_pg(graph)
    _failed_reset_all(removals, full=False, at="graph")
    _handed_to_remove_again(_chat(sid), sid)
    res = asyncio.run(onboard.onboard_reset_system(sid, wait=True, full=False, retry=True))
    assert res.get("status") == "reset" and res.get("kept_registration") is True, res
    assert cat.get(sid)["status"] == "registered" and CONN_ARN in secrets
    assert sm.deleted == [] and sid not in graph


def test_the_onboarding_runtime_hands_it_to_the_card(cat, secrets, sm, graph, removals, sessions,
                                                     monkeypatch):
    sid, _arn = _edited_pg(graph)
    _failed_reset_all(removals, full=False, at="agentcore")
    sessions.put(sid, "mapped")
    monkeypatch.setenv("SYSTEM_ID", "onboarding")
    res = oa.reset_system(sid)
    assert res.get("reason") == "remove_in_app" and "Remove again" in res["error"], res
    assert sessions.status(sid) == "mapped"
    assert cat.get(sid) is not None and CONN_ARN in secrets and sm.deleted == []


# ── onboarded, a removal that did not finish ────────────────────────────────

@pytest.mark.parametrize("at", ["agentcore", "graph"])
def test_an_onboarded_system_whose_default_card_removal_failed_is_handed_to_its_card(
        cat, secrets, sm, graph, removals, sessions, at):
    sid, _arn = _edited_pg(graph)
    _onboarded(cat, graph, sid)
    _failed_card_removal(removals, sid, full=False, at=at)
    node = graph[sid]
    assert (node["status"], node["removal_incomplete"], node["removal_full"]) == \
        ("onboarded", True, False)
    cleanups = list(removals.single_cleanups)
    res = _chat(sid)
    _handed_to_remove_again(res, sid)
    assert removals.single_cleanups == cleanups, "the chat ran the AgentCore cleanup"
    _kept(cat, secrets, sm, graph, removals, sid, deleted_before=[], mode="default")


def test_an_onboarded_system_whose_default_reset_all_failed_is_handed_to_its_card(
        cat, secrets, sm, graph, removals, sessions):
    sid, _arn = _edited_pg(graph)
    _onboarded(cat, graph, sid)
    _failed_reset_all(removals, full=False, at="graph")
    res = _chat(sid)
    _handed_to_remove_again(res, sid)
    _kept(cat, secrets, sm, graph, removals, sid, deleted_before=[], mode="default")


@pytest.mark.parametrize("how", ["card", "reset_all"])
def test_an_onboarded_system_whose_full_removal_failed_is_finished_by_the_chat(
        cat, secrets, sm, graph, removals, sessions, how):
    sid, _arn = _edited_pg(graph)
    _onboarded(cat, graph, sid)
    if how == "card":
        _failed_card_removal(removals, sid, full=True, at="agentcore")
    else:
        _failed_reset_all(removals, full=True, at="agentcore")
    assert graph[sid]["removal_full"] is True
    sessions.put(sid, "onboarded")
    res = removed_by_the_chat(_chat, sid)
    assert res.get("status") == "reset", res
    assert cat.get(sid) is None and sid not in graph
    assert sm.deleted == [{"SecretId": CONN_ARN, "RecoveryWindowInDays": 7}]
    assert sessions.status(sid) is None


def test_the_node_read_carries_the_recorded_mode(cat, secrets, sm, graph, removals, monkeypatch):
    """A graph that answers only the properties the query names."""
    import agents.db as db
    sid, _arn = _edited_pg(graph)
    _failed_reset_all(removals, full=True, at="agentcore")
    monkeypatch.setattr(db, "neptune_query_checked", removals.query)
    row = oa._removal_marker_row(sid)
    assert (row.get("removal_incomplete"), row.get("removal_full")) == (True, True), row
