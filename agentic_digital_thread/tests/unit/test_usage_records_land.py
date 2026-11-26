# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Usage records that land: the ``adt-usage`` table contract, the Quick async
path recording usage at job completion, and the admin dashboard reading it.

Seen on a deployed account (2026-10-21): no usage table existed and the ECS task
had no ``USAGE_TABLE_NAME``, so ``server._store_ask_usage`` wrote into
``UsageStorage(None)`` and the admin usage stats read nothing; the Quick async
path (``/api/ask/async``) never recorded usage at all.

Pins:
  * a configured ``UsageStorage`` writes the row with the table's keys
    (``user_id`` + ``timestamp``) and a bounded-retention ``ttl``;
  * an unset table is ONE visible warning per process, never an exception on
    the request path;
  * the stack literals and the app-side contract stay in lockstep (the synth
    tests in ``tests/test_usage_table_wiring.py`` read the templates);
  * one Quick async job stores exactly ONE usage record, the same
    ``UsageRecord`` shape the web chat stores, through ``_store_ask_usage``,
    and only when its terminal record is ``done`` AND the worker's write of it
    landed (the job was still its own): a stream error, a raised error or an
    abandoned job records nothing;
  * ``GET /api/admin/dashboard`` (admin only, offloaded) sums those records.

Offline: the chat client, the usage service and the table are faked.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import agents.admin_routes as admin_routes
import agents.server as server
import agents.usage as usage
from agents import storage as st
from agents.events import DoneEvent, ErrorEvent, MessageEvent, ToolUseEvent
from agents.job_store import JobStore
from agents.models import AskRequest

REPO_ROOT = Path(__file__).resolve().parents[3]


class _FakeTable:
    def __init__(self):
        self.items: list[dict] = []

    def put_item(self, Item):  # noqa: N803 - boto3 kwarg
        self.items.append(Item)


def _record(**overrides) -> dict:
    base = usage.UsageRecord(
        user_id="sub-a", timestamp="2026-10-21T12:00:00+00:00", session_id="session-1",
        model_id="m", input_tokens=0, output_tokens=4, total_tokens=4, latency_ms=10,
        tool_usage={"tools": []}).to_dict()
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _fresh_warning_dedupe():
    st.reset_unconfigured_warnings()
    yield
    st.reset_unconfigured_warnings()


# ── the table contract ─────────────────────────────────────────────────────

def test_a_configured_usage_store_writes_the_keys_and_a_ttl():
    store = st.UsageStorage(st.USAGE_TABLE_DEFAULT, "us-east-1")
    table = _FakeTable()
    store._db._table = table
    assert store.store(_record()) is True
    item = table.items[0]
    assert item["user_id"] == "sub-a" and item["timestamp"] == "2026-10-21T12:00:00+00:00"
    ttl = int(item[st.USAGE_TTL_ATTRIBUTE])
    assert abs(ttl - (time.time() + st.USAGE_TTL_DAYS * 86400)) < 120


def test_a_callers_ttl_wins():
    store = st.UsageStorage(st.USAGE_TABLE_DEFAULT, "us-east-1")
    store._db._table = table = _FakeTable()
    store.store(_record(ttl=42))
    assert table.items[0]["ttl"] == 42


def test_an_unset_usage_table_warns_once_and_never_raises(caplog):
    store = st.UsageStorage(None, "us-east-1")
    with caplog.at_level(logging.DEBUG, logger="agents.storage"):
        assert store.store(_record()) is True
        assert store.store(_record()) is True
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "storage_unconfigured" in warnings[0] and "UsageStorage.put_item" in warnings[0]


