# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the ASYNC single-system reset mode (?wait=false).

The UI removes a system through a background job so the HTTP response returns
immediately (a synchronous cascade can outlive the CloudFront 120 s origin
timeout — the husk-record failure mode) and polls
GET /api/onboard/reset/{id}/status, keeping the system's onboard/remove
actions disabled until the job leaves "running". These tests pin:

- wait=True (default) keeps the original synchronous contract
  (scripts/reonboard.py and the e2e suite depend on it);
- wait=False returns {"status": "started"} immediately, runs the SAME cascade
  in the background, and transitions status none -> running -> done;
- a second wait=False while running returns "already-running" (no double
  cascade);
- a failing cascade surfaces state="error" with the exception text (never
  swallowed);
- the registry is a ``JobStore("reset")`` (not a process-local dict): with a
  shared backend a status poll on ANOTHER replica sees the job state instead
  of "none" (which re-opened the re-onboard race that strands a husk record),
  and with NO backend / a failing backend it degrades fail-soft to in-memory
  (offline unit lane) with the stable 'job_store_fallback' warning.

All side effects are mocked (synthetic sys-a1 id, same fixtures style as
test_onboard_reset_labels.py). Fully offline — the shared backend is a fake
in-memory DynamoDB Table double (same pattern as test_job_store.py).
"""
import asyncio
import time
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("GRAPH_IDENTIFIER", "g-unittest")

import agents.onboard as onboard  # noqa: E402
from agents.job_store import JobStore  # noqa: E402


@pytest.fixture(autouse=True)
def _no_proposal_store(monkeypatch):
    """The offboard cascade now retires the system's pending proposals (a
    DynamoDB write). These tests pin other steps, so stub that seam here;
    tests/unit/test_offboard_retirement.py covers it directly."""
    import agents.offboard_retirement as retirement
    monkeypatch.setattr(retirement, "retire_pending_for_offboarded_systems", lambda ids: 0)


@pytest.fixture(autouse=True)
def _fresh_reset_jobs(monkeypatch):
    """Isolate each test with a fresh, memory-only JobStore (no ``.clear()`` on
    JobStore by design — the store is instance-scoped, so swap the instance)."""
    store = JobStore("reset", table=None)  # explicit no-backend: pure in-memory
    monkeypatch.setattr(onboard, "_RESET_JOBS", store)
    yield store


class _FakeTable:
    """Minimal shared DynamoDB Table double (same shape as test_job_store.py)."""

    def __init__(self, store: dict):
        self._store = store  # shared dict simulates the durable table

    def put_item(self, Item, **_kw):  # noqa: N803 (boto3 kwarg)
        self._store[Item["system_id"]] = dict(Item)

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803
        item = self._store.get(Key["system_id"])
        return {"Item": item} if item is not None else {}

    def delete_item(self, Key, **_kw):  # noqa: N803
        self._store.pop(Key["system_id"], None)


class _BrokenTable:
    """Backend double whose every call raises."""

    def put_item(self, Item, **_kw):  # noqa: N803
        raise RuntimeError("dynamodb unavailable")

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803
        raise RuntimeError("dynamodb unavailable")

    def delete_item(self, Key, **_kw):  # noqa: N803
        raise RuntimeError("dynamodb unavailable")


def _fake_catalog(system_id):
    fake = MagicMock()
    fake.get.return_value = {"id": system_id, "status": "onboarded"}
    fake.remove.return_value = True
    return fake


def _patched(system_id, cleanup=None):
    """Context managers stack: every reset side effect mocked."""
    if cleanup is None:
        cleanup = MagicMock(return_value={"runtimes_awaited": [], "targets_awaited": [],
                                          "wait_timed_out": False})
    return (
        patch.object(onboard, "neptune_write", MagicMock()),
        patch.object(onboard, "catalog", _fake_catalog(system_id)),
        patch.object(onboard, "_agent_manager", MagicMock()),
        patch.object(onboard, "remove_system_agent", MagicMock()),
        patch.object(onboard, "_reset_cleanup_agentcore_sync", cleanup),
    )


def _run(coro):
    return asyncio.run(coro)


async def _drive_to_terminal(system_id="sys-a1", cleanup=None):
    """Start a wait=False reset and spin the loop until the job leaves 'running'."""
    patches = _patched(system_id, cleanup=cleanup)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        started = await onboard.onboard_reset_system(system_id, wait=False)
        status = await onboard.onboard_reset_status(system_id)
        # Deadline-bounded, with a REAL sleep. `asyncio.sleep(0)` only yields to
        # the event loop, so a fixed number of bare yields cannot reliably let
        # work running off-loop finish: the job stayed 'running' on a loaded
        # Linux CI runner while passing on a developer machine. Wall-clock waits
        # let the worker actually progress, and the deadline keeps it bounded.
        deadline = time.monotonic() + 10.0
        while status["state"] == "running" and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
            status = await onboard.onboard_reset_status(system_id)
    return started, status


def test_sync_default_returns_full_reset_payload():
    """wait defaults to True: response carries the completed cascade result."""
    patches = _patched("sys-a1")
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = _run(onboard.onboard_reset_system("sys-a1"))
    assert result["status"] == "reset"
    assert result["system_id"] == "sys-a1"


def test_async_mode_starts_job_and_reaches_done():
    """wait=False: immediate 'started', then the polled status reaches 'done'
    with the cascade result attached."""
    started, status = _run(_drive_to_terminal())
    assert started == {"status": "started", "system_id": "sys-a1"}
    assert status["state"] == "done", status
    assert status["result"]["status"] == "reset"


def test_async_mode_guards_double_start(_fresh_reset_jobs):
    """A second wait=False while a job is running must not start a second
    cascade."""
    _fresh_reset_jobs["sys-a1"] = {"state": "running"}

    async def _drive():
        patches = _patched("sys-a1")
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            return await onboard.onboard_reset_system("sys-a1", wait=False)

    result = _run(_drive())
    assert result == {"status": "already-running", "system_id": "sys-a1"}


def test_async_mode_surfaces_cascade_error():
    """A failing cascade lands state='error' with a public category message and
    a reference; the UI renders it on the card instead of silently unblocking,
    and the exception text stays in the server log."""

    def _boom(sid, *_deadline):
        raise RuntimeError("gateway exploded")

    _, status = _run(_drive_to_terminal(cleanup=MagicMock(side_effect=_boom)))
    assert status["state"] == "error", status
    assert "Reference: " in status["error"] and "gateway exploded" not in status["error"]


def test_status_unknown_system_returns_none_state():
    status = _run(onboard.onboard_reset_status("never-reset"))
    assert status == {"state": "none", "system_id": "never-reset"}


# --------------------------------------------------------------------------- #
# JobStore backing (replica-safe job state)
# --------------------------------------------------------------------------- #

def test_registry_is_a_reset_jobstore():
    """The registry must be JobStore-backed (kind='reset'), not a plain dict —
    a process-local dict re-opens the husk-strand re-onboard race on
    multi-replica deploys (see agents/job_store.py module docstring).
    The autouse fixture swaps the instance, so pin the SOURCE declaration."""
    import ast
    import inspect
    tree = ast.parse(Path(inspect.getfile(onboard)).read_text())
    decl = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "_RESET_JOBS" for t in node.targets)
    )
    assert isinstance(decl.value, ast.Call), "must construct JobStore, not a dict"
    assert getattr(decl.value.func, "id", None) == "JobStore"
    assert decl.value.args and decl.value.args[0].value == "reset"


def test_status_poll_on_another_replica_sees_running(monkeypatch):
    """A status poll routed to a DIFFERENT replica (fresh store, shared
    backend) must see 'running' — not 'none' — while the delete is running."""
    shared: dict = {}
    replica_a = JobStore("reset", table=_FakeTable(shared))
    replica_b = JobStore("reset", table=_FakeTable(shared))
    replica_a["sys-a1"] = {"state": "running"}

    monkeypatch.setattr(onboard, "_RESET_JOBS", replica_b)
    status = _run(onboard.onboard_reset_status("sys-a1"))
    assert status == {"system_id": "sys-a1", "state": "running"}


def test_terminal_state_visible_across_replicas(monkeypatch):
    """running→done written on one replica is readable on another (poll after
    an LB re-route or an app restart mid-reset must not report 'none')."""
    shared: dict = {}
    replica_a = JobStore("reset", table=_FakeTable(shared))
    monkeypatch.setattr(onboard, "_RESET_JOBS", replica_a)
    _, status = _run(_drive_to_terminal())
    assert status["state"] == "done"

    replica_b = JobStore("reset", table=_FakeTable(shared))
    monkeypatch.setattr(onboard, "_RESET_JOBS", replica_b)
    status_b = _run(onboard.onboard_reset_status("sys-a1"))
    assert status_b["state"] == "done"
    assert status_b["result"]["status"] == "reset"


def test_a_broken_backend_starts_no_removal(monkeypatch):
    """The removal's running record is what every app task, and every
    onboarding writer, reads to know a removal runs. This test used to pin
    the opposite: a removal whose shared table was broken degraded to this
    process's memory and ran, so another task answered its status ``none``
    and could let a re-onboard race it. Now the record is written strictly
    before anything is removed, so a broken table starts nothing (503,
    nothing removed, retryable), and a status that cannot be read is a 503,
    never ``none``. A store with no shared table configured (local, unit)
    keeps its memory-only behaviour (test_status_*)."""
    monkeypatch.setattr(onboard, "_RESET_JOBS", JobStore("reset", table=_BrokenTable()))
    with pytest.raises(HTTPException) as err:
        _run(_drive_to_terminal())
    assert err.value.status_code == 503 and "Nothing was removed" in err.value.detail
    with pytest.raises(HTTPException) as err:
        _run(onboard.onboard_reset_status("sys-a1"))
    assert err.value.status_code == 503


class TestPostResetDemoReregistration:
    """Reset of a BUNDLED demo system self-heals to 'registered' (seeded envs).

    Without it, a reset demo system vanishes from the catalog until the next
    ECS bounce (startup is the only other registrar) — breaking the
    reset → re-onboard flow the e2e clean-slate branch drives."""

    def _patch_env(self, monkeypatch, *, enabled=True, connect=None):
        import agents.onboard as ob
        monkeypatch.setattr("agents.demo.is_enabled", lambda: enabled)
        calls = []

        def fake_connect(**kw):
            calls.append(kw)
            return connect if connect is not None else {"status": "created"}

        monkeypatch.setattr("agents.onboard_agent.register_demo_system", fake_connect)
        monkeypatch.setattr("agents.demo.db_routing.demo_pg_dsn",
                            lambda schema: f"postgresql://demo/{schema}")
        return ob, calls

    def test_demo_system_reregisters_with_manifest_values(self, monkeypatch):
        ob, calls = self._patch_env(monkeypatch)
        assert ob._reregister_demo_system("cmms") is True
        assert len(calls) == 1
        kw = calls[0]
        assert kw["system_id"] == "cmms"
        assert kw["name"] and kw["connection_string"].startswith("postgresql://demo/")

    def test_base_mode_is_noop(self, monkeypatch):
        ob, calls = self._patch_env(monkeypatch, enabled=False)
        assert ob._reregister_demo_system("cmms") is False
        assert calls == []

    def test_non_demo_system_stays_removed(self, monkeypatch):
        ob, calls = self._patch_env(monkeypatch)
        assert ob._reregister_demo_system("my-custom-athena") is False
        assert calls == []

    def test_connect_error_reports_false_never_raises(self, monkeypatch, caplog):
        ob, _ = self._patch_env(monkeypatch, connect={"error": "boom"})
        caplog.set_level("WARNING", logger="agents.onboard")
        assert ob._reregister_demo_system("cmms") is False
        assert "re-registration" in caplog.text


# ── full-flag semantics + serialized removals (2026-08-06) ───────────────────


def test_default_keeps_registration_and_flips_to_registered():
    """DEFAULT (full absent) mirrors reset-all: the catalog registration is
    KEPT (no remove call), the system flips back to 'registered', and the
    payload says so."""
    patches = _patched("sys-a1")
    with patches[0], patches[1] as _p, patches[2], patches[3], patches[4]:
        cat = onboard.catalog
        result = _run(onboard.onboard_reset_system("sys-a1"))
    assert result["status"] == "reset"
    assert result["kept_registration"] is True
    assert result["removed"] is False
    cat.update_status.assert_called_once_with("sys-a1", "registered")
    cat.remove.assert_not_called()


def test_full_true_removes_registration_entirely():
    """full=true preserves the prior behavior: registration removed, demo
    re-registration attempted."""
    patches = _patched("sys-a1")
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        cat = onboard.catalog
        result = _run(onboard.onboard_reset_system("sys-a1", full=True))
    assert result["kept_registration"] is False
    assert result["removed"] is True
    cat.remove.assert_called_once_with("sys-a1")
    cat.update_status.assert_not_called()


def test_concurrent_removal_of_other_system_is_409(_fresh_reset_jobs):
    """While system A's cascade runs, starting system B's removal is refused
    with 409 naming A — removals are serialized (shared gateway, minutes-long
    cascade). Same-system dedupe (already-running) is unchanged."""
    from fastapi import HTTPException

    _fresh_reset_jobs[onboard._ACTIVE_RESET_KEY] = {"system_id": "sys-a1"}
    _fresh_reset_jobs["sys-a1"] = {"state": "running"}

    async def _drive():
        patches = _patched("sys-b2")
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            return await onboard.onboard_reset_system("sys-b2", wait=False)

    with pytest.raises(HTTPException) as exc:
        _run(_drive())
    assert exc.value.status_code == 409
    assert "sys-a1" in exc.value.detail


def test_stale_active_marker_self_heals(_fresh_reset_jobs):
    """A marker whose job is no longer running must NOT block new removals
    (a crashed replica cannot wedge the whole removal surface)."""
    _fresh_reset_jobs[onboard._ACTIVE_RESET_KEY] = {"system_id": "sys-a1"}
    _fresh_reset_jobs["sys-a1"] = {"state": "done"}

    async def _drive():
        patches = _patched("sys-b2")
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            return await onboard.onboard_reset_system("sys-b2", wait=False)

    result = _run(_drive())
    assert result == {"status": "started", "system_id": "sys-b2"}


@pytest.mark.parametrize("wait", [False, True])
def test_two_concurrent_removals_start_exactly_one(monkeypatch, _fresh_reset_jobs, wait):
    """Two removals of DIFFERENT systems whose preflights overlap in two
    worker threads: exactly one starts, the other is refused with 409. The
    preflight now awaits a thread, so only the claim lock keeps the
    check-then-write of the removal marker atomic (the loop did it for free)."""
    import threading
    from fastapi import HTTPException

    both_checked = threading.Barrier(2, timeout=0.5)

    def fake_verify(system_id, full=False):
        try:
            both_checked.wait()  # hold each preflight until the other is in too
        except threading.BrokenBarrierError:
            pass
        return None

    cascades = []

    async def fake_job(system_id, full=False):
        cascades.append(system_id)

    async def fake_steps(system_id, full=False, **_kw):
        cascades.append(system_id)
        await asyncio.sleep(0.2)  # the marker stays set while it runs
        return {"status": "reset", "system_id": system_id}

    monkeypatch.setattr(onboard, "_reset_verify_onboarded", fake_verify)
    monkeypatch.setattr(onboard, "_run_reset_job", fake_job)
    monkeypatch.setattr(onboard, "_run_reset_steps", fake_steps)

    async def _drive():
        results = await asyncio.gather(
            onboard.onboard_reset_system("sys-a1", wait=wait),
            onboard.onboard_reset_system("sys-b2", wait=wait),
            return_exceptions=True)
        deadline = time.monotonic() + 5.0
        while not cascades and time.monotonic() < deadline:
            await asyncio.sleep(0.01)  # a started background job runs next
        await asyncio.sleep(0.05)  # room for a second cascade, if one started
        return results

    results = _run(_drive())
    refused = [r for r in results if isinstance(r, HTTPException)]
    accepted = [r for r in results if not isinstance(r, BaseException)]
    assert len(refused) == 1 and refused[0].status_code == 409, results
    assert len(accepted) == 1, results
    assert len(cascades) == 1, cascades
    winner = cascades[0]
    assert _fresh_reset_jobs.get(onboard._ACTIVE_RESET_KEY) == {"system_id": winner}
    assert winner in refused[0].detail
