# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""discover's refusal steers the chat forward, never into removing an
onboarded system.

discover refuses while the system has a durable onboarding session. Its
refusal said "Call reset_system first to re-discover", whatever the session's
status and whether or not the system was onboarded. For an onboarded system
the chat's reset_system is a full removal (graph data, agent runtime, gateway
target, registration), so a chat asked to re-onboard an onboarded system
called discover a second time, was told to reset, and removed the system the
user wanted re-onboarded (seen live on a rehearsal account, 2026-10-10).

Now the refusal names the session's status and the tool that continues it
(discovered: map_fields, mapped: resolve_identities, resolved and
provisioning: provision_system). For a system that is not onboarded it still
says reset_system starts the onboarding over. For an onboarded system it
says reset_system would remove it completely, and that to start over the
user removes it with Remove system on its card, keeping the registration,
and onboards it again; the same holds for the chat provision's refusals and
failure text, and for the tool descriptions and the prompt the model reads.
Offline: the session store and the graph are fakes.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

import agents.connection_resolver as cr
import agents.onboard as onboard
import agents.onboard_agent as oa
import agents.onboard_chat_lifecycle as lifecycle
from agents.catalog import SystemCatalog
from agents.job_store import JobStore
from agents.onboarding_session import (DuplicateProvisionError, OutOfOrderError,
                                       SessionExistsError, SessionNotFoundError)
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: F401 - fixture
    SID as WORLD_SID, TABLES, _agent_tools, _chat_provision_answer, _chat_provision_to_the_end,
    _chat_until_resolved, world)

SID = "plant-cmms"
NEXT_TOOL = {"discovered": "map_fields", "mapped": "resolve_identities",
             "resolved": "provision_system", "provisioning": "provision_system"}


def _recommends_reset(text: str) -> bool:
    """Whether ``text`` tells the model to call reset_system (a "do not call
    reset_system" does not)."""
    return re.search(r"(?<!not )call reset_system", text, re.IGNORECASE) is not None


class _Sessions:
    def __init__(self):
        self.items: dict = {}

    def put(self, status: str, age_s: float = 0.0):
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
        self.items[SID] = {"system_id": SID, "status": status, "updated_at": stamp}

    def get_session(self, system_id):
        if system_id not in self.items:
            raise SessionNotFoundError(system_id)
        return dict(self.items[system_id])

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
        raise DuplicateProvisionError(system_id)

    def delete_session_if_status(self, system_id, status):
        item = self.items.get(system_id)
        if not item or item["status"] != status:
            raise OutOfOrderError(system_id)
        del self.items[system_id]

    def delete_session(self, system_id):
        self.items.pop(system_id, None)


@pytest.fixture
def sessions(monkeypatch):
    store = _Sessions()
    monkeypatch.setattr(oa, "_store", lambda: store)
    return store


@pytest.fixture
def nodes(monkeypatch):
    """The System nodes the checked node read sees (id -> node)."""
    graph: dict = {}
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: graph.get(sid) or {})
    return graph


@pytest.fixture
def system(monkeypatch, sessions, nodes):
    cat = SystemCatalog([])
    monkeypatch.setattr(oa, "catalog", cat)
    monkeypatch.setattr(cat, "_graph_rows", lambda: [])
    cat.add({"id": SID, "name": "Plant CMMS", "status": "registered", "schema": "cmms",
             "connection_type": "PostgreSQL", "connection_origin": "demo"})
    monkeypatch.setattr(cr, "resolve_connection", lambda sid: {"system_id": sid})
    monkeypatch.setattr(oa, "_discover_backend_result", lambda *a, **k: {
        "tables": [{"name": "assets", "row_count": 3, "columns": [{"name": "asset_num"}]}]})
    return cat


def _onboarded(nodes):
    nodes[SID] = {"id": SID, "status": "onboarded"}


def _refusal(sessions, status, age_s=0.0) -> dict:
    sessions.put(status, age_s)
    res = oa.discover(SID)
    assert res.get("error", "").startswith(f"System {SID} already has an onboarding session"), res
    assert sessions.items[SID]["status"] == status, "the refused discover changed the session"
    return res


# ── (a) the refusal names the session's status and the next tool ───────────

