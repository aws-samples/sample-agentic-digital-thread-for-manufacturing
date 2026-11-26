# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the global /api/onboard/reset AgentCore cleanup preservation.

Regression for the built-in-target wipe: onboard_reset previously deleted EVERY
gateway target whose name != 'orchestrator' and EVERY adt_* runtime except
adt_orchestrator. That also removed the app-provisioned built-ins —
``digital-thread`` (the graph-tools target: explore_graph/get_system_schema/
save_value_mapping/save_correlation AND get_query_workflow, which BOTH the
orchestrator and the external AWS Quick client depend on) and ``onboarding``
(the onboarding-flow target) — breaking graph access + Quick + re-onboarding
until an app restart.

These tests assert, with NO live Neptune/AWS/HTTP (a mocked
bedrock-agentcore-control client), that reset DELETES only the user-onboarded
data-system targets/runtimes (lake-athena, telem-sitewise) and PRESERVES the
built-ins (orchestrator + digital-thread + onboarding) and their runtimes.
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


@pytest.fixture(autouse=True)
def _no_proposal_store(monkeypatch):
    """The reset retires removed systems' pending proposals (a DynamoDB write);
    stubbed here — tests/unit/test_offboard_retirement.py covers it directly."""
    import agents.offboard_retirement as retirement
    monkeypatch.setattr(retirement, "retire_pending_for_offboarded_systems", lambda ids: 0)


# The three built-in gateway targets that must survive a global reset.
_BUILTIN_TARGETS = ("orchestrator", "digital-thread", "onboarding")
# Their runtimes, per the canonical adt_<name> convention (hyphens → underscores).
_BUILTIN_RUNTIMES = ("adt_orchestrator", "adt_digital_thread", "adt_onboarding")
# Two representative user-onboarded data systems (targets + matching runtimes).
_DATA_TARGETS = ("lake-athena", "telem-sitewise")
_DATA_RUNTIMES = ("adt_lake_athena", "adt_telem_sitewise")


def test_preserved_sets_are_sourced_from_target_name_constants():
    """The preserved set is DRY-derived from the servers' TARGET_NAME constants and
    the adt_<name> runtime convention — not hardcoded literals."""
    assert onboard.PRESERVED_BUILTIN_TARGETS == frozenset(_BUILTIN_TARGETS)
    assert onboard.PRESERVED_BUILTIN_RUNTIMES == frozenset(_BUILTIN_RUNTIMES)
    # digital-thread's hyphen MUST be folded to an underscore in the runtime name.
    assert "adt_digital_thread" in onboard.PRESERVED_BUILTIN_RUNTIMES
    assert "adt_digital-thread" not in onboard.PRESERVED_BUILTIN_RUNTIMES


def _make_control_client(deleted_targets, deleted_runtimes):
    """A fake bedrock-agentcore-control client returning a MIX of built-in +
    data-system targets/runtimes, recording every delete into the given lists.

    The list_* methods read from LIVE dicts that shrink as deletes are issued, so
    the reset's block-until-ABSENT wait (_wait_*_absent) sees the deleted names
    disappear immediately (mirrors real AgentCore DELETING → gone) and returns
    without any real sleeps."""
    control = MagicMock()

    live_targets = {name: f"tid-{name}" for name in (*_BUILTIN_TARGETS, *_DATA_TARGETS)}
    live_runtimes = {name: f"arn:.../{name}" for name in (*_BUILTIN_RUNTIMES, *_DATA_RUNTIMES)}

    control.list_gateway_targets.side_effect = lambda **kw: {
        "items": [{"name": name, "targetId": tid} for name, tid in live_targets.items()]
    }
    control.list_agent_runtimes.side_effect = lambda **kw: {
        "agentRuntimes": [
            {"agentRuntimeName": name, "agentRuntimeArn": arn}
            for name, arn in live_runtimes.items()
        ]
    }

    def _del_target(gatewayIdentifier, targetId):
        deleted_targets.append(targetId)
        for name, tid in list(live_targets.items()):
            if tid == targetId:
                del live_targets[name]

    def _del_runtime(agentRuntimeId):
        deleted_runtimes.append(agentRuntimeId)
        for name, arn in list(live_runtimes.items()):
            if arn.rsplit("/", 1)[-1] == agentRuntimeId:
                del live_runtimes[name]

    control.delete_gateway_target.side_effect = _del_target
    control.delete_agent_runtime.side_effect = _del_runtime
    return control


def _run_reset_capturing_agentcore():
    """Invoke onboard_reset with graph/catalog/agent_manager mocked and a fake
    control client. Returns (deleted_target_ids, deleted_runtime_ids, result)."""
    deleted_targets: list[str] = []
    deleted_runtimes: list[str] = []
    control = _make_control_client(deleted_targets, deleted_runtimes)

    fake_catalog = MagicMock()
    fake_catalog.get_onboarded.return_value = []
    fake_catalog.reset_all.return_value = None

    import boto3
    with patch.object(onboard, "neptune_write", MagicMock()), \
            patch.object(onboard, "catalog", fake_catalog), \
            patch.object(onboard, "core_concept_merges", return_value=[]), \
            patch.object(onboard, "reset_agents", MagicMock()), \
            patch.object(onboard, "_agent_manager", MagicMock()), \
            patch.dict(os.environ, {"AGENTCORE_GATEWAY_ID": "gw-test"}), \
            patch.object(boto3, "client", return_value=control):
        result = asyncio.run(onboard.onboard_reset())
    return deleted_targets, deleted_runtimes, result


def test_reset_deletes_only_data_targets_and_preserves_builtins():
    deleted_targets, deleted_runtimes, result = _run_reset_capturing_agentcore()

    # Only the user-onboarded DATA-system targets are deleted.
    assert set(deleted_targets) == {f"tid-{n}" for n in _DATA_TARGETS}, deleted_targets
    # None of the built-in targets are touched.
    for name in _BUILTIN_TARGETS:
        assert f"tid-{name}" not in deleted_targets, name

    # Only the DATA-system runtimes are deleted (id is the ARN's last path segment).
    assert set(deleted_runtimes) == set(_DATA_RUNTIMES), deleted_runtimes
    for name in _BUILTIN_RUNTIMES:
        assert name not in deleted_runtimes, name

    # The handler still reports the counts (2 targets + 2 runtimes deleted).
    assert result["status"] == "reset"
    assert result["targets_deleted"] == len(_DATA_TARGETS)
    assert result["runtimes_deleted"] == len(_DATA_RUNTIMES)


_TESTS = [
    test_preserved_sets_are_sourced_from_target_name_constants,
    test_reset_deletes_only_data_targets_and_preserves_builtins,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} reset-preserve tests passed")
