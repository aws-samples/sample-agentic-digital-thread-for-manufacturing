# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The chat's provision reads the schema again in its job, not in the call.

``provision_system`` checks the id, the catalog entry and the session, takes
the system's provision claim, starts the job and answers ``provisioning`` at
once. The job reads the schema again (the wizard's discover, which for an MCP
server calls each of its tools) and checks the session's mappings and rules
against it before it claims the session or writes anything. A refusal it
finds (the schema cannot be read, a mapped field or a rule source is gone)
ends the job with nothing written, the claim released and the session still
``resolved``; the next call answers that refusal, with the text it always
had. Offline, on the fixture of test_chat_provision_is_the_wizard_provision.py.
"""
from __future__ import annotations

import copy
import json
import logging
import threading

import agents.onboard as onboard
import agents.onboard_agent as oa
import agents.onboard_chat_lifecycle as lifecycle
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: F401 - fixture
    JOB_WAIT, SID, TABLES, World, _agent_tools, _chat_until_resolved, _wait_for, world)

#: The bound on an answer that comes at once. The schema read is held for
#: :data:`JOB_WAIT`, so an answer that waits for it never comes in time.
AT_ONCE = 10.0

GONE = "Ent/Site/Line1/StationA/Good"


def _answer_within(fn, seconds: float) -> dict:
    """``fn()`` on a thread (as the agent runs a tool); its answer, or ``{}``
    when none came within ``seconds``."""
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(fn()), daemon=True)
    t.start()
    t.join(seconds)
    return dict(out)


def _slow_schema_read(world):
    """The connector's read of the system waits on an event."""
    reading, release, reads = threading.Event(), threading.Event(), []
    real = world._discover

    def _slow(entry, system_id=""):
        reads.append(entry)
        reading.set()
        release.wait(JOB_WAIT)
        return real(entry, system_id=system_id)
    world.mp.setattr(oa, "_discover_mcp", _slow)
    return reading, release, reads


def _without(column: str) -> list:
    tables = copy.deepcopy(TABLES)
    tables[0]["columns"] = [c for c in tables[0]["columns"] if c["name"] != column]
    return tables


# ── the call does not wait for the schema read ─────────────────────────────

def test_the_first_call_answers_while_the_schema_is_still_being_read(world):
    _chat_until_resolved()
    reading, release, reads = _slow_schema_read(world)
    provision = _agent_tools().provision_system
    before = len(world.writes)
    try:
        started = _answer_within(lambda: provision(SID), AT_ONCE)
        assert started.get("status") == "provisioning" and started.get("job_id"), started
        assert reading.wait(JOB_WAIT)
        assert world.store.get_session(SID)["status"] == "resolved", "claimed before the check"
        assert world.job_bodies == [] and len(world.writes) == before
        assert onboard._provision_running(SID), "the provision claim is not held"
    finally:
        release.set()
    _wait_for(lambda: world.store.get_session(SID)["status"] == "onboarded")
    assert len(world.job_bodies) == 1 and len(reads) == 1


def test_a_check_while_the_schema_is_read_answers_provisioning_and_starts_nothing(world):
    _chat_until_resolved()
    reading, release, reads = _slow_schema_read(world)
    provision = _agent_tools().provision_system
    try:
        first = _answer_within(lambda: provision(SID), AT_ONCE)
        assert reading.wait(JOB_WAIT)
        second = _answer_within(lambda: provision(SID), AT_ONCE)
    finally:
        release.set()
    assert first.get("status") == "provisioning", first
    assert second.get("status") == "provisioning", second
    assert second.get("job_id") == first["job_id"], (first, second)
    assert "Still provisioning" in second["message"], second
    _wait_for(lambda: world.store.get_session(SID)["status"] == "onboarded")
    assert len(reads) == 1 and len(world.job_bodies) == 1


def test_a_check_waits_for_the_schema_read_and_the_provision_to_end(monkeypatch):
    world = World(monkeypatch, check_wait=JOB_WAIT)
    _chat_until_resolved()
    reading, release, _reads = _slow_schema_read(world)
    provision = _agent_tools().provision_system
    try:
        first = _answer_within(lambda: provision(SID), AT_ONCE)
        assert first.get("status") == "provisioning", first
        assert reading.wait(JOB_WAIT)
        threading.Timer(0.3, release.set).start()
        checked = provision(SID)
    finally:
        release.set()
    assert checked.get("status") == "onboarded", checked     # one check, the finished answer