@pytest.mark.parametrize("onboarded", [False, True], ids=["not-onboarded", "onboarded"])
@pytest.mark.parametrize("status", sorted(NEXT_TOOL))
def test_the_refusal_names_the_status_and_the_tool_that_continues_it(
        system, sessions, nodes, status, onboarded):
    if onboarded:
        _onboarded(nodes)
    res = _refusal(sessions, status)
    assert f"'{status}'" in res["error"], res
    assert f"continue with {NEXT_TOOL[status]}" in res["error"] or (
        f"call {NEXT_TOOL[status]} to check on it" in res["error"]), res
    assert res.get("session_status") == status
    assert res.get("next_tool") == NEXT_TOOL[status]
    assert res.get("reason") == "onboarding_in_progress"


def test_a_discover_still_saving_its_session_is_waited_for(system, sessions, nodes):
    res = _refusal(sessions, "discovering", age_s=5)
    assert "try again shortly" in res["error"], res
    assert not _recommends_reset(res["error"]), res
    assert "next_tool" not in res


@pytest.mark.parametrize("status", ["failed", "onboarded"])
def test_a_finished_session_names_no_next_tool(system, sessions, nodes, status):
    res = _refusal(sessions, status)
    assert f"'{status}'" in res["error"] and "next_tool" not in res, res


# ── (b) an onboarded system: reset_system is not the way ───────────────────

@pytest.mark.parametrize("status", ["discovered", "mapped", "resolved", "failed", "onboarded"])
def test_for_an_onboarded_system_the_refusal_does_not_recommend_reset(
        system, sessions, nodes, status):
    _onboarded(nodes)
    res = _refusal(sessions, status)
    err = res["error"]
    assert not _recommends_reset(err), err
    assert "Do not call reset_system" in err, err
    assert "removes an onboarded system completely" in err, err
    assert "registration" in err
    assert "Remove system on its card in the Onboard view" in err, err
    assert "keeping the connection registration" in err, err


def test_a_stale_discover_of_an_onboarded_system_does_not_recommend_reset(
        system, sessions, nodes):
    _onboarded(nodes)
    res = _refusal(sessions, "discovering", age_s=oa.DISCOVERING_STALE_AFTER_SECONDS + 60)
    assert not _recommends_reset(res["error"]), res
    assert "removes an onboarded system completely" in res["error"], res


def test_when_the_graph_cannot_be_read_the_refusal_does_not_recommend_reset(
        system, sessions, nodes, monkeypatch):
    """The removal guard's read answers; the refusal's own read fails: unknown
    is never treated as 'not onboarded'."""
    real = oa._removal_marker_row
    calls = {"n": 0}

    def _second_read_fails(sid):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("graph down")
        return real(sid)
    monkeypatch.setattr(oa, "_removal_marker_row", _second_read_fails)
    res = _refusal(sessions, "resolved")
    assert calls["n"] == 2, calls
    assert not _recommends_reset(res["error"]), res
    assert "could not be checked" in res["error"], res
    assert "continue with provision_system" in res["error"], res


# ── (c) a system that is not onboarded: reset still starts it over ─────────

@pytest.mark.parametrize("status", ["discovered", "mapped", "resolved", "failed", "onboarded"])
def test_for_a_system_that_is_not_onboarded_reset_still_starts_it_over(
        system, sessions, nodes, status):
    res = _refusal(sessions, status)
    err = res["error"]
    assert _recommends_reset(err), err
    assert "start its onboarding over" in err, err
    assert "clears only its onboarding session" in err, err
    assert "removes an onboarded system completely" not in err


def test_a_stale_discover_of_a_system_that_is_not_onboarded_is_started_over_by_reset(
        system, sessions, nodes):
    res = _refusal(sessions, "discovering", age_s=oa.DISCOVERING_STALE_AFTER_SECONDS + 60)
    assert _recommends_reset(res["error"]), res
    assert "stopped" in res["error"], res


def test_a_lost_create_race_answers_the_session_the_winner_wrote(
        system, sessions, nodes, monkeypatch):
    """The session appears between the gate's read and its create: the
    refusal answers the session as it is now."""
    real_get = sessions.get_session
    reads = {"n": 0}

    def _get(system_id):
        reads["n"] += 1
        if reads["n"] == 1:
            raise SessionNotFoundError(system_id)
        return real_get(system_id)
    sessions.put("discovered")
    monkeypatch.setattr(sessions, "get_session", _get)
    res = oa.discover(SID)
    assert res.get("session_status") == "discovered", res
    assert "continue with map_fields" in res["error"], res
    assert "was not saved" in res["error"], res


# ── a sessioned discover reads nothing from the source ──────────────────────

