# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""An async ask's result reaches only the caller that submitted it.

``/api/ask/result/{job_id}`` used to show an UNOWNED job (no delegated
``actor``) to any signed-in session holding its id, although the job records
the admitted ``submitter``. Visibility is now: a delegated job (``actor``
stamped, ADR-0079) needs that exact verified actor; any other job needs a
caller with no delegated actor whose admitted principal is exactly the
recorded submitter. The shared Quick service path keeps working: its
principal (``quick-gateway``) is recorded and matched like any other. A
mismatch answers the same ``unknown`` shape as a missing id. The worker's
terminal record keeps the submitter so a finished job stays scoped.
"""
from __future__ import annotations

import asyncio
import time
import types

import pytest

import agents.server as server
from agents.auth.cognito import UserInfo
from agents.job_store import JobStore
from agents.models import AskRequest

SUB = "delegated-user-sub"


class _StubClient:
    def __init__(self, *a, **k):
        pass

    def stream(self, prompt, session_id, user_id, write_memory=True,
               extra_payload=None, **_k):
        from agents.events import MessageEvent
        yield MessageEvent(content="answer")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    import agents.chat_client as chat_client_mod
    monkeypatch.setattr(server, "_ASK_JOBS", JobStore("ask", table=None))
    monkeypatch.setattr(server, "get_config",
                        lambda: types.SimpleNamespace(has_runtime=True))
    monkeypatch.setattr(chat_client_mod, "AgentCoreChatClient", _StubClient)


def _wait_terminal(job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = server._ask_jobs().get(job_id)
        if isinstance(job, dict) and job.get("status") != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("job never finished")


def _request(principal, delegated=False):
    user = UserInfo(user_id=principal, username=principal)
    return types.SimpleNamespace(state=types.SimpleNamespace(user=user, delegated=delegated))


def _poll(job_id, request):
    return asyncio.run(server.ask_result(job_id, request, wait=0))


def _unknown(job_id):
    return {"status": "unknown", "error": f"No such job: {job_id}"}


def _submit(principal, actor=None):
    job_id = server.ask_async(AskRequest(question="q"), actor, principal)["job_id"]
    _wait_terminal(job_id)
    return job_id


def test_another_signed_in_user_cannot_read_the_result():
    job_id = _submit("user-a")
    assert _poll(job_id, _request("user-b")) == _unknown(job_id)


def test_the_submitter_reads_the_result():
    job_id = _submit("user-a")
    assert _poll(job_id, _request("user-a")) == {"status": "done", "answer": "answer"}


def test_the_finished_record_keeps_the_submitter():
    job_id = _submit("user-a")
    assert server._ask_jobs().get(job_id)["submitter"] == "user-a"


def test_a_running_job_is_scoped_too():
    jobs = server._ask_jobs()
    jobs["abcdef012345"] = {"status": "running", "started_at": server._ask_wall_clock(),
                            "submitter": "user-a"}
    assert _poll("abcdef012345", _request("user-b")) == _unknown("abcdef012345")


def test_the_quick_service_principal_reads_its_own_job():
    job_id = _submit("quick-gateway")
    assert _poll(job_id, _request("quick-gateway"))["status"] == "done"


def test_a_signed_in_user_cannot_read_a_quick_service_job():
    job_id = _submit("quick-gateway")
    assert _poll(job_id, _request("user-b")) == _unknown(job_id)


def test_a_delegated_caller_cannot_read_an_unowned_job():
    job_id = _submit("quick-gateway")
    assert _poll(job_id, _request(SUB, delegated=True)) == _unknown(job_id)


def test_a_delegated_job_needs_its_exact_actor():
    job_id = _submit("quick-gateway", actor=SUB)
    assert _poll(job_id, _request(SUB, delegated=True))["status"] == "done"
    assert _poll(job_id, _request("someone-else", delegated=True)) == _unknown(job_id)
    assert _poll(job_id, _request("quick-gateway")) == _unknown(job_id)


def test_no_request_fails_closed_for_a_submitted_job():
    job_id = _submit("user-a")
    assert _poll(job_id, None) == _unknown(job_id)


def test_visibility_rule():
    visible = server._job_visible_to
    assert visible({"submitter": "u"}, None, "u") is True
    assert visible({"submitter": "u"}, None, "v") is False
    assert visible({"submitter": "u"}, "u", "u") is False   # delegated caller, unowned job
    assert visible({"actor": "s", "submitter": "qg"}, "s", "qg") is True
    assert visible({"actor": "s", "submitter": "qg"}, None, "qg") is False
    assert visible({}, None, "u") is False                   # no submitter recorded
    assert visible({}, None, None) is True                   # local mode: no principal at all
