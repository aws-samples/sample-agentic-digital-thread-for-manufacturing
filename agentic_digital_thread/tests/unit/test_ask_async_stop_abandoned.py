# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An async ask abandoned at the 20-minute ceiling is stopped, not left running.

The result route answered ``error`` at the ceiling and wrote a tombstone, but
the run itself kept going: the app never kept the AgentCore runtime session it
invoked, so nothing could stop it, and the worker thread stayed blocked on the
runtime's stream. It kept spending model tokens and data-lake scans, and while
two such runs were alive every new Quick question was refused.

Now the submit mints the runtime session id, keeps it on the job record and
invokes the runtime with it. Abandoning the job (by a poll, or by the worker's
own process when nobody polls) stops that runtime session and closes the
worker's stream, so the worker ends and its lease is released. The 10-minute
takeover still lets a long run finish: only the ceiling stops a run.
Offline: JobStore runs memory-only (or over a fake shared table); the chat
client is a scripted stand-in and the stop call is recorded, never sent.
"""

import asyncio
import re
import threading
import time
import types

import pytest

import agents.server as server
from agents.events import DoneEvent, ErrorEvent, MessageEvent
from agents.job_store import JobStore
from agents.models import AskRequest
from tests.unit.test_ask_async_abandoned_bound_shared import _SharedTable

T0 = 1_800_000_000.0
ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/adt_orchestrator-abc"
RSID_RE = re.compile(r"session-[0-9a-f]{28}")
ABANDONED = "the query ran longer than 20 minutes and was abandoned; ask again"


class _FakeBody:
    """The runtime's response body; closing it is what the cancel does."""

    def close(self):
        pass


class _CancellableClient:
    """A wedged run: invoked and answered (the handle is armed like the real
    client's), then the stream blocks until its cancel handle closes it."""

    calls: list = []

    def __init__(self, *a, **k):
        pass

    def stream(self, prompt, session_id, user_id, write_memory=True, *,
               runtime_session_id=None, cancel=None, **_k):
        type(self).calls.append({"runtime_session_id": runtime_session_id,
                                 "cancel": cancel})
        if cancel is not None:
            if not cancel.begin_invoke():
                return
            cancel.arm(_FakeBody())
        deadline = time.time() + 10
        while time.time() < deadline and not (cancel is not None and cancel.cancelled):
            time.sleep(0.01)
        if cancel is not None and cancel.cancelled:
            yield ErrorEvent(message="stream closed", details="")
            yield DoneEvent()
            return
        yield MessageEvent(content="late answer")
        yield DoneEvent()


@pytest.fixture
def env(monkeypatch):
    import agents.chat_client as chat_client_mod
    table = _SharedTable()
    a, b = JobStore("ask", table=table), JobStore("ask", table=table)
    clock = {"now": T0}
    stops: list = []

    def _stop(runtime_session_id, *, runtime_arn=None, region=None):
        stops.append({"runtime_session_id": runtime_session_id,
                      "runtime_arn": runtime_arn, "region": region})
        return "stopped"

    monkeypatch.setattr(server, "_ASK_JOBS", a)
    monkeypatch.setattr(server, "get_config", lambda: types.SimpleNamespace(
        has_runtime=True, agentcore_runtime_arn=ARN, aws_region="us-east-1"))
    monkeypatch.setattr(chat_client_mod, "AgentCoreChatClient", _CancellableClient)
    monkeypatch.setattr(chat_client_mod, "stop_runtime_session", _stop)
    monkeypatch.setattr(server, "_ask_wall_clock", lambda: clock["now"])
    monkeypatch.setattr(server, "_ASK_LEASE_RENEW_SECONDS", 30.0)   # passes are test-woken
    monkeypatch.setattr(server, "_ASK_LOCAL_WORKERS", {})
    monkeypatch.setattr(server, "_ASK_LOCAL_RUNS", {})
    monkeypatch.setattr(server, "_ASK_REAPER", {"thread": None})
    monkeypatch.setattr(server, "_ASK_STOPPING", set())
    monkeypatch.setattr(server, "_ASK_RENEWER", {"thread": None})
    _drain_renewers()
    _CancellableClient.calls = []
    yield types.SimpleNamespace(a=a, b=b, clock=clock, stops=stops)
    for call in _CancellableClient.calls:
        if call["cancel"] is not None:
            call["cancel"].cancel()
    _wait(lambda: not any(t.is_alive() and getattr(t, "_target", None) is server._run_ask_job
                          for t in threading.enumerate()))
    _drain_renewers()