def _source_reads_fail(monkeypatch) -> list:
    """Connection resolution and the backend discovery, failing the test if
    a discover reaches them."""
    reached: list = []

    def _fail(*a, **_k):
        reached.append(a)
        raise AssertionError("discover read the source of a system that has a session")
    monkeypatch.setattr(cr, "resolve_connection", _fail)
    monkeypatch.setattr(oa, "_discover_backend_result", _fail)
    return reached


@pytest.mark.parametrize("onboarded", [False, True], ids=["not-onboarded", "onboarded"])
@pytest.mark.parametrize("status", ["discovering", "discovered", "mapped", "resolved",
                                    "provisioning", "failed", "onboarded"])
def test_a_discover_of_a_system_with_a_session_reads_nothing_from_the_source(
        system, sessions, nodes, monkeypatch, status, onboarded):
    if onboarded:
        _onboarded(nodes)
    reached = _source_reads_fail(monkeypatch)
    res = _refusal(sessions, status)
    assert reached == [], "the refused discover read the source"
    assert "discover did not run again" in res["error"], res


def test_the_incident_re_discover_reads_nothing_from_the_source(world):
    _chat_until_resolved()
    before = world.discovers
    assert "error" in oa.discover(WORLD_SID)
    assert world.discovers == before, "the refused discover read the source again"


def test_a_discover_that_loses_the_create_race_says_its_read_was_not_saved(
        system, sessions, nodes, monkeypatch):
    """No session when discover checks; another discover saves one while
    this one reads the source: this one's read ran and is not saved, and the
    answer says so (not "did not run again") and names the saved session's
    next step."""
    reads: list = []

    def _backend(*a, **_k):
        reads.append(a)
        sessions.put("discovered")  # the other discover's session
        return {"tables": [{"name": "assets", "row_count": 3,
                            "columns": [{"name": "asset_num"}]}]}
    monkeypatch.setattr(oa, "_discover_backend_result", _backend)
    res = oa.discover(SID)
    assert len(reads) == 1, reads
    err = res.get("error", "")
    assert "did not run again" not in err, res
    assert "another discover" in err and "was not saved" in err, res
    assert res.get("session_status") == "discovered", res
    assert res.get("next_tool") == "map_fields" and "continue with map_fields" in err, res
    assert sessions.items[SID]["status"] == "discovered"


# ── the advice is true for the runtime's reset too ──────────────────────────

@pytest.fixture
def runtime_process(monkeypatch, system):
    """The onboarding runtime container (its reset_system starts over or
    hands off; it cannot run the app's removal)."""
    monkeypatch.setenv("SYSTEM_ID", "onboarding")
    return system


@pytest.fixture
def app_process(monkeypatch, system):
    """The app process (its reset_system is the card's removal or a
    start-over under the provision claim)."""
    monkeypatch.delenv("SYSTEM_ID", raising=False)
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))
    monkeypatch.setattr(onboard, "_RESET_DOMAIN", onboard._ResetDomain())
    monkeypatch.setattr(onboard, "catalog", system)
    return system


STALE = oa.DISCOVERING_STALE_AFTER_SECONDS + 60
_ADVISED = [("discovered", 0), ("mapped", 0), ("resolved", 0), ("failed", 0),
            ("onboarded", 0), ("discovering", STALE)]


@pytest.mark.parametrize("process", ["runtime_process", "app_process"])
@pytest.mark.parametrize("status,age_s", _ADVISED,
                         ids=[f"{s}{'-stale' if a else ''}" for s, a in _ADVISED])
def test_where_discover_advises_reset_both_resets_start_it_over(
        request, sessions, nodes, process, status, age_s):
    """A system that is not onboarded: wherever discover's refusal says
    reset_system clears only the onboarding session, the reset of either
    process does exactly that, and discover runs again."""
    request.getfixturevalue(process)
    res = _refusal(sessions, status, age_s)
    assert _recommends_reset(res["error"]) and "clears only its onboarding session" in \
        res["error"], res
    cleared = oa.reset_system(SID)
    assert cleared.get("status") == "session_cleared", cleared
    assert cleared.get("previous_status") == status
    assert SID not in sessions.items
    assert oa.discover(SID).get("total_tables") == 1
    assert sessions.items[SID]["status"] == "discovered"


def test_the_runtime_reset_clears_a_stale_discovering_session(runtime_process, sessions, nodes):
    sessions.put("discovering", STALE)
    res = oa.reset_system(SID)
    assert res.get("status") == "session_cleared", res
    assert res.get("previous_status") == "discovering"
    assert runtime_process.get(SID)["status"] == "registered"


