# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the single event loop used to serialise for free.

Inline on the one uvicorn loop, a sync handler body could never overlap
another. Hoisted onto the offload pool, two requests can. Each test here runs
two requests at once against a hook that records how many bodies are inside a
critical stretch together: overlapping bodies show up at once (both inside),
serialised ones never do. The race tests for the reconcile repair slot and the
reset marker live with their routes (test_graph_snapshot.py,
test_onboard_reset_async.py).

Offline: no AWS, no network.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import types
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class _Overlap:
    """Call it from the critical stretch of each body. If two bodies are
    inside at once both see it immediately; a serialised body waits out
    *hold* seconds alone. ``peak`` is the most bodies ever inside together."""

    def __init__(self, hold: float = 0.5, result=None):
        self._lock = threading.Lock()
        self._inside: set[int] = set()
        self._both = threading.Event()
        self._hold = hold
        self._result = result
        self.peak = 0

    def __call__(self, *_args, **_kwargs):
        me = threading.get_ident()
        with self._lock:
            self._inside.add(me)
            self.peak = max(self.peak, len(self._inside))
            if len(self._inside) >= 2:
                self._both.set()
        self._both.wait(self._hold)
        with self._lock:
            self._inside.discard(me)
        return self._result() if callable(self._result) else self._result


def _admin_request(body: dict | None = None):
    req = types.SimpleNamespace(state=types.SimpleNamespace(is_admin=True))

    async def _json():
        return body or {}
    req.json = _json
    return req


# -- settings save and runtime-auth repair share one runtime-config lock -------

@pytest.mark.parametrize("second", ["save", "restore"])
def test_runtime_config_writers_never_overlap(monkeypatch, second):
    """A settings save and a second save, or a save and the auth repair, each
    read-modify-write runtime config; they must not run at the same time."""
    from agents import admin_routes

    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("M2M_DISCOVERY_URL", "https://disc/")
    monkeypatch.setenv("M2M_CLIENT_ID", "client-1")
    monkeypatch.delenv("ALLOWED_MODELS", raising=False)
    overlap = _Overlap(result=list)
    monkeypatch.setattr(admin_routes, "_put_model_params", overlap)
    monkeypatch.setattr(admin_routes, "_repair_system_runtimes", overlap)
    monkeypatch.setattr(admin_routes, "_orchestrator_runtime_model", lambda region: None)
    monkeypatch.setattr(admin_routes, "_update_orchestrator_runtime_model",
                        lambda region, model: "")
    monkeypatch.setattr(admin_routes, "_llm_settings_result",
                        lambda *_a: {"status": "ok"})
    save_body = {"orchestrator_model": "", "agent_model": "us.amazon.nova-pro-v1:0"}

    async def main():
        first = admin_routes.update_llm_settings_route(_admin_request(save_body))
        other = (admin_routes.update_llm_settings_route(_admin_request(save_body))
                 if second == "save"
                 else admin_routes.restore_runtime_auth_route(_admin_request()))
        return await asyncio.gather(first, other)

    with patch("boto3.client", lambda *_a, **_k: object()):
        results = asyncio.run(main())
    assert results[0] == {"status": "ok"}, results
    assert overlap.peak == 1, f"runtime-config writers overlapped (peak {overlap.peak})"


# -- a double-submitted runtime answer lands once ------------------------------