def test_stack_literals_match_the_app_contract():
    db = (REPO_ROOT / "stacks" / "adt_database_stack.py").read_text(encoding="utf-8")
    compute = (REPO_ROOT / "stacks" / "adt_compute_stack.py").read_text(encoding="utf-8")
    assert f'USAGE_TABLE_NAME = "{st.USAGE_TABLE_DEFAULT}"' in db
    assert f'USAGE_TTL_ATTRIBUTE = "{st.USAGE_TTL_ATTRIBUTE}"' in db
    assert f'"USAGE_TABLE_NAME": "{st.USAGE_TABLE_DEFAULT}"' in compute
    assert f"table/{st.USAGE_TABLE_DEFAULT}\"" in compute


# ── the Quick async path records usage at completion ──────────────────────

class _ScriptedClient:
    script: list = []

    def __init__(self):
        pass

    def stream(self, prompt, session_id, user_id, write_memory=True, **_k):
        assert write_memory is False
        yield from type(self).script


class _RaisingClient(_ScriptedClient):
    def stream(self, *a, **k):
        raise RuntimeError("runtime unavailable")
        yield  # pragma: no cover


class _GatedClient(_ScriptedClient):
    """A stream that blocks until its gate opens (a run past the ceiling)."""

    gate: threading.Event = threading.Event()

    def stream(self, *a, **k):
        type(self).gate.wait(5)
        yield MessageEvent(content="Late answer.")
        yield DoneEvent()


class _UsageSpy:
    """Records each usage write with the job's status at the time of the write."""

    def __init__(self):
        self.records: list = []
        self.status_at_write: list = []

    def store(self, record):
        self.records.append(record)
        job = server._ask_jobs().get(record.session_id.removeprefix("async-")) or {}
        self.status_at_write.append(job.get("status"))
        return True


@pytest.fixture
def async_world(monkeypatch):
    import agents.chat_client as chat_client_mod
    spy = _UsageSpy()
    monkeypatch.setattr(server, "_ASK_JOBS", JobStore("ask", table=None))
    monkeypatch.setattr(server, "get_config", lambda: types.SimpleNamespace(has_runtime=True))
    monkeypatch.setattr(usage, "_service", spy)
    monkeypatch.setattr(chat_client_mod, "AgentCoreChatClient", _ScriptedClient)
    monkeypatch.setenv("BEDROCK_MODEL", "global.anthropic.claude-sonnet-5-5")
    _ScriptedClient.script = []
    return types.SimpleNamespace(spy=spy, chat=chat_client_mod)


def _wait_worker(job_id: str) -> None:
    """Wait until the job's worker thread has ended (usage is written last)."""
    deadline = time.time() + 5
    while any(t.is_alive() and getattr(t, "_target", None) is server._run_ask_job
              and job_id in (getattr(t, "_args", ()) or ()) for t in threading.enumerate()):
        assert time.time() < deadline, "worker never ended"
        time.sleep(0.02)


def _run_job(script, actor=None) -> str:
    _ScriptedClient.script = list(script)
    job_id = server.ask_async(AskRequest(question="Which lots shipped late?"),
                              actor=actor)["job_id"]
    _wait_worker(job_id)
    assert server._ask_jobs().get(job_id, {}).get("status") != "running"
    return job_id


def test_one_quick_async_job_stores_exactly_one_usage_record(async_world):
    job_id = _run_job([MessageEvent(content="Checking two systems."),
                       ToolUseEvent(tool_name="erp___query_erp"),
                       MessageEvent(content="Two lots shipped late."), DoneEvent()])
    assert server._ask_jobs().get(job_id)["status"] == "done"
    assert len(async_world.spy.records) == 1
    record = async_world.spy.records[0]
    assert isinstance(record, usage.UsageRecord)
    assert record.session_id == f"async-{job_id}"
    assert record.user_id == "async-user"
    assert record.model_id == "global.anthropic.claude-sonnet-5-5"
    # Same counting as the web chat's _store_ask_usage: streamed words.
    assert record.output_tokens == record.total_tokens == 7
    assert record.tool_usage == {"tools": ["erp___query_erp"]}
    assert record.latency_ms >= 0 and record.user_email is None


