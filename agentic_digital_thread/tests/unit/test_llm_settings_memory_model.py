# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The memory extraction model on the Admin LLM settings routes.

GET /api/admin/llm-settings reports ``memory_model`` (or ``mixed``), whether
it may be changed here and why not, ``memory_model_error`` when a read fails
(never a 500), and whether the next deploy would keep the model the memory
runs. The switch is a background job of its own, because AgentCore takes
about two minutes to settle and an origin request lasts at most 120 s:
POST /api/admin/llm-settings/memory-model answers 202 with a job id (400
before any write when the model is refused, 409 while another switch holds
the slot, 503 when the shared slot store cannot decide), and
GET /api/admin/llm-settings/memory-model/job[/{id}] reports it. The worker
updates the memory and reads it back FIRST, then writes SSM
``/adt/llm-memory-model``, so SSM never names a model the memory does not
run. PUT /api/admin/llm-settings never switches the memory. Offline: boto3
is faked, and the worker runs inline unless a test starts a real thread.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import threading
import time
import types
from unittest.mock import patch

import pytest

from tests.unit._memory_model_fakes import MEMORY_ID, NEW, OLD, Clock, FakeControl
from tests.unit._memory_model_fakes import memory_record as _memory

SSM_MEMORY = "/adt/llm-memory-model"
SSM_ORCH = "/adt/llm-orchestrator-model"
SSM_AGENT = "/adt/llm-agent-model"
SWITCH_ROUTE = "/api/admin/llm-settings/memory-model"


class FakeSSM:
    def __init__(self, *, fail_on: str = "", fail_reads_of: str = ""):
        self.store: dict[str, str] = {}
        self.fail_on = fail_on
        self.fail_reads_of = fail_reads_of
        self.log: list[str] = []
        self.after_put = None   # called after a successful put (name, value)

    def get_parameter(self, Name):  # noqa: N803 - boto3 casing
        if Name not in self.store:
            raise Exception(f"ParameterNotFound: {Name}")
        return {"Parameter": {"Value": self.store[Name]}}

    def get_parameters(self, Names):  # noqa: N803 - boto3 casing
        if self.fail_reads_of in Names:
            raise RuntimeError("AccessDenied arn:aws:ssm:us-east-1:111122223333:parameter/x")
        return {"Parameters": [{"Name": n, "Value": self.store[n]} for n in Names
                               if n in self.store],
                "InvalidParameters": [n for n in Names if n not in self.store]}

    def put_parameter(self, Name, Value, Type="String", Overwrite=True):  # noqa: N803
        self.log.append(f"put {Name}")
        if Name == self.fail_on:
            raise RuntimeError("AccessDenied arn:aws:ssm:us-east-1:111122223333:parameter/x")
        self.store[Name] = Value
        if self.after_put is not None:
            self.after_put(Name, Value)
        return {}


class _NoRuntimes:
    def list_agent_runtimes(self, **_kwargs):
        return {"agentRuntimes": []}


class Recorder(FakeControl):
    """The memory control plane, logging its UpdateMemory into a shared log
    so the order against the SSM writes is visible."""

    def __init__(self, log: list, **kwargs):
        super().__init__(busy_reads=1, **kwargs)
        self.log = log

    def update_memory(self, **kwargs):
        self.log.append("update_memory")
        return super().update_memory(**kwargs)

    def list_agent_runtimes(self, **_kwargs):
        return {"agentRuntimes": []}


class _LazyModule:
    """A module imported on first use so that, before it existed, each test
    failed on its own rather than the whole file at collection."""

    def __init__(self, name: str):
        self._name = name

    def __getattr__(self, attr):
        import importlib
        return getattr(importlib.import_module(self._name), attr)


mm = _LazyModule("agents.memory_model")
jobs = _LazyModule("agents.memory_model_job")


def _inline_worker(target, *args):
    target(*args)


@pytest.fixture
def env(monkeypatch):
    from agents import memory_model as mm_module
    from agents import model_config
    from agents.demo import workshop
    from agents.job_store import JobStore
    # The SSM client is cached per process; start from none so boto3.client's
    # fake below is the one the settings read uses.
    model_config._reset_ssm_clients()
    monkeypatch.setattr(model_config, "_SSM_CLIENTS", {})
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("BEDROCK_MODEL", "global.anthropic.claude-sonnet-5-5")
    monkeypatch.setenv("SYSTEM_AGENT_MODEL", "global.anthropic.claude-haiku-5-5")
    monkeypatch.delenv("ALLOWED_MODELS", raising=False)
    monkeypatch.setattr(workshop, "is_enabled", lambda: False)
    monkeypatch.setattr(mm_module, "configured_memory_id", lambda: MEMORY_ID)
    # The app's config is loaded, as on a running app: the settings view reads
    # the memory id from it and never loads it.
    monkeypatch.setattr(mm_module, "loaded_memory_id", lambda: (True, MEMORY_ID),
                        raising=False)
    # No real sleeping in the post-update wait.
    monkeypatch.setattr(mm_module, "POLL_INTERVAL_SECONDS", 0.0)
    # A fresh in-memory save slot per test (the deployed store is DynamoDB).
    monkeypatch.setattr(mm_module, "_SAVE_SLOTS", JobStore("memory-model-save", table=None),
                        raising=False)
    try:
        from agents import memory_model_job
    except ImportError:   # before the job module existed: its tests fail one by one
        memory_model_job = None
    if memory_model_job is not None:
        monkeypatch.setattr(memory_model_job, "_JOBS", JobStore("memory-model-job", table=None))
        monkeypatch.setattr(memory_model_job, "_start_worker", _inline_worker)
    return monkeypatch