def test_double_submitted_runtime_answer_lands_once(monkeypatch):
    """Two submissions of the same answer: one lands and supersedes the
    question, the other finds it gone (404), as when the loop serialised them."""
    import agents.context_interview.routes as routes
    import agents.self_learning as sl
    from agents.context_interview.landing import LandingResult
    from agents.context_interview.questions import LAND_IDENTIFIER_SEMANTICS

    row = {"mapping_id": "q-row-1", "discovered_at": "2026-08-07T00:00:00+00:00",
           "from_system": "sys-a", "reason": "empty_join",
           "to_description": "Queries joining sys-a and sys-b return no rows.",
           "proposed_type": "steward_question", "status": "pending",
           "source": "agent", "concept": "that value"}
    monkeypatch.setattr(sl, "get_all_mappings", lambda: [dict(row)])

    class SupersedingTable:
        def update_item(self, **_kwargs):
            row["status"] = "superseded"

    monkeypatch.setattr(sl, "_get_table", lambda: SupersedingTable())
    overlap = _Overlap()
    landed: list[str] = []

    def fake_land(question, answer, *, system_id="", actor="interview", concept_id=""):
        overlap()
        landed.append(answer)
        return LandingResult(outcome="proposed", question_id=question.id,
                             landing=LAND_IDENTIFIER_SEMANTICS,
                             proposal_ids=[f"p-{len(landed)}"], clean_text=answer)

    monkeypatch.setattr(routes, "land_answer", fake_land)
    turn = routes.RuntimeTurnRequest(question_id="q-row-1", answer="strip the prefix")

    async def main():
        return await asyncio.gather(routes.runtime_turn_route(turn),
                                    routes.runtime_turn_route(turn),
                                    return_exceptions=True)

    results = asyncio.run(main())
    refused = [r for r in results if isinstance(r, HTTPException)]
    accepted = [r for r in results if isinstance(r, dict)]
    assert len(landed) == 1, landed
    assert len(accepted) == 1 and accepted[0]["question_superseded"] is True, results
    assert len(refused) == 1 and refused[0].status_code == 404, results


# -- provision confirms serialise per system -----------------------------------

@pytest.mark.parametrize("systems, peak", [(("erp", "erp"), 1), (("erp", "mes"), 2)])
def test_provision_confirms_serialise_per_system(monkeypatch, systems, peak):
    """Two confirms of the same system never provision at once; confirms of
    different systems still run in parallel."""
    import agents.onboard as onboard

    overlap = _Overlap(result=lambda: {"tables_written": 1})
    monkeypatch.setattr(onboard, "_trusted_provision_body", lambda body: body)
    monkeypatch.setattr(onboard, "_do_provision", overlap)
    monkeypatch.setattr(onboard, "_persist_schema_relation_kinds", lambda *_a: None)

    async def main():
        return await asyncio.gather(*(
            onboard.onboard_provision_confirm_route({"system_id": sid})
            for sid in systems))

    results = asyncio.run(main())
    assert [r["status"] for r in results] == ["done", "done"], results
    assert overlap.peak == peak, overlap.peak


# -- every runtime-config writer is serialised by ONE lock --------------------

class _Runtime:
    """One AgentCore runtime (adt_erp) behind a fake control plane: get
    returns a copy of its current config, update replaces it (the real API
    is full replacement). *on_get* runs after every read, before it returns."""

    def __init__(self, on_get=None):
        self.env = {"SYSTEM_ID": "erp", "SYSTEM_AGENT_MODEL": "old-model"}
        self.on_get = on_get or (lambda: None)
        self.writes: list[dict] = []
        self.lock_held_at_write: list[bool] = []

    def list_agent_runtimes(self, **_kwargs):
        return {"agentRuntimes": [{"agentRuntimeName": "adt_erp",
                                   "agentRuntimeId": "rt-erp", "status": "READY"}]}

    def get_agent_runtime(self, agentRuntimeId=None):  # noqa: N803 - boto3 kwarg
        details = {"agentRuntimeId": "rt-erp", "status": "READY",
                   "environmentVariables": dict(self.env),
                   "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "uri:1"}},
                   "roleArn": "arn:role",
                   "networkConfiguration": {"networkMode": "PUBLIC"},
                   "protocolConfiguration": {"serverProtocol": "MCP"}}
        self.on_get()  # after the read: whatever lands now, the caller misses
        return details

    def update_agent_runtime(self, **kwargs):
        import agents.runtime_provision_helpers as rph
        lock = getattr(rph, "RUNTIME_CONFIG_LOCK", None)
        self.lock_held_at_write.append(bool(lock is not None and lock._is_owned()))
        self.env = dict(kwargs["environmentVariables"])
        self.writes.append(kwargs)
        return {}


class _NoSSM:
    def get_parameter(self, Name):  # noqa: N803 - boto3 API casing
        raise Exception(f"ParameterNotFound: {Name}")

    def put_parameter(self, **_kwargs):
        return {}