def _drain_renewers() -> None:
    """End lease renewers left by earlier tests: with no live worker in the
    (patched) registry, a woken renewer exits instead of making a pass."""
    def _renewers():
        return [t for t in threading.enumerate()
                if t.name in ("ask-lease-renewer", "ask-reaper") and t.is_alive()]
    deadline = time.time() + 5
    while _renewers() and time.time() < deadline:
        server._ASK_RENEWER_WAKE.set()
        time.sleep(0.02)
    server._ASK_RENEWER_WAKE.clear()


def _wait(cond, seconds: float = 5.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def _submit(env, question: str = "which machines are down?") -> str:
    reply = server.ask_async(AskRequest(question=question))
    assert reply["status"] == "running", reply
    assert _wait(lambda: len(_CancellableClient.calls) >= 1)
    return reply["job_id"]


def _poll(job_id: str) -> dict:
    return asyncio.run(server.ask_result(job_id, wait=0.0))


def _overdue(env, job_id: str) -> None:
    env.clock["now"] = env.a.get(job_id)["started_at"] + server._ASK_RUN_CEILING_SECONDS + 1


def _worker_gone(job_id: str) -> bool:
    return job_id not in server._ASK_LOCAL_WORKERS and job_id not in server._ASK_LOCAL_RUNS


def _detached(env) -> dict:
    return dict((env.a.get(server._ASK_ACTIVE_KEY) or {}).get("detached") or {})


# --- the runtime session is the app's own --------------------------------

def test_submit_keeps_the_runtime_session_it_invokes_with(env):
    job_id = _submit(env)
    record = env.a.get(job_id)
    rsid = record.get("runtime_session_id")
    assert isinstance(rsid, str) and RSID_RE.fullmatch(rsid), record
    assert _CancellableClient.calls[0]["runtime_session_id"] == rsid
    assert _CancellableClient.calls[0]["cancel"] is not None
    assert "runtime_session_id" not in _poll(job_id), "never exposed to the caller"


def test_each_submit_gets_its_own_runtime_session(env):
    first = _submit(env, "q one")
    first_rsid = env.a.get(first)["runtime_session_id"]
    _CancellableClient.calls[0]["cancel"].cancel()
    assert _wait(lambda: _worker_gone(first))
    second = _submit(env, "q two")
    assert env.a.get(second)["runtime_session_id"] != first_rsid


# --- abandoning stops the run ---------------------------------------------

def test_a_poll_past_the_ceiling_stops_the_runtime_session_and_the_worker(env):
    job_id = _submit(env)
    rsid = env.a.get(job_id)["runtime_session_id"]
    _overdue(env, job_id)
    assert _poll(job_id) == {"status": "error", "error": "ask_failed", "error_detail": ABANDONED}
    assert _wait(lambda: env.stops), "the stop runs in the background"
    assert env.stops == [{"runtime_session_id": rsid, "runtime_arn": ARN,
                          "region": "us-east-1"}]
    assert _wait(lambda: _worker_gone(job_id)), "the worker's stream was closed"
    assert _wait(lambda: job_id not in _detached(env)), "its lease is released"
    tomb = env.a.get(job_id)
    assert tomb["status"] == "error" and tomb["abandoned"] is True, tomb
    assert _poll(job_id)["error_detail"] == ABANDONED
    time.sleep(0.1)
    assert len(env.stops) == 1, "a later poll of the tombstone stops nothing"


def test_the_workers_own_process_abandons_and_stops_it_when_nobody_polls(env):
    job_id = _submit(env)
    rsid = env.a.get(job_id)["runtime_session_id"]
    _overdue(env, job_id)
    server._ASK_RENEWER_WAKE.set()
    assert _wait(lambda: _worker_gone(job_id))
    tomb = env.a.get(job_id)
    assert tomb["status"] == "error" and tomb["abandoned"] is True, tomb
    assert tomb["error_detail"] == ABANDONED
    assert [s["runtime_session_id"] for s in env.stops] == [rsid]
    assert _poll(job_id)["error_detail"] == ABANDONED
    time.sleep(0.1)
    assert len(env.stops) == 1


def test_a_job_abandoned_by_another_replica_is_stopped_by_the_workers_process(env):
    job_id = _submit(env)
    rsid = env.a.get(job_id)["runtime_session_id"]
    _overdue(env, job_id)
    # Replica B's poll wrote the tombstone; whether B's own stop worked is
    # unknown here, so the process running the worker stops it too.
    assert env.b.finish_if_running(job_id, {"status": "error", "error": "ask_failed",
                                            "error_detail": ABANDONED, "abandoned": True,
                                            "runtime_session_id": rsid})
    server._ASK_RENEWER_WAKE.set()
    assert _wait(lambda: _worker_gone(job_id)), "this process closed its worker's stream"
    assert [s["runtime_session_id"] for s in env.stops] == [rsid]


def test_a_failed_stop_keeps_the_worker_counted_and_is_retried(env, monkeypatch, caplog):
    import agents.chat_client as chat_client_mod
    outcomes = ["failed:ThrottlingException", "stopped"]
    calls: list = []

    def _flaky(runtime_session_id, **_k):
        calls.append(runtime_session_id)
        return outcomes.pop(0) if outcomes else "not_found"
    monkeypatch.setattr(chat_client_mod, "stop_runtime_session", _flaky)
    job_id = _submit(env)
    _overdue(env, job_id)
    with caplog.at_level("WARNING"):
        assert _poll(job_id)["error_detail"] == ABANDONED
        assert _wait(lambda: calls)
        assert _wait(lambda: any("stop failed" in r.getMessage() and job_id in r.getMessage()
                                 for r in caplog.records))
    time.sleep(0.1)
    assert not _worker_gone(job_id), "the run may still be going: keep reading"
    assert job_id in _detached(env), "and keep counting toward the abandoned bound"
    server._ASK_RENEWER_WAKE.set()            # the next renewer pass retries the stop
    assert _wait(lambda: _worker_gone(job_id))
    assert len(calls) == 2


def test_no_stop_when_the_worker_finished_first(env):
    env.a["job-done"] = {"status": "done", "answer": "42", "started_at": T0,
                         "runtime_session_id": "session-" + "a" * 28}
    running_view = {"status": "running", "started_at": T0,
                    "runtime_session_id": "session-" + "a" * 28}
    env.clock["now"] = T0 + server._ASK_RUN_CEILING_SECONDS + 1
    assert server._expire_overdue_ask(env.a, "job-done", running_view)["status"] == "done"
    time.sleep(0.1)
    assert env.stops == []


def test_an_older_record_without_a_runtime_session_is_abandoned_as_before(env):
    env.a["job-old"] = {"status": "running", "started_at": T0, "submitter": None}
    env.clock["now"] = T0 + server._ASK_RUN_CEILING_SECONDS + 1
    assert _poll("job-old")["error_detail"] == ABANDONED
    time.sleep(0.1)
    assert env.stops == []


def test_a_malformed_runtime_session_on_the_record_is_never_sent(env):
    env.a["job-bad"] = {"status": "running", "started_at": T0,
                        "runtime_session_id": "short"}
    env.clock["now"] = T0 + server._ASK_RUN_CEILING_SECONDS + 1
    assert _poll("job-bad")["error_detail"] == ABANDONED
    time.sleep(0.1)
    assert env.stops == []


def test_no_stop_without_a_configured_runtime(env, monkeypatch):
    monkeypatch.setattr(server, "get_config", lambda: types.SimpleNamespace(has_runtime=True))
    env.a["job-x"] = {"status": "running", "started_at": T0,
                      "runtime_session_id": "session-" + "b" * 28}
    env.clock["now"] = T0 + server._ASK_RUN_CEILING_SECONDS + 1
    assert _poll("job-x")["error_detail"] == ABANDONED
    time.sleep(0.1)
    assert env.stops == []


# --- guards: only the ceiling stops a run ---------------------------------

def test_guard_the_ten_minute_takeover_does_not_stop_the_older_run(env):
    first = _submit(env, "q one")
    env.clock["now"] = env.a.get(first)["started_at"] + server._ASK_STALE_SECONDS + 1
    second = server.ask_async(AskRequest(question="q two"))
    assert second["status"] == "running", second
    assert env.stops == []
    assert env.a.get(first)["status"] == "running"
    assert not _CancellableClient.calls[0]["cancel"].cancelled


def test_guard_a_run_under_the_ceiling_is_not_stopped(env):
    job_id = _submit(env)
    env.clock["now"] = env.a.get(job_id)["started_at"] + server._ASK_RUN_CEILING_SECONDS - 1
    assert _poll(job_id) == {"status": "running", "job_id": job_id}
    server._ASK_RENEWER_WAKE.set()
    time.sleep(0.2)
    assert env.a.get(job_id)["status"] == "running"
    assert env.stops == []
    assert not _CancellableClient.calls[0]["cancel"].cancelled


# --- retries outlive the job record, and reaping never starves renewal ----

def test_a_failed_stop_is_retried_after_the_job_record_has_expired(env, monkeypatch):
    import agents.chat_client as chat_client_mod
    outcomes = ["failed:AccessDeniedException", "stopped"]
    calls: list = []

    def _flaky(runtime_session_id, **_k):
        calls.append(runtime_session_id)
        return outcomes.pop(0) if outcomes else "not_found"
    monkeypatch.setattr(chat_client_mod, "stop_runtime_session", _flaky)
    job_id = _submit(env)
    rsid = env.a.get(job_id)["runtime_session_id"]
    _overdue(env, job_id)
    assert _poll(job_id)["error_detail"] == ABANDONED
    assert _wait(lambda: calls) and calls == [rsid]
    time.sleep(0.1)
    assert not _worker_gone(job_id)
    del env.a[job_id]                        # the tombstone's one-hour TTL ran out
    assert env.a.get(job_id) is None
    server._ASK_RENEWER_WAKE.set()
    assert _wait(lambda: _worker_gone(job_id)), "the worker's own process still stops it"
    assert calls == [rsid, rsid]


def test_a_local_run_past_the_ceiling_with_no_record_is_stopped(env):
    job_id = _submit(env)
    rsid = env.a.get(job_id)["runtime_session_id"]
    del env.a[job_id]
    env.clock["now"] = T0 + server._ASK_RUN_CEILING_SECONDS + 1
    server._ASK_RENEWER_WAKE.set()
    assert _wait(lambda: _worker_gone(job_id))
    assert [s["runtime_session_id"] for s in env.stops] == [rsid]


def test_guard_a_local_run_under_the_ceiling_with_no_record_is_left_alone(env):
    job_id = _submit(env)
    del env.a[job_id]
    env.clock["now"] = T0 + server._ASK_RUN_CEILING_SECONDS - 1
    server._ASK_RENEWER_WAKE.set()
    time.sleep(0.2)
    assert env.stops == [] and not _worker_gone(job_id)


def test_a_slow_ceiling_check_never_holds_up_lease_renewal(env, monkeypatch):
    job_id = _submit(env)
    renewals: list = []
    real_renew = server._renew_detached_leases

    def _counting(jobs, live, now):
        renewals.append(now)
        return real_renew(jobs, live, now)
    monkeypatch.setattr(server, "_renew_detached_leases", _counting)
    gate = threading.Event()
    real_get = env.a.get

    def _slow_get(key, default=None):
        if key == job_id and threading.current_thread().name != "MainThread":
            gate.wait(10)                    # a store read that hangs
        return real_get(key, default)
    monkeypatch.setattr(env.a, "get", _slow_get)
    try:
        server._ASK_RENEWER_WAKE.set()
        assert _wait(lambda: len(renewals) >= 1)
        time.sleep(0.1)                      # the ceiling check is now stuck in its read
        server._ASK_RENEWER_WAKE.set()
        assert _wait(lambda: len(renewals) >= 2, 2.0), "the next renewal still ran"
    finally:
        gate.set()


def test_a_stop_thread_that_cannot_start_still_answers_abandoned(env, monkeypatch, caplog):
    env.a["job-nt"] = {"status": "running", "started_at": T0,
                       "runtime_session_id": "session-" + "d" * 28}
    env.clock["now"] = T0 + server._ASK_RUN_CEILING_SECONDS + 1
    real_start = threading.Thread.start

    def _start(self):
        if self.name == "ask-stop":
            raise RuntimeError("can't start new thread")
        return real_start(self)
    monkeypatch.setattr(threading.Thread, "start", _start)
    with caplog.at_level("WARNING"):
        assert _poll("job-nt") == {"status": "error", "error": "ask_failed",
                                   "error_detail": ABANDONED}
    assert "job-nt" not in server._ASK_STOPPING, "a later pass may try again"
    assert any("job-nt" in r.getMessage() for r in caplog.records)


# --- two expiries of the same job ------------------------------------------

def test_a_second_expiry_of_the_same_job_keeps_it_counted(env):
    """The poll and the ceiling check can both give the job up at once. The
    one whose conditional write loses must not undo the winner's detached
    entry: the worker is still alive and must keep counting toward the bound."""
    job_id = _submit(env)
    stale_view = dict(env.a.get(job_id))
    _overdue(env, job_id)
    first = server._expire_overdue_ask(env.a, job_id, stale_view)
    assert first["status"] == "error" and job_id in _detached(env)
    second = server._expire_overdue_ask(env.a, job_id, stale_view)
    assert second["status"] == "error" and second.get("abandoned") is True
    assert job_id in _detached(env), "the losing expiry kept the worker counted"


def test_guard_an_expiry_that_loses_to_the_finished_worker_drops_the_entry(env):
    """If the worker itself finished first, the expiry's entry is undone."""
    job_id = _submit(env)
    stale_view = dict(env.a.get(job_id))
    _overdue(env, job_id)
    assert env.a.finish_if_running(job_id, {"status": "done", "answer": "42"})
    out = server._expire_overdue_ask(env.a, job_id, stale_view)
    assert out["status"] == "done"
    assert job_id not in _detached(env)


@pytest.mark.parametrize("read", [None, "stale-running", "malformed"])
def test_a_losing_expiry_whose_read_proves_nothing_keeps_the_entry(env, monkeypatch, read):
    """The loser's follow-up read may be stale (an eventually consistent read
    from before the winner's write), failed (None) or malformed. None of those
    proves the worker finished, so the entry stays; if the worker did finish,
    the unrenewed lease runs out after the grace."""
    job_id = _submit(env)
    stale_view = dict(env.a.get(job_id))
    _overdue(env, job_id)
    assert server._expire_overdue_ask(env.a, job_id, stale_view)["status"] == "error"
    real_get = env.a.get
    answer = {"stale-running": dict(stale_view), "malformed": "not a record"}.get(read)
    monkeypatch.setattr(env.a, "get", lambda key, default=None: (
        answer if key == job_id else real_get(key, default)))
    server._expire_overdue_ask(env.a, job_id, stale_view)
    monkeypatch.setattr(env.a, "get", real_get)
    assert job_id in _detached(env)


def test_guard_a_losing_expiry_after_a_plain_error_drops_the_entry(env):
    """A non-abandoned error is the worker's own terminal write: it finished."""
    job_id = _submit(env)
    stale_view = dict(env.a.get(job_id))
    _overdue(env, job_id)
    assert env.a.finish_if_running(job_id, {"status": "error", "error": "ask_failed"})
    server._expire_overdue_ask(env.a, job_id, stale_view)
    assert job_id not in _detached(env)


# --- round 4 sweep: the lease must exist, the stop must not outrun the invoke

def test_a_lost_detach_is_repaired_while_the_given_up_worker_lives(env, monkeypatch, caplog):
    """If recording the worker as detached never lands, the abandon still
    stands; the ceiling check re-adds the entry while the worker lives, so a
    worker whose stop failed keeps counting toward the bound."""
    import agents.chat_client as chat_client_mod
    monkeypatch.setattr(chat_client_mod, "stop_runtime_session",
                        lambda *_a, **_k: "failed:ThrottlingException")
    job_id = _submit(env)
    _overdue(env, job_id)
    real_mutate = env.a.mutate
    lost = {"n": 0}

    def _lose_the_detach(key, fn, **kw):
        if key == server._ASK_ACTIVE_KEY and lost["n"] == 0:
            lost["n"] += 1
            fn(env.a.get(key))
            return False
        return real_mutate(key, fn, **kw)
    monkeypatch.setattr(env.a, "mutate", _lose_the_detach)
    with caplog.at_level("WARNING"):
        assert _poll(job_id)["error_detail"] == ABANDONED
    assert any("detach" in r.getMessage() and job_id in r.getMessage() for r in caplog.records)
    assert job_id not in _detached(env)
    server._ASK_RENEWER_WAKE.set()
    assert _wait(lambda: job_id in _detached(env)), "the ceiling check re-added it"
    assert not _worker_gone(job_id)


def test_not_found_before_the_invoke_prevents_the_invoke(env, monkeypatch):
    """A run given up before its runtime was invoked has no session yet, so the
    stop answers not found. The worker must then never invoke the runtime:
    closing only our side afterwards would leave that run going uncounted."""
    from agents.chat_client import StreamCancellation
    cancel = StreamCancellation()
    server._ASK_LOCAL_RUNS["job-pre"] = server._LocalAskRun(cancel, "session-" + "e" * 28, T0)
    import agents.chat_client as chat_client_mod
    monkeypatch.setattr(chat_client_mod, "stop_runtime_session", lambda *_a, **_k: "not_found")
    server._stop_ask_runtime("job-pre", "session-" + "e" * 28, ARN, "us-east-1")
    assert cancel.cancelled
    assert cancel.begin_invoke() is False


def test_not_found_while_the_invoke_is_in_flight_leaves_the_worker_reading(env, monkeypatch):
    from agents.chat_client import StreamCancellation
    cancel = StreamCancellation()
    assert cancel.begin_invoke() is True          # invoke sent, no body yet
    server._ASK_LOCAL_RUNS["job-mid"] = server._LocalAskRun(cancel, "session-" + "f" * 28, T0)
    import agents.chat_client as chat_client_mod
    monkeypatch.setattr(chat_client_mod, "stop_runtime_session", lambda *_a, **_k: "not_found")
    server._stop_ask_runtime("job-mid", "session-" + "f" * 28, ARN, "us-east-1")
    assert not cancel.cancelled, "the session may not exist yet: retry, don't tear down"


def test_not_found_after_the_stream_opened_closes_it(env, monkeypatch):
    from agents.chat_client import StreamCancellation

    class _Body:
        closed = False

        def close(self):
            type(self).closed = True
    cancel = StreamCancellation()
    assert cancel.begin_invoke() is True
    cancel.arm(_Body())
    server._ASK_LOCAL_RUNS["job-arm"] = server._LocalAskRun(cancel, "session-" + "0" * 28, T0)
    import agents.chat_client as chat_client_mod
    monkeypatch.setattr(chat_client_mod, "stop_runtime_session", lambda *_a, **_k: "not_found")
    server._stop_ask_runtime("job-arm", "session-" + "0" * 28, ARN, "us-east-1")
    assert cancel.cancelled and _Body.closed


def test_a_lost_lease_renewal_is_reported_and_retried_soon(env, monkeypatch, caplog):
    job_id = _submit(env)
    _overdue(env, job_id)
    import agents.chat_client as chat_client_mod
    monkeypatch.setattr(chat_client_mod, "stop_runtime_session",
                        lambda *_a, **_k: "failed:ThrottlingException")
    assert _poll(job_id)["error_detail"] == ABANDONED
    assert _wait(lambda: job_id in _detached(env))
    real_mutate = env.a.mutate
    calls = {"lost": 0, "renew": 0}

    def _lose_one_renewal(key, fn, **kw):
        if key == server._ASK_ACTIVE_KEY and getattr(fn, "__name__", "") == "_renew":
            calls["renew"] += 1
            if calls["lost"] == 0:
                calls["lost"] += 1
                fn(env.a.get(key))
                return False
        return real_mutate(key, fn, **kw)
    monkeypatch.setattr(env.a, "mutate", _lose_one_renewal)
    monkeypatch.setattr(server, "_ASK_LEASE_RETRY_SECONDS", 0.05, raising=False)
    with caplog.at_level("WARNING"):
        server._ASK_RENEWER_WAKE.set()
        assert _wait(lambda: calls["renew"] >= 2, 3.0), "retried well before the next interval"
    assert any("renewal" in r.getMessage() for r in caplog.records)


def test_renew_reports_a_lost_write_but_not_an_empty_one(env):
    class _Loses:
        def mutate(self, key, fn, **_k):
            fn({"status": "idle", "detached": {"q": T0}})
            return False
    with pytest.raises(server._LeaseRenewalLost):
        server._renew_detached_leases(_Loses(), {"q"}, T0 + 60)
    assert server._renew_detached_leases(_Loses(), {"other"}, T0 + 60) == 0


# --- round 5: the not-found decision uses the state from BEFORE the stop ----

def test_a_run_given_up_before_its_invoke_is_refused_and_no_stop_is_sent(env, monkeypatch):
    from agents.chat_client import StreamCancellation
    import agents.chat_client as chat_client_mod
    calls: list = []
    monkeypatch.setattr(chat_client_mod, "stop_runtime_session",
                        lambda *a, **k: calls.append(a) or "not_found")
    cancel = StreamCancellation()
    server._ASK_LOCAL_RUNS["job-pre2"] = server._LocalAskRun(cancel, "session-" + "1" * 28, T0)
    server._stop_ask_runtime("job-pre2", "session-" + "1" * 28, ARN, "us-east-1")
    assert calls == [], "no session can exist yet, so there is nothing to stop"
    assert cancel.begin_invoke() is False


def test_a_not_found_that_overlaps_the_runtime_answering_leaves_the_worker_reading(
        env, monkeypatch):
    """The stop was sent while the invoke was in flight; the runtime answered
    (the body armed) before the stop's not-found came back. That not-found
    may predate the new session, so it must not close our side."""
    from agents.chat_client import StreamCancellation
    import agents.chat_client as chat_client_mod
    entered, release = threading.Event(), threading.Event()

    def _slow_not_found(*_a, **_k):
        entered.set()
        assert release.wait(5)
        return "not_found"
    monkeypatch.setattr(chat_client_mod, "stop_runtime_session", _slow_not_found)

    class _Body:
        closed = False

        def close(self):
            self.closed = True
    cancel, body = StreamCancellation(), _Body()
    assert cancel.begin_invoke() is True
    server._ASK_LOCAL_RUNS["job-ovl"] = server._LocalAskRun(cancel, "session-" + "2" * 28, T0)
    t = threading.Thread(target=server._stop_ask_runtime,
                         args=("job-ovl", "session-" + "2" * 28, ARN, "us-east-1"))
    t.start()
    assert entered.wait(5)
    cancel.arm(body)                              # the runtime answered meanwhile
    release.set()
    t.join(5)
    assert not cancel.cancelled and not body.closed


def test_a_stopped_answer_closes_our_side_even_if_the_stream_opened_meanwhile(
        env, monkeypatch):
    from agents.chat_client import StreamCancellation
    import agents.chat_client as chat_client_mod
    monkeypatch.setattr(chat_client_mod, "stop_runtime_session", lambda *_a, **_k: "stopped")
    cancel = StreamCancellation()
    assert cancel.begin_invoke() is True
    server._ASK_LOCAL_RUNS["job-st"] = server._LocalAskRun(cancel, "session-" + "3" * 28, T0)
    server._stop_ask_runtime("job-st", "session-" + "3" * 28, ARN, "us-east-1")
    assert cancel.cancelled
