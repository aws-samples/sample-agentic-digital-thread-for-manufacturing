# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""How long the memory extraction model switch's requests and worker can take.

A request through the app's origin lasts at most 120 s (CloudFront's origin
read timeout and the ALB idle timeout), so every call the switch's routes
make is bounded, and so is the worker, whose longest run must stay below the
save slot's 900 s stale bound:

* the shared DynamoDB table (the save slot and the job records) is reached
  through a client with a 1 s connect timeout, a 3 s read timeout and two
  attempts (botocore standard mode), checked on the real built clients;
* AgentCore and SSM calls through clients with 3 s / 10 s and two attempts;
* one call takes at most ``attempts * (connect + read)`` plus botocore's
  wait before its retry (up to 1 s, and on botocore's newer retry path up to
  5 s more when the service asks for it with ``x-amz-retry-after``), read
  from botocore itself;
* the POST and each status route make at most two store calls one after the
  other on any path (counted here on every path), so each answers within
  28 s;
* the worker's longest run counts the wait, one poll interval, and every
  AgentCore, SSM and store call it can make outside the wait (counted on a
  run here), and stays below the stale bound.

Offline: the clients are built but never called; the counted runs use table
doubles.
"""
from __future__ import annotations

import json
import time
import types

import pytest

from tests.unit._memory_model_fakes import NEW, Clock
from tests.unit.test_job_store_slot import _FakeSlotTable
from tests.unit.test_llm_settings_memory_model import (  # noqa: F401 - env is a fixture
    FakeSSM,
    Recorder,
    _body,
    _boto,
    _code,
    _job,
    _post,
    env,
    jobs,
    mm,
)

ORIGIN_TIMEOUT_SECONDS = 120.0
TARGET_SECONDS = 30.0
#: The most store calls a switch route makes one after the other.
ROUTE_STORE_CALLS = 2


class _Table(_FakeSlotTable):
    """The shared table as one app task reaches it: every call counted;
    reads and writes fail while ``fail_get`` / ``fail_put(item)`` say so."""

    def __init__(self, store: dict | None = None):
        super().__init__(store)
        self.calls = 0
        self.fail_get = False
        self.fail_put = None

    def get_item(self, Key, **_kwargs):  # noqa: N803
        self.calls += 1
        if self.fail_get:
            raise RuntimeError("ThrottlingException")
        return super().get_item(Key)

    def put_item(self, Item, **kwargs):  # noqa: N803
        self.calls += 1
        if self.fail_put is not None and self.fail_put(Item):
            raise RuntimeError("ThrottlingException")
        return super().put_item(Item, **kwargs)


def _stores(env, shared: dict) -> tuple[_Table, _Table]:
    from agents.job_store import JobStore
    slot_table, job_table = _Table(shared), _Table(shared)
    env.setattr("agents.memory_model._SAVE_SLOTS", JobStore("memory-model", table=slot_table))
    env.setattr("agents.memory_model_job._JOBS", JobStore("memory-model-job", table=job_table))
    return slot_table, job_table


def _calls(*tables) -> int:
    return sum(t.calls for t in tables)


def _reset(*tables) -> None:
    for t in tables:
        t.calls = 0


# -- the clients carry the bounds (inspected on the real built clients) -----------------------

@pytest.fixture
def table_env(monkeypatch):
    monkeypatch.setenv("ONBOARDING_SESSIONS_TABLE", "adt-onboarding-sessions")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr("agents.memory_model._SAVE_SLOTS", None)
    monkeypatch.setattr("agents.memory_model_job._JOBS", None)
    return monkeypatch


def _config(client):
    config = client.meta.config
    return config.connect_timeout, config.read_timeout, config.retries


@pytest.mark.parametrize("opener", ["_strict_table", "_get_table"])
def test_the_save_slot_and_the_job_records_use_the_bounded_store_client(table_env, opener):
    store_bounds = (mm.STORE_CONNECT_TIMEOUT_SECONDS, mm.STORE_READ_TIMEOUT_SECONDS,
                    {"total_max_attempts": mm.STORE_MAX_ATTEMPTS, "mode": "standard"})
    assert store_bounds == (1.0, 3.0, {"total_max_attempts": 2, "mode": "standard"})
    for store in (mm._save_slots(), jobs._jobs()):
        table = getattr(store, opener)()
        assert _config(table.meta.client) == store_bounds


def test_the_agentcore_and_ssm_clients_make_two_attempts(table_env):
    control_bounds = (mm.CONTROL_CONNECT_TIMEOUT_SECONDS, mm.CONTROL_READ_TIMEOUT_SECONDS,
                      {"total_max_attempts": mm.CONTROL_MAX_ATTEMPTS, "mode": "standard"})
    assert control_bounds == (3.0, 10.0, {"total_max_attempts": 2, "mode": "standard"})
    assert _config(mm.control_client("us-east-1")) == control_bounds
    assert _config(mm._ssm_client("us-east-1")) == control_bounds


# -- one call's bound, from botocore's own retry wait ------------------------------------------

def test_the_wait_before_a_retry_is_what_botocore_standard_mode_allows(monkeypatch):
    """The longest wait botocore puts before a second attempt: its jittered
    backoff (at most 1 s, a throttling error's scale), plus up to 5 s more for
    an ``x-amz-retry-after`` the service sends. botocore 1.43 honoured that
    header only behind its ``AWS_NEW_RETRIES_2026`` gate; 1.44 always does, so
    the bound is the larger of the gate off and on."""
    import botocore.retries.standard as standard

    class Throttling:
        @staticmethod
        def is_throttling_error_from_context(_context):
            return True

    backoff = standard.ExponentialBackoff(random=lambda: 1.0, throttling_detector=Throttling())
    asked = types.SimpleNamespace(attempt_number=1, http_response=types.SimpleNamespace(
        headers={"x-amz-retry-after": "3600000"}))
    plain = types.SimpleNamespace(attempt_number=1, http_response=None)
    waits = []
    for new_retries in (False, True):
        monkeypatch.setattr(standard, "NEW_RETRIES_ENABLED", new_retries, raising=False)
        assert backoff.delay_amount(plain) == 1.0
        waits.append(backoff.delay_amount(asked))
    assert max(waits) == mm.RETRY_WAIT_SECONDS == 6.0


def test_one_call_takes_at_most_its_attempts_and_the_wait_between():
    assert mm.STORE_CALL_SECONDS == (
        mm.STORE_MAX_ATTEMPTS * (mm.STORE_CONNECT_TIMEOUT_SECONDS + mm.STORE_READ_TIMEOUT_SECONDS)
        + (mm.STORE_MAX_ATTEMPTS - 1) * mm.RETRY_WAIT_SECONDS) == 14.0
    assert mm.CONTROL_CALL_SECONDS == (
        mm.CONTROL_MAX_ATTEMPTS
        * (mm.CONTROL_CONNECT_TIMEOUT_SECONDS + mm.CONTROL_READ_TIMEOUT_SECONDS)
        + (mm.CONTROL_MAX_ATTEMPTS - 1) * mm.RETRY_WAIT_SECONDS) == 32.0


# -- the routes: at most two store calls one after the other, on every path ----------------------

def test_each_route_answers_well_below_the_origin_timeout():
    assert jobs.POST_STORE_CALLS == jobs.STATUS_STORE_CALLS == ROUTE_STORE_CALLS
    assert jobs.POST_MAX_SECONDS == jobs.POST_STORE_CALLS * mm.STORE_CALL_SECONDS == 28.0
    assert jobs.STATUS_MAX_SECONDS == jobs.STATUS_STORE_CALLS * mm.STORE_CALL_SECONDS == 28.0
    for bound in (jobs.POST_MAX_SECONDS, jobs.STATUS_MAX_SECONDS):
        assert bound < TARGET_SECONDS < ORIGIN_TIMEOUT_SECONDS


def _pending_worker(env) -> list:
    pending: list = []
    env.setattr("agents.memory_model_job._start_worker",
                lambda target, *args: pending.append((target, args)))
    return pending


def test_a_post_that_starts_a_switch_makes_one_store_call(env):
    slot_table, job_table = _stores(env, {})
    _pending_worker(env)
    res = _post({"memory_model": NEW}, FakeSSM(), Recorder([]))
    assert _code(res) == 202
    assert _calls(slot_table, job_table) <= ROUTE_STORE_CALLS
    assert job_table.calls == 0   # the job's records are the worker's


def test_a_post_refused_while_another_holds_the_slot_makes_two_store_calls(env):
    slot_table, job_table = _stores(env, {})
    _pending_worker(env)
    assert _code(_post({"memory_model": NEW}, FakeSSM(), Recorder([]))) == 202
    _reset(slot_table, job_table)
    assert _code(_post({"memory_model": NEW}, FakeSSM(), Recorder([]))) == 409
    assert _calls(slot_table, job_table) <= ROUTE_STORE_CALLS


def test_a_post_the_store_cannot_decide_makes_two_store_calls(env):
    slot_table, job_table = _stores(env, {})
    slot_table.fail_put = lambda _item: True
    slot_table.fail_get = True
    assert _code(_post({"memory_model": NEW}, FakeSSM(), Recorder([]))) == 503
    assert _calls(slot_table, job_table) <= ROUTE_STORE_CALLS


def test_a_post_whose_records_cannot_be_written_still_makes_one_store_call(env):
    """The job record is the worker's first write, not the request's: a job
    table that refuses writes costs the POST nothing."""
    slot_table, job_table = _stores(env, {})
    job_table.fail_put = lambda _item: True
    _pending_worker(env)
    assert _code(_post({"memory_model": NEW}, FakeSSM(), Recorder([]))) == 202
    assert _calls(slot_table, job_table) <= ROUTE_STORE_CALLS


def test_a_post_whose_worker_will_not_start_makes_two_store_calls(env):
    slot_table, job_table = _stores(env, {})

    def no_thread(*_args):
        raise RuntimeError("can't start new thread")
    env.setattr("agents.memory_model_job._start_worker", no_thread)
    assert _code(_post({"memory_model": NEW}, FakeSSM(), Recorder([]))) == 503
    assert _calls(slot_table, job_table) <= ROUTE_STORE_CALLS


def _status_cases(env):
    """Every state a status read can meet: (name, job id, tables)."""
    shared: dict = {}
    slot_table, job_table = _stores(env, shared)
    pending = _pending_worker(env)
    started = _body(_post({"memory_model": NEW}, FakeSSM(), Recorder([])))
    job_id = started["job_id"]
    yield "running, no record yet", job_id, (slot_table, job_table)
    target, args = pending.pop()
    with _boto(FakeSSM(), Recorder([])):
        target(*args)
    yield "done", job_id, (slot_table, job_table)
    job_table.fail_get = True
    yield "record unreadable", job_id, (slot_table, job_table)
    slot_table.fail_get = True
    yield "nothing readable", job_id, (slot_table, job_table)
    job_table.fail_get = slot_table.fail_get = False
    yield "unknown id", "0123456789abcdef", (slot_table, job_table)


def test_no_status_read_makes_more_than_two_store_calls(env):
    seen = []
    for name, job_id, tables in _status_cases(env):
        for which in (job_id, None):
            _reset(*tables)
            _job(which)
            assert _calls(*tables) <= ROUTE_STORE_CALLS, (name, which)
        seen.append(name)
    assert len(seen) == 5


# -- the worker's longest run, below the stale bound ---------------------------------------------

def test_the_longest_switch_job_stays_below_the_slot_stale_bound():
    """Measured from the job's ``started_at``, which is read before the
    POST's claim, so the claim is one of the store calls. The margin below
    the 900 s stale bound is 37 s; the slot does not go stale while a job
    runs anyway, because its heartbeats move ``started_at`` far more often
    (:data:`JOB_SILENCE_MAX_SECONDS`)."""
    assert mm.JOB_CONTROL_CALLS == 5 and mm.JOB_STORE_CALLS == 7
    assert mm.JOB_MAX_SECONDS == (
        mm.POLL_TIMEOUT_SECONDS + mm.POLL_INTERVAL_SECONDS
        + mm.JOB_CONTROL_CALLS * mm.CONTROL_CALL_SECONDS
        + mm.JOB_STORE_CALLS * mm.STORE_CALL_SECONDS) == 863.0
    assert mm.SAVE_SLOT_STALE_SECONDS == 900.0
    assert mm.SAVE_SLOT_STALE_SECONDS - mm.JOB_MAX_SECONDS == 37.0
    assert jobs.JOB_STALE_SECONDS == mm.SAVE_SLOT_STALE_SECONDS


def test_the_longest_silence_of_a_running_switch_is_well_inside_the_quiet_bound():
    """A running switch writes its slot (the claim, a heartbeat, the release)
    at least this often; a slot quiet for :data:`SILENT_SECONDS` is a switch
    that stopped or could not record how it ended. The longest gap is from
    the final heartbeat to the release: that heartbeat, the SSM write, the
    read after it, the terminal record and the release."""
    assert mm.JOB_SILENCE_MAX_SECONDS == (3 * mm.STORE_CALL_SECONDS
                                          + 2 * mm.CONTROL_CALL_SECONDS) == 106.0
    # The other gaps are shorter: the claim to the ownership check before
    # UpdateMemory (the claim, the job record, GetMemory, the check); that
    # check to the first heartbeat in the wait (the check, UpdateMemory, the
    # first poll, the heartbeat); two heartbeats in the wait (one, a poll
    # interval, a read, the next).
    for gap in (3 * mm.STORE_CALL_SECONDS + mm.CONTROL_CALL_SECONDS,
                2 * mm.STORE_CALL_SECONDS + 2 * mm.CONTROL_CALL_SECONDS,
                2 * mm.STORE_CALL_SECONDS + mm.POLL_INTERVAL_SECONDS + mm.CONTROL_CALL_SECONDS):
        assert gap < mm.JOB_SILENCE_MAX_SECONDS
    assert 2 * mm.JOB_SILENCE_MAX_SECONDS <= jobs.SILENT_SECONDS < mm.SAVE_SLOT_STALE_SECONDS


def test_the_job_bound_counts_every_call_the_worker_makes_outside_the_wait(env):
    """A switch whose memory settles on the read after its last heartbeat
    inside the wait: besides the reads and heartbeats that finish inside the
    wait (one fewer heartbeat than polls), the POST's claim and the worker
    make exactly ``JOB_CONTROL_CALLS`` AgentCore/SSM calls and
    ``JOB_STORE_CALLS`` store calls, the counts :data:`JOB_MAX_SECONDS` adds
    up from the job's ``started_at`` (read before the claim)."""
    slot_table, job_table = _stores(env, {})
    pending = _pending_worker(env)
    clock = Clock()
    env.setattr("agents.memory_model._clock", clock)
    env.setattr("agents.memory_model._sleep", clock.sleep)
    env.setattr("agents.memory_model.POLL_INTERVAL_SECONDS", 5.0)
    ssm, control = FakeSSM(), Recorder([])
    control.busy_reads = 2                     # polls at 0 and 5 s busy, 10 s settled
    assert _code(_post({"memory_model": NEW}, ssm, control)) == 202
    assert _calls(slot_table, job_table) == 1  # the claim, counted with the job's calls
    touched: list = []
    store = mm._save_slots()
    real_touch = store.touch_slot

    def counting_touch(*args, **kwargs):
        touched.append(time.time())
        return real_touch(*args, **kwargs)
    env.setattr(store, "touch_slot", counting_touch)
    target, args = pending.pop()
    with _boto(ssm, control):
        target(*args)
    record = json.loads(slot_table._store[mm._save_slots()._key(mm.SAVE_SLOT_KEY)]["job_state"])
    assert record["status"] == "idle" and record["result"]["status"] == "done"
    # The first touch is the ownership check before UpdateMemory, the last
    # the final heartbeat (the check before the SSM write).
    in_wait_heartbeats = len(touched) - 2
    polls = control.gets - 2                   # minus the first GetMemory and the read after
    assert in_wait_heartbeats == 2 and polls == 3
    control_calls = control.gets + len(control.updates) + len(ssm.log)
    assert control_calls - (polls - 1) == mm.JOB_CONTROL_CALLS
    assert _calls(slot_table, job_table) - (in_wait_heartbeats - 1) == mm.JOB_STORE_CALLS