def _cloud_manager(monkeypatch, admin_routes):
    from agents.agent_manager import AgentManager
    mgr = AgentManager(mode="cloud", gateway_url="https://gw/mcp",
                       ecr_image_uri="uri:1", runtime_role_arn="arn:role")
    monkeypatch.setattr(mgr, "list_agents", lambda: [{"system_id": "erp"}])
    monkeypatch.setattr(mgr, "_merge_business_context_secret", lambda *_a: None)
    monkeypatch.setattr(mgr, "_await_runtime_ready", lambda *_a: True)
    monkeypatch.setattr(mgr, "_verify_runtime_warm", lambda *_a: False)
    monkeypatch.setattr(admin_routes, "_get_agent_manager", lambda: mgr)
    monkeypatch.setattr("agents.business_context.read_business_context",
                        lambda system_id: {"business_context": "ctx", "enabled": True})
    return mgr


def test_business_context_refresh_never_reverts_a_concurrent_model_save(monkeypatch):
    """Reproduction: the business-context roll reads the
    runtime, a Settings agent-model save lands, then the roll writes back
    what it read. The save reports ok while the runtime keeps the old model.
    With one lock across both read-modify-writes the save waits for the roll
    (or the roll for the save) and the new model sticks."""
    from agents import admin_routes

    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", "old-model")
    monkeypatch.delenv("ALLOWED_MODELS", raising=False)
    save_done = threading.Event()
    saves: list = []
    started: list = []

    def save():
        try:
            saves.append(admin_routes.update_llm_settings(
                _admin_request(), {"orchestrator_model": "",
                                   "agent_model": "us.amazon.nova-pro-v1:0"}))
        finally:
            save_done.set()

    def on_get():
        # The roll's first read: start the save now and give it every chance
        # to land before the roll writes.
        if threading.current_thread().name == "bc-roll" and not started:
            started.append(threading.Thread(target=save, name="model-save"))
            started[0].start()
            save_done.wait(0.5)

    runtime = _Runtime(on_get)
    _cloud_manager(monkeypatch, admin_routes)
    clients = {"ssm": _NoSSM(), "bedrock-agentcore-control": runtime}
    roll: list = []
    with patch("boto3.client", lambda service, **_kw: clients[service]):
        bc = threading.Thread(target=lambda: roll.append(
            admin_routes._refresh_business_context_batched("erp")), name="bc-roll")
        bc.start()
        bc.join(5.0)
        started[0].join(5.0)

    assert roll and roll[0][0] == "ok", roll
    assert saves and saves[0]["status"] == "ok", saves
    assert saves[0]["agent_runtime_refresh"]["ok"] == ["erp"], saves
    assert runtime.env["SYSTEM_AGENT_MODEL"] == "us.amazon.nova-pro-v1:0", (
        "the business-context roll reverted the saved agent model")


def test_every_runtime_writer_holds_the_runtime_config_lock(monkeypatch):
    """Each app-side writer of a runtime's config does its write while
    holding RUNTIME_CONFIG_LOCK: the settings save (orchestrator + fleet),
    the auth repair, the business-context roll, the connection resync and
    the provisioning image refresh."""
    from agents import admin_routes

    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", "old-model")
    monkeypatch.setenv("M2M_DISCOVERY_URL", "https://disc/")
    monkeypatch.setenv("M2M_CLIENT_ID", "client-1")
    monkeypatch.delenv("ALLOWED_MODELS", raising=False)
    runtime = _Runtime()
    mgr = _cloud_manager(monkeypatch, admin_routes)
    monkeypatch.setattr(mgr, "_resync_rewrite_secret", lambda *_a: "arn:secret")
    monkeypatch.setattr(mgr, "_apply_business_context", lambda _s, cfg, _r: cfg)
    monkeypatch.setattr(admin_routes, "_orchestrator_runtime_model", lambda region: "old-orch")
    monkeypatch.setattr(runtime, "list_agent_runtimes", lambda **_kw: {"agentRuntimes": [
        {"agentRuntimeName": "adt_erp", "agentRuntimeId": "rt-erp", "status": "READY"},
        {"agentRuntimeName": "adt_orchestrator", "agentRuntimeId": "rt-erp",
         "status": "READY"}]})
    clients = {"ssm": _NoSSM(), "bedrock-agentcore-control": runtime}
    writers = {
        "settings save": lambda: admin_routes.update_llm_settings(
            _admin_request(), {"orchestrator_model": "us.anthropic.claude-sonnet-4-5",
                               "agent_model": "us.amazon.nova-pro-v1:0"}),
        "auth repair": lambda: admin_routes.restore_runtime_auth(_admin_request()),
        "business-context roll": lambda: admin_routes._refresh_business_context_batched("erp"),
        "connection resync": lambda: mgr.resync_cloud_connection("erp", "MCP Server", {}),
        "image refresh": lambda: mgr._refresh_existing_runtime_image(
            runtime, "rt-erp", "adt_erp", {"SYSTEM_ID": "erp"}, None),
    }
    with patch("boto3.client", lambda service, **_kw: clients[service]):
        for name, write in writers.items():
            before = len(runtime.writes)
            write()
            assert len(runtime.writes) > before, f"{name} wrote nothing"
    assert runtime.lock_held_at_write and all(runtime.lock_held_at_write), \
        runtime.lock_held_at_write


