# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Where a memory extraction model switch's outcome is read from, on any
app task.

The worker records its outcome twice, both where every task reads them: the
job record (a strict write), then the exact-owner release of the save slot,
which carries the same result (status, message, model, whether it changed,
finish time). A status read takes the slot first, then the job record, and
answers:

* the result the slot carries for this job, or a finished job record;
* ``running`` while the slot holds the job, until the slot has been quiet
  for ``SILENT_SECONDS`` (a running switch writes it at least every
  ``JOB_SILENCE_MAX_SECONDS``), then ``unknown``;
* ``unknown`` for a job record still ``running`` once the slot no longer
  holds that job (it ended without a recorded outcome);
* 503 when neither store can say, 404 for a job neither knows.

Nothing is kept as a task-local copy: a read on another task, or on this one,
sees only what the shared table took. Two app tasks are two sets of stores
on one shared table double. Offline.
"""
from __future__ import annotations

import json

import pytest

from tests.unit._memory_model_fakes import NEW
from tests.unit.test_job_store_slot import _FakeSlotTable
from tests.unit.test_llm_settings_memory_model import (  # noqa: F401 - env is a fixture
    SSM_MEMORY,
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


class _Table(_FakeSlotTable):
    """The shared table as one app task reaches it; its reads and writes
    fail while ``fail_get`` / ``fail_put(item)`` say so."""

    def __init__(self, store: dict):
        super().__init__(store)
        self.fail_get = False
        self.fail_put = None

    def get_item(self, Key, **_kwargs):  # noqa: N803
        if self.fail_get:
            raise RuntimeError("ThrottlingException")
        return super().get_item(Key)

    def put_item(self, Item, **kwargs):  # noqa: N803
        if self.fail_put is not None and self.fail_put(Item):
            raise RuntimeError("ThrottlingException")
        return super().put_item(Item, **kwargs)


class _Task:
    """One app task: its own slot and job stores on the shared table."""

    def __init__(self, shared: dict):
        from agents.job_store import JobStore
        self.slot_table, self.job_table = _Table(shared), _Table(shared)
        self.slots = JobStore("memory-model", table=self.slot_table)
        self.records = JobStore("memory-model-job", table=self.job_table)

    def use(self, env) -> "_Task":
        env.setattr("agents.memory_model._SAVE_SLOTS", self.slots)
        env.setattr("agents.memory_model_job._JOBS", self.records)
        return self


def _state(item: dict) -> dict:
    return json.loads(item["job_state"])


def _terminal(item: dict) -> bool:
    return _state(item).get("status") in ("done", "error")


def _release(item: dict) -> bool:
    return item.get("slot_status") == "idle"


def _run_switch(env, task: _Task, ssm, control) -> str:
    """POST on *task*, then run its worker there; the job id."""
    pending: list = []
    env.setattr("agents.memory_model_job._start_worker",
                lambda target, *args: pending.append((target, args)))
    task.use(env)
    res = _post({"memory_model": NEW}, ssm, control)
    assert _code(res) == 202, _body(res)
    target, args = pending.pop()
    with _boto(ssm, control):
        target(*args)
    return _body(res)["job_id"]


def _slot_item(shared: dict) -> dict:
    (item,) = [v for k, v in shared.items() if k.endswith(":" + mm.SAVE_SLOT_KEY)]
    return item


# -- the job record refused, the slot release took the outcome -------------------------------

def test_a_terminal_record_the_store_refused_is_read_from_the_released_slot_on_any_task(env):
    shared: dict = {}
    worker_task, reader_task = _Task(shared), _Task(shared)
    worker_task.job_table.fail_put = _terminal
    ssm, control = FakeSSM(), Recorder([])
    job_id = _run_switch(env, worker_task, ssm, control)
    assert ssm.store == {SSM_MEMORY: NEW}
    slot = _state(_slot_item(shared))
    assert slot["status"] == "idle" and slot["job_id"] == job_id
    result = slot["result"]
    assert result["status"] == "done" and result["job_id"] == job_id
    assert result["memory_model"] == NEW and result["memory_model_changed"] is True
    assert result["message"] == mm.done_message(NEW, True)
    assert result["finished_at"] >= result["started_at"]
    # The backend has recovered; another task reads the real outcome from both routes.
    worker_task.job_table.fail_put = None
    reader_task.use(env)
    for res in (_job(job_id), _job()):
        assert _code(res) == 200
        assert _body(res) == result
    # The released slot does not block the next switch.
    assert _code(_post({"memory_model": NEW}, FakeSSM(), Recorder([]))) == 202


def test_the_same_task_never_answers_from_a_local_copy(env):
    """No task-local fallback: the job store's fail-soft dict surface is never
    written, and this task reads what the shared table holds."""
    shared: dict = {}
    task = _Task(shared)
    task.job_table.fail_put = _terminal

    def no_local_copy(*_args, **_kwargs):
        raise AssertionError("the switch wrote a task-local job record")
    env.setattr(type(task.records), "__setitem__", no_local_copy)
    job_id = _run_switch(env, task, FakeSSM(), Recorder([]))
    assert task.records._mem.get(job_id, {}).get("status") != "done"
    assert _body(_job(job_id))["status"] == "done"    # from the slot's release


def test_both_records_refused_reads_as_unknown_once_the_slot_is_quiet(env):
    shared: dict = {}
    worker_task, reader_task = _Task(shared), _Task(shared)
    worker_task.job_table.fail_put = _terminal
    worker_task.slot_table.fail_put = _release
    ssm, control = FakeSSM(), Recorder([])
    job_id = _run_switch(env, worker_task, ssm, control)
    assert ssm.store == {SSM_MEMORY: NEW}
    worker_task.job_table.fail_put = worker_task.slot_table.fail_put = None
    reader_task.use(env)
    # Nothing tells a switch that could not record its end from one still
    # writing SSM: it reads as running, never as done...
    now = jobs._now()
    assert _body(_job(job_id))["status"] == "running"
    # ...until its slot has been quiet for longer than a running switch ever is.
    env.setattr("agents.memory_model_job._now", lambda: now + jobs.SILENT_SECONDS + 1)
    for res in (_job(job_id), _job()):
        assert _code(res) == 200
        body = _body(res)
        assert body["status"] == "unknown" and body["job_id"] == job_id
        message = body["message"]
        assert NEW in message and "could not record" in message
        assert "Reload this page to read what the memory runs" in message


def test_a_running_record_the_slot_no_longer_holds_is_unknown(env):
    """The outcome went only into the slot, and a later switch has taken the
    slot since: the first job's record still says running, but it is not."""
    shared: dict = {}
    first_task, later_task = _Task(shared), _Task(shared)
    first_task.job_table.fail_put = _terminal
    first = _run_switch(env, first_task, FakeSSM(), Recorder([]))
    first_task.job_table.fail_put = None
    pending: list = []
    env.setattr("agents.memory_model_job._start_worker",
                lambda target, *args: pending.append((target, args)))
    later_task.use(env)
    assert _code(_post({"memory_model": NEW}, FakeSSM(), Recorder([]))) == 202
    body = _body(_job(first))
    assert body["status"] == "unknown" and body["job_id"] == first
    assert "no longer running" in body["message"] and "could not be recorded" in body["message"]


