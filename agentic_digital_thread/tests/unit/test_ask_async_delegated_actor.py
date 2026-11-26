# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Async ask with a VERIFIED delegated actor (ADR-0079 D7).

Pins:
  * the delegated actor OVERRIDES the untrusted body ``user_id``;
  * the job record carries ``actor`` / ``actor_kind: delegated`` (running AND
    terminal records);
  * the runtime payload carries the RESERVED ``delegatedSubject`` key via
    ``extra_payload``; the anonymous path passes ``extra_payload=None`` and
    stores no actor fields (byte-identical to today);
  * an owned job is returned ONLY to its verified actor — anyone else sees
    the not-found ``unknown`` shape; unowned jobs are unchanged;
  * the route reads the actor ONLY from ``request.state.delegated`` (set by
    the auth middleware after verification).

Offline: memory-only JobStore, stubbed chat client.
"""

import asyncio
import time
import types

import pytest

import agents.server as server
from agents.auth.forwarding import DELEGATED_SUBJECT_FIELD
from agents.auth.cognito import UserInfo
from agents.job_store import JobStore
from agents.models import AskRequest

SUB = "delegated-user-42"


class _StubClient:
    calls: list = []

    def __init__(self):
        pass

    def stream(self, prompt, session_id, user_id, write_memory=True,
               extra_payload=None, **_k):
        type(self).calls.append({"user_id": user_id, "extra_payload": extra_payload,
                                 "write_memory": write_memory})
        from agents.events import MessageEvent
        yield MessageEvent(content="answer")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    import agents.chat_client as chat_client_mod
    monkeypatch.setattr(server, "_ASK_JOBS", JobStore("ask", table=None))
    monkeypatch.setattr(server, "get_config",
                        lambda: types.SimpleNamespace(has_runtime=True))
    _StubClient.calls = []
    monkeypatch.setattr(chat_client_mod, "AgentCoreChatClient", _StubClient)


def _wait_terminal(job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = server._ask_jobs().get(job_id)
        if isinstance(job, dict) and job.get("status") != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("job never finished")


def _request(sub=None, delegated=False):
    user = UserInfo(user_id=sub or "quick-gateway", username=sub or "quick-gateway")
    return types.SimpleNamespace(state=types.SimpleNamespace(user=user, delegated=delegated))


def test_delegated_actor_overrides_body_user_and_owns_the_job():
    accepted = server.ask_async(AskRequest(question="q", user_id="spoofed"), SUB)
    first = server._ask_jobs().get(accepted["job_id"])  # running OR done — both stamped
    assert first["actor"] == SUB and first["actor_kind"] == "delegated"
    done = _wait_terminal(accepted["job_id"])
    assert done["actor"] == SUB and done["actor_kind"] == "delegated"
    (call,) = _StubClient.calls
    assert call["user_id"] == SUB
    assert call["extra_payload"] == {DELEGATED_SUBJECT_FIELD: SUB}
    assert call["write_memory"] is False


def test_anonymous_ask_is_unchanged():
    accepted = server.ask_async(AskRequest(question="q"))
    job_id = accepted["job_id"]
    done = _wait_terminal(job_id)
    # The admitted submitter rides into the terminal record (None here: no
    # principal), so a finished job stays visible only to its submitter.
    assert set(done) == {"status", "answer", "submitter"} and done["submitter"] is None
    (call,) = _StubClient.calls
    assert call["user_id"] == "async-user"
    assert call["extra_payload"] is None


def test_owned_job_is_visible_only_to_its_actor():
    job_id = server.ask_async(AskRequest(question="q"), SUB)["job_id"]
    _wait_terminal(job_id)
    for other in (_request(), _request("someone-else", delegated=True), None):
        result = asyncio.run(server.ask_result(job_id, other))
        assert result == {"status": "unknown", "error": f"No such job: {job_id}"}
    owner = asyncio.run(server.ask_result(job_id, _request(SUB, delegated=True)))
    assert owner == {"status": "done", "answer": "answer"}


def test_unowned_job_is_visible_only_to_its_submitter():
    """An unowned job answers only a caller with no delegated actor whose
    admitted principal submitted it (here the shared quick-gateway service
    principal); a delegated caller or another principal gets ``unknown``."""
    job_id = server.ask_async(AskRequest(question="q"), None, "quick-gateway")["job_id"]
    _wait_terminal(job_id)
    for other in (_request("x", delegated=True), _request("someone-else")):
        result = asyncio.run(server.ask_result(job_id, other))
        assert result == {"status": "unknown", "error": f"No such job: {job_id}"}
    result = asyncio.run(server.ask_result(job_id, _request()))
    assert result["status"] == "done"


def test_delegated_actor_comes_only_from_the_verified_state_flag():
    # a user object alone (e.g. the anonymous quick-gateway identity) is NOT a
    # delegated actor — only the middleware's verified flag makes one
    assert server._delegated_actor(_request("quick-gateway", delegated=False)) is None
    assert server._delegated_actor(_request(SUB, delegated=True)) == SUB
    assert server._delegated_actor(types.SimpleNamespace(state=types.SimpleNamespace())) is None
    assert server._delegated_actor(None) is None


def test_route_passes_the_verified_actor_to_the_offloaded_body(monkeypatch):
    seen = []

    async def _offload(fn, *args):
        seen.append((fn, args))
        return {"ok": True}

    monkeypatch.setattr(server, "offload", _offload)
    body = AskRequest(question="q")
    asyncio.run(server.ask_async_route(body, _request(SUB, delegated=True)))
    # The verified actor, then the admitted principal (the middleware sets
    # request.state.user to the delegated subject).
    assert seen == [(server.ask_async, (body, SUB, SUB))]