# -- the business-context PUT and the connection reconnect leave the loop ------

def _route_endpoint(router, path_suffix: str, method: str):
    for route in router.routes:
        if route.path.endswith(path_suffix) and method in route.methods:
            return route.endpoint
    raise AssertionError(f"no {method} route ending {path_suffix}")


def test_business_context_put_rolls_the_runtime_off_the_loop(monkeypatch):
    """The PUT's graph write and runtime roll (which polls READY for up to
    100 s) run in the offload pool; the loop keeps serving meanwhile."""
    import time

    import agents.business_context as bc
    import agents.connection_resolver as cr
    from agents import admin_routes
    from agents.catalog import catalog

    monkeypatch.setattr(catalog, "get", lambda system_id: {"id": system_id})
    monkeypatch.setattr(bc, "write_business_context", lambda *_a: True)
    monkeypatch.setattr(cr, "audit_connection_event", lambda *_a, **_k: None)
    roll_threads: list[str] = []

    def slow_roll(system_id):
        roll_threads.append(threading.current_thread().name)
        time.sleep(0.3)
        return "ok", {system_id: {"status": "ok", "readiness": "ready_verified"}}

    monkeypatch.setattr(admin_routes, "_refresh_business_context_batched", slow_roll)
    endpoint = _route_endpoint(admin_routes.router, "/business-context/{system_id}", "PUT")
    request = _admin_request({"business_context": "Lines are numbered L1-L4.",
                              "enabled": True})
    ticks = {"n": 0}

    async def ticker(stop):
        while not stop.is_set():
            ticks["n"] += 1
            await asyncio.sleep(0.01)

    async def main():
        stop = asyncio.Event()
        t = asyncio.create_task(ticker(stop))
        result = await endpoint("erp", request)
        stop.set()
        await t
        return result

    result = asyncio.run(main())
    assert result["status"] == "ok" and result["runtime_refresh"] == "ok", result
    assert roll_threads and roll_threads[0].startswith("adt-offload-general"), roll_threads
    assert ticks["n"] >= 10, ticks


def test_guided_confirm_context_rolls_off_the_loop(monkeypatch):
    """The guided confirm's batched roll takes
    RUNTIME_CONFIG_LOCK and polls READY; it must run in the offload pool, so
    the loop keeps serving while another thread holds the lock."""
    import time

    from agents.context_interview import routes as guided
    from agents.runtime_provision_helpers import RUNTIME_CONFIG_LOCK

    roll_threads: list[str] = []

    def roll(systems):
        roll_threads.append(threading.current_thread().name)
        with RUNTIME_CONFIG_LOCK:  # as refresh_business_context does
            pass
        return {s: "ok" for s in systems}

    monkeypatch.setattr(guided, "_known_system_ids", lambda: {"erp"})
    monkeypatch.setattr(guided, "_roll", roll)
    monkeypatch.setattr(guided, "_audit", lambda *_a, **_k: None)
    import agents.business_context as bc
    monkeypatch.setattr(bc, "write_business_context", lambda *_a: True)
    body = guided.ConfirmContextRequest(business_context="Lines are numbered L1-L4.",
                                        systems=["erp"])
    request = types.SimpleNamespace(state=types.SimpleNamespace(user=None))
    ticks = {"n": 0}
    held = threading.Event()

    def hold_lock():  # a Settings save holding the lock across its fleet loop
        with RUNTIME_CONFIG_LOCK:
            held.set()
            time.sleep(0.4)

    async def ticker(stop):
        while not stop.is_set():
            ticks["n"] += 1
            await asyncio.sleep(0.01)

    async def main():
        holder = threading.Thread(target=hold_lock)
        holder.start()
        held.wait(2)
        stop = asyncio.Event()
        t = asyncio.create_task(ticker(stop))
        result = await guided.guided_confirm_context(body, request)
        stop.set()
        await t
        holder.join()
        return result

    result = asyncio.run(main())
    assert result["status"] == "ok", result
    assert roll_threads and roll_threads[0].startswith("adt-offload-general"), roll_threads
    assert ticks["n"] >= 10, ticks