def test_the_runtime_reset_leaves_a_recent_discovering_session_alone(
        runtime_process, sessions, nodes):
    sessions.put("discovering", 5)
    res = oa.reset_system(SID)
    assert res.get("reason") == "discovering", res
    assert "Try again shortly" in res["error"], res
    assert sessions.items[SID]["status"] == "discovering"


def test_the_runtime_reset_still_leaves_a_provisioning_session_alone(
        runtime_process, sessions, nodes):
    """The runtime cannot tell whether the app's provision of it runs."""
    sessions.put("provisioning")
    res = oa.reset_system(SID)
    assert res.get("reason") == "provisioning", res
    assert sessions.items[SID]["status"] == "provisioning"


# ── one classification for the advice and the reset ────────────────────────

@pytest.mark.parametrize("node,plan", [
    ({}, "start_over"),
    ({"id": SID, "connection_config": "{}"}, "start_over"),
    ({"id": SID, "status": "onboarded"}, "remove"),
    ({"id": SID, "status": "onboarded", "removal_incomplete": True, "removal_full": True},
     "remove"),
    ({"id": SID, "status": "onboarded", "removal_incomplete": True}, "remove_again"),
    ({"id": SID, "removal_incomplete": True}, "remove_again"),
    ({"id": SID, "removal_incomplete": True, "removal_full": True}, "remove_again"),
], ids=["no-node", "connection-only", "onboarded", "onboarded-full-marker",
        "onboarded-default-marker", "default-marker", "full-marker"])
def test_the_reset_plan_of_each_node(node, plan):
    assert oa._reset_plan(node) == plan


@pytest.mark.parametrize("caller", ["advice", "runtime_reset", "app_reset"])
def test_the_advice_and_both_resets_decide_with_the_same_plan(
        request, sessions, nodes, monkeypatch, caller):
    seen: list = []
    real = oa._reset_plan
    monkeypatch.setattr(oa, "_reset_plan", lambda node: seen.append(dict(node)) or real(node))
    _onboarded(nodes)
    if caller == "advice":
        oa._start_over_advice(SID, "not onboarded")
    else:
        request.getfixturevalue("runtime_process" if caller == "runtime_reset"
                                else "app_process")
        monkeypatch.setattr(onboard, "start_removal_for_chat",
                            lambda *a, **k: {"status": "removing"})
        oa.reset_system(SID)
    assert seen and seen[-1].get("status") == "onboarded", seen


def _marker_appears_after_the_guard(monkeypatch, marker: dict):
    """The removal guard's node read finds the system onboarded; a removal
    marks the node before the refusal reads it again."""
    calls = {"n": 0}

    def _row(sid):
        calls["n"] += 1
        node = {"id": sid, "status": "onboarded"}
        return node if calls["n"] == 1 else {**node, **marker}
    monkeypatch.setattr(oa, "_removal_marker_row", _row)
    return calls


def test_a_default_removal_marked_after_the_guard_is_advised_remove_again(
        system, sessions, nodes, monkeypatch):
    ensured: list = []
    monkeypatch.setattr(system, "ensure_removal_failed", lambda row: ensured.append(row))
    calls = _marker_appears_after_the_guard(monkeypatch, {"removal_incomplete": True})
    res = _refusal(sessions, "resolved")
    err = res["error"]
    assert calls["n"] == 2, calls
    assert not _recommends_reset(err), err
    assert "Remove again on its card in the Onboard view" in err, err
    assert "keeps the registration" in err, err
    assert "Remove system on its card" not in err, err
    assert "reset_system does not start it over" in err, err
    assert ensured == [], "the advice changed the catalog"


def test_a_full_removal_marked_after_the_guard_says_reset_would_finish_it(
        system, sessions, nodes, monkeypatch):
    _marker_appears_after_the_guard(monkeypatch, {"removal_incomplete": True,
                                                  "removal_full": True})
    res = _refusal(sessions, "resolved")
    err = res["error"]
    assert not _recommends_reset(err), err
    assert "reset_system would finish it" in err, err
    assert "registration" in err and "Remove again on its card" in err, err
    assert "Remove system on its card" not in err, err


def test_the_chat_provision_texts_of_a_marked_system_advise_remove_again(nodes):
    nodes[SID] = {"id": SID, "status": "onboarded", "removal_incomplete": True}
    for name, text in _texts(SID).items():
        assert not _recommends_reset(text), (name, text)
        assert "Remove again on its card" in text, (name, text)


# ── the incident, end to end in the app: re-onboard an onboarded system ────

