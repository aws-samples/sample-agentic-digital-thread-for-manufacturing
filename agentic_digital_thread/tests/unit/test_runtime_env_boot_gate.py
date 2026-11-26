# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Runtime boot fail-fast on incomplete contract env (ADR-0069) — offline.

The ``adt_onboarding`` runtime ran its whole life without ``GRAPH_IDENTIFIER``:
``db._neptune_execute`` fell back to the literal ``"adt"`` (used by Neptune
Analytics as the endpoint HOSTNAME — a non-existent host), every graph read
returned ``[]`` through the load-bearing degrade contract, and the agent told
stewards the catalog was empty. Nothing validated any runtime's env at boot.

Pinned here:

* ``runtime_env_contract.missing_required(role, environ)`` is the ONE
  validator, wired into ``create_onboarding_server`` (construction) and
  ``runtime_app._assert_runtime_env`` (the ``__main__`` dispatch for the
  orchestrator/system/digital-thread modes);
* a missing/empty required key ⇒ RAISE naming the key;
* a complete env ⇒ constructs/passes;
* local/DEV_MODE (``config.local_mode_env``) ⇒ NEVER raises — the local env
  is intentionally minimal (ADR-0023);
* the 50-line gate on every function this fix added/changed.

Values below are neutral placeholders (engine neutrality ratchet).
"""

import inspect

import pytest

from agents import runtime_app
from agents.config import local_mode_env
from agents.onboarding_runtime_server import (
    _assert_required_env,
    create_onboarding_server,
)
from agents.runtime_env_contract import STALE_CHECKED_KEYS, missing_required

_ONBOARDING_ENV = {
    "ONBOARDING_SESSIONS_TABLE": "sessions-table",
    "AWS_REGION": "us-east-1",
    "GRAPH_IDENTIFIER": "g-0000000000",
}


def _cloud_env(monkeypatch, env: dict) -> None:
    """Exactly *env* for the required keys; no local-mode flags."""
    for flag in ("AGENT_MODE", "DEV_MODE"):
        monkeypatch.delenv(flag, raising=False)
    for key in set(_ONBOARDING_ENV) | set().union(*STALE_CHECKED_KEYS.values()):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


# --------------------------------------------------------------------------- #
# Onboarding server construction
# --------------------------------------------------------------------------- #

def test_missing_graph_identifier_raises_naming_the_key(monkeypatch):
    env = dict(_ONBOARDING_ENV)
    del env["GRAPH_IDENTIFIER"]
    _cloud_env(monkeypatch, env)
    with pytest.raises(RuntimeError, match="GRAPH_IDENTIFIER"):
        create_onboarding_server()


def test_empty_required_key_raises_too(monkeypatch):
    _cloud_env(monkeypatch, {**_ONBOARDING_ENV, "GRAPH_IDENTIFIER": "  "})
    with pytest.raises(RuntimeError, match="GRAPH_IDENTIFIER"):
        create_onboarding_server()


def test_every_missing_key_is_named(monkeypatch):
    _cloud_env(monkeypatch, {})
    with pytest.raises(RuntimeError) as excinfo:
        _assert_required_env()
    for key in STALE_CHECKED_KEYS["onboarding"]:
        assert key in str(excinfo.value)


def test_complete_cloud_env_constructs(monkeypatch):
    _cloud_env(monkeypatch, _ONBOARDING_ENV)
    mcp = create_onboarding_server()
    assert mcp is not None


@pytest.mark.parametrize("flag,value", [("AGENT_MODE", "local"),
                                        ("DEV_MODE", "1")])
def test_local_mode_never_raises_on_minimal_env(monkeypatch, flag, value):
    _cloud_env(monkeypatch, {})
    monkeypatch.setenv(flag, value)
    assert local_mode_env() is True
    _assert_required_env()  # no raise
    assert create_onboarding_server() is not None


# --------------------------------------------------------------------------- #
# runtime_app dispatch validator (orchestrator/system roles)
# --------------------------------------------------------------------------- #

def test_runtime_app_validator_raises_for_starved_orchestrator(monkeypatch):
    _cloud_env(monkeypatch, {"AWS_REGION": "us-east-1"})
    with pytest.raises(RuntimeError) as excinfo:
        runtime_app._assert_runtime_env("orchestrator")
    assert "GRAPH_IDENTIFIER" in str(excinfo.value)
    assert "orchestrator" in str(excinfo.value)


def test_runtime_app_validator_passes_on_complete_role_env(monkeypatch):
    for role in ("orchestrator", "system"):
        _cloud_env(monkeypatch, {k: f"value-{k.lower()}"
                                 for k in STALE_CHECKED_KEYS[role]})
        runtime_app._assert_runtime_env(role)  # no raise


def test_runtime_app_validator_skips_in_local_mode(monkeypatch):
    _cloud_env(monkeypatch, {})
    monkeypatch.setenv("AGENT_MODE", "local")
    for role in ("orchestrator", "system", "onboarding"):
        runtime_app._assert_runtime_env(role)  # no raise


def test_dispatch_wires_the_validator_for_every_non_onboarding_mode():
    """The __main__ dispatch is module-level (not directly invocable offline):
    pin by source that each non-onboarding branch calls the validator with the
    contract role, and the onboarding branch relies on construction-time
    validation instead (create_onboarding_server)."""
    source = inspect.getsource(runtime_app)
    dispatch = source.split('if __name__ == "__main__":', 1)[1]
    assert dispatch.count('_assert_runtime_env("system")') == 2
    assert dispatch.count('_assert_runtime_env("orchestrator")') == 1
    onboarding_branch = dispatch.split("_ONBOARDING_ID:", 1)[1]
    assert "_run_onboarding()" in onboarding_branch


def test_validator_consumes_the_one_contract_validator(monkeypatch):
    """No second required-set spelling: the boot gates surface exactly what
    missing_required computes."""
    _cloud_env(monkeypatch, {})
    expected = missing_required("system", {})
    with pytest.raises(RuntimeError) as excinfo:
        runtime_app._assert_runtime_env("system")
    for key in expected:
        assert key in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 50-line gate on every touched/new function (project invariant)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("func", [
    runtime_app._assert_runtime_env,
    _assert_required_env,
    create_onboarding_server,
    missing_required,
    local_mode_env,
])
def test_new_and_changed_functions_stay_within_50_lines(func):
    assert len(inspect.getsource(func).splitlines()) <= 50, func.__name__
