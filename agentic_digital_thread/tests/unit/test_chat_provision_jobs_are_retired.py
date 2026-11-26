# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The chat's provision jobs registry holds at most the live job of a system.

``onboard_chat_lifecycle._CHAT_JOBS`` records, per system id, the chat's
provision job this process runs: while it reads the schema again, and the
refusal it found until a call gives it. A refusal is taken from the registry
in one step that checks it is still the current job's, so a call that read
the registry before another call gave the refusal and a new provision
started answers that new provision, not the old refusal. A record leaves the
registry once it has nothing more to answer: a refusal once given, a job
another request superseded at once, a job that provisioned once its final
answer was given. Offline, on the fixture of
test_chat_provision_is_the_wizard_provision.py.
"""
from __future__ import annotations

import copy
import threading

import pytest

import agents.onboard as onboard
import agents.onboard_chat_lifecycle as lifecycle
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: F401 - fixture
    JOB_WAIT, SID, TABLES, _agent_tools, _chat_provision_answer, _chat_until_resolved,
    _wait_for, world)
from tests.unit.test_chat_provision_reads_the_schema_in_its_job import (
    GONE, _slow_schema_read, _without)


def _registry() -> dict:
    return dict(lifecycle._CHAT_JOBS)


def _refused_once(world) -> dict:
    """A provision whose job refused (a mapped field is gone); its refusal
    not given yet."""
    _chat_until_resolved()
    world.discover_tables = _without(GONE)
    started = _agent_tools().provision_system(SID)
    assert started.get("status") == "provisioning", started
    _wait_for(lambda: not onboard._provision_running(SID))
    return started


# ── a call holding an old refusal answers the current job ──────────────────

def test_a_call_that_read_an_old_refusal_answers_the_provision_that_replaced_it(world):
    _refused_once(world)
    provision = _agent_tools().provision_system
    read_it, resume = threading.Event(), threading.Event()
    real = lifecycle._chat_job
    held = {"once": False}

    def _held_after_reading(system_id):
        record = real(system_id)
        if threading.current_thread().name == "call-a" and not held["once"]:
            held["once"] = True
            read_it.set()
            resume.wait(JOB_WAIT)
        return record
    world.mp.setattr(lifecycle, "_chat_job", _held_after_reading)
    out: dict = {}
    call_a = threading.Thread(target=lambda: out.update(provision(SID)), name="call-a",
                              daemon=True)
    call_a.start()
    reading = release = None
    try:
        assert read_it.wait(JOB_WAIT)                       # A has read the refusal
        given = provision(SID)                              # B gives it
        assert given.get("reason") == "schema_changed", given
        world.discover_tables = copy.deepcopy(TABLES)
        reading, release, _reads = _slow_schema_read(world)
        newer = provision(SID)                              # C starts a new provision
        assert newer.get("status") == "provisioning" and newer.get("job_id"), newer
        assert reading.wait(JOB_WAIT)
        resume.set()                                        # A goes on
        call_a.join(JOB_WAIT)
        assert not call_a.is_alive()
    finally:
        resume.set()
        if release is not None:
            release.set()
    assert out.get("reason") != "schema_changed", f"A gave the old refusal: {out}"
    assert out.get("status") == "provisioning", out
    assert out.get("job_id") == newer["job_id"], (out, newer)
    _wait_for(lambda: world.store.get_session(SID)["status"] == "onboarded")
    assert len(world.job_bodies) == 1


def test_a_call_while_the_first_one_takes_the_claim_answers_its_job(world):
    """The claim and the job's record are taken together: a call that comes
    between them waits for the record and answers that job, it is not
    refused as busy."""
    _chat_until_resolved()
    reading, release, _reads = _slow_schema_read(world)
    claimed, resume = threading.Event(), threading.Event()
    real_claim = onboard._claim_provision

    def _held_after_claiming(system_id):
        claim = real_claim(system_id)
        if threading.current_thread().name == "call-a":
            claimed.set()
            resume.wait(JOB_WAIT)
        return claim
    world.mp.setattr(onboard, "_claim_provision", _held_after_claiming)
    provision = _agent_tools().provision_system
    out_a: dict = {}
    out_b: dict = {}
    call_a = threading.Thread(target=lambda: out_a.update(provision(SID)), name="call-a",
                              daemon=True)
    call_b = threading.Thread(target=lambda: out_b.update(provision(SID)), name="call-b",
                              daemon=True)
    try:
        call_a.start()
        assert claimed.wait(JOB_WAIT)
        call_b.start()
        resume.set()
        call_a.join(JOB_WAIT)
        call_b.join(JOB_WAIT)
        assert not call_a.is_alive() and not call_b.is_alive()
    finally:
        resume.set()
        release.set()
    assert out_a.get("status") == "provisioning" and out_a.get("job_id"), out_a
    assert out_b.get("status") == "provisioning", out_b
    assert out_b.get("job_id") == out_a["job_id"], (out_a, out_b)
    _wait_for(lambda: world.store.get_session(SID)["status"] == "onboarded")
    assert len(world.job_bodies) == 1


# ── the registry is empty once a job has nothing more to answer ───────────

def _refused_and_given(world):
    _refused_once(world)
    assert _registry().get(SID, {}).get("phase") == lifecycle.REFUSED
    given = _agent_tools().provision_system(SID)
    assert given.get("reason") == "schema_changed", given


def _onboarded_and_answered(world):
    _chat_until_resolved()
    finished = _chat_provision_answer()
    assert finished.get("status") == "onboarded", finished


def _failed_and_answered(world):
    _chat_until_resolved()
    world.mp.setattr(onboard, "_do_provision", lambda body: {"error": "x"})
    finished = _chat_provision_answer()
    assert finished.get("status") == "failed", finished


def _superseded(world):
    """Another request claims the session while the chat's job reads the
    schema: that one provisions, the chat's job ends."""
    _chat_until_resolved()
    reading, release, _reads = _slow_schema_read(world)
    try:
        started = _agent_tools().provision_system(SID)
        assert started.get("status") == "provisioning", started
        assert reading.wait(JOB_WAIT)
        world.store.claim_provisioning(SID, provision_job_id="elsewhere")
    finally:
        release.set()
    _wait_for(lambda: not onboard._provision_running(SID))
    assert onboard._provision_jobs.get(started["job_id"])["status"] == "error"
    assert world.store.get_session(SID)["provision_job_id"] == "elsewhere"
    assert world.job_bodies == []


@pytest.mark.parametrize("ends", [_refused_and_given, _onboarded_and_answered,
                                  _failed_and_answered, _superseded],
                         ids=lambda f: f.__name__.strip("_"))
def test_the_registry_holds_nothing_once_the_job_has_nothing_to_answer(world, ends):
    ends(world)
    assert SID not in _registry(), _registry()


def test_a_refusal_for_an_onboarding_started_over_since_is_dropped_not_given(world):
    _refused_once(world)
    assert _agent_tools().reset_system(SID).get("status") == "session_cleared"
    world.discover_tables = copy.deepcopy(TABLES)
    _chat_until_resolved()
    with onboard._provision_in_flight(SID):              # the wizard provisions it meanwhile
        busy = _agent_tools().provision_system(SID)
    assert busy.get("reason") == "provisioning", busy   # not the old refusal
    assert SID not in _registry(), _registry()