def test_guided_answer_during_a_confirm_survives_the_draft_clear(monkeypatch):
    """The confirm now runs off the loop for the
    whole roll, so a guided answer can land on the loop while it runs. The
    clear at the end must remove only what was confirmed: the answer and its
    landing that arrived mid-confirm stay for the next confirm (on main the
    blocked loop kept them out of the window; a blanket clear lost them)."""
    from agents.context_interview import routes as guided
    from agents.context_interview import state

    state.reset_for_tests()
    state.append_draft("Lines are numbered L1-L4.")
    state.record_landing({"question_id": "q-lines", "text": "Lines are numbered L1-L4."})
    rolling, release = threading.Event(), threading.Event()

    def roll(systems):
        rolling.set()
        release.wait(5)
        return {s: "ok" for s in systems}

    written: list[str] = []
    monkeypatch.setattr(guided, "_known_system_ids", lambda: {"erp"})
    monkeypatch.setattr(guided, "_roll", roll)
    monkeypatch.setattr(guided, "_audit", lambda *_a, **_k: None)
    import agents.business_context as bc
    monkeypatch.setattr(bc, "write_business_context",
                        lambda _sid, text, _enabled: written.append(text) or True)
    body = guided.ConfirmContextRequest(systems=["erp"])  # confirm the held draft
    request = types.SimpleNamespace(state=types.SimpleNamespace(user=None))

    async def answer_mid_confirm():
        while not rolling.is_set():
            await asyncio.sleep(0.01)
        # What _apply_turn does on the loop for a business-context answer.
        state.append_draft("Scrap is recorded per shift.")
        state.record_landing({"question_id": "q-scrap", "text": "Scrap is recorded per shift."})
        release.set()

    async def main():
        late = asyncio.create_task(answer_mid_confirm())
        result = await guided.guided_confirm_context(body, request)
        await late
        return result

    try:
        result = asyncio.run(main())
        assert result["status"] == "ok", result
        assert written == ["Lines are numbered L1-L4."], written
        assert state.draft_context() == "Scrap is recorded per shift."
        assert [e["question_id"] for e in state.landings()] == ["q-scrap"]
    finally:
        state.reset_for_tests()


def _blocking_confirm_setup(monkeypatch):
    """Patch the guided confirm so its write blocks until released.

    Returns (guided, state, written, writing, release)."""
    from agents.context_interview import routes as guided
    from agents.context_interview import state

    state.reset_for_tests()
    writing, release = threading.Event(), threading.Event()
    written: list[str] = []

    def write(_sid, text, _enabled):
        writing.set()
        release.wait(5)
        written.append(text)
        return True

    monkeypatch.setattr(guided, "_known_system_ids", lambda: {"erp"})
    monkeypatch.setattr(guided, "_roll", lambda systems: {s: "ok" for s in systems})
    monkeypatch.setattr(guided, "_audit", lambda *_a, **_k: None)
    import agents.business_context as bc
    monkeypatch.setattr(bc, "write_business_context", write)
    return guided, state, written, writing, release


def _request():
    return types.SimpleNamespace(state=types.SimpleNamespace(user=None))


