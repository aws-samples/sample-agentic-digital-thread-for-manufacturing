# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A finished async ask answers every poll the same way until its record expires.

On 2026-10-16 (on a rehearsal account) a Quick lot-total ask finished while
the bridge's second GET of a tool call was waiting. That GET returned to the
bridge after its timeout, so the bridge kept the stale ``running`` it already
had. The app had built the ``done`` reply for that GET and popped the record
as it did, so the next ``get_query_result`` got ``No such job`` and the answer
was gone. Delivery is now repeatable: the result route never frees a finished
record. It answers until the record expires, an hour after its last write: a
table item past its ``ttl`` reads as absent although DynamoDB deletes it only
later, and a memory copy of a finished record stops at the same horizon. A
memory-only store also keeps only the newest ``_ASK_MEMORY_FINISHED_MAX``
finished records (the copies it may drop are the ones delivery used to free).
Owner visibility (ADR-0079) is checked on every poll, so a re-delivery never
reaches anyone but the job's owner.

Offline: memory-only JobStores and a fake DynamoDB table; stubbed chat client.
"""

import asyncio
import threading
import time
import types

import pytest

import agents.server as server
from agents.auth.cognito import UserInfo
from agents.job_store import JobStore
from agents.models import AskRequest

OWNER = "delegated-user-42"


class _Table:
    """A fake DynamoDB table that records every delete."""

    def __init__(self):
        self.items: dict = {}
        self.deletes: list = []
        self.fail_reads = False
        self.lock = threading.Lock()

    def get_item(self, Key, **_kw):  # noqa: N803 - boto3 name
        if self.fail_reads:
            raise RuntimeError("ProvisionedThroughputExceededException")
        with self.lock:
            item = self.items.get(Key["system_id"])
            return {"Item": dict(item)} if item is not None else {}

    def put_item(self, Item, **_kw):  # noqa: N803 - boto3 name
        with self.lock:
            self.items[Item["system_id"]] = dict(Item)

    def delete_item(self, Key, **_kw):  # noqa: N803 - boto3 name
        with self.lock:
            self.deletes.append(Key["system_id"])
            self.items.pop(Key["system_id"], None)


class _StubClient:
    def stream(self, prompt, session_id, user_id, write_memory=True, **_k):
        from agents.events import MessageEvent
        yield MessageEvent(content="Lot 4 total: 13,233,115")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    import agents.chat_client as chat_client_mod
    monkeypatch.setattr(server, "_ASK_JOBS", JobStore("ask", table=None))
    monkeypatch.setattr(server, "get_config", lambda: types.SimpleNamespace(has_runtime=True))
    monkeypatch.setattr(chat_client_mod, "AgentCoreChatClient", _StubClient)


def _poll(job_id: str, request=None) -> dict:
    return asyncio.run(server.ask_result(job_id, request, wait=0.0))


def _request(sub=None, delegated=False):
    user = UserInfo(user_id=sub or "quick-gateway", username=sub or "quick-gateway")
    return types.SimpleNamespace(state=types.SimpleNamespace(user=user, delegated=delegated))


def _finished(jobs, job_id: str, record: dict) -> None:
    """Stage ``job_id`` as submitted, then finished by its worker."""
    jobs[job_id] = {"status": "running", "started_at": time.time(),
                    **{k: record[k] for k in ("actor", "actor_kind") if k in record}}
    assert jobs.finish_if_running(job_id, record)


def _wait_terminal(job_id: str) -> dict:
    deadline = time.time() + 5
    while time.time() < deadline:
        job = server._ask_jobs().get(job_id)
        if isinstance(job, dict) and job.get("status") != "running":
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never finished")


# -- 1. done ---------------------------------------------------------------- #

def test_a_done_job_polled_twice_answers_done_both_times():
    jobs = server._ask_jobs()
    _finished(jobs, "job-done", {"status": "done", "answer": "Lot 4 total: 13,233,115",
                                 "warning": "answer may be incomplete: query reported timeout"})
    expected = {"status": "done", "answer": "Lot 4 total: 13,233,115",
                "warning": "answer may be incomplete: query reported timeout"}
    assert _poll("job-done") == expected
    assert _poll("job-done") == expected
    assert _poll("job-done") == expected


def test_delivery_never_deletes_the_shared_record_and_any_replica_redelivers(monkeypatch):
    table = _Table()
    replica_a, replica_b = JobStore("ask", table=table), JobStore("ask", table=table)
    replica_a["job-shared"] = {"status": "done", "answer": "42"}
    monkeypatch.setattr(server, "_ASK_JOBS", replica_a)
    assert _poll("job-shared") == {"status": "done", "answer": "42"}
    monkeypatch.setattr(server, "_ASK_JOBS", replica_b)
    assert _poll("job-shared") == {"status": "done", "answer": "42"}
    assert table.deletes == []


def test_the_submitted_flow_delivers_the_answer_on_every_poll_and_frees_the_slot():
    job_id = server.ask_async(AskRequest(question="lot 4 total?"))["job_id"]
    _wait_terminal(job_id)
    first, second = _poll(job_id), _poll(job_id)
    assert first == second == {"status": "done", "answer": "Lot 4 total: 13,233,115"}
    # The one-ask slot was released by the worker; re-delivery does not hold it.
    again = server.ask_async(AskRequest(question="and lot 5?"))
    assert again["status"] == "running"
    _wait_terminal(again["job_id"])


# -- 2. error --------------------------------------------------------------- #

def test_a_non_abandoned_error_polled_twice_answers_the_same_error():
    jobs = server._ask_jobs()
    _finished(jobs, "job-err", {"status": "error", "error": "ask_failed",
                                "error_detail": "query failed: timeout"})
    expected = {"status": "error", "error": "ask_failed",
                "error_detail": "query failed: timeout"}
    assert _poll("job-err") == expected
    assert _poll("job-err") == expected


def test_guard_an_abandoned_tombstone_still_answers_the_same_error():
    jobs = server._ask_jobs()
    now = time.time()
    jobs["job-old"] = {"status": "running",
                       "started_at": now - server._ASK_RUN_CEILING_SECONDS - 5}
    first = _poll("job-old")
    assert first == {"status": "error", "error": "ask_failed",
                     "error_detail": server._ASK_RUN_CEILING_DETAIL}
    assert _poll("job-old") == first
    assert jobs.get("job-old")["abandoned"] is True


# -- 3. owner visibility on every re-delivery (ADR-0079) -------------------- #

def test_an_owned_job_is_redelivered_only_to_its_owner():
    jobs = server._ask_jobs()
    _finished(jobs, "job-owned", {"status": "done", "answer": "yours",
                                  "actor": OWNER, "actor_kind": "delegated"})
    owner = _request(OWNER, delegated=True)
    missing = {"status": "unknown", "error": "No such job: job-owned"}
    assert _poll("job-owned", owner) == {"status": "done", "answer": "yours"}
    for other in (_request(), _request("someone-else", delegated=True), None):
        assert _poll("job-owned", other) == missing
    assert _poll("job-owned", owner) == {"status": "done", "answer": "yours"}
    for other in (_request(), _request("someone-else", delegated=True), None):
        assert _poll("job-owned", other) == missing


# -- 5. the memory-only store stays bounded --------------------------------- #

def test_the_memory_only_ask_store_keeps_only_the_newest_finished_records(monkeypatch):
    monkeypatch.delenv("ONBOARDING_SESSIONS_TABLE", raising=False)
    monkeypatch.setattr(server, "_ASK_MEMORY_FINISHED_MAX", 4)
    monkeypatch.setattr(server, "_ASK_JOBS", None)
    jobs = server._ask_jobs()
    now = time.time()
    # Written first, so oldest: never dropped (not what delivery used to free).
    jobs["tomb"] = {"status": "error", "error": "ask_failed", "abandoned": True,
                    "error_detail": server._ASK_RUN_CEILING_DETAIL, "started_at": now}
    jobs["live"] = {"status": "running", "started_at": now}
    for n in range(6):
        jobs[f"done-{n}"] = {"status": "done", "answer": f"a{n}"}
    jobs["err-6"] = {"status": "error", "error": "ask_failed", "error_detail": "query failed: timeout"}
    for gone in ("done-0", "done-1", "done-2"):
        assert _poll(gone)["status"] == "unknown"
    for kept in ("done-3", "done-4", "done-5"):
        assert _poll(kept) == {"status": "done", "answer": f"a{kept[-1]}"}
    assert _poll("err-6")["status"] == "error"
    assert _poll("tomb")["error_detail"] == server._ASK_RUN_CEILING_DETAIL
    assert _poll("live") == {"status": "running", "job_id": "live"}


def test_the_ask_store_bound_drops_only_this_processes_copy_never_the_shared_record(monkeypatch):
    monkeypatch.setattr(server, "_ASK_MEMORY_FINISHED_MAX", 2)
    table = _Table()
    monkeypatch.setattr(server, "_ASK_JOBS", None)
    monkeypatch.setattr("agents.job_store.JobStore._get_table", lambda self: table)
    jobs = server._ask_jobs()
    for n in range(4):
        jobs[f"done-{n}"] = {"status": "done", "answer": f"a{n}"}
    assert sorted(jobs._mem) == ["done-2", "done-3"]
    assert table.deletes == []
    assert _poll("done-0") == {"status": "done", "answer": "a0"}   # from the table


# -- 6. a finished answer stops answering at its record's expiry ------------- #
#
# The shared table's TTL is an hour past the record's last write, but DynamoDB
# removes an expired item lazily, so an expired item still reads until then.
# The store treats it as gone, and a memory-only copy stops at the same horizon.

def _expired_row(table, store, job_id):
    table.items[store._key(job_id)]["ttl"] = int(time.time()) - 3600


def test_an_expired_but_undeleted_row_of_an_anonymous_job_answers_unknown(monkeypatch):
    table = _Table()
    jobs = JobStore("ask", table=table)
    monkeypatch.setattr(server, "_ASK_JOBS", jobs)
    _finished(jobs, "job-old", {"status": "done", "answer": "an answer from last week"})
    _expired_row(table, jobs, "job-old")
    assert _poll("job-old") == {"status": "unknown", "error": "No such job: job-old"}
    assert "job-old" not in jobs._mem
    assert table.items and table.deletes == []


def test_an_expired_but_undeleted_row_of_an_owned_job_answers_unknown_to_its_owner(monkeypatch):
    table = _Table()
    jobs = JobStore("ask", table=table)
    monkeypatch.setattr(server, "_ASK_JOBS", jobs)
    _finished(jobs, "job-mine", {"status": "done", "answer": "yours, last week",
                                 "actor": OWNER, "actor_kind": "delegated"})
    _expired_row(table, jobs, "job-mine")
    missing = {"status": "unknown", "error": "No such job: job-mine"}
    for poller in (_request(OWNER, delegated=True), _request(), None):
        assert _poll("job-mine", poller) == missing


def test_a_fresh_row_still_redelivers(monkeypatch):
    table = _Table()
    jobs = JobStore("ask", table=table)
    monkeypatch.setattr(server, "_ASK_JOBS", jobs)
    _finished(jobs, "job-new", {"status": "done", "answer": "fresh"})
    table.items[jobs._key("job-new")]["ttl"] = int(time.time()) + 60
    assert _poll("job-new") == _poll("job-new") == {"status": "done", "answer": "fresh"}


def _clocked_ask_store(monkeypatch):
    """The server's own ask store, on a clock the test moves (``_ask_wall_clock``)."""
    clock = {"now": time.time()}
    monkeypatch.setattr(server, "_ask_wall_clock", lambda: clock["now"])
    monkeypatch.setattr(server, "_ASK_JOBS", None)
    return clock


