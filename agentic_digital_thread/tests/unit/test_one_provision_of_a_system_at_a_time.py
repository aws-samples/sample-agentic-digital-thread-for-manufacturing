# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""One provision of a system at a time, whoever starts it.

The wizard's ``POST /provision`` (and ``/provision/confirm``) and the chat's
``provision_system`` run the same provision in this process, and two of them
for one system would interleave its replace-style schema and rule writes and
its post-passes. They share one per-system claim, taken when the request is
accepted: whichever starts second is refused (the wizard with a 409, the chat
with an error that says so), and the claim is released however the first one
ends. Offline, on the fixture of test_chat_provision_is_the_wizard_provision.py.
"""
from __future__ import annotations

import asyncio
import copy
import threading

import pytest
from fastapi import HTTPException

import agents.onboard as onboard
from agents.models import DiscoverRequest, ProvisionRequest
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: F401 - fixture
    SID, TABLES, World, _agent_tools, _block_the_job, _chat_provision_answer,
    _chat_until_resolved, _wait_for, world)


def _wizard_request(world) -> ProvisionRequest:
    """The body the wizard posts after its own discover and map."""
    entry = world.catalog.get(SID)
    disc = asyncio.run(onboard.onboard_discover(DiscoverRequest(
        system_id=SID, connection_string=entry["connection_string"],
        connection_type=entry["connection_type"], connection_config={})))
    rows = [{"field": f"get_tags.{c['name']}", "table": "get_tags", "concept": "sensor-reading",
             "confidence": 0.9, "mappings": [{"concept": "sensor-reading", "role": "measure",
                                              "confidence": 0.9}]}
            for c in TABLES[0]["columns"]]
    rows = onboard._attach_map_provenance(
        {"mappings": rows}, onboard._mapping_schema_metadata(disc["tables"]))["mappings"]
    return ProvisionRequest(
        system_id=SID, system_name=entry["name"], plant=entry["plant"],
        schema=disc.get("schema_name") or "", rules=[], mappings=rows,
        connection_type=entry["connection_type"], connection_string=entry["connection_string"],
        connection_config={})


def _wizard_start(world) -> dict:
    return asyncio.run(onboard.onboard_provision(_wizard_request(world)))


def _wizard_end(job_id: str) -> dict:
    return _wait_for(lambda: (lambda r: r if r.get("status") != "running" else None)(
        asyncio.run(onboard.onboard_provision_status(job_id))))


def _refused_409(fn) -> HTTPException:
    with pytest.raises(HTTPException) as refused:
        fn()
    assert refused.value.status_code == 409, refused.value
    assert "being provisioned" in str(refused.value.detail)
    return refused.value


def _gate_the_wizard_job(world):
    """Hold the wizard's job thread before it runs anything: the request has
    been accepted and answered, its job has not started yet."""
    gate = threading.Event()
    real = onboard._run_provision_job

    def _late(*args, **kwargs):
        gate.wait(10)
        return real(*args, **kwargs)
    world.mp.setattr(onboard, "_run_provision_job", _late)
    return gate.set


# ── whichever starts second is refused ──────────────────────────────────────

def test_a_wizard_provision_during_a_chat_provision_is_refused(world):
    _chat_until_resolved()
    release = _block_the_job(world)
    try:
        assert _agent_tools().provision_system(SID)["status"] == "provisioning"
        assert world.entered.wait(10)
        _refused_409(lambda: _wizard_start(world))
    finally:
        release()
    _wait_for(lambda: world.store.get_session(SID)["status"] == "onboarded")
    assert len(world.job_bodies) == 1


def test_a_chat_provision_once_the_wizard_request_is_accepted_is_refused(world):
    _chat_until_resolved()
    release = _gate_the_wizard_job(world)
    try:
        started = _wizard_start(world)
        assert started.get("status") == "running", started
        chat = _agent_tools().provision_system(SID)
    finally:
        release()
    assert chat.get("reason") == "provisioning" and "status" not in chat, chat
    assert "being provisioned" in chat["error"]
    assert world.store.get_session(SID)["status"] == "resolved"
    assert _wizard_end(started["job_id"])["status"] == "done"
    assert len(world.job_bodies) == 1


def test_a_second_wizard_provision_is_refused(world):
    release = _gate_the_wizard_job(world)
    try:
        first = _wizard_start(world)
        _refused_409(lambda: _wizard_start(world))
    finally:
        release()
    assert _wizard_end(first["job_id"])["status"] == "done"
    assert len(world.job_bodies) == 1


def test_a_confirm_during_a_chat_provision_is_refused(world):
    _chat_until_resolved()
    body = _wizard_request(world).model_dump(by_alias=True)
    release = _block_the_job(world)
    try:
        _agent_tools().provision_system(SID)
        assert world.entered.wait(10)
        out = onboard.onboard_provision_confirm({"provision_body": body})
    finally:
        release()
    assert out.get("status") == "error" and out.get("reason") == "provisioning", out
    assert "being provisioned" in out["detail"] and out["retryable"] is True
    _wait_for(lambda: world.store.get_session(SID)["status"] == "onboarded")
    assert len(world.job_bodies) == 1


# ── the claim is released however the first one ends ────────────────────────

def _wizard_done(world):
    assert _wizard_end(_wizard_start(world)["job_id"])["status"] == "done"


def _wizard_error(world):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(onboard, "_do_provision", lambda body: {"error": "x"})
        assert _wizard_end(_wizard_start(world)["job_id"])["status"] == "error"


def _thread_exceptions(mp) -> list:
    """Exceptions that escape a thread from here on (none may)."""
    escaped: list = []
    mp.setattr(threading, "excepthook", lambda args: escaped.append(args.exc_value))
    return escaped


def _wizard_raises(world):
    with pytest.MonkeyPatch.context() as mp:
        escaped = _thread_exceptions(mp)
        mp.setattr(onboard, "_run_provision_job_steps", _raise_from_the_job)
        started = _wizard_start(world)
        assert _wizard_end(started["job_id"]) == {"status": "error", "error": "provision_failed"}
        assert escaped == [], escaped


def _raise_from_the_job(*_a, **_k):
    raise RuntimeError("the job died")


def _chat_raises(world):
    _chat_until_resolved()
    with pytest.MonkeyPatch.context() as mp:
        escaped = _thread_exceptions(mp)
        mp.setattr(onboard, "_run_provision_job_steps", _raise_from_the_job)
        started = _agent_tools().provision_system(SID)
        _wait_for(lambda: world.store.get_session(SID)["status"] == "failed")
        _wait_for(lambda: (onboard._provision_jobs.get(started["job_id"]) or {}).get("status")
                  == "error")
        assert escaped == [], escaped


def _chat_done(world):
    _chat_until_resolved()
    _agent_tools().provision_system(SID)
    _wait_for(lambda: world.store.get_session(SID)["status"] == "onboarded")


def _chat_failed(world):
    _chat_until_resolved()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(onboard, "_do_provision", lambda body: {"error": "x"})
        _agent_tools().provision_system(SID)
        _wait_for(lambda: world.store.get_session(SID)["status"] == "failed")


def _chat_cannot_read_the_source(world):
    _chat_until_resolved()
    world.discover_error = "Failed to connect to MCP server"
    assert _chat_provision_answer().get("reason") == "discover_failed"
    world.discover_error = ""


def _chat_schema_changed(world):
    _chat_until_resolved()
    world.discover_tables = [{**copy.deepcopy(TABLES[0]), "columns": TABLES[0]["columns"][:1]}]
    assert _chat_provision_answer().get("reason") == "schema_changed"
    world.discover_tables = copy.deepcopy(TABLES)


@pytest.mark.parametrize("first", [_wizard_done, _wizard_error, _wizard_raises, _chat_done,
                                   _chat_failed, _chat_raises, _chat_cannot_read_the_source,
                                   _chat_schema_changed], ids=lambda f: f.__name__.strip("_"))
def test_the_claim_is_released_and_taken_again(world, first):
    first(world)
    assert not onboard._provision_running(SID)
    release = _gate_the_wizard_job(world)
    try:
        nxt = _wizard_start(world)                       # admitted
        _refused_409(lambda: _wizard_start(world))       # and it holds the claim
    finally:
        release()
    assert _wizard_end(nxt["job_id"])["status"] == "done"
    assert not onboard._provision_running(SID)


# ── a job that raises still ends ────────────────────────────────────────────

def test_a_wizard_job_that_raises_ends_in_error_and_the_poll_says_so(world):
    with pytest.MonkeyPatch.context() as mp:
        escaped = _thread_exceptions(mp)
        mp.setattr(onboard, "_run_provision_job_steps", _raise_from_the_job)
        started = _wizard_start(world)
        _wait_for(lambda: (onboard._provision_jobs.get(started["job_id"]) or {}).get("status")
                  != "running")
        assert onboard._provision_jobs.get(started["job_id"]) == {
            "status": "error", "error": "provision_failed"}
        polled = asyncio.run(onboard.onboard_provision_status(started["job_id"]))
        assert polled == {"status": "error", "error": "provision_failed"}, polled
        assert escaped == [], escaped
    assert not onboard._provision_running(SID)


def test_a_chat_job_that_raises_ends_failed_and_says_so(world):
    _chat_raises(world)
    checked = _agent_tools().provision_system(SID)
    assert checked["status"] == "failed" and checked["reason"] == "provision_failed", checked
    assert not onboard._provision_running(SID)