def test_overlapping_guided_confirms_lose_no_answer(monkeypatch):
    """Two overlapping confirms each wrote their
    own snapshot (last writer wins on the System node) and the earlier one's
    clear ran against the later one's state. Repro: confirm A blocks in its
    write; an answer lands; confirm B writes and clears both answers; A then
    finishes and overwrites the node with the OLD text, and the late answer is
    gone from the node, the draft and the manifest while both return ok. Now
    the second confirm is refused with 409 and writes nothing, and the late
    answer stays in the draft for the next confirm."""
    guided, state, written, writing, release = _blocking_confirm_setup(monkeypatch)
    state.append_draft("Lines are numbered L1-L4.")
    state.record_landing({"question_id": "q-lines", "text": "Lines are numbered L1-L4."})

    async def main():
        first = asyncio.create_task(guided.guided_confirm_context(
            guided.ConfirmContextRequest(systems=["erp"]), _request()))
        while not writing.is_set():
            await asyncio.sleep(0.01)
        state.append_draft("Scrap is recorded per shift.")
        state.record_landing({"question_id": "q-scrap", "text": "Scrap is recorded per shift."})
        second = None
        try:
            await guided.guided_confirm_context(
                guided.ConfirmContextRequest(systems=["erp"]), _request())
        except HTTPException as exc:
            second = exc
        finally:
            release.set()
        return await first, second

    try:
        result, second = asyncio.run(main())
        assert result["status"] == "ok", result
        assert second is not None and second.status_code == 409, second
        assert second.detail == guided.CONFIRM_IN_FLIGHT_DETAIL, second.detail
        assert written == ["Lines are numbered L1-L4."], written
        assert state.draft_context() == "Scrap is recorded per shift."
        assert [e["question_id"] for e in state.landings()] == ["q-scrap"]
    finally:
        release.set()
        state.reset_for_tests()


def test_guided_confirm_holds_its_slot_until_the_write_finishes(monkeypatch):
    """A client that disconnects cancels the handler, but the offloaded write
    keeps running: the slot must stay held until the write is done, or a new
    confirm could overlap it. A sequential confirm afterwards is accepted."""
    guided, state, written, writing, release = _blocking_confirm_setup(monkeypatch)
    state.append_draft("Lines are numbered L1-L4.")

    async def main():
        first = asyncio.create_task(guided.guided_confirm_context(
            guided.ConfirmContextRequest(systems=["erp"]), _request()))
        while not writing.is_set():
            await asyncio.sleep(0.01)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(HTTPException) as during:
            await guided.guided_confirm_context(
                guided.ConfirmContextRequest(systems=["erp"]), _request())
        release.set()
        for _ in range(500):  # the cancelled confirm's write finishes off-loop
            if written and state.draft_context() == "":
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)  # its done-callback releases the slot on the loop
        state.append_draft("Scrap is recorded per shift.")
        after = await guided.guided_confirm_context(
            guided.ConfirmContextRequest(systems=["erp"]), _request())
        return during.value, after

    try:
        during, after = asyncio.run(main())
        assert during.status_code == 409, during
        assert after["status"] == "ok", after
        assert written == ["Lines are numbered L1-L4.", "Scrap is recorded per shift."], written
        assert state.draft_context() == ""
    finally:
        release.set()
        state.reset_for_tests()


def test_guided_confirm_releases_its_slot_when_the_write_raises(monkeypatch):
    from agents.context_interview import routes as guided
    from agents.context_interview import state

    state.reset_for_tests()
    state.append_draft("Lines are numbered L1-L4.")
    calls = {"n": 0}

    def flaky(systems, clean):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("graph unavailable")
        return {s: {"written": True} for s in systems}

    monkeypatch.setattr(guided, "_write_context_batched", flaky)
    monkeypatch.setattr(guided, "_audit", lambda *_a, **_k: None)

    async def main():
        with pytest.raises(RuntimeError):
            await guided.guided_confirm_context(
                guided.ConfirmContextRequest(systems=["erp"]), _request())
        return await guided.guided_confirm_context(
            guided.ConfirmContextRequest(systems=["erp"]), _request())

    try:
        assert asyncio.run(main())["status"] == "ok"
        assert calls["n"] == 2
        import agents.business_context as bc
        assert not getattr(bc, "_SAVING", set()), bc._SAVING
    finally:
        state.reset_for_tests()


# -- the admin PUT and the guided confirm never save one system at once --------

