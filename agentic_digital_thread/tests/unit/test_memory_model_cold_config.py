# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""No config load on the memory model's request path.

Loading the app's config (``config.get_config`` -> ``AppConfig.from_env``)
can run discovery: the orchestrator runtime listing and up to 100
``list_memories`` pages, on clients with default timeouts. It runs when
nothing is loaded yet, and again every 15 s while the orchestrator ARN is
unresolved. Neither request of the memory model switch may wait on it:

* the settings GET reads the memory id only from a config already loaded
  (``config.peek_config``); before that it reads nothing, says the settings
  are still loading, and starts ONE background load;
* the switch POST checks only the model, the allowlist and the workshop
  before its 202; the worker finds the memory, and ends the job with the
  no-memory error when there is none.

``AppConfig.from_env`` is replaced by a recorder (a discovery stand-in), and
the real memory lookups are used. Offline.
"""
from __future__ import annotations

import threading
import time

import pytest

from agents import config
from agents import memory_model as memory_model_module
from tests.unit._memory_model_fakes import MEMORY_ID, NEW, OLD, FakeControl
from tests.unit.test_llm_settings_memory_model import (  # noqa: F401 - env is a fixture
    FakeSSM,
    Recorder,
    _body,
    _boto,
    _code,
    _get,
    _job,
    _post,
    env,
    mm,
)

#: The real lookups, taken before any fixture replaces them.
_REAL_LOOKUPS = {name: getattr(memory_model_module, name)
                 for name in ("configured_memory_id", "loaded_memory_id")
                 if hasattr(memory_model_module, name)}


def _app_config(memory_id: "str | None"):
    """A config as a load leaves it, the orchestrator runtime resolved."""
    from dataclasses import fields
    values = {f.name: None for f in fields(config.AppConfig)}
    values.update(memory_id=memory_id, aws_region="us-east-1", dev_mode=False,
                  dev_user_id="dev-user-001", guardrail_enabled=False,
                  agentcore_runtime_arn=("arn:aws:bedrock-agentcore:us-east-1:111122223333:"
                                         "runtime/adt_orchestrator-AbCd"))
    return config.AppConfig(**values)


class _Discovery:
    """``AppConfig.from_env`` replaced: records the thread each load ran on,
    and (``hold``) waits until released, as a slow discovery would."""

    def __init__(self, memory_id: "str | None" = MEMORY_ID, hold: bool = False):
        self.memory_id = memory_id
        self.threads: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        if not hold:
            self.release.set()

    def load(self):
        self.threads.append(threading.current_thread().name)
        self.entered.set()
        self.release.wait(5)
        return _app_config(self.memory_id)


@pytest.fixture
def cold(env):
    """Nothing loaded yet, the real lookups, and a recording discovery."""
    for name, fn in _REAL_LOOKUPS.items():
        env.setattr(memory_model_module, name, fn)
    env.setattr(config, "_cached_config", None)
    env.setattr(config, "_cached_config_loaded_at", 0.0)
    env.setattr(memory_model_module, "_WARMUP", None, raising=False)
    discovery = _Discovery()
    env.setattr(config.AppConfig, "from_env", classmethod(lambda cls: discovery.load()))
    env.discovery = discovery
    return env


def _memory_fields(view: dict) -> dict:
    return {k: v for k, v in view.items() if k.startswith("memory_model")}


def test_peek_config_returns_what_is_loaded_and_never_loads(cold):
    assert config.peek_config() is None
    assert cold.discovery.threads == []
    loaded = config.get_config()
    assert config.peek_config() is loaded
    assert len(cold.discovery.threads) == 1


def test_a_get_before_the_config_loads_makes_no_discovery_call_and_answers_at_once(cold):
    discovery = _Discovery(hold=True)
    cold.setattr(config.AppConfig, "from_env", classmethod(lambda cls: discovery.load()))
    clients: list = []
    control = FakeControl()
    began = time.monotonic()
    from agents.admin_routes import get_llm_settings_route
    import asyncio
    from types import SimpleNamespace
    with _boto(FakeSSM(), control, clients):
        view = asyncio.run(get_llm_settings_route(SimpleNamespace(state=SimpleNamespace(is_admin=True))))
    assert time.monotonic() - began < 1.0
    assert _memory_fields(view) == {"memory_model": "", "memory_model_selectable": False,
                                    "memory_model_locked_reason": mm.LOADING_REASON}
    assert control.gets == 0 and "bedrock-agentcore-control" not in clients
    # ONE background load, off the request's thread; a second view starts no other.
    assert discovery.entered.wait(5)
    assert _memory_fields(_get(control)) == _memory_fields(view)
    assert discovery.threads == ["memory-model-config-load"]
    discovery.release.set()
    mm._WARMUP.join(5)
    # Once loaded, the view reads the memory.
    loaded = _get(control)
    assert loaded["memory_model"] == OLD and loaded["memory_model_selectable"] is True
    assert discovery.threads == ["memory-model-config-load"]


def test_a_get_whose_load_cannot_start_still_answers(cold):
    def no_thread(_target):
        raise RuntimeError("can't start new thread")
    cold.setattr(memory_model_module, "_start_warmup", no_thread, raising=False)
    view = _get(FakeControl())
    assert view["memory_model_selectable"] is False
    assert view["memory_model_locked_reason"] == mm.LOADING_REASON
    assert cold.discovery.threads == []


def test_a_get_with_a_loaded_config_and_no_memory_reads_nothing(cold):
    cold.setattr(config, "_cached_config", _app_config(None))
    control = FakeControl()
    view = _get(control)
    assert control.gets == 0
    assert view["memory_model_selectable"] is False
    assert "not configured" in view["memory_model_locked_reason"]
    assert cold.discovery.threads == []


def test_a_post_before_the_config_loads_makes_no_discovery_call_and_answers_202(cold):
    pending: list = []
    cold.setattr("agents.memory_model_job._start_worker",
                 lambda target, *args: pending.append((target, args)))
    clients: list = []
    ssm, control = FakeSSM(), Recorder([])
    res = _post({"memory_model": NEW}, ssm, control, clients)
    assert _code(res) == 202, _body(res)
    assert cold.discovery.threads == [] and clients == [] and control.gets == 0
    # The worker finds the memory (it has no web deadline) and switches it.
    target, args = pending.pop()
    with _boto(ssm, control):
        target(*args)
    assert len(cold.discovery.threads) == 1
    job = _body(_job(_body(res)["job_id"]))
    assert job["status"] == "done", job
    assert ssm.store == {"/adt/llm-memory-model": NEW}


@pytest.mark.parametrize("case", ["not_a_memory_model", "workshop", "allowlist_without_one"])
def test_the_posts_refusals_make_no_discovery_call(cold, case):
    from agents.demo import workshop
    model = NEW
    if case == "not_a_memory_model":
        model = "global.anthropic.claude-sonnet-4-6"
    elif case == "workshop":
        cold.setattr(workshop, "is_enabled", lambda: True)
    else:
        cold.setenv("ALLOWED_MODELS", "anthropic.claude-sonnet-4-6")
    clients: list = []
    res = _post({"memory_model": model}, FakeSSM(), Recorder([]), clients)
    assert _code(res) == 400, _body(res)
    assert cold.discovery.threads == [] and clients == []


def test_a_worker_with_no_memory_ends_with_the_no_memory_error(cold):
    cold.discovery.memory_id = None
    clients: list = []
    ssm, control = FakeSSM(), Recorder([])
    res = _post({"memory_model": NEW}, ssm, control, clients)
    assert _code(res) == 202, _body(res)
    job = _body(_job(_body(res)["job_id"]))
    assert job["status"] == "error" and job["message"] == mm.NO_MEMORY
    assert job["memory_model_changed"] is False
    assert control.gets == 0 and control.updates == [] and ssm.log == []
    assert len(cold.discovery.threads) == 1     # the worker's load, inline here