def test_a_memory_only_finished_answer_stops_at_the_same_horizon(monkeypatch):
    monkeypatch.delenv("ONBOARDING_SESSIONS_TABLE", raising=False)
    clock = _clocked_ask_store(monkeypatch)
    jobs = server._ask_jobs()
    t0 = clock["now"]
    _finished(jobs, "job-done", {"status": "done", "answer": "42"})
    _finished(jobs, "job-err", {"status": "error", "error": "ask_failed",
                                "error_detail": "query failed: timeout"})
    jobs["tomb"] = {"status": "error", "error": "ask_failed", "abandoned": True,
                    "error_detail": server._ASK_RUN_CEILING_DETAIL, "started_at": t0}
    clock["now"] = t0 + 3599
    assert _poll("job-done") == {"status": "done", "answer": "42"}
    assert _poll("job-err")["status"] == "error"
    clock["now"] = t0 + 3600
    assert _poll("job-done") == {"status": "unknown", "error": "No such job: job-done"}
    assert _poll("job-err") == {"status": "unknown", "error": "No such job: job-err"}
    clock["now"] = t0 + 7200                      # the memory tombstone is unchanged
    assert _poll("tomb")["error_detail"] == server._ASK_RUN_CEILING_DETAIL


def test_a_copy_kept_after_the_table_fails_stops_at_the_items_own_expiry(monkeypatch):
    clock = _clocked_ask_store(monkeypatch)
    table = _Table()
    monkeypatch.setattr("agents.job_store.JobStore._get_table", lambda self: table)
    jobs = server._ask_jobs()
    t0 = clock["now"]
    _finished(jobs, "job-done", {"status": "done", "answer": "42"})
    clock["now"] = t0 + 1800
    assert _poll("job-done") == {"status": "done", "answer": "42"}   # read from the table
    table.fail_reads = True                                          # memory from now on
    clock["now"] = t0 + 3599
    assert _poll("job-done") == {"status": "done", "answer": "42"}
    clock["now"] = t0 + 3600
    assert _poll("job-done") == {"status": "unknown", "error": "No such job: job-done"}