def _gated_saves(monkeypatch):
    """Patch both business-context writers so the FIRST graph write blocks
    until released; later writes go straight through.

    Returns (guided, state, admin_routes, written, writing, release)."""
    import agents.business_context as bc
    import agents.connection_resolver as cr
    from agents import admin_routes
    from agents.catalog import catalog

    guided, state, _w, _writing, _release = _blocking_confirm_setup(monkeypatch)
    monkeypatch.setattr(bc, "_SAVING", set(), raising=False)  # a failed test never leaks a claim
    writing, release = threading.Event(), threading.Event()
    written: list[tuple[str, str]] = []

    def write(sid, text, _enabled):
        if not writing.is_set():
            writing.set()
            release.wait(5)
        written.append((sid, text))
        return True

    monkeypatch.setattr(bc, "write_business_context", write)
    monkeypatch.setattr(catalog, "get", lambda system_id: {"id": system_id})
    monkeypatch.setattr(cr, "audit_connection_event", lambda *_a, **_k: None)
    monkeypatch.setattr(admin_routes, "_refresh_business_context_batched",
                        lambda sid: ("ok", {sid: {"status": "ok"}}))
    return guided, state, admin_routes, written, writing, release


def _put(admin_routes, system_id: str, text: str):
    return admin_routes.put_business_context_route(
        system_id, _admin_request({"business_context": text, "enabled": True}))


def _confirm(guided, system_id: str):
    return guided.guided_confirm_context(
        guided.ConfirmContextRequest(systems=[system_id]), _request())


async def _until(event: threading.Event):
    while not event.is_set():
        await asyncio.sleep(0.01)


def test_admin_put_during_a_guided_confirm_of_the_same_system_is_refused(monkeypatch):
    """The PUT and the confirm both write
    the System node off the loop, and only confirms were serialised. Repro:
    a confirm blocks in its write; an admin PUT of the same system writes and
    returns ok; the confirm then lands its older text over it, and both
    report ok. Now the PUT is refused with 409 and writes nothing, and a PUT
    after the confirm finishes is accepted."""
    guided, state, admin_routes, written, writing, release = _gated_saves(monkeypatch)
    state.append_draft("guided-old")

    async def main():
        first = asyncio.create_task(_confirm(guided, "erp"))
        await _until(writing)
        try:
            with pytest.raises(HTTPException) as during:
                await _put(admin_routes, "erp", "admin-new")
        finally:
            release.set()
        confirmed = await first
        after = await _put(admin_routes, "erp", "admin-new")
        return during.value, confirmed, after

    try:
        during, confirmed, after = asyncio.run(main())
        assert during.status_code == 409, during
        assert "already being saved" in during.detail, during.detail
        assert confirmed["status"] == "ok", confirmed
        assert after["status"] == "ok", after
        assert written == [("erp", "guided-old"), ("erp", "admin-new")], written
    finally:
        release.set()
        state.reset_for_tests()


def test_guided_confirm_during_an_admin_put_of_the_same_system_is_refused(monkeypatch):
    """The other order: a confirm of a system the PUT is still saving gets
    409, keeps its draft, and gives the confirm slot back, so a confirm after
    the PUT finishes is accepted."""
    guided, state, admin_routes, written, writing, release = _gated_saves(monkeypatch)
    state.append_draft("guided-new")

    async def main():
        first = asyncio.create_task(_put(admin_routes, "erp", "admin-old"))
        await _until(writing)
        try:
            with pytest.raises(HTTPException) as during:
                await _confirm(guided, "erp")
            draft_during = state.draft_context()
        finally:
            release.set()
        put = await first
        after = await _confirm(guided, "erp")
        return during.value, draft_during, put, after

    try:
        during, draft_during, put, after = asyncio.run(main())
        assert during.status_code == 409, during
        assert "already being saved" in during.detail, during.detail
        assert draft_during == "guided-new"
        assert put["status"] == "ok", put
        assert after["status"] == "ok", after
        assert written == [("erp", "admin-old"), ("erp", "guided-new")], written
        assert state.draft_context() == ""
    finally:
        release.set()
        state.reset_for_tests()


