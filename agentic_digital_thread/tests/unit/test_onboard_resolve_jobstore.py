# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The onboarding ``/resolve`` async job state must be cross-replica visible.

Same root-cause class as the map/provision B1 fix: ``/resolve`` runs the
identity analysis in a BACKGROUND THREAD and returns a ``job_id`` the client
polls via ``/resolve/status/{job_id}``. When that job state was a per-PROCESS
in-memory ``dict`` a poll routed by the load balancer to a DIFFERENT replica
than the one that started the job returned ``"Unknown job_id"``. Migrating
``_resolve_jobs`` to the shared :class:`JobStore` (kind ``"resolve"``) makes a
job written on one replica readable on any other — mirroring ``_map_jobs`` /
``_provision_jobs`` exactly.

Fully offline: a fake in-memory DynamoDB Table double stands in for the shared
store; no AWS, no network. Synthetic identifiers only.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from agents import onboard
from agents.job_store import JobStore


class _FakeTable:
    """Minimal shared DynamoDB Table double (put_item/get_item/delete_item)."""

    def __init__(self, store: dict):
        self._store = store  # shared dict simulates the durable, cross-instance table

    def put_item(self, Item):  # noqa: N803 (boto3 kwarg)
        self._store[Item["system_id"]] = dict(Item)

    def get_item(self, Key):  # noqa: N803
        item = self._store.get(Key["system_id"])
        return {"Item": item} if item is not None else {}

    def delete_item(self, Key):  # noqa: N803
        self._store.pop(Key["system_id"], None)


def test_resolve_jobs_is_shared_jobstore():
    """``_resolve_jobs`` is the durable, kind-namespaced JobStore (not a dict)."""
    assert isinstance(onboard._resolve_jobs, JobStore)
    assert onboard._resolve_jobs._kind == "resolve"


def test_resolve_status_reads_done_job_written_by_another_replica():
    """A resolve job COMPLETED on one replica is returned by a /resolve/status
    poll on ANOTHER replica, then removed."""
    shared: dict = {}
    poller = JobStore("resolve", table=_FakeTable(shared))
    worker = JobStore("resolve", table=_FakeTable(shared))  # "replica that ran the job"

    worker["job-x"] = {"status": "done", "result": {"rules": [{"id": "r1"}]}}

    with patch.object(onboard, "_resolve_jobs", poller):
        res = asyncio.run(onboard.onboard_resolve_status("job-x"))

    assert res["status"] == "done"
    assert res["rules"] == [{"id": "r1"}]
    # Consumed: gone from the shared backend afterward.
    assert poller.get("job-x") is None


def test_resolve_status_reads_error_job_written_by_another_replica():
    shared: dict = {}
    poller = JobStore("resolve", table=_FakeTable(shared))
    worker = JobStore("resolve", table=_FakeTable(shared))
    worker["job-e"] = {"status": "error", "error": "boom"}

    with patch.object(onboard, "_resolve_jobs", poller):
        res = asyncio.run(onboard.onboard_resolve_status("job-e"))

    assert res == {"status": "error", "error": "resolve_failed"}
    assert poller.get("job-e") is None


def test_resolve_status_running_job_visible_across_replicas():
    shared: dict = {}
    poller = JobStore("resolve", table=_FakeTable(shared))
    worker = JobStore("resolve", table=_FakeTable(shared))
    worker["job-r"] = {"status": "running"}

    with patch.object(onboard, "_resolve_jobs", poller):
        res = asyncio.run(onboard.onboard_resolve_status("job-r"))

    assert res == {"status": "running"}


def test_resolve_status_unknown_job_reports_error():
    poller = JobStore("resolve", table=None)  # no backend -> empty
    with patch.object(onboard, "_resolve_jobs", poller):
        res = asyncio.run(onboard.onboard_resolve_status("nope"))
    assert "error" in res
    assert "nope" in res["error"]


def test_run_resolve_job_persists_result_to_shared_store():
    """The background worker writes the completed result through the shared store,
    so a subsequent cross-replica status poll can read it."""
    shared: dict = {}
    worker = JobStore("resolve", table=_FakeTable(shared))
    body = object()  # _do_resolve is patched, so the body is never inspected
    # Identifier-shaped on both sides: the evidence gate (resolve_gates) lets an
    # unmeasured rule through only when both field names look like identifiers.
    rule = {"id": "r1", "concept": "work-order", "source_field": "t.wo_number",
            "target_system": "sys-b", "target_field": "u.order_ref"}

    with patch.object(onboard, "_resolve_jobs", worker), \
         patch.object(onboard, "_do_resolve", return_value={"rules": [rule]}):
        onboard._run_resolve_job("job-w", body)

    # Readable from an independent "replica" store over the same shared backend.
    # (Wave 4, item E: the worker now ALWAYS threads the additive `join_candidates`
    # key onto the result — [] here because the body double has no system_id, so
    # the best-effort discovery pass is skipped. The evidence gate adds the
    # additive `rejected_rules` key the same way, and the stage timer the
    # additive `timings` key: durations and counts only.)
    reader = JobStore("resolve", table=_FakeTable(shared))
    stored = reader.get("job-w")
    timings = stored["result"].pop("timings")
    assert stored == {
        "status": "done",
        "result": {"rules": [rule], "rejected_rules": [], "join_candidates": [],
                   "resolve_job_id": "job-w"},
    }
    assert set(timings) == {"total_s", "stages_s", "counts"}
    assert [s for s in timings["stages_s"] if "." not in s][:3] == [
        "concept_pass", "overlap_gate", "affinity_pass"]
    assert timings["counts"]["rules"] == 1