def _request(body):
    req = types.SimpleNamespace(state=types.SimpleNamespace(is_admin=True))

    async def _json():
        if isinstance(body, Exception):
            raise body
        return body
    req.json = _json
    return req


def _boto(ssm, control, clients=None):
    def fake_client(service_name, **_kwargs):
        if clients is not None:
            clients.append(service_name)
        if service_name == "ssm":
            return ssm
        if service_name == "bedrock-agentcore-control":
            return control
        raise RuntimeError(f"unexpected boto3 client in test: {service_name}")
    return patch("boto3.client", fake_client)


def _put(body: dict, ssm, control, clients=None):
    from agents.admin_routes import update_llm_settings_route
    with _boto(ssm, control, clients):
        return asyncio.run(update_llm_settings_route(_request(body)))


def _get(control, ssm=None):
    from agents.admin_routes import get_llm_settings_route
    with _boto(ssm or FakeSSM(), control):
        return asyncio.run(get_llm_settings_route(types.SimpleNamespace(state=types.SimpleNamespace(is_admin=True))))


def _post(body, ssm, control, clients=None):
    from agents.admin_routes import start_memory_model_switch_route
    with _boto(ssm, control, clients):
        return asyncio.run(start_memory_model_switch_route(_request(body)))


def _job(job_id: str | None = None):
    from agents import admin_routes
    if job_id is None:
        return asyncio.run(admin_routes.memory_model_job_route(types.SimpleNamespace(state=types.SimpleNamespace(is_admin=True))))
    return asyncio.run(admin_routes.memory_model_job_by_id_route(job_id, types.SimpleNamespace(state=types.SimpleNamespace(is_admin=True))))


def _body(res):
    return json.loads(res.body) if hasattr(res, "body") else res


def _code(res) -> int:
    return getattr(res, "status_code", 200)


def _switch(model: str, ssm, control, clients=None) -> dict:
    """POST a switch (the worker runs inline) and return its job record as
    the status route reports it."""
    res = _post({"memory_model": model}, ssm, control, clients)
    assert _code(res) == 202, _body(res)
    started = _body(res)
    assert started["status"] == "running" and started["job_id"], started
    reported = _job(started["job_id"])
    assert _code(reported) == 200, _body(reported)
    return _body(reported)


def _slot():
    return mm._save_slots().get(mm.SAVE_SLOT_KEY)


# -- GET ---------------------------------------------------------------------------

def test_get_reports_the_memory_model(env):
    res = _get(FakeControl())
    assert res["memory_model"] == OLD
    assert res["memory_model_selectable"] is True
    assert res["memory_model_locked_reason"] == ""
    assert "memory_model_error" not in res
    # The existing fields are unchanged.
    assert res["orchestrator_model"] == "global.anthropic.claude-sonnet-5-5"
    assert res["agent_model"] == "global.anthropic.claude-haiku-5-5"


def test_get_reports_mixed(env):
    memory = _memory()
    memory["strategies"][2]["configuration"]["extraction"]["customExtractionConfiguration"][
        "userPreferenceExtractionOverride"]["modelId"] = NEW
    assert _get(FakeControl(memory))["memory_model"] == "mixed"


def test_get_never_500s_on_a_failed_memory_read(env):
    class Broken(_NoRuntimes):
        def get_memory(self, **_kwargs):
            raise RuntimeError("boom 111122223333")
    res = _get(Broken())
    assert res["memory_model"] == ""
    assert res["memory_model_error"] and "111122223333" not in res["memory_model_error"]
    assert res["orchestrator_model"]


def test_get_on_the_workshop_is_read_only(env):
    from agents.demo import workshop
    env.setattr(workshop, "is_enabled", lambda: True)
    res = _get(FakeControl(_memory("us.amazon.nova-pro-v1:0")))
    assert res["memory_model"] == "us.amazon.nova-pro-v1:0"
    assert res["memory_model_selectable"] is False
    assert "workshop" in res["memory_model_locked_reason"].lower()
    assert "memory_model_unsaved" not in res


