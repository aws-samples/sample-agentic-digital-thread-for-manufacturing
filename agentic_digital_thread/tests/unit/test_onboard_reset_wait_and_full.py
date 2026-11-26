# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the reset block-until-DELETED wait + the opt-in full reset flag.

Two behaviors, both offline (NO live Neptune/AWS/HTTP — a mocked
bedrock-agentcore-control client, an injected clock, and an in-memory catalog):

  1. BLOCK-UNTIL-DELETED — both reset endpoints, after issuing the AgentCore
     deletions, must POLL until the deleted runtime/target names are ABSENT (the
     runtime lingers in DELETING before it disappears; re-onboarding earlier hits a
     ConflictException / stranded CREATE_FAILED). The shared _wait_*_absent helpers
     are bounded + FAIL-SAFE: on timeout they log + return False (never raise, never
     hang past the deadline).

  2. OPT-IN FULL RESET — ``full=true`` on the GLOBAL reset ADDITIONALLY clears
     catalog registrations (catalog.clear_all) for a truly-blank slate; the DEFAULT
     keeps registrations exactly as before (catalog.reset_all only re-flags to
     'registered'). Built-in preservation still holds under full=true.

All fixtures are synthetic.
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import pytest  # noqa: E402
import agents.onboard as onboard  # noqa: E402
from agents.catalog import SystemCatalog  # noqa: E402


@pytest.fixture(autouse=True)
def _no_proposal_store(monkeypatch):
    """The reset retires removed systems' pending proposals (a DynamoDB write);
    stubbed here — tests/unit/test_offboard_retirement.py covers it directly."""
    import agents.offboard_retirement as retirement
    monkeypatch.setattr(retirement, "retire_pending_for_offboarded_systems", lambda ids: 0)


# ── Injected clock (deterministic; no real time passes) ──────────────────────
class _Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


# ─────────────────────────────────────────────────────────────────────────────
# 1. BLOCK-UNTIL-DELETED — shared wait helper
# ─────────────────────────────────────────────────────────────────────────────
class _RuntimeControl:
    """Fake control-plane: reports the runtime present for the first ``present_polls``
    list calls (DELETING), then absent (fully gone). Counts list calls."""

    def __init__(self, present_polls, name="adt_lake_x"):
        self.present_polls = present_polls
        self.name = name
        self.calls = 0

    def list_agent_runtimes(self):
        self.calls += 1
        if self.calls <= self.present_polls:
            return {"agentRuntimes": [
                {"agentRuntimeName": self.name, "agentRuntimeArn": f"arn:.../{self.name}"}
            ]}
        return {"agentRuntimes": []}


def test_wait_runtimes_absent_polls_until_gone():
    """Returns True ONLY once the deleted runtime disappears — after polling through
    the DELETING window (simulated: present for 2 polls, then absent)."""
    clock = _Clock()
    control = _RuntimeControl(present_polls=2, name="adt_lake_x")
    ok = onboard._wait_runtimes_absent(
        control, ["adt_lake_x"], deadline_s=100, poll_s=1,
        sleep=clock.sleep, monotonic=clock.monotonic,
    )
    assert ok is True
    # 2 "present" polls + 1 "absent" poll = 3 list calls; only returns on the absent one.
    assert control.calls == 3, control.calls


def test_wait_runtimes_absent_failsafe_on_timeout():
    """On timeout (runtime never disappears) the wait returns False WITHOUT raising
    and WITHOUT hanging past the deadline (fail-safe)."""
    clock = _Clock()
    control = MagicMock()
    control.list_agent_runtimes.return_value = {"agentRuntimes": [
        {"agentRuntimeName": "adt_stuck", "agentRuntimeArn": "arn:.../adt_stuck"}
    ]}
    ok = onboard._wait_runtimes_absent(
        control, ["adt_stuck"], deadline_s=10, poll_s=1,
        sleep=clock.sleep, monotonic=clock.monotonic,
    )
    assert ok is False
    # Bounded: it stopped at/after the deadline rather than looping forever.
    assert clock.t >= 10


def test_wait_runtimes_absent_failsafe_on_list_error():
    """A list_agent_runtimes error is fail-safe too: return False, do not raise."""
    control = MagicMock()
    control.list_agent_runtimes.side_effect = RuntimeError("control plane down")
    assert onboard._wait_runtimes_absent(control, ["adt_x"]) is False


def test_wait_targets_absent_noop_without_gateway():
    """No gateway id (or no names) → nothing to wait on → True immediately."""
    control = MagicMock()
    assert onboard._wait_targets_absent(control, "", ["sys-x"]) is True
    assert onboard._wait_targets_absent(control, "gw", []) is True
    control.list_gateway_targets.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# 2. FULL RESET FLAG — global endpoint
# ─────────────────────────────────────────────────────────────────────────────
def _seed_catalog():
    return SystemCatalog([
        {"id": "sys-a", "name": "A", "status": "onboarded", "schema": "sa", "plant": "p1"},
        {"id": "sys-b", "name": "B", "status": "registered", "schema": "sb", "plant": "p1"},
    ])


