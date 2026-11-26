# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The async ask 429 names the running job only to the caller it belongs to.

The app admits ONE async ask at a time. Its 429 ``busy`` body carries
``running_job_id`` only when the job in the active slot is the caller's:
visible per ``_job_visible_to`` and, for a verified delegated actor (ADR-0079),
owned by that actor. Pins:
  * owned job: named to its actor, never to another actor or an anonymous caller;
  * unowned job: named only to the principal that submitted it with no
    delegated actor (the service-auth Quick path polls it, workshop module 1b),
    never to another principal (a web user, the kiosk) or a delegated actor;
  * ``running_job_id`` is always in the body (null when nothing is named);
  * a missing, finished or stale record names nothing;
  * ``status`` and ``error`` are unchanged in every case;
  * reading the slot after a failed claim never changes it.

Offline: memory-only JobStore, stubbed chat client.
"""

import json
import threading
import time
import types

import pytest

import agents.server as server
from agents.job_store import JobStore
from agents.models import AskRequest

A = "delegated-user-a"
B = "delegated-user-b"
BUSY_ERROR = "an async query is already running — retry shortly"


class _BlockingClient:
    """Chat client stand-in that holds the job at 'running' until released."""

    release = threading.Event()

    def __init__(self):
        pass

    def stream(self, prompt, session_id, user_id, write_memory=True, **_k):
        type(self).release.wait(5.0)
        from agents.events import MessageEvent
        yield MessageEvent(content="answer")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    import agents.chat_client as chat_client_mod
    monkeypatch.setattr(server, "_ASK_JOBS", JobStore("ask", table=None))
    monkeypatch.setattr(server, "get_config",
                        lambda: types.SimpleNamespace(has_runtime=True))
    _BlockingClient.release = threading.Event()
    monkeypatch.setattr(chat_client_mod, "AgentCoreChatClient", _BlockingClient)
    yield
    _BlockingClient.release.set()


def _hold_slot(job_id="job-running", record=None, started_at=None):
    """Claim the slot for *job_id* and write its job record (None = no record)."""
    jobs = server._ask_jobs()
    now = time.time() if started_at is None else started_at
    assert jobs.claim_slot(server._ASK_ACTIVE_KEY,
                           {"status": "running", "job_id": job_id,
                            "started_at": now},
                           stale_seconds=server._ASK_STALE_SECONDS, now=now)
    if record is not None:
        jobs[job_id] = record
    return job_id


def _busy_body(actor=None, principal=None):
    response = server.ask_async(AskRequest(question="q"), actor, principal)
    assert response.status_code == 429
    body = json.loads(response.body)
    assert body["status"] == "busy" and body["error"] == BUSY_ERROR
    return body


def _owned(actor):
    return {"status": "running", "started_at": time.time(),
            "actor": actor, "actor_kind": "delegated"}


UNOWNED = {"status": "running", "started_at": 0.0}


@pytest.mark.parametrize("record, caller, named", [
    (_owned(A), A, True),
    (_owned(A), B, False),
    (_owned(A), None, False),
    (UNOWNED, None, True),
    (UNOWNED, B, False),
], ids=["owned-same", "owned-other", "owned-anonymous",
        "unowned-anonymous", "unowned-delegated"])
def test_running_job_id_is_named_only_to_its_caller(record, caller, named):
    job_id = _hold_slot(record=dict(record))
    body = _busy_body(caller)
    if named:
        assert body["running_job_id"] == job_id
    else:
        assert body["running_job_id"] is None
        assert set(body) == {"status", "error", "running_job_id"}


def test_missing_job_record_names_nothing():
    _hold_slot(record=None)  # slot claimed, record not yet written
    assert _busy_body()["running_job_id"] is None


@pytest.mark.parametrize("status", ["done", "error"])
def test_a_finished_record_still_holding_the_slot_names_nothing(status):
    _hold_slot(record={"status": status, "answer": "x"})
    assert _busy_body()["running_job_id"] is None


def test_a_stale_slot_names_nothing():
    jobs = server._ask_jobs()
    old = time.time() - server._ASK_STALE_SECONDS - 5
    _hold_slot(record=dict(UNOWNED), started_at=old)
    # A stale slot is taken over by the claim itself, so ask the helper directly.
    assert server._busy_running_job_id(jobs, None, time.time()) is None


def test_the_reserved_slot_key_is_never_named():
    jobs = server._ask_jobs()
    _hold_slot(job_id=server._ASK_ACTIVE_KEY, record=None)
    assert server._busy_running_job_id(jobs, None, time.time()) is None


def test_reading_the_slot_after_a_failed_claim_leaves_it_unchanged():
    _hold_slot(record=_owned(A))
    before = dict(server._ask_jobs().get(server._ASK_ACTIVE_KEY))
    _busy_body(B)
    _busy_body(A)
    assert server._ask_jobs().get(server._ASK_ACTIVE_KEY) == before


def _wait_done(job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = server._ask_jobs().get(job_id)
        if isinstance(job, dict) and job.get("status") == "done":
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never finished")


def test_end_to_end_a_running_job_is_named_to_its_owner_only():
    first = server.ask_async(AskRequest(question="first"), A)
    assert first["status"] == "running"
    try:
        assert _busy_body(A)["running_job_id"] == first["job_id"]
        assert _busy_body(B)["running_job_id"] is None
        assert _busy_body(None)["running_job_id"] is None
    finally:
        _BlockingClient.release.set()
    _wait_done(first["job_id"])


def test_end_to_end_anonymous_path_names_the_running_job():
    first = server.ask_async(AskRequest(question="first"))
    try:
        assert _busy_body(None)["running_job_id"] == first["job_id"]
    finally:
        _BlockingClient.release.set()
    _wait_done(first["job_id"])


QUICK = "quick-gateway"
WEB = "web-user-1"


@pytest.mark.parametrize("submitter, caller, named", [
    (QUICK, QUICK, True),
    (QUICK, WEB, False),
    (WEB, QUICK, False),
    (WEB, WEB, True),
    (None, QUICK, False),
], ids=["quick-to-quick", "quick-to-web", "web-to-quick", "web-to-web",
        "legacy-record-to-quick"])
def test_an_unowned_job_is_named_only_to_the_principal_that_submitted_it(
        submitter, caller, named):
    record = dict(UNOWNED, started_at=time.time())
    if submitter is not None:
        record["submitter"] = submitter
    job_id = _hold_slot(record=record)
    body = _busy_body(principal=caller)
    assert body["running_job_id"] == (job_id if named else None)


def test_a_submitted_job_records_its_principal():
    first = server.ask_async(AskRequest(question="first"), None, QUICK)
    assert server._ask_jobs().get(first["job_id"])["submitter"] == QUICK
    assert _busy_body(principal=QUICK)["running_job_id"] == first["job_id"]
    assert _busy_body(principal=WEB)["running_job_id"] is None


@pytest.mark.parametrize("started_at", [10 ** 4000, "nan", "inf", "-inf",
                                        float("nan"), None, "not-a-number"])
def test_a_garbled_slot_timestamp_names_nothing_and_never_raises(started_at):
    jobs = server._ask_jobs()
    jobs[server._ASK_ACTIVE_KEY] = {"status": "running", "job_id": "job-x",
                                    "started_at": started_at}
    jobs["job-x"] = dict(UNOWNED)
    assert server._busy_running_job_id(jobs, None, time.time()) is None
