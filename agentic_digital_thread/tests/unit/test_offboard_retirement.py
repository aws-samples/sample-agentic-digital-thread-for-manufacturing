# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offboard completion is honest, and it retires the removed system's queue.

Live 2026-09-27 (throwaway ``synth-adr0063`` on the demo):
  * the async reset reported ``state: done`` with ``wait_timed_out: true`` while
    the VPC-mode runtime was still DELETING (it vanished ~3 min later) — a
    re-onboard at that point races the delete (the husk-record class);
  * 24 PENDING machine proposals (join_path/insight) still named the removed
    system in the steward queue.

Pinned here:
  1. a timed-out delete-wait raises ``ResetIncompleteError`` BEFORE the catalog
     step (registration kept => retryable); async => state ``error``, sync => 503;
  2. the async job waits with the long deadline, the sync path keeps 180 s;
  3. retirement supersedes only pending machine rows naming the system, never
     writes ``rejected``, spares steward rows, and loses races gracefully.

Offline — no AWS, no Neptune.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.offboard_retirement as retirement  # noqa: E402
import agents.onboard as onboard  # noqa: E402
from fastapi import HTTPException  # noqa: E402

_SID = "synth-x1"


@pytest.fixture(autouse=True)
def _fresh_jobs(monkeypatch):
    from agents.job_store import JobStore
    jobs = JobStore("reset", table=None)
    monkeypatch.setattr(onboard, "_RESET_JOBS", jobs)
    return jobs


def _cleanup(timed_out: bool) -> MagicMock:
    return MagicMock(return_value={
        "runtimes_awaited": ["adt_synth_x1"], "targets_awaited": [_SID],
        "wait_timed_out": timed_out})


def _cascade(cleanup, retire=None):
    """Patch every side effect of the cascade; returns (patches, catalog, retire)."""
    cat = MagicMock()
    cat.get.return_value = {"id": _SID, "status": "onboarded"}
    cat.remove.return_value = True
    retire = retire or MagicMock(return_value=3)
    patches = [
        patch.object(onboard, "neptune_write", MagicMock()),
        patch.object(onboard, "catalog", cat),
        patch.object(onboard, "_agent_manager", MagicMock()),
        patch.object(onboard, "remove_system_agent", MagicMock()),
        patch.object(onboard, "_reset_cleanup_agentcore_sync", cleanup),
        patch.object(retirement, "retire_pending_for_offboarded_systems", retire),
    ]
    return patches, cat, retire


def _with(patches, coro_fn):
    for p in patches:
        p.start()
    try:
        return asyncio.run(coro_fn())
    finally:
        for p in reversed(patches):
            p.stop()


# ── 1. honest completion ─────────────────────────────────────────────────────
def test_timed_out_wait_raises_before_catalog_step():
    patches, cat, retire = _cascade(_cleanup(timed_out=True))
    with pytest.raises(onboard.ResetIncompleteError, match="still deleting"):
        _with(patches, lambda: onboard._run_reset_steps(_SID, full=True))
    cat.remove.assert_not_called()
    # Only the removal_failed state, with its mode
    # (test_offboard_removal_marker_restart.py, test_removal_mode_restart.py):
    # the registration is kept and the card offers the retry.
    cat.mark_removal_failed.assert_called_once_with(_SID, True)
    cat.update_status.assert_not_called()
    retire.assert_not_called()


def _destructive(writes) -> list:
    """The graph writes that DELETE something (the removal marker is not one)."""
    return [c for c in writes.call_args_list if "DELETE" in str(c.args[0] if c.args else "")]


def test_completed_wait_retires_and_reports_count():
    patches, cat, retire = _cascade(_cleanup(timed_out=False))
    result = _with(patches, lambda: onboard._run_reset_steps(_SID, full=True))
    retire.assert_called_once_with([_SID])
    cat.remove.assert_called_once_with(_SID)
    assert result["proposals_retired"] == 3
    assert result["wait_timed_out"] is False


def test_sync_reset_surfaces_incomplete_as_503(_fresh_jobs):
    patches, _, _ = _cascade(_cleanup(timed_out=True))
    with pytest.raises(HTTPException) as exc:
        _with(patches, lambda: onboard.onboard_reset_system(_SID, wait=True, full=True))
    assert exc.value.status_code == 503
    assert _fresh_jobs[_SID]["state"] == "error"


def _drive_async(patches):
    async def _go():
        await onboard.onboard_reset_system(_SID, wait=False, full=True)
        deadline = time.monotonic() + 10.0
        status = await onboard.onboard_reset_status(_SID)
        while status["state"] == "running" and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
            status = await onboard.onboard_reset_status(_SID)
        return status
    return _with(patches, _go)