def test_a_delegated_quick_job_records_the_verified_actor(async_world):
    _run_job([MessageEvent(content="Answer."), DoneEvent()], actor="idp-subject-1")
    assert [r.user_id for r in async_world.spy.records] == ["idp-subject-1"]


def test_usage_is_written_only_after_the_done_record_landed(async_world):
    _run_job([MessageEvent(content="Answer."), DoneEvent()])
    assert async_world.spy.status_at_write == ["done"]


def test_a_job_whose_stream_reported_an_error_records_no_usage(async_world):
    job_id = _run_job([ErrorEvent(message="x", details="x", category="timeout",
                                  correlation_id="c"), DoneEvent()])
    assert server._ask_jobs().get(job_id)["status"] == "error"
    assert async_world.spy.records == []


def test_an_abandoned_job_records_no_usage_when_its_worker_finishes(async_world, monkeypatch):
    clock = {"now": time.time()}
    monkeypatch.setattr(server, "_ask_wall_clock", lambda: clock["now"])
    monkeypatch.setattr(async_world.chat, "AgentCoreChatClient", _GatedClient)
    _GatedClient.gate = threading.Event()
    job_id = server.ask_async(AskRequest(question="A slow question?"))["job_id"]
    clock["now"] = (server._ask_jobs().get(job_id)["started_at"]
                    + server._ASK_RUN_CEILING_SECONDS + 1)
    assert asyncio.run(server.ask_result(job_id, wait=0.0))["status"] == "error"
    _GatedClient.gate.set()
    _wait_worker(job_id)
    job = server._ask_jobs().get(job_id)
    assert job["status"] == "error" and job.get("abandoned") is True
    assert async_world.spy.records == []


def test_a_job_whose_stream_raised_records_nothing(async_world, monkeypatch):
    monkeypatch.setattr(async_world.chat, "AgentCoreChatClient", _RaisingClient)
    job_id = _run_job([])
    assert server._ask_jobs().get(job_id)["status"] == "error"
    assert async_world.spy.records == []


def test_the_async_path_reuses_the_web_paths_usage_writer():
    import inspect
    source = inspect.getsource(server._async_ask_answer)
    assert "_store_ask_usage(" in source and "UsageRecord(" not in source


# ── the admin dashboard reads the records ─────────────────────────────────

class _AdminInject(BaseHTTPMiddleware):
    def __init__(self, app, is_admin: bool):
        super().__init__(app)
        self._is_admin = is_admin

    async def dispatch(self, request, call_next):
        request.state.is_admin = self._is_admin
        return await call_next(request)


class _FakeUsageService:
    records: list = []

    def get_by_time_range(self, start, end):
        return list(type(self).records)


def _dashboard(is_admin=True):
    app = FastAPI()
    app.include_router(admin_routes.router)
    app.add_middleware(_AdminInject, is_admin=is_admin)
    return TestClient(app).get("/api/admin/dashboard")


def test_the_dashboard_sums_web_and_quick_records(monkeypatch):
    monkeypatch.setattr(admin_routes, "UsageService", _FakeUsageService)
    _FakeUsageService.records = [
        _record(user_id="sub-a", total_tokens=10),
        _record(user_id="sub-a", total_tokens=20, session_id="session-2"),
        _record(user_id="async-user", total_tokens=6, session_id="async-abc"),
    ]
    body = _dashboard().json()
    assert body == {"total_requests": 3, "total_tokens": 36, "unique_users": 2,
                    "avg_tokens_per_request": 12, "period_days": 7}


def test_the_dashboard_is_admin_only(monkeypatch):
    monkeypatch.setattr(admin_routes, "UsageService", _FakeUsageService)
    assert _dashboard(is_admin=False).status_code == 403


def test_the_dashboard_route_only_offloads():
    import ast
    import inspect
    import textwrap
    node = ast.parse(textwrap.dedent(inspect.getsource(admin_routes.dashboard_route))).body[0]
    returns = [s for s in node.body if isinstance(s, ast.Return)]
    assert len(returns) == 1 and returns[0].value.value.func.id == "offload"
