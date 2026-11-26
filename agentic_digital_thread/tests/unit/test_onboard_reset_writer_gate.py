# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The reset-in-progress writer gate (workshop offboard→re-add safety).

An async (?wait=false) reset cascade deletes a system's graph subtree over many
DETACH DELETE statements. A discover/map/provision that starts mid-cascade
races those deletes and leaves a mixed old/new graph — the "weird state" this
gate exists to make impossible. The writers answer a structured
``reset_in_progress`` refusal until the job leaves ``running``.
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
from agents.job_store import JobStore  # noqa: E402
from agents.models import DiscoverRequest, MapRequest, ProvisionRequest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_jobs(monkeypatch):
    """Isolate the reset job registry per test (a memory-only JobStore)."""
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=None))
    yield


def _mark_running(system_id: str) -> None:
    onboard._RESET_JOBS[system_id] = {"state": "running"}


def test_gate_only_fires_while_running():
    assert onboard._reset_in_progress("cmms") is False
    _mark_running("cmms")
    assert onboard._reset_in_progress("cmms") is True
    onboard._RESET_JOBS["cmms"] = {"state": "done"}
    assert onboard._reset_in_progress("cmms") is False
    onboard._RESET_JOBS["cmms"] = {"state": "error", "error": "x"}
    assert onboard._reset_in_progress("cmms") is False
    assert onboard._reset_in_progress("") is False


def test_gate_fails_open_on_a_broken_job_store(monkeypatch):
    class Broken:
        def get(self, k):
            raise RuntimeError("store down")
    monkeypatch.setattr(onboard, "_RESET_JOBS", Broken())
    assert onboard._reset_in_progress("cmms") is False


def _run(coro):
    """Fresh loop per call — xdist workers may own (or have closed) the default loop."""
    return asyncio.new_event_loop().run_until_complete(coro)


def test_discover_refuses_mid_reset():
    _mark_running("cmms")
    result = _run(
        onboard._onboard_discover_integrity(DiscoverRequest(system_id="cmms")))
    assert result.get("reason") == "reset_in_progress"
    assert "being removed" in result.get("error", "")


def test_map_refuses_mid_reset():
    _mark_running("cmms")
    result = _run(onboard.onboard_map(MapRequest(system_id="cmms", tables=[])))
    assert result.get("reason") == "reset_in_progress"


def test_provision_refuses_mid_reset():
    _mark_running("cmms")
    result = _run(onboard.onboard_provision(ProvisionRequest(system_id="cmms")))
    assert result.get("reason") == "reset_in_progress"


def test_other_systems_are_unaffected(monkeypatch):
    """The gate is per-system: resetting cmms must not block onboarding erp."""
    _mark_running("cmms")
    assert onboard._reset_in_progress("erp") is False


def test_refusal_names_the_status_poll():
    """The refusal must be actionable: it points at the reset status route."""
    _mark_running("cmms")
    body = onboard._reset_conflict_or_none("cmms")
    assert "/api/onboard/reset/cmms/status" in body["error"]