def test_saves_of_different_systems_still_overlap(monkeypatch):
    """The claim is per system: a PUT of another system is not held up by a
    confirm, and two PUTs of one system are not allowed to overlap."""
    guided, state, admin_routes, written, writing, release = _gated_saves(monkeypatch)
    state.append_draft("guided-erp")

    async def main():
        first = asyncio.create_task(_confirm(guided, "erp"))
        await _until(writing)
        try:
            other = await _put(admin_routes, "mes", "admin-mes")
        finally:
            release.set()
        await first
        second_put = asyncio.create_task(_put(admin_routes, "mes", "one"))
        await asyncio.sleep(0)  # the first PUT has claimed "mes" on the loop
        with pytest.raises(HTTPException) as again:
            await _put(admin_routes, "mes", "two")
        return other, await second_put, again.value

    try:
        other, put_one, again = asyncio.run(main())
        assert other["status"] == "ok", other
        assert put_one["status"] == "ok", put_one
        assert again.status_code == 409, again
        assert ("mes", "admin-mes") in written and ("mes", "two") not in written, written
    finally:
        release.set()
        state.reset_for_tests()


def test_a_cancelled_admin_put_holds_its_claim_until_the_write_finishes(monkeypatch):
    """A disconnect cancels the PUT handler while its write keeps running; the
    system stays claimed until the write is done, then the claim is free."""
    import agents.business_context as bc

    guided, state, admin_routes, written, writing, release = _gated_saves(monkeypatch)

    async def main():
        first = asyncio.create_task(_put(admin_routes, "erp", "admin-old"))
        await _until(writing)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        held = set(bc._SAVING)
        release.set()
        for _ in range(500):
            if not bc._SAVING:
                break
            await asyncio.sleep(0.01)
        return held

    try:
        held = asyncio.run(main())
        assert held == {"erp"}, held
        assert not bc._SAVING, bc._SAVING
        assert written == [("erp", "admin-old")], written
    finally:
        release.set()
        state.reset_for_tests()


def test_guided_confirm_without_a_concurrent_answer_clears_everything(monkeypatch):
    """The usual case is unchanged: a confirm with nothing landing meanwhile
    leaves an empty draft and an empty landings manifest."""
    from agents.context_interview import routes as guided
    from agents.context_interview import state

    state.reset_for_tests()
    state.append_draft("Lines are numbered L1-L4.")
    state.record_landing({"question_id": "q-lines", "text": "Lines are numbered L1-L4."})
    monkeypatch.setattr(guided, "_known_system_ids", lambda: {"erp"})
    monkeypatch.setattr(guided, "_roll", lambda systems: {s: "ok" for s in systems})
    monkeypatch.setattr(guided, "_audit", lambda *_a, **_k: None)
    import agents.business_context as bc
    monkeypatch.setattr(bc, "write_business_context", lambda *_a: True)
    request = types.SimpleNamespace(state=types.SimpleNamespace(user=None))
    try:
        result = asyncio.run(guided.guided_confirm_context(
            guided.ConfirmContextRequest(systems=["erp"]), request))
        assert result["status"] == "ok", result
        assert state.draft_context() == ""
        assert state.landings() == []
    finally:
        state.reset_for_tests()


def test_connection_update_reconnects_off_the_loop(monkeypatch):
    """The whole connection update (resolve, write, and the reconnect that
    rewrites the agent-config secret and rolls the runtime) runs in the
    offload pool, where the reconnect can take the runtime-config lock."""
    import agents.connection_resolver as cr
    from agents import admin_routes
    from agents.catalog import catalog

    monkeypatch.delenv("HIGHBYTE_I3X_TOKEN", raising=False)  # the shim sets it
    monkeypatch.setattr(catalog, "get", lambda system_id: {
        "id": system_id, "connection_type": "MCP Server"})
    monkeypatch.setattr(cr, "resolve_connection",
                        lambda system_id: {"url": "https://mcp.example.test/mcp"})
    monkeypatch.setattr(cr, "write_connection", lambda *_a, **_k: None)
    monkeypatch.setattr(cr, "audit_connection_event", lambda *_a, **_k: None)
    reconnect_threads: list[str] = []

    def reconnect(system_id, connection_type, cat_entry):
        reconnect_threads.append(threading.current_thread().name)
        return True

    monkeypatch.setattr(admin_routes, "_reconnect_system", reconnect)
    result = asyncio.run(admin_routes.update_highbyte_token(_admin_request({"token": "tok"})))
    assert result["status"] == "ok" and result["reconnected"] is True, result
    assert reconnect_threads and reconnect_threads[0].startswith(
        "adt-offload-general"), reconnect_threads
