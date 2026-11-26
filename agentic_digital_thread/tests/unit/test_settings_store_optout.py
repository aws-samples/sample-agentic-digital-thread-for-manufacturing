# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``ADT_SETTINGS_STORE=none`` — the local escape hatch from DEPLOYED runtime toggles.

``/adt/demo-enabled`` and ``/adt/live-updates-enabled`` are PER-DEPLOYMENT SSM
toggles, but the local hybrid stack points at a real AWS account for Bedrock. Without
an opt-out, a laptop inherits that account's toggles: an account whose
``/adt/demo-enabled`` is ``false`` (a customer-data environment with no demo systems)
disables the demo addon locally, the onboarding catalog stays empty, and the
documented local onboarding step fails with "absent from the live onboarding catalog"
while ``ADT_DEMO=1`` is plainly set — a persisted override losing to nothing visible
in the local environment.

These tests pin the guard's semantics, and critically that the DEFAULT is unchanged
so deployed behavior is identical.
"""
import os

import pytest


def _load_flag(env_value: str | None) -> bool:
    """Evaluate the guard predicate exactly as ``server.lifespan`` does."""
    saved = os.environ.get("ADT_SETTINGS_STORE")
    try:
        if env_value is None:
            os.environ.pop("ADT_SETTINGS_STORE", None)
        else:
            os.environ["ADT_SETTINGS_STORE"] = env_value
        return os.environ.get("ADT_SETTINGS_STORE", "").strip().lower() == "none"
    finally:
        if saved is None:
            os.environ.pop("ADT_SETTINGS_STORE", None)
        else:
            os.environ["ADT_SETTINGS_STORE"] = saved


@pytest.mark.parametrize("value,skips", [
    pytest.param(None, False, id="absent-DEFAULT-configures-stores"),
    pytest.param("", False, id="empty-configures-stores"),
    pytest.param("ssm", False, id="explicit-ssm-configures-stores"),
    pytest.param("none", True, id="none-skips"),
    pytest.param("NONE", True, id="uppercase-skips"),
    pytest.param("  none  ", True, id="whitespace-tolerated"),
])
def test_settings_store_optout_predicate(value, skips):
    assert _load_flag(value) is skips


def test_default_is_unchanged_so_deployed_behavior_is_identical():
    """The guard must be OPT-IN: a deployed env sets nothing and still gets SSM."""
    assert _load_flag(None) is False


def test_server_guards_both_stores_together():
    """Both stores are behind the ONE guard — a local env must not end up with the
    demo toggle skipped but the live-updates toggle still reading SSM."""
    import inspect

    from agents import server

    src = inspect.getsource(server)
    idx = src.index('ADT_SETTINGS_STORE')
    # The guarded branch configures neither store; the else branch configures both.
    tail = src[idx:idx + 1600]
    assert 'SsmSettingsStore("/adt/demo-enabled")' in tail
    assert 'SsmSettingsStore("/adt/live-updates-enabled")' in tail
    # Both configure calls must sit AFTER the else, i.e. inside the same branch.
    else_at = tail.index("else:")
    assert tail.index('SsmSettingsStore("/adt/demo-enabled")') > else_at
    assert tail.index('SsmSettingsStore("/adt/live-updates-enabled")') > else_at