def test_async_reset_incomplete_is_error_not_done():
    cleanup = _cleanup(timed_out=True)
    patches, cat, _ = _cascade(cleanup)
    status = _drive_async(patches)
    assert status["state"] == "error", status
    assert "still deleting" in status["error"]
    cat.remove.assert_not_called()


def test_async_reset_waits_with_the_long_deadline():
    cleanup = _cleanup(timed_out=False)
    patches, _, _ = _cascade(cleanup)
    status = _drive_async(patches)
    assert status["state"] == "done", status
    assert cleanup.call_args.args[1] == onboard._ASYNC_DELETE_WAIT_DEADLINE_S
    assert onboard._ASYNC_DELETE_WAIT_DEADLINE_S > onboard._DELETE_WAIT_DEADLINE_S


def test_delete_wait_deadline_reaches_both_waiters(monkeypatch):
    seen = {}
    monkeypatch.setattr(onboard, "_wait_runtimes_absent",
                        lambda c, names, deadline_s: seen.setdefault("rt", deadline_s) or True)
    monkeypatch.setattr(onboard, "_wait_targets_absent",
                        lambda c, g, names, deadline_s: seen.setdefault("tg", deadline_s) or True)
    control = MagicMock()
    control.list_gateway_targets.return_value = {"items": [{"name": _SID, "targetId": "t1"}]}
    monkeypatch.setattr("agents.runtime_provision_helpers.list_all_agent_runtimes",
                        lambda c: [{"agentRuntimeName": "adt_synth_x1",
                                    "agentRuntimeArn": "arn:x/rt1"}])
    # Both waiters now draw from ONE shared absolute deadline (remaining budget),
    # not a fresh full deadline each — so each sees at most the window, never a
    # multiplied one. The delete is accepted instantly here, so both see ≈777.
    deadline_at = onboard.time.monotonic() + 777.0
    onboard._reset_delete_gateway_target(control, "gw", _SID, deadline_at=deadline_at)
    onboard._reset_delete_runtime(control, _SID, deadline_at=deadline_at)
    assert 0 < seen["tg"] <= 777.0
    assert 0 < seen["rt"] <= 777.0


# ── 2. retirement contract ───────────────────────────────────────────────────
def _row(mid, status="pending", source="agent", frm=_SID, to="cmms"):
    return {"mapping_id": mid, "discovered_at": "2026-09-27T00:00:00Z",
            "status": status, "source": source, "from_system": frm, "to_system": to}


def test_retirable_rows_filters_status_side_and_steward_source():
    rows = [
        _row("a"),                                   # machine, from side
        _row("b", frm="cmms", to=_SID),              # machine, to side
        _row("c", source="user"),                    # steward-authored: spared
        _row("d", source="steward"),                 # steward-authored: spared
        _row("e", status="rejected"),                # history: never touched
        _row("f", frm="erp", to="cmms"),             # other systems
        _row("g", source="context-placement"),       # machine source
    ]
    assert [r["mapping_id"] for r in retirement.retirable_rows(rows, _SID)] == ["a", "b", "g"]