def _onboarded_without_a_session(world):
    """A system onboarded with no chat session left (the wizard keeps none)."""
    _chat_until_resolved()
    assert _chat_provision_to_the_end()["status"] == "onboarded"
    world.table.items.pop(WORLD_SID)
    assert world._node(WORLD_SID).get("status") == "onboarded"


def test_re_onboarding_an_onboarded_system_goes_forward_not_to_reset(world):
    _onboarded_without_a_session(world)
    _chat_until_resolved()                                   # the chat's re-onboard
    again = oa.discover(WORLD_SID)                           # the model calls discover again
    assert again.get("next_tool") == "provision_system", again
    assert not _recommends_reset(again["error"]), again
    assert "removes an onboarded system completely" in again["error"], again
    finished = _chat_provision_to_the_end()                  # the step it names
    assert finished["status"] == "onboarded", finished
    assert world._node(WORLD_SID).get("status") == "onboarded"
    assert world.catalog.get(WORLD_SID) is not None


def test_a_half_done_onboarding_of_a_new_system_is_still_started_over_by_reset(world):
    _chat_until_resolved()
    again = oa.discover(WORLD_SID)
    assert _recommends_reset(again["error"]), again
    assert _agent_tools().reset_system(WORLD_SID).get("status") == "session_cleared"
    assert "error" not in oa.discover(WORLD_SID)


def test_a_schema_change_on_a_re_onboard_does_not_recommend_reset(world):
    _onboarded_without_a_session(world)
    _chat_until_resolved()
    world.discover_tables = [{**TABLES[0], "columns": TABLES[0]["columns"][1:]}]
    res = _chat_provision_answer()
    assert res.get("reason") == "schema_changed", res
    assert not _recommends_reset(res["error"]), res
    assert "Remove system on its card in the Onboard view" in res["error"], res


# ── the chat provision's other texts, onboarded or not ──────────────────────

def _texts(system_id: str) -> dict:
    return {
        "schema_changed": lifecycle._schema_changed(system_id, ["t.c"], [])["error"],
        "no_mapped_columns": lifecycle._no_mapped_columns(system_id, ["t.c"])["error"],
        "failed": lifecycle._failure_text({}, system_id),
        "interrupted": lifecycle._failure_text({"error": "provision_interrupted"}, system_id),
    }


def test_the_chat_provision_texts_of_an_onboarded_system_do_not_recommend_reset(nodes):
    _onboarded(nodes)
    for name, text in _texts(SID).items():
        assert not _recommends_reset(text), (name, text)
        assert "removes an onboarded system completely" in text, (name, text)


def test_the_chat_provision_texts_of_a_new_system_still_recommend_reset(nodes):
    for name, text in _texts(SID).items():
        assert _recommends_reset(text), (name, text)


def test_a_failure_with_its_own_detail_is_kept(nodes):
    _onboarded(nodes)
    assert lifecycle._failure_text({"detail": "boom"}, SID) == "boom"


# ── (d) what the model reads about reset_system ─────────────────────────────

def _flat(text: str) -> str:
    """A tool description with its docstring line breaks folded to spaces."""
    return " ".join(text.split())


@pytest.mark.parametrize("tool", [oa.reset_system, lifecycle.reset_system],
                         ids=["runtime", "app"])
def test_reset_system_says_it_is_not_the_way_to_rediscover_an_onboarded_system(tool):
    description = _flat(tool.tool_spec["description"])
    assert "It is not the way to run discover again on an onboarded system" in description
    assert "An onboarded system is removed completely" in description
    assert "Remove system on its card in the Onboard view" in description
    # the existing behaviour sentences stay
    assert "A system that is not onboarded is never removed here." in description
    assert "only its onboarding session is" in description


def test_provision_system_no_longer_says_reset_starts_any_system_over():
    description = _flat(lifecycle.provision_system.tool_spec["description"])
    assert "start the system over with reset_system" not in description
    assert "(reset_system then starts the system over)" not in description
    assert "for an onboarded system that is not reset_system" in description


def test_discover_says_its_refusal_names_the_next_step():
    assert "names the session's status and the tool to call next" in \
        _flat(oa.discover.tool_spec["description"])


@pytest.mark.parametrize("prompt", ["ONBOARD_AGENT_PROMPT", "ONBOARD_AGENT_PROMPT_CLOUD"])
def test_the_prompt_says_reset_is_not_for_rediscovering_an_onboarded_system(prompt):
    text = getattr(oa, prompt)
    assert "never call it to run discover again on an onboarded system" in text
    assert "Call reset_system first to re-discover" not in text