def _run_onboard_reset(catalog_obj, *, full=False, control=None):
    """Run onboard_reset with all side-effects mocked. When ``control`` is None the
    AgentCore sweep is stubbed as a clean no-op (focus on catalog)."""
    import boto3

    # control=None: skip AgentCore entirely by stubbing the sweep as a clean,
    # verified no-op (a failing boto3 client is now an INCOMPLETE reset => 503).
    boto_patch = (patch.object(boto3, "client", return_value=control) if control is not None
                  else patch.object(onboard, "_global_reset_cleanup_agentcore_sync",
                                    return_value={"runtimes_awaited": [],
                                                  "targets_awaited": [],
                                                  "wait_timed_out": False}))
    with patch.object(onboard, "neptune_write", MagicMock()), \
            patch.object(onboard, "catalog", catalog_obj), \
            patch.object(onboard, "core_concept_merges", return_value=[]), \
            patch.object(onboard, "reset_agents", MagicMock()), \
            patch.object(onboard, "_agent_manager", MagicMock()), \
            patch.dict(os.environ, {"AGENTCORE_GATEWAY_ID": "gw-test"}), \
            boto_patch:
        return asyncio.run(onboard.onboard_reset(full=full))


def test_default_reset_keeps_registrations():
    """DEFAULT (no flag): entries REMAIN in the catalog, just re-flagged to
    'registered' (connect config kept). registrations_cleared == 0, full == False."""
    cat = _seed_catalog()
    result = _run_onboard_reset(cat, full=False)
    assert result["status"] == "reset"
    assert result["full"] is False
    assert result["registrations_cleared"] == 0
    # Both systems still present, all 'registered' (reset_all semantics unchanged).
    remaining = cat.get_all()
    assert {s["id"] for s in remaining} == {"sys-a", "sys-b"}
    assert all(s["status"] == "registered" for s in remaining)


def test_full_reset_clears_registrations():
    """full=true: catalog registrations are REMOVED (truly blank), and the payload
    reports how many were cleared."""
    cat = _seed_catalog()
    result = _run_onboard_reset(cat, full=True)
    assert result["status"] == "reset"
    assert result["full"] is True
    assert result["registrations_cleared"] == 2, result
    assert cat.get_all() == []  # truly blank


# ── Dynamic fake control (deletions reflected) for built-in preservation ──────
_BUILTIN_TARGETS = ("orchestrator", "digital-thread", "onboarding")
_BUILTIN_RUNTIMES = ("adt_orchestrator", "adt_digital_thread", "adt_onboarding")
_DATA_TARGETS = ("lake-athena", "telem-sitewise")
_DATA_RUNTIMES = ("adt_lake_athena", "adt_telem_sitewise")


def _dynamic_control(deleted_targets, deleted_runtimes):
    control = MagicMock()
    live_t = {n: f"tid-{n}" for n in (*_BUILTIN_TARGETS, *_DATA_TARGETS)}
    live_r = {n: f"arn:.../{n}" for n in (*_BUILTIN_RUNTIMES, *_DATA_RUNTIMES)}
    control.list_gateway_targets.side_effect = lambda **kw: {
        "items": [{"name": n, "targetId": tid} for n, tid in live_t.items()]}
    control.list_agent_runtimes.side_effect = lambda **kw: {
        "agentRuntimes": [{"agentRuntimeName": n, "agentRuntimeArn": a} for n, a in live_r.items()]}

    def _dt(gatewayIdentifier, targetId):
        deleted_targets.append(targetId)
        for n, tid in list(live_t.items()):
            if tid == targetId:
                del live_t[n]

    def _dr(agentRuntimeId):
        deleted_runtimes.append(agentRuntimeId)
        for n, a in list(live_r.items()):
            if a.rsplit("/", 1)[-1] == agentRuntimeId:
                del live_r[n]

    control.delete_gateway_target.side_effect = _dt
    control.delete_agent_runtime.side_effect = _dr
    return control


def test_full_reset_still_preserves_builtins_and_awaits_deleted():
    """Under full=true, built-in targets/runtimes are STILL preserved, only the
    user-onboarded ones are deleted, and the payload lists the awaited (blocked-on)
    names."""
    deleted_targets, deleted_runtimes = [], []
    control = _dynamic_control(deleted_targets, deleted_runtimes)
    cat = _seed_catalog()
    result = _run_onboard_reset(cat, full=True, control=control)

    # Built-ins untouched; only data systems deleted.
    assert set(deleted_targets) == {f"tid-{n}" for n in _DATA_TARGETS}, deleted_targets
    assert set(deleted_runtimes) == set(_DATA_RUNTIMES), deleted_runtimes
    for bt in _BUILTIN_TARGETS:
        assert f"tid-{bt}" not in deleted_targets
    for br in _BUILTIN_RUNTIMES:
        assert br not in deleted_runtimes

    # Awaited names surfaced; the block-until-absent wait completed (not timed out).
    assert set(result["runtimes_awaited"]) == set(_DATA_RUNTIMES), result
    assert set(result["targets_awaited"]) == set(_DATA_TARGETS), result
    assert result["wait_timed_out"] is False
    # full-reset semantics still applied.
    assert result["full"] is True
    assert result["registrations_cleared"] == 2


_TESTS = [
    test_wait_runtimes_absent_polls_until_gone,
    test_wait_runtimes_absent_failsafe_on_timeout,
    test_wait_runtimes_absent_failsafe_on_list_error,
    test_wait_targets_absent_noop_without_gateway,
    test_default_reset_keeps_registrations,
    test_full_reset_clears_registrations,
    test_full_reset_still_preserves_builtins_and_awaits_deleted,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} reset wait/full tests passed")