def test_a_running_switch_reads_as_running_on_another_task(env):
    shared: dict = {}
    worker_task, reader_task = _Task(shared), _Task(shared)
    pending: list = []
    env.setattr("agents.memory_model_job._start_worker",
                lambda target, *args: pending.append((target, args)))
    worker_task.use(env)
    job_id = _body(_post({"memory_model": NEW}, FakeSSM(), Recorder([])))["job_id"]
    reader_task.use(env)
    for res in (_job(job_id), _job()):
        body = _body(res)
        assert body["status"] == "running" and body["job_id"] == job_id
        assert body["memory_model"] == NEW


@pytest.mark.parametrize("finished", [False, True])
def test_an_unreadable_slot_falls_back_to_the_job_record(env, finished):
    shared: dict = {}
    task = _Task(shared)
    pending: list = []
    env.setattr("agents.memory_model_job._start_worker",
                lambda target, *args: pending.append((target, args)))
    task.use(env)
    job_id = _body(_post({"memory_model": NEW}, FakeSSM(), Recorder([])))["job_id"]
    target, args = pending.pop()
    if finished:
        with _boto(FakeSSM(), Recorder([])):
            target(*args)
    else:
        jobs._jobs().put_strict(job_id, {"status": "running", "job_id": job_id,
                                         "memory_model": NEW, "started_at": jobs._now()})
    task.slot_table.fail_get = True
    body = _body(_job(job_id))
    assert body["status"] == ("done" if finished else "running")
    assert _code(_job()) == 503      # the latest switch is the slot's to name


def test_a_job_neither_store_can_read_is_503_and_one_neither_knows_is_404(env):
    shared: dict = {}
    task = _Task(shared).use(env)
    assert _code(_job("0123456789abcdef")) == 404
    task.slot_table.fail_get = task.job_table.fail_get = True
    assert _code(_job("0123456789abcdef")) == 503


def test_a_worker_that_will_not_start_leaves_its_refusal_in_the_slot(env):
    shared: dict = {}
    _Task(shared).use(env)

    def no_thread(*_args):
        raise RuntimeError("can't start new thread")
    env.setattr("agents.memory_model_job._start_worker", no_thread)
    control, ssm = Recorder([]), FakeSSM()
    res = _post({"memory_model": NEW}, ssm, control)
    assert _code(res) == 503 and "could not be started" in _body(res)["message"]
    latest = _body(_job())
    assert latest["status"] == "error" and "could not be started" in latest["message"]
    assert control.gets == 0 and ssm.log == []
    env.setattr("agents.memory_model_job._start_worker", lambda target, *args: None)
    assert _code(_post({"memory_model": NEW}, FakeSSM(), Recorder([]))) == 202