# ── a refusal the job finds is the next answer, with nothing written ───────

def _refused_by_the_job(world, caplog) -> tuple:
    """The first call (started), then, once its job ended, the next call."""
    caplog.set_level(logging.WARNING, logger="agents.onboard_chat_lifecycle")
    provision = _agent_tools().provision_system
    before = len(world.writes)
    started = provision(SID)
    assert started.get("status") == "provisioning" and started.get("job_id"), started
    _wait_for(lambda: not onboard._provision_running(SID))
    assert world.store.get_session(SID)["status"] == "resolved"
    assert world.job_bodies == [] and world.manager.started == []
    assert len(world.writes) == before, "the refused provision wrote to the graph"
    assert onboard._provision_jobs.get(started["job_id"])["status"] == "refused"
    return started, provision(SID)


def _refusal_logs(caplog) -> list:
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "refused" in r.getMessage()]


def test_a_schema_change_the_job_finds_is_the_next_answer(world, caplog):
    _chat_until_resolved()
    world.discover_tables = _without(GONE)
    started, refused = _refused_by_the_job(world, caplog)
    assert refused == lifecycle._schema_changed(SID, [f"get_tags.{GONE}"], []), refused
    logs = _refusal_logs(caplog)
    assert len(logs) == 1 and SID in logs[0] and "schema_changed" in logs[0], logs
    assert f"get_tags.{GONE}" in logs[0], logs


def test_a_rule_source_the_job_finds_missing_is_the_next_answer(world, caplog):
    _chat_until_resolved()
    field = "get_tags.Ent/Site/Line1/StationA/Retired"
    rule = {"concept": "quality-metric", "source_system": SID, "source_field": field,
            "source_table": "get_tags", "source_pattern": "", "target_system": "plant-mes",
            "target_field": "good_count", "target_table": "runs", "target_pattern": "",
            "confidence": 0.8, "reasoning": "shared counter"}
    world.table.items[SID]["rules"] = json.dumps([rule])
    _started, refused = _refused_by_the_job(world, caplog)
    assert refused == lifecycle._schema_changed(SID, [], [field]), refused
    assert not any("IdentityRule" in c for c in world.cyphers())
    assert len(_refusal_logs(caplog)) == 1


def test_a_source_the_job_cannot_read_is_the_next_answer(world, caplog):
    _chat_until_resolved()
    world.discover_error = "Failed to connect to MCP server"
    _started, refused = _refused_by_the_job(world, caplog)
    assert refused.get("reason") == "discover_failed" and "status" not in refused, refused
    assert refused["error"].startswith(f"The schema of '{SID}' could not be read again, so "
                                       "nothing was provisioned: "), refused
    assert "Failed to connect" in refused["error"]


def test_once_given_a_refusal_the_next_call_reads_the_schema_again(world, caplog):
    _chat_until_resolved()
    world.discover_tables = _without(GONE)
    started, _refused = _refused_by_the_job(world, caplog)
    world.discover_tables = copy.deepcopy(TABLES)
    again = _agent_tools().provision_system(SID)
    assert again.get("status") == "provisioning", again
    assert again.get("job_id") and again["job_id"] != started["job_id"], again
    _wait_for(lambda: world.store.get_session(SID)["status"] == "onboarded")


def test_a_refusal_is_not_given_after_the_onboarding_was_started_over(world, caplog):
    _chat_until_resolved()
    world.discover_tables = _without(GONE)
    caplog.set_level(logging.WARNING, logger="agents.onboard_chat_lifecycle")
    started = _agent_tools().provision_system(SID)
    assert started.get("status") == "provisioning", started
    _wait_for(lambda: not onboard._provision_running(SID))
    assert _agent_tools().reset_system(SID).get("status") == "session_cleared"
    world.discover_tables = copy.deepcopy(TABLES)
    _chat_until_resolved()
    fresh = _agent_tools().provision_system(SID)
    assert fresh.get("status") == "provisioning", fresh
    _wait_for(lambda: world.store.get_session(SID)["status"] == "onboarded")
