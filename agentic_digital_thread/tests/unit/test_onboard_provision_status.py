# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``GET /api/onboard/provision/status/{job_id}`` — the done payload the wizard
reads its provision statistics from.

Live finding (2026-09-18 workshop test): every SUCCESSFUL provision ended with
"Provisioned — completion confirmed; statistics could not be recovered", even
though ``_do_provision`` returned ``neptune_nodes_created`` /
``identity_rules_created``. Root cause: the route returned
``{"status": "done", **result}`` and the provision result carries its OWN
``"status": "provisioned"`` — the spread overrode ``done``. The wizard only
recognises ``status == "done"``, so it kept polling; the route had already
DELETED the job on that first read, the next poll got ``Unknown job_id`` and
the wizard fell back to the catalog ("onboarded" → ``stats_unavailable``).
The seed harness never noticed because it polls the catalog, not the job.

Two fixes, both pinned here: the status key is ``done`` regardless of what the
result carries (the result's own status rides along as ``provision_status``),
and a completed record survives re-reads for a grace window instead of being
deleted on the first read (StrictMode double effects, duplicate polls, edge
retries all re-read).
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import agents.onboard as onboard
from agents.job_store import JobStore

RESULT = {
    "status": "provisioned",
    "tool_name": "query_sys_a",
    "mcp_url": "",
    "neptune_nodes_created": 412,
    "identity_rules_created": 3,
    "identity_rules_rejected": 0,
    "join_paths": [],
    "value_mappings": [],
}


def _store():
    return JobStore("provision", table=None)


def _status(job_id):
    return asyncio.run(onboard.onboard_provision_status(job_id))


def test_done_payload_reports_status_done_and_carries_the_counters():
    store = _store()
    store["job-1"] = {"status": "done", "result": dict(RESULT)}
    with patch.object(onboard, "_provision_jobs", store):
        res = _status("job-1")
    assert res["status"] == "done", res
    assert res["provision_status"] == "provisioned"
    assert res["neptune_nodes_created"] == 412
    assert res["identity_rules_created"] == 3
    assert res["tool_name"] == "query_sys_a"
    assert "error" not in res


def test_done_record_survives_a_second_read_within_the_grace_window():
    store = _store()
    store["job-2"] = {"status": "done", "result": dict(RESULT)}
    with patch.object(onboard, "_provision_jobs", store):
        first = _status("job-2")
        second = _status("job-2")
    assert first["status"] == "done"
    assert second["status"] == "done", second
    assert second["neptune_nodes_created"] == 412
    assert "job-2" in store


def test_done_record_is_reaped_after_the_grace_window(monkeypatch):
    store = _store()
    store["job-3"] = {"status": "done", "result": dict(RESULT)}
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(onboard.time, "time", lambda: clock["now"])
    with patch.object(onboard, "_provision_jobs", store):
        assert _status("job-3")["status"] == "done"
        clock["now"] += onboard.PROVISION_DONE_GRACE_SECONDS + 1
        late = _status("job-3")
        assert late["status"] == "done"            # the reaping read still answers
        assert "job-3" not in store
        assert "Unknown job_id" in _status("job-3")["error"]


def test_error_record_follows_the_same_grace_semantics():
    store = _store()
    store["job-4"] = {"status": "error", "error": "provision_failed"}
    with patch.object(onboard, "_provision_jobs", store):
        first = _status("job-4")
        second = _status("job-4")
    assert first == {"status": "error", "error": "provision_failed"}
    assert second == first


def test_running_and_unknown_are_unchanged():
    store = _store()
    store["job-5"] = {"status": "running"}
    with patch.object(onboard, "_provision_jobs", store):
        assert _status("job-5") == {"status": "running"}
        assert "Unknown job_id" in _status("nope")["error"]
