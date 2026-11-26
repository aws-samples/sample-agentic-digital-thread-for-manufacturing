# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0047 posture must reach the app AND every runtime, or not at all.

``RESPONSE_INTEGRITY_MODE`` had no plumbing: it was read in code but set
nowhere, so a deployment could not opt into `annotate` (the ADR's recommended
posture) without hand-editing env on each surface. ADR-0047 is explicit that
the flag "must be set consistently on the app AND the agent runtimes" — a
half-configured posture (app annotating, agents not) is the failure mode.

Two properties matter:
  * OPT-IN is preserved — unset ⇒ the key is ABSENT from every payload, so the
    code default (report-only) stays the single source of truth and existing
    deployments are byte-unchanged.
  * When set, EVERY role carries it (orchestrator, onboarding, system).

Offline/pure: the contract is stdlib-only and never touches os.environ.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agents import runtime_env_contract as contract

_ROLE_KWARGS = {
    "system": {"system_id": "sys-a", "system_name": "A", "schema": "public",
               "connection_type": "Athena", "system_agent_model": "m"},
}


def _sources(**overrides):
    return contract.RuntimeEnvSources(region="us-east-1", **overrides)


@pytest.mark.parametrize("role", ["orchestrator", "onboarding", "system"])
def test_unset_means_absent_for_every_role(role):
    """OPT-IN preserved: the contract never turns gating on by itself."""
    env = contract.build_env(role, _sources(), **_ROLE_KWARGS.get(role, {}))
    assert "RESPONSE_INTEGRITY_MODE" not in env


@pytest.mark.parametrize("role", ["orchestrator", "onboarding", "system"])
def test_set_reaches_every_role(role):
    env = contract.build_env(
        role, _sources(response_integrity_mode="annotate"),
        **_ROLE_KWARGS.get(role, {}))
    assert env["RESPONSE_INTEGRITY_MODE"] == "annotate"


def test_value_is_normalized():
    env = contract.build_env(
        "orchestrator", _sources(response_integrity_mode="  ANNOTATE  "))
    assert env["RESPONSE_INTEGRITY_MODE"] == "annotate"


def test_blank_string_is_still_absent():
    """Whitespace is not an opt-in."""
    env = contract.build_env("orchestrator", _sources(response_integrity_mode="   "))
    assert "RESPONSE_INTEGRITY_MODE" not in env


def test_read_from_environment_sources():
    src = contract.RuntimeEnvSources.from_ambient(
        {"AWS_REGION": "us-east-1", "RESPONSE_INTEGRITY_MODE": "annotate"})
    assert src.response_integrity_mode == "annotate"
    assert contract.build_env("orchestrator", src)["RESPONSE_INTEGRITY_MODE"] == "annotate"


def test_registered_as_live_swap_not_stale():
    """An operator flips it per deploy and it is ABSENT on a default env — the
    drift lane must never report it stale."""
    assert "RESPONSE_INTEGRITY_MODE" in contract.LIVE_SWAP_KEYS


# --------------------------------------------------------------------------- #
# The RECONCILE path — how EXISTING runtimes adopt a posture change.
#
# The live gap this pins: the per-system and orchestrator refresh paths
# reconcile rather than rebuild, so an opted-in deploy updated the app and the
# orchestrator while the SYSTEM runtimes kept the old posture (observed:
# app+orchestrator annotate, adt_lake_* absent) — the half-configured state
# ADR-0047 explicitly warns about.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ["orchestrator", "onboarding", "system"])
def test_reconcile_propagates_the_posture_to_existing_runtimes(role):
    existing = {"SOME_OPERATOR_KEY": "keep-me"}
    out = contract.reconcile_env(
        role, existing, _sources(response_integrity_mode="annotate"))
    assert out["RESPONSE_INTEGRITY_MODE"] == "annotate"
    assert out["SOME_OPERATOR_KEY"] == "keep-me"  # untouched passthrough


@pytest.mark.parametrize("role", ["orchestrator", "onboarding", "system"])
def test_reconcile_overwrites_a_stale_posture(role):
    out = contract.reconcile_env(
        role, {"RESPONSE_INTEGRITY_MODE": "report-only"},
        _sources(response_integrity_mode="annotate"))
    assert out["RESPONSE_INTEGRITY_MODE"] == "annotate"


@pytest.mark.parametrize("role", ["orchestrator", "onboarding", "system"])
def test_reconcile_leaves_the_existing_value_when_source_is_empty(role):
    """OPT-IN both ways: a deploy that did not set a mode must not clear one."""
    out = contract.reconcile_env(
        role, {"RESPONSE_INTEGRITY_MODE": "annotate"}, _sources())
    assert out["RESPONSE_INTEGRITY_MODE"] == "annotate"
    # ...and it is not invented where absent.
    assert "RESPONSE_INTEGRITY_MODE" not in contract.reconcile_env(
        role, {}, _sources())


# --------------------------------------------------------------------------- #
# The other two surfaces (text-level: these files are not importable here)
# --------------------------------------------------------------------------- #
_REPO = Path(__file__).resolve().parents[3]


def test_app_task_env_carries_the_context_override():
    src = (_REPO / "stacks" / "adt_compute_stack.py").read_text(encoding="utf-8")
    assert '"RESPONSE_INTEGRITY_MODE"' in src
    assert 'try_get_context("response_integrity_mode")' in src


def test_deploy_script_threads_it_stickily_to_cdk_and_runtimes():
    src = (_REPO / "agentic_digital_thread" / "deploy.sh").read_text(encoding="utf-8")
    assert "--integrity-mode)" in src, "operator flag missing"
    assert "/adt/response-integrity-mode" in src, "sticky SSM parameter missing"
    # Exported for the runtime-env contract AND passed as CDK context for the app.
    assert 'export RESPONSE_INTEGRITY_MODE="$INTEGRITY_MODE"' in src
    assert "-c response_integrity_mode=$INTEGRITY_MODE" in src
    assert "$_INTEGRITY_CDK_FLAG" in src.split("npx cdk deploy")[1][:600]