class _Race(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


def _patch_store(monkeypatch, rows, update):
    import agents.self_learning as sl
    monkeypatch.setattr(sl, "_get_table", lambda: "tbl")
    monkeypatch.setattr(sl, "_query_by_status", lambda status: rows if status == "pending" else [])
    monkeypatch.setattr(sl, "_update_status", update)


def test_retire_supersedes_conditionally_and_never_rejects(monkeypatch):
    calls = []

    def _update(table, mid, disc, status, extra=None, *, expect_status=None):
        calls.append((mid, status, extra["superseded_reason"], expect_status))

    _patch_store(monkeypatch, [_row("a"), _row("c", source="user")], _update)
    assert retirement.retire_pending_for_offboarded_system(_SID) == 1
    assert calls == [("a", "superseded", "system_offboarded", "pending")]


def test_retire_tolerates_a_lost_race_but_raises_other_errors(monkeypatch):
    def _race(*a, **k):
        raise _Race()

    _patch_store(monkeypatch, [_row("a")], _race)
    assert retirement.retire_pending_for_offboarded_system(_SID) == 0

    def _boom(*a, **k):
        raise RuntimeError("store down")

    _patch_store(monkeypatch, [_row("a")], _boom)
    with pytest.raises(RuntimeError, match="store down"):
        retirement.retire_pending_for_offboarded_system(_SID)


def test_retire_blank_system_is_a_noop():
    assert retirement.retire_pending_for_offboarded_system("") == 0


# ── 3. timed-out waits and retries ───────────────────────────────────────────
def test_incomplete_keeps_graph_and_retry_is_admitted(_fresh_jobs):
    """Timed-out wait: no graph deletes (the :System node survives with its
    removal marker, so a retry is admitted even after a restart), and a second
    removal call proceeds."""
    cleanup = _cleanup(timed_out=True)
    patches, cat, _ = _cascade(cleanup)
    writes = MagicMock()
    patches[0] = patch.object(onboard, "neptune_write", writes)
    status = _drive_async(patches)
    assert status["state"] == "error"
    assert _destructive(writes) == [], writes.call_args_list

    cleanup.return_value = {**cleanup.return_value, "wait_timed_out": False}
    status = _drive_async(patches)
    assert status["state"] == "done", status
    assert writes.called
    cat.remove.assert_called_once_with(_SID)


def test_unverifiable_absence_is_incomplete(monkeypatch):
    """A list/delete error during cleanup must never read as a clean removal."""
    import boto3

    def _client(*_a, **_k):
        control = MagicMock()
        control.list_gateway_targets.side_effect = RuntimeError("throttled")
        return control

    monkeypatch.setattr(boto3, "client", _client)
    monkeypatch.setattr("agents.runtime_provision_helpers.list_all_agent_runtimes",
                        MagicMock(side_effect=RuntimeError("throttled")))
    monkeypatch.setenv("AGENTCORE_GATEWAY_ID", "gw-1")
    assert onboard._reset_cleanup_agentcore_sync(_SID, 5.0)["wait_timed_out"] is True


def test_retirement_failure_is_retryable_incomplete():
    patches, cat, _ = _cascade(_cleanup(timed_out=False),
                               retire=MagicMock(side_effect=RuntimeError("ddb down")))
    writes = MagicMock()
    patches[0] = patch.object(onboard, "neptune_write", writes)
    with pytest.raises(onboard.ResetIncompleteError, match="could not be retired"):
        _with(patches, lambda: onboard._run_reset_steps(_SID, full=True))
    assert _destructive(writes) == [], writes.call_args_list
    cat.remove.assert_not_called()


def test_system_match_is_case_insensitive_but_never_short_name():
    rows = [_row("a", frm="SYNTH-X1"), _row("b", frm="synth"), _row("c", frm="", to="Synth-X1")]
    assert [r["mapping_id"] for r in retirement.retirable_rows(rows, _SID)] == ["a", "c"]


def test_offboard_supersedes_do_not_load_the_flipflop_breaker():
    import agents.self_learning as sl
    prior = [
        {"status": "superseded", "superseded_reason": retirement.OFFBOARDED_REASON},
        {"status": "superseded", "superseded_reason": retirement.OFFBOARDED_REASON},
        {"status": "superseded"},
    ]
    assert sl._supersedes_since_adjudication(prior) == 1
    assert sl._flipflop_review_reason(prior).startswith("flip-flopped 1 times")


def test_removal_race_supersedes_do_not_load_the_flipflop_breaker():
    """Onboarding supersedes a proposal it filed against a peer whose removal
    started meanwhile (``retire_pending_ids``): lifecycle, like offboarding."""
    import agents.self_learning as sl
    prior = [
        {"status": "superseded", "superseded_reason": retirement.SYSTEM_BEING_REMOVED_REASON},
        {"status": "superseded", "superseded_reason": retirement.SYSTEM_BEING_REMOVED_REASON},
        {"status": "superseded"},
    ]
    assert sl._supersedes_since_adjudication(prior) == 1
    assert sl._flipflop_review_reason(prior).startswith("flip-flopped 1 times")


# ── 4. global /reset parity ──────────────────────────────────────────────────
def _global(cleanup_result, retire=None):
    cat = MagicMock()
    cat.get_onboarded.return_value = [{"id": "sys-a"}, {"id": "sys-b"}]
    cat.clear_all.return_value = 2
    writes = MagicMock()
    retire = retire or MagicMock(return_value=5)
    patches = [
        patch.object(onboard, "neptune_write", writes),
        patch.object(onboard, "catalog", cat),
        patch.object(onboard, "core_concept_merges", return_value=[]),
        patch.object(onboard, "reset_agents", MagicMock()),
        patch.object(onboard, "_agent_manager", MagicMock()),
        patch.object(onboard, "_global_reset_cleanup_agentcore_sync",
                     MagicMock(return_value=cleanup_result)),
        patch.object(retirement, "retire_pending_for_offboarded_systems", retire),
    ]
    return patches, cat, writes, retire


def test_global_reset_incomplete_is_503_before_graph_and_catalog():
    patches, cat, writes, retire = _global(
        {"runtimes_awaited": ["adt_sys_a"], "targets_awaited": ["sys-a"], "wait_timed_out": True})
    with pytest.raises(HTTPException) as exc:
        _with(patches, lambda: onboard.onboard_reset(full=True))
    assert exc.value.status_code == 503
    assert "all systems" in exc.value.detail
    assert _destructive(writes) == [], writes.call_args_list
    cat.reset_all.assert_not_called()
    cat.clear_all.assert_not_called()
    retire.assert_not_called()


def test_global_reset_retires_every_removed_system_once():
    patches, cat, writes, retire = _global(
        {"runtimes_awaited": ["adt_sys_c"], "targets_awaited": ["sys-a", "sys-c"],
         "wait_timed_out": False})
    result = _with(patches, lambda: onboard.onboard_reset(full=False))
    retire.assert_called_once_with(["sys-a", "sys-b", "sys-c"])
    assert result["proposals_retired"] == 5
    assert result["targets_deleted"] == 2 and result["runtimes_deleted"] == 1
    assert writes.called
    cat.reset_all.assert_called_once()


def test_global_reset_retirement_failure_is_503_and_keeps_graph():
    patches, cat, writes, _ = _global(
        {"runtimes_awaited": [], "targets_awaited": [], "wait_timed_out": False},
        retire=MagicMock(side_effect=RuntimeError("ddb down")))
    with pytest.raises(HTTPException) as exc:
        _with(patches, lambda: onboard.onboard_reset())
    assert exc.value.status_code == 503
    assert _destructive(writes) == [], writes.call_args_list
    cat.reset_all.assert_not_called()


def test_global_cleanup_list_error_is_unverified(monkeypatch):
    import boto3
    control = MagicMock()
    control.list_gateway_targets.side_effect = RuntimeError("throttled")
    monkeypatch.setattr(boto3, "client", lambda *a, **k: control)
    monkeypatch.setenv("AGENTCORE_GATEWAY_ID", "gw-1")
    assert onboard._global_reset_cleanup_agentcore_sync()["wait_timed_out"] is True


def test_plural_retirement_reads_queue_once_and_dedupes(monkeypatch):
    reads, updates = [], []
    import agents.self_learning as sl
    rows = [_row("a", frm="sys-a", to="sys-b"), _row("b", frm="sys-b", to="erp"),
            _row("c", frm="erp", to="plm")]
    monkeypatch.setattr(sl, "_get_table", lambda: "tbl")
    monkeypatch.setattr(sl, "_query_by_status", lambda st: reads.append(st) or rows)
    monkeypatch.setattr(sl, "_update_status",
                        lambda t, mid, *a, **k: updates.append(mid))
    assert retirement.retire_pending_for_offboarded_systems(["sys-a", "sys-b", ""]) == 2
    assert reads == ["pending"]
    assert sorted(updates) == ["a", "b"]


# ── 5. short-name graph delete spares sibling sites ──────────────────────────
@pytest.mark.parametrize("graph_ids, expected", [
    ([{"id": "mes-mil"}, {"id": "mes-det"}], "mes-mil"),   # sibling still present
    ([{"id": "mes-mil"}, {"id": "erp"}], "mes"),           # last of its family
    ([{"id": "mes-mil"}], "mes"),
    ([], "mes-mil"),              # neptune_query degraded to [] => unverified
    ([{"id": "erp"}], "mes-mil"),  # sentinel (own node) missing => unverified
])
def test_short_name_only_when_verified_and_no_sibling(monkeypatch, graph_ids, expected):
    monkeypatch.setattr(onboard, "neptune_query", lambda q, p=None: graph_ids)
    assert onboard._short_name_for_delete("mes-mil") == expected


def test_short_name_is_resolved_before_the_system_node_delete(monkeypatch):
    order = []
    monkeypatch.setattr(onboard, "neptune_query",
                        lambda q, p=None: order.append("lookup") or [{"id": "mes-mil"}])
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda q, p=None: order.append(q.split(" DETACH")[0]) or True)
    onboard._reset_delete_graph_artifacts("mes-mil")
    assert order[0] == "lookup"
    assert any("(s:System {id: $sid})" in step for step in order[1:])


def test_unhyphenated_system_needs_no_lookup(monkeypatch):
    monkeypatch.setattr(onboard, "neptune_query", MagicMock(side_effect=AssertionError))
    assert onboard._short_name_for_delete("erp") == "erp"


def test_global_reset_retires_graph_only_systems(monkeypatch):
    patches, _, _, retire = _global(
        {"runtimes_awaited": [], "targets_awaited": [], "wait_timed_out": False})
    patches.append(patch.object(onboard, "_graph_system_ids", return_value=["lake_raw"]))
    _with(patches, lambda: onboard.onboard_reset())
    retire.assert_called_once_with(["lake_raw", "sys-a", "sys-b"])