def _awaited_offload(fn) -> ast.Call:
    """The ``offload(...)`` call an async route returns, after the docstring
    and the optional body read."""
    tree = ast.parse(inspect.getsource(fn))
    (handler,) = tree.body
    assert isinstance(handler, ast.AsyncFunctionDef)
    body = [n for n in handler.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    last = body[-1]
    assert isinstance(last, ast.Return) and isinstance(last.value, ast.Await)
    return last.value.value


def test_get_route_offloads_its_sync_body():
    """The GET reads AgentCore, so it follows the offload rule: the handler's
    admin check is followed by one statement that awaits ``offload`` on the
    sync ``get_llm_settings``."""
    from agents import admin_routes
    tree = ast.parse(inspect.getsource(admin_routes.get_llm_settings_route))
    (fn,) = tree.body
    body = [n for n in fn.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    assert len(body) == 2
    assert ast.unparse(body[0]) == "_check_admin(request)"
    call = _awaited_offload(admin_routes.get_llm_settings_route)
    assert call.func.id == "offload" and call.args[0].id == "get_llm_settings"
    assert not inspect.iscoroutinefunction(admin_routes.get_llm_settings)


# -- GET: would the next deploy keep the model the memory runs? ---------------------------

def test_get_warns_when_nothing_saved_would_put_the_default_back(env):
    """The live failure: the memory runs Sonnet 5.5, SSM holds nothing, so the
    next deploy that updates the memory puts the default (Haiku 5.5) back."""
    res = _get(FakeControl(_memory(NEW)))
    assert res["memory_model"] == NEW
    assert res["memory_model_unsaved"] is True
    assert res["memory_model_next_deploy"] == OLD
    assert "memory_model_saved" not in res and "memory_model_error" not in res


def test_get_says_nothing_when_the_memory_runs_the_default_with_nothing_saved(env):
    res = _get(FakeControl())
    assert "memory_model_unsaved" not in res and "memory_model_next_deploy" not in res


def test_get_with_the_running_model_saved_says_nothing(env):
    ssm = FakeSSM()
    ssm.store[SSM_MEMORY] = NEW
    res = _get(FakeControl(_memory(NEW)), ssm)
    assert "memory_model_unsaved" not in res and "memory_model_saved" not in res


# -- PUT never switches the memory -----------------------------------------------------------

@pytest.mark.parametrize("model", [NEW, OLD, "made.up-model"])
def test_put_with_a_memory_model_is_refused_before_any_write_and_names_the_switch(env, model):
    log, clients = [], []
    ssm, control = FakeSSM(), Recorder(log)
    res = _put({"orchestrator_model": "global.anthropic.claude-sonnet-5", "agent_model": "",
                "memory_model": model}, ssm, control, clients)
    assert _code(res) == 400
    message = _body(res)["message"]
    assert SWITCH_ROUTE in message
    assert "Nothing was changed" in message
    assert ssm.log == [] and control.updates == [] and control.gets == 0 and clients == []
    assert _slot() is None


def test_put_with_a_blank_memory_model_is_the_orchestrator_and_agent_save(env):
    ssm, control = FakeSSM(), Recorder([])
    res = _put({"orchestrator_model": "", "agent_model": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
                "memory_model": "  "}, ssm, control)
    assert res["status"] == "ok", res
    assert control.updates == [] and control.gets == 0
    assert SSM_MEMORY not in ssm.store


def test_put_without_memory_model_never_touches_the_memory(env):
    log = []
    ssm, control = FakeSSM(), Recorder(log)
    res = _put({"orchestrator_model": "", "agent_model": "global.anthropic.claude-haiku-4-5-20251001-v1:0"},
               ssm, control)
    assert res["status"] == "ok", res
    assert not any(key.startswith("memory_model") for key in res)
    assert control.updates == [] and control.gets == 0
    assert SSM_MEMORY not in ssm.store


class _Fleet:
    mode = "cloud"

    def __init__(self, outcomes: dict):
        self.outcomes = outcomes

    def list_agents(self):
        return [{"system_id": s} for s in self.outcomes]

    def refresh_agent_model(self, system_id, _model):
        return self.outcomes[system_id]


_HAIKU_45 = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_put_answers_the_orchestrator_and_agent_result_only(env):
    """As on b4182b45: no memory field and no memory message, also when the
    agent fleet refresh left runtimes behind."""
    env.setattr("agents.admin_routes._get_agent_manager", lambda: _Fleet(
        {"sys-ok": "ok", "sys-failed": "failed"}))
    res = _put({"orchestrator_model": "", "agent_model": _HAIKU_45}, FakeSSM(), Recorder([]))
    assert set(res) == {"status", "orchestrator_model", "agent_model", "agent_runtime_refresh",
                        "reonboard_required"}, res
    assert res["agent_runtime_refresh"]["failed"] == ["sys-failed"]


# -- POST: refusals before any write --------------------------------------------------

def _assert_nothing_touched(ssm, control, clients) -> None:
    assert ssm.log == [] and control.updates == [] and control.gets == 0 and clients == []
    assert _slot() is None


@pytest.mark.parametrize("model", [
    "global.anthropic.claude-sonnet-4-6",   # a catalog model, not a memory model
    "openai.gpt-5.6-luna",
    "made.up-model",
])
def test_a_switch_to_a_non_memory_model_is_refused_before_any_write(env, model):
    clients: list = []
    ssm, control = FakeSSM(), Recorder([])
    res = _post({"memory_model": model}, ssm, control, clients)
    assert _code(res) == 400
    assert "memory_model" in _body(res)["message"]
    _assert_nothing_touched(ssm, control, clients)


@pytest.mark.parametrize("body", [{}, {"memory_model": "  "}, ["x"], ValueError("not json"),
                                  {"memory_model": NEW, "orchestrator_model": "x"}])
def test_a_switch_without_one_memory_model_is_refused_before_any_write(env, body):
    clients: list = []
    ssm, control = FakeSSM(), Recorder([])
    res = _post(body, ssm, control, clients)
    assert _code(res) == 400
    assert _body(res)["status"] == "error" and _body(res)["message"]
    _assert_nothing_touched(ssm, control, clients)


def test_a_switch_to_a_memory_model_outside_the_allowlist_is_refused(env):
    env.setenv("ALLOWED_MODELS", "anthropic.claude-sonnet-5,anthropic.claude-haiku-4-5-20251001-v1:0")
    clients: list = []
    ssm, control = FakeSSM(), Recorder([])
    res = _post({"memory_model": "global.anthropic.claude-haiku-5-5"}, ssm, control, clients)
    assert _code(res) == 400
    assert "ALLOWED_MODELS" in _body(res)["message"]
    _assert_nothing_touched(ssm, control, clients)


def test_a_switch_is_refused_on_the_workshop(env):
    from agents.demo import workshop
    env.setattr(workshop, "is_enabled", lambda: True)
    clients: list = []
    ssm, control = FakeSSM(), Recorder([])
    res = _post({"memory_model": NEW}, ssm, control, clients)
    assert _code(res) == 400
    _assert_nothing_touched(ssm, control, clients)


def test_a_switch_without_a_memory_is_the_jobs_error_and_touches_nothing(env):
    """The request does not look the memory up (that can load the app's
    config); the worker does, and with no memory it ends the job with the
    no-memory error before any AgentCore or SSM call."""
    env.setattr("agents.memory_model.configured_memory_id", lambda: None)
    clients: list = []
    ssm, control = FakeSSM(), Recorder([])
    job = _switch(NEW, ssm, control, clients)
    assert job["status"] == "error", job
    assert job["memory_model_changed"] is False
    assert job["message"] == mm.NO_MEMORY
    assert "not configured" in job["message"] and "Nothing was changed" in job["message"]
    assert ssm.log == [] and control.updates == [] and control.gets == 0 and clients == []
    assert _slot()["status"] == "idle"


def test_a_non_admin_switch_is_403(env):
    from fastapi import HTTPException
    from agents.admin_routes import start_memory_model_switch_route
    req = _request({"memory_model": NEW})
    req.state.is_admin = False
    with pytest.raises(HTTPException) as raised:
        asyncio.run(start_memory_model_switch_route(req))
    assert raised.value.status_code == 403


# -- POST: the job -------------------------------------------------------------------

def test_the_switch_updates_the_memory_first_then_writes_ssm(env):
    log = []
    ssm, control = FakeSSM(), Recorder(log)
    ssm.log = log
    job = _switch(NEW, ssm, control)
    assert job["status"] == "done", job
    assert job["memory_model"] == NEW and job["memory_model_changed"] is True
    assert NEW in job["message"] and "next deploy" in job["message"]
    assert ssm.store == {SSM_MEMORY: NEW}
    assert log == ["update_memory", f"put {SSM_MEMORY}"]
    assert mm.read_state(control, MEMORY_ID).model_id == NEW
    assert job["finished_at"] >= job["started_at"]


def test_the_switch_resolves_the_id_for_the_region(env):
    ssm, control = FakeSSM(), Recorder([])
    job = _switch("us.anthropic.claude-sonnet-5-5", ssm, control)
    assert job["memory_model"] == NEW
    assert ssm.store[SSM_MEMORY] == NEW


def test_the_answer_is_202_with_the_running_record_and_the_slot_owner(env):
    """The POST answers before the worker runs; the job id is the slot owner."""
    pending: list = []
    env.setattr("agents.memory_model_job._start_worker",
                lambda target, *args: pending.append((target, args)))
    clients: list = []
    ssm, control = FakeSSM(), Recorder([])
    res = _post({"memory_model": NEW}, ssm, control, clients)
    assert _code(res) == 202
    started = _body(res)
    assert started["status"] == "running" and started["memory_model"] == NEW
    assert _slot()["status"] == "running" and _slot()["job_id"] == started["job_id"]
    assert _body(_job(started["job_id"]))["status"] == "running"
    assert control.gets == 0 and ssm.log == [] and clients == []
    target, args = pending[0]
    with _boto(ssm, control):
        target(*args)
    assert _body(_job(started["job_id"]))["status"] == "done"
    assert _slot()["status"] == "idle"


def test_saving_the_model_the_memory_runs_is_the_repair(env):
    """Nothing saved while the memory runs Sonnet 5.5 (the live end state):
    saving Sonnet 5.5 writes SSM with no UpdateMemory, and the warning goes."""
    ssm, control = FakeSSM(), Recorder([], memory=_memory(NEW))
    assert _get(control, ssm)["memory_model_unsaved"] is True
    job = _switch(NEW, ssm, control)
    assert job["status"] == "done", job
    assert job["memory_model_changed"] is False
    assert "already ran" in job["message"]
    assert control.updates == []
    assert ssm.store == {SSM_MEMORY: NEW}
    after = _get(control, ssm)
    assert "memory_model_unsaved" not in after and "memory_model_saved" not in after


def test_a_switch_that_takes_131_seconds_finishes_and_saves(env):
    """adt-demo, 2026-10-27: AgentCore settled about 131 s after UpdateMemory.
    The old 120 s wait gave up 11 s before that and saved nothing."""
    clock = Clock()
    env.setattr("agents.memory_model._clock", clock)
    env.setattr("agents.memory_model._sleep", clock.sleep)
    env.setattr("agents.memory_model.POLL_INTERVAL_SECONDS", 5.0)
    ssm, control = FakeSSM(), Recorder([])
    control.busy_reads = 27   # reads at 0, 5, ..., 130 s are UPDATING; 135 s settles
    job = _switch(NEW, ssm, control)
    assert job["status"] == "done", job
    assert clock.now >= 131
    assert ssm.store == {SSM_MEMORY: NEW}


def test_a_failed_memory_update_writes_nothing(env):
    memory = _memory()
    memory["strategies"][0]["type"] = "SEMANTIC"   # not a custom override
    ssm, control = FakeSSM(), Recorder([], memory=memory)
    job = _switch(NEW, ssm, control)
    assert job["status"] == "error"
    assert job["memory_model_changed"] is False
    assert "Nothing was saved" in job["message"]
    assert ssm.store == {} and ssm.log == [] and control.updates == []


def test_an_update_that_times_out_writes_nothing_and_says_what_was_read(env):
    ssm, control = FakeSSM(), Recorder([])
    control.busy_reads = 10_000
    env.setattr("agents.memory_model.POLL_TIMEOUT_SECONDS", 0.0)
    job = _switch(NEW, ssm, control)
    assert job["status"] == "error"
    assert "UPDATING" in job["message"]
    assert ssm.store == {}


def test_an_unexpected_memory_error_is_a_public_error(env):
    class Broken(_NoRuntimes):
        def get_memory(self, **_kwargs):
            raise RuntimeError("arn:aws:bedrock-agentcore:us-east-1:111122223333:memory/x")
    ssm = FakeSSM()
    job = _switch(NEW, ssm, Broken())
    assert job["status"] == "error"
    assert "111122223333" not in job["message"]
    assert ssm.store == {}


def test_ssm_failure_after_the_update_says_the_next_deploy_would_not_keep_it(env):
    ssm, control = FakeSSM(fail_on=SSM_MEMORY), Recorder([])
    job = _switch(NEW, ssm, control)
    assert job["status"] == "error"
    assert job["memory_model_changed"] is True
    message = job["message"]
    assert NEW in message and "next deploy" in message
    assert "111122223333" not in message


def test_a_worker_that_raises_records_a_public_error_and_releases_the_slot(env):
    def boom(*_args, **_kwargs):
        raise RuntimeError("boom arn:aws:iam::111122223333:role/x")
    env.setattr("agents.memory_model.run_save", boom)
    job = _switch(NEW, FakeSSM(), Recorder([]))
    assert job["status"] == "error"
    assert NEW in job["message"] and "111122223333" not in job["message"]
    assert _slot()["status"] == "idle"
    assert _code(_post({"memory_model": NEW}, FakeSSM(), Recorder([]))) == 202


# -- a retry on a memory that is not settled ---------------------------------------------

def _unsettled(kind: str) -> tuple[dict, str]:
    """A memory left by a failed or unfinished switch, and the model a retry asks for."""
    if kind == "failed":
        return _memory(NEW, status="FAILED"), NEW
    if kind == "failed_strategy":
        memory = _memory(NEW)
        memory["strategies"][2]["status"] = "FAILED"
        return memory, NEW
    if kind == "updating":
        return _memory(NEW, status="UPDATING"), NEW
    memory = _memory(OLD)   # missing_model_id: the other four overrides run OLD
    memory["strategies"][0]["configuration"]["extraction"]["customExtractionConfiguration"][
        "semanticExtractionOverride"].pop("modelId")
    return memory, OLD


@pytest.mark.parametrize("kind", ["failed", "failed_strategy", "updating", "missing_model_id"])
def test_a_retry_on_an_unsettled_memory_updates_nothing_and_writes_no_ssm(env, kind):
    memory, target = _unsettled(kind)
    ssm, control = FakeSSM(), Recorder([], memory=memory)
    job = _switch(target, ssm, control)
    assert job["status"] == "error", job
    assert job["memory_model_changed"] is False
    assert "Nothing was saved" in job["message"]
    assert control.updates == [] and ssm.log == [] and ssm.store == {}


# -- one switch at a time across app tasks ---------------------------------------------------

def _hold_slot(owner: str = "another-task", started_at: float | None = None) -> None:
    mm._save_slots().claim_slot(
        mm.SAVE_SLOT_KEY,
        {"status": "running", "job_id": owner,
         "started_at": time.time() if started_at is None else started_at},
        stale_seconds=mm.SAVE_SLOT_STALE_SECONDS)


def test_a_switch_while_another_holds_the_slot_is_409_before_any_write(env):
    _hold_slot()
    clients: list = []
    ssm, control = FakeSSM(), Recorder([])
    res = _post({"memory_model": NEW}, ssm, control, clients)
    assert _code(res) == 409
    body = _body(res)
    assert body["status"] == "error"
    assert "another" in body["message"].lower() and "in progress" in body["message"]
    assert control.gets == 0 and control.updates == [] and ssm.log == [] and clients == []
    assert _slot()["job_id"] == "another-task"


def test_a_stale_slot_is_taken_over(env):
    _hold_slot(started_at=time.time() - mm.SAVE_SLOT_STALE_SECONDS - 60)
    ssm, control = FakeSSM(), Recorder([])
    job = _switch(NEW, ssm, control)
    assert job["status"] == "done", job
    assert ssm.store == {SSM_MEMORY: NEW}


def test_the_wait_and_the_stale_bounds_outlast_a_measured_switch():
    """About 131 s was measured live; the wait allows several times that,
    and neither the slot nor a job record goes stale while a worker runs."""
    assert mm.POLL_TIMEOUT_SECONDS >= 600
    assert mm.JOB_MAX_SECONDS > mm.POLL_TIMEOUT_SECONDS
    assert mm.SAVE_SLOT_STALE_SECONDS >= 900
    assert mm.SAVE_SLOT_STALE_SECONDS > mm.JOB_MAX_SECONDS
    assert jobs.JOB_STALE_SECONDS > mm.JOB_MAX_SECONDS


@pytest.mark.parametrize("first_memory", ["ok", "unsupported"])
def test_the_slot_is_released_after_a_switch_or_a_failure(env, first_memory):
    memory = _memory()
    if first_memory == "unsupported":
        memory["strategies"][0]["type"] = "SEMANTIC"
    first = _switch(NEW, FakeSSM(), Recorder([], memory=memory))
    assert first["status"] == ("done" if first_memory == "ok" else "error")
    assert _slot()["status"] == "idle"
    second = _switch(NEW, FakeSSM(), Recorder([]))
    assert second["status"] == "done", second


def _real_worker(threads: list):
    def start(target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        threads.append(thread)
    return start


def test_the_post_answers_at_once_and_a_second_switch_is_409_while_one_runs(env):
    """The live failure shape: the POST answers 202 within a second while the
    worker waits on AgentCore; a second POST meanwhile is the 409, and the
    page reads the running job from the job route."""
    entered, release = threading.Event(), threading.Event()

    class Slow(Recorder):
        def get_memory(self, memoryId):  # noqa: N803
            if not entered.is_set():
                entered.set()
                assert release.wait(10)
            return super().get_memory(memoryId)

    threads: list = []
    env.setattr("agents.memory_model_job._start_worker", _real_worker(threads))
    ssm, control = FakeSSM(), Slow([])
    env.setattr("agents.memory_model.control_client", lambda region: control)
    env.setattr("agents.memory_model._ssm_client", lambda region: ssm)
    began = time.monotonic()
    first = _post({"memory_model": NEW}, ssm, control)
    assert _code(first) == 202 and time.monotonic() - began < 1.0
    job_id = _body(first)["job_id"]
    try:
        assert entered.wait(10)
        assert _body(_job(job_id))["status"] == "running"
        assert _body(_job()) == _body(_job(job_id))
        assert _code(_post({"memory_model": NEW}, ssm, control)) == 409
    finally:
        release.set()
        threads[0].join(10)
    assert _body(_job(job_id))["status"] == "done"
    assert _body(_job())["job_id"] == job_id
    assert len(control.updates) == 1
    assert ssm.store == {SSM_MEMORY: NEW}


def test_a_long_wait_heartbeats_the_slot(env):
    touched: list[str] = []
    store = mm._save_slots()
    real_touch = store.touch_slot

    def spy(job_id, owner, **kwargs):
        touched.append(owner)
        return real_touch(job_id, owner, **kwargs)
    env.setattr(store, "touch_slot", spy)
    control = Recorder([])
    control.busy_reads = 3
    job = _switch(NEW, FakeSSM(), control)
    assert job["status"] == "done", job
    assert len(touched) >= 3 and set(touched) == {job["job_id"]}


def test_a_slot_lost_during_the_wait_writes_no_ssm(env):
    """Another task took the slot over while this switch waited: the
    heartbeat of that poll stops the job, which writes no SSM and says
    UpdateMemory was sent."""
    class TakenOver(Recorder):
        def get_memory(self, memoryId):  # noqa: N803
            if self.updates:
                mm._save_slots()[mm.SAVE_SLOT_KEY] = {
                    "status": "running", "job_id": "another-task", "started_at": time.time()}
            return super().get_memory(memoryId)

    ssm, control = FakeSSM(), TakenOver([])
    job = _switch(NEW, ssm, control)
    assert job["status"] == "error", job
    assert job["memory_model_changed"] is False
    message = job["message"]
    assert NEW in message and "another" in message
    assert "UpdateMemory was sent" in message and "Nothing was saved for the next deploy" in message
    assert ssm.log == [] and ssm.store == {}
    assert len(control.updates) == 1 and control.gets == 2
    # The other task's claim is left alone.
    assert _slot()["job_id"] == "another-task"


def test_a_slot_lost_after_the_last_poll_writes_no_ssm(env):
    class TakenOverLate(Recorder):
        def get_memory(self, memoryId):  # noqa: N803
            if self.updates and self.gets == 2:   # the read that settles
                mm._save_slots()[mm.SAVE_SLOT_KEY] = {
                    "status": "running", "job_id": "another-task", "started_at": time.time()}
            return super().get_memory(memoryId)

    ssm, control = FakeSSM(), TakenOverLate([])
    job = _switch(NEW, ssm, control)
    assert job["status"] == "error", job
    assert job["memory_model_changed"] is True
    assert NEW in job["message"] and "another" in job["message"]
    assert ssm.log == [] and ssm.store == {}
    assert _slot()["job_id"] == "another-task"


# -- the save slot is never decided from one task's memory ----------------------------------

class _ThrottledTable:
    """A configured table whose every call raises a backend error (a
    throttle, an IAM or configuration error)."""

    def __init__(self):
        self.puts = 0

    def put_item(self, **_kwargs):
        self.puts += 1
        raise RuntimeError("ThrottlingException arn:aws:dynamodb:us-east-1:111122223333:table/x")

    def get_item(self, **_kwargs):
        raise RuntimeError("ThrottlingException arn:aws:dynamodb:us-east-1:111122223333:table/x")


def _shared_slot_table_class():
    """The slot primitive's table double (``test_job_store_slot.py``), also
    answering the consistent reads the switch makes."""
    from tests.unit.test_job_store_slot import _FakeSlotTable

    class Shared(_FakeSlotTable):
        def get_item(self, Key, **_kwargs):  # noqa: N803
            return super().get_item(Key)
    return Shared


_SharedSlotTable = _shared_slot_table_class()


def _assert_not_coordinated(res, ssm, control, clients) -> None:
    assert _code(res) == 503
    body = _body(res)
    assert body["status"] == "error"
    assert "could not be coordinated" in body["message"]
    assert "try again" in body["message"].lower()
    assert "111122223333" not in body["message"]
    assert control.gets == 0 and control.updates == [] and ssm.log == [] and clients == []


def test_a_slot_table_that_errors_refuses_the_switch_on_every_task_before_any_write(env):
    from agents.job_store import JobStore
    tables = []
    for _task in ("task-1", "task-2"):
        table = _ThrottledTable()
        tables.append(table)
        env.setattr("agents.memory_model._SAVE_SLOTS", JobStore("memory-model", table=table))
        clients: list = []
        ssm, control = FakeSSM(), Recorder([])
        res = _post({"memory_model": NEW}, ssm, control, clients)
        _assert_not_coordinated(res, ssm, control, clients)
    assert all(table.puts >= 1 for table in tables)


def test_a_disabled_slot_backend_refuses_the_switch_before_any_write(env):
    from agents.job_store import JobStore
    table = _SharedSlotTable()
    store = JobStore("memory-model", table=table)
    store._disable_backend(RuntimeError("an earlier fail-soft error"))
    env.setattr("agents.memory_model._SAVE_SLOTS", store)
    clients: list = []
    ssm, control = FakeSSM(), Recorder([])
    res = _post({"memory_model": NEW}, ssm, control, clients)
    _assert_not_coordinated(res, ssm, control, clients)
    assert table.puts == []


def test_a_job_record_that_cannot_be_written_stops_the_switch_before_any_change(env):
    """The job's record is the worker's first write, not the request's: when
    the shared table will not take it, the switch stops before it reads or
    changes the memory or SSM, frees the slot, and the slot's release
    carries why (every task reads the slot)."""
    from agents.job_store import JobStore
    env.setattr("agents.memory_model_job._JOBS", JobStore("memory-model-job", table=_ThrottledTable()))
    clients: list = []
    ssm, control = FakeSSM(), Recorder([])
    res = _post({"memory_model": NEW}, ssm, control, clients)
    assert _code(res) == 202
    assert control.gets == 0 and control.updates == [] and ssm.log == [] and clients == []
    assert _slot()["status"] == "idle"
    job = _body(_job(_body(res)["job_id"]))
    assert job["status"] == "error"
    assert "could not be coordinated" in job["message"] and "Nothing was changed" in job["message"]
    assert "111122223333" not in job["message"]


def test_a_switch_on_a_shared_slot_table_is_one_at_a_time_across_tasks(env):
    from agents.job_store import JobStore
    shared: dict = {}
    task1 = JobStore("memory-model", table=_SharedSlotTable(shared))
    task2 = JobStore("memory-model", table=_SharedSlotTable(shared))
    owner = "task-1-switch"
    assert task1.claim_slot(mm.SAVE_SLOT_KEY,
                            {"status": "running", "job_id": owner, "started_at": time.time()},
                            stale_seconds=mm.SAVE_SLOT_STALE_SECONDS, strict=True)
    env.setattr("agents.memory_model._SAVE_SLOTS", task2)
    ssm, control = FakeSSM(), Recorder([])
    res = _post({"memory_model": NEW}, ssm, control)
    assert _code(res) == 409
    assert control.gets == 0 and ssm.log == []
    assert task1.release_slot(mm.SAVE_SLOT_KEY, owner, {"status": "idle", "job_id": owner},
                              strict=True)
    job = _switch(NEW, ssm, control)
    assert job["status"] == "done", job
    assert ssm.store == {SSM_MEMORY: NEW}
    (item,) = shared.values()
    assert item["slot_status"] == "idle"


def test_a_heartbeat_that_fails_mid_wait_stops_the_switch_before_ssm(env):
    from agents.job_store import JobStore

    class Breaks(_SharedSlotTable):
        broken = False

        def put_item(self, Item, **kwargs):  # noqa: N803
            if self.broken:
                raise RuntimeError("ThrottlingException")
            return super().put_item(Item, **kwargs)

    table = Breaks()
    env.setattr("agents.memory_model._SAVE_SLOTS", JobStore("memory-model", table=table))

    class BreaksOnUpdate(Recorder):
        def update_memory(self, **kwargs):
            out = super().update_memory(**kwargs)
            table.broken = True
            return out

    ssm, control = FakeSSM(), BreaksOnUpdate([])
    control.busy_reads = 3
    job = _switch(NEW, ssm, control)
    assert job["status"] == "error", job
    assert "UpdateMemory was sent" in job["message"]
    assert "Nothing was saved for the next deploy" in job["message"]
    assert len(control.updates) == 1 and control.gets == 2
    assert ssm.log == [] and ssm.store == {}


# -- the read after the SSM write requires a settled memory ----------------------------------

@pytest.mark.parametrize("unsettle, word", [
    ("memory_failed", "FAILED"),
    ("strategy_failed", "FAILED"),
    ("memory_updating", "UPDATING"),
])
def test_a_memory_not_settled_after_the_ssm_write_is_an_error(env, unsettle, word):
    ssm, control = FakeSSM(), Recorder([])

    def after(name, _value):
        if name != SSM_MEMORY:
            return
        if unsettle == "memory_failed":
            control.final_status = "FAILED"
        elif unsettle == "memory_updating":
            control.final_status = "UPDATING"
        else:
            control.memory["strategies"][1]["status"] = "FAILED"
    ssm.after_put = after
    job = _switch(NEW, ssm, control)
    assert job["status"] == "error", job
    message = job["message"]
    assert word in message                      # state.describe()
    assert ("failure" if word == "FAILED" else "busy") in message
    assert f"{NEW} is now saved for the next deploy" in message
    assert ssm.store == {SSM_MEMORY: NEW}


def test_a_memory_moved_after_the_ssm_write_is_reported_and_ssm_not_repaired(env):
    other = _HAIKU_45
    ssm, control = FakeSSM(), Recorder([])

    def moved(name, value):
        if name == SSM_MEMORY:
            control.memory = _memory(other)
    ssm.after_put = moved
    job = _switch(NEW, ssm, control)
    assert job["status"] == "error", job
    assert NEW in job["message"] and other in job["message"]
    assert ssm.store == {SSM_MEMORY: NEW}


# -- the job status routes ---------------------------------------------------------------

def test_an_unknown_job_is_404_not_500(env):
    res = _job("0123456789abcdef")
    assert _code(res) == 404
    assert _body(res)["status"] == "error" and _body(res)["message"]


def test_no_switch_yet_reads_as_none(env):
    assert _body(_job()) == {"status": "none"}


def test_a_job_whose_worker_stopped_is_reported_unknown_not_running(env):
    """A switch whose slot has been quiet for ``SILENT_SECONDS`` stopped (a
    replaced task) or could not record how it ended: the read says its
    outcome is unknown, never "running" forever and never done."""
    old = time.time() - jobs.SILENT_SECONDS - 60
    _hold_slot("dead-worker", started_at=old)
    jobs._jobs().put_strict("dead-worker", {"status": "running", "job_id": "dead-worker",
                                            "memory_model": NEW, "started_at": old})
    record = _body(_job("dead-worker"))
    assert record["status"] == "unknown" and record["stale"] is True
    assert "has not reported how it ended" in record["message"]
    assert NEW in record["message"]
    assert mm._save_slots().release_slot(mm.SAVE_SLOT_KEY, "dead-worker",
                                         {"status": "idle", "job_id": "dead-worker"})
    _hold_slot("live-worker")
    jobs._jobs().put_strict("live-worker", {"status": "running", "job_id": "live-worker",
                                            "memory_model": NEW, "started_at": time.time()})
    assert _body(_job("live-worker"))["status"] == "running"


@pytest.mark.parametrize("which", ["by_id", "current"])
def test_a_status_read_that_fails_is_503_never_500(env, which):
    from agents.job_store import JobStore
    env.setattr("agents.memory_model_job._JOBS", JobStore("memory-model-job", table=_ThrottledTable()))
    env.setattr("agents.memory_model._SAVE_SLOTS", JobStore("memory-model", table=_ThrottledTable()))
    res = _job("0123456789abcdef" if which == "by_id" else None)
    assert _code(res) == 503
    assert "111122223333" not in _body(res)["message"]


def test_the_switch_routes_offload_their_sync_bodies():
    from agents import admin_routes
    call = _awaited_offload(admin_routes.start_memory_model_switch_route)
    assert call.func.id == "offload" and call.args[0].id == "start_memory_model_switch"
    call = _awaited_offload(admin_routes.memory_model_job_route)
    assert call.func.id == "offload" and call.args[0].id == "memory_model_job"
    call = _awaited_offload(admin_routes.memory_model_job_by_id_route)
    assert call.func.id == "offload" and call.args[0].id == "memory_model_job"


def test_the_switch_routes_are_registered():
    from agents.admin_routes import router
    routes = {(r.path, method) for r in router.routes for method in getattr(r, "methods", ())}
    assert (SWITCH_ROUTE, "POST") in routes
    assert ("/api/admin/llm-settings/memory-model/job", "GET") in routes
    assert ("/api/admin/llm-settings/memory-model/job/{job_id}", "GET") in routes


def test_get_reports_a_saved_model_the_memory_does_not_run(env):
    ssm = FakeSSM()
    ssm.store[SSM_MEMORY] = NEW
    res = _get(FakeControl(), ssm)
    assert res["memory_model"] == OLD
    assert res["memory_model_saved"] == NEW


def test_get_never_500s_on_a_failed_saved_model_read(env):
    res = _get(FakeControl(), FakeSSM(fail_reads_of=SSM_MEMORY))
    assert res["memory_model"] == OLD
    assert res["memory_model_error"] and "111122223333" not in res["memory_model_error"]
    assert "memory_model_saved" not in res
    assert res["orchestrator_model"]


def test_get_drift_compares_the_model_the_next_deploy_resolves(env):
    env.setenv("AWS_REGION", "eu-west-1")
    ssm = FakeSSM()
    ssm.store[SSM_MEMORY] = "us.amazon.nova-pro-v1:0"
    res = _get(FakeControl(_memory("eu.amazon.nova-pro-v1:0")), ssm)
    assert res["memory_model"] == "eu.amazon.nova-pro-v1:0"
    assert "memory_model_saved" not in res and "memory_model_saved_invalid" not in res
    assert "memory_model_unsaved" not in res


def test_get_reports_an_invalid_saved_model_apart_from_drift(env):
    ssm = FakeSSM()
    ssm.store[SSM_MEMORY] = "global.anthropic.claude-sonnet-4-6"
    res = _get(FakeControl(), ssm)
    assert res["memory_model_saved_invalid"] == "global.anthropic.claude-sonnet-4-6"
    assert "memory_model_saved" not in res


# -- ownership is shown again right before UpdateMemory ---------------------------------------

def test_a_worker_that_lost_its_slot_before_the_update_sends_neither_mutating_call(env):
    """The worker stalls before UpdateMemory past the 900 s stale bound (a
    slow GetMemory, here) and another task's switch takes the slot over: the
    worker's ownership check right before UpdateMemory fails on the shared
    table, so it sends neither UpdateMemory nor the SSM write, and leaves
    the other switch's claim alone."""
    from agents.job_store import JobStore
    shared: dict = {}
    env.setattr("agents.memory_model._SAVE_SLOTS",
                JobStore("memory-model", table=_SharedSlotTable(shared)))
    other_task = JobStore("memory-model", table=_SharedSlotTable(shared))
    took_over: list = []

    class StallsPastTheStaleBound(Recorder):
        def get_memory(self, memoryId):  # noqa: N803
            if not took_over:
                later = time.time() + mm.SAVE_SLOT_STALE_SECONDS + 1
                took_over.append(other_task.claim_slot(
                    mm.SAVE_SLOT_KEY,
                    {"status": "running", "job_id": "another-switch", "started_at": later},
                    stale_seconds=mm.SAVE_SLOT_STALE_SECONDS, now=later, strict=True))
            return super().get_memory(memoryId)

    ssm, control = FakeSSM(), StallsPastTheStaleBound([])
    job = _switch(NEW, ssm, control)
    assert took_over == [True]
    assert job["status"] == "error", job
    assert job["memory_model_changed"] is False
    message = job["message"]
    assert "did not send UpdateMemory" in message and "another save took over" in message
    assert "Nothing was saved" in message
    assert control.updates == [] and control.gets == 1
    assert ssm.log == [] and ssm.store == {}
    (item,) = shared.values()
    assert item["slot_owner"] == "another-switch" and item["slot_status"] == "running"
