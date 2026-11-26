# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Async ask submit/poll routes (the AWS Quick path — /api/ask/async + result).

Pins the additive async-query contract:
  * the two routes exist alongside an UNCHANGED /api/ask + /api/ask/sync;
  * the poll's server-side wait stays BELOW the API Gateway 29s REST
    integration timeout (the 504-avoidance constant);
  * submit requires cloud/runtime mode (no silent local degrade);
  * ONE concurrent async ask app-wide via the atomic JobStore slot —
    a second submit answers 429 and the slot is released on completion
    AND on error (a failed run never wedges at "running");
  * a finished result answers every poll the same way until its record
    expires (delivery is repeatable; tests/unit/test_ask_result_redelivery.py).

Offline: the chat client is stubbed; JobStore runs memory-only.
"""

import asyncio
import json
import time

import pytest

import agents.server as server
from agents.job_store import JobStore
from agents.models import AskRequest


class _StubEvent:
    pass


class _StubClient:
    """AgentCoreChatClient stand-in streaming two message chunks."""

    fail = False

    def __init__(self):
        pass

    def stream(self, prompt, session_id, user_id, write_memory=True, **_k):
        assert write_memory is False  # machine flow — never a Memory writer
        if type(self).fail:
            raise RuntimeError("runtime unavailable")
        from agents.events import MessageEvent
        yield MessageEvent(content="Hello ")
        yield MessageEvent(content="world")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Memory-only JobStore, runtime-mode config, stubbed chat client."""
    import types
    import agents.chat_client as chat_client_mod
    monkeypatch.setattr(server, "_ASK_JOBS", JobStore("ask", table=None))
    monkeypatch.setattr(server, "get_config",
                        lambda: types.SimpleNamespace(has_runtime=True))
    _StubClient.fail = False
    monkeypatch.setattr(chat_client_mod, "AgentCoreChatClient", _StubClient)
    yield


def _wait_terminal(job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = server._ask_jobs().get(job_id)
        if isinstance(job, dict) and job.get("status") != "running":
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never left 'running'")


def test_routes_are_registered_and_web_paths_unchanged():
    paths = {(r.path, m) for r in server.app.routes
             for m in (getattr(r, "methods", None) or [])}
    assert ("/api/ask/async", "POST") in paths
    assert ("/api/ask/result/{job_id}", "GET") in paths
    # ADDITIVE: the web UI SSE path and the sync path are still there.
    assert ("/api/ask", "POST") in paths
    assert ("/api/ask/sync", "POST") in paths


def test_result_wait_stays_under_the_api_gateway_integration_timeout():
    # The API Gateway REST integration timeout is 29s; the server-side wait
    # must stay below it or every long poll surfaces as a 504.
    assert server._ASK_RESULT_WAIT_SECONDS == 20.0
    assert server._ASK_RESULT_WAIT_SECONDS < 29


def test_submit_requires_a_question():
    assert server.ask_async(AskRequest(question="")) == {
        'error': 'No question provided'}


def test_submit_requires_the_runtime_no_silent_local_degrade(monkeypatch):
    import types
    monkeypatch.setattr(server, "get_config",
                        lambda: types.SimpleNamespace(has_runtime=False))
    result = server.ask_async(AskRequest(question="q"))
    assert "requires the AgentCore runtime" in result["error"]


def test_submit_then_poll_delivers_the_answer_on_every_poll():
    accepted = server.ask_async(AskRequest(question="what changed?"))
    assert accepted["status"] == "running"
    job_id = accepted["job_id"]
    assert _wait_terminal(job_id)["status"] == "done"

    result = asyncio.run(server.ask_result(job_id))
    assert result == {"status": "done", "answer": "Hello world"}
    # Delivery is repeatable: a re-poll (a caller whose first reply never
    # reached it) gets the same answer until the record expires.
    assert asyncio.run(server.ask_result(job_id)) == result


def _running_job(monkeypatch):
    """A job that stays running, and a recorder in place of the hold's sleep."""
    slept = []

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    jobs = server._ask_jobs()
    jobs["job-hold"] = {"status": "running"}
    return slept


@pytest.mark.parametrize("wait,most", [(None, 21.0), (20.0, 19.5), (19.0, 18.0), (3.0, 3.0),
                                       (1.0, 0.0), (0.0, 0.0), (-5.0, 0.0), (float("nan"), 0.0),
                                       (500.0, 19.5)])
def test_the_hold_never_outlasts_the_wait_the_caller_asked_for(monkeypatch, wait, most):
    """A hold that outlasts the caller's timeout sends its reply to a GET
    nobody reads, and the caller's call ends on a stale 'running'. ``wait``
    bounds the hold (capped at the server's own); no ``wait`` is unchanged."""
    slept = _running_job(monkeypatch)
    kwargs = {} if wait is None else {"wait": wait}
    assert asyncio.run(server.ask_result("job-hold", **kwargs))["status"] == "running"
    assert sum(slept) == pytest.approx(most)


def test_slow_record_reads_cannot_stretch_the_hold_past_wait(monkeypatch):
    """``wait`` bounds wall time, not just the sleeps.
    Each read here takes 1 s, so only five 1.5 s steps fit a 13 s wait."""
    clock = [0.0]

    async def _sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    monkeypatch.setattr(server, "_ask_result_clock", lambda: clock[0])
    jobs = server._ask_jobs()
    jobs["job-slow"] = {"status": "running"}
    real_get = jobs.get

    def _slow_get(key, default=None):
        clock[0] += 1.0
        return real_get(key, default)

    monkeypatch.setattr(jobs, "get", _slow_get)
    assert asyncio.run(server.ask_result("job-slow", wait=13.0))["status"] == "running"
    assert clock[0] <= 13.0


def test_second_submit_while_running_answers_429():
    jobs = server._ask_jobs()
    now = time.time()
    assert jobs.claim_slot(server._ASK_ACTIVE_KEY,
                           {"status": "running", "job_id": "other",
                            "started_at": now},
                           stale_seconds=server._ASK_STALE_SECONDS, now=now)
    response = server.ask_async(AskRequest(question="q"))
    assert response.status_code == 429
    assert json.loads(response.body)["status"] == "busy"


def test_error_is_stored_honestly_and_the_slot_is_released():
    _StubClient.fail = True
    accepted = server.ask_async(AskRequest(question="q"))
    job = _wait_terminal(accepted["job_id"])
    # JobStore sanitizes the "error" field to the "<kind>_failed" sentinel;
    # the detail carries the exception TYPE only (never message/stack).
    assert job["status"] == "error" and job["error"] == "ask_failed"
    assert job["error_detail"] == "query failed: RuntimeError"

    # The slot was released in the worker's finally — a new submit succeeds.
    _StubClient.fail = False
    again = server.ask_async(AskRequest(question="q"))
    assert again["status"] == "running"
    _wait_terminal(again["job_id"])


def test_unknown_and_reserved_job_ids_answer_unknown(monkeypatch):
    monkeypatch.setattr(server, "_ASK_RESULT_WAIT_SECONDS", 0.0)
    assert asyncio.run(server.ask_result("nope"))["status"] == "unknown"
    # The slot record itself is never a pollable job.
    assert asyncio.run(
        server.ask_result(server._ASK_ACTIVE_KEY))["status"] == "unknown"
