# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the dedicated-Quick-gateway override (ADR-0071).

``AgentManager.ensure_query_bridge_target`` must provision the `query-bridge`
target on the DEDICATED Quick gateway (``QUICK_GATEWAY_ID`` / ``QUICK_GATEWAY_URL``
task env — the bridge-only `adt-quick-gw`), not on the manager's default shared
gateway (``AGENTCORE_GATEWAY_ID``) that every onboarded system uses. When the env
is unset it must REFUSE (QuickGatewayNotConfiguredError) rather than fall back to
the shared gateway, and after provisioning it must remove a stale `query-bridge`
target a pre-dedicated deployment left on the shared gateway.

The gateway-target control-plane calls the bridge path makes — the fast-path
``list_gateway_targets`` probe, ``create_gateway_target``, the conflict-path
``list_gateway_targets``, ``synchronize_gateway_targets``, and the stale-target
sweep's ``list``/``delete_gateway_target`` — are captured by a fake
``bedrock-agentcore-control`` and asserted on their ``gatewayIdentifier``.
Runtime creation/warmup is stubbed (it is exercised elsewhere).

Runnable two ways:
  * pytest:      pytest agentic_digital_thread/tests/unit/test_agent_manager_quick_gateway.py
  * standalone:  python agentic_digital_thread/tests/unit/test_agent_manager_quick_gateway.py
"""
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents.agent_manager import (  # noqa: E402
    AgentManager, QUERY_BRIDGE_SYSTEM_ID, QuickGatewayNotConfiguredError,
)

_SHARED_GW_ID = "shared-gw-111"
_SHARED_GW_URL = "https://shared-gw-111.gateway.example.com/mcp"
_QUICK_GW_ID = "quick-gw-222"
_QUICK_GW_URL = "https://quick-gw-222.gateway.example.com/mcp"
_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/rt-bridge"


class _ConflictException(Exception):
    pass


class _FakeControl:
    """bedrock-agentcore-control stand-in recording every gateway id addressed.

    ``existing_target`` makes create_gateway_target raise ConflictException so the
    reuse path (list on the SAME gateway) is exercised too. ``shared_targets`` is
    what list_gateway_targets returns for the SHARED gateway (the stale-target
    sweep); ``shared_list_error`` makes that listing raise."""

    class exceptions:  # noqa: N801 - boto3 shape
        ConflictException = _ConflictException

    def __init__(self, existing_target: bool = False,
                 shared_targets: list[dict] | None = None,
                 shared_list_error: Exception | None = None):
        self._existing_target = existing_target
        self._shared_targets = shared_targets or []
        self._shared_list_error = shared_list_error
        self.calls: list[tuple[str, str]] = []  # (api, gatewayIdentifier)
        self.deleted: list[tuple[str, str]] = []  # (gatewayIdentifier, targetId)

    def list_gateway_targets(self, gatewayIdentifier=None):  # noqa: N803
        self.calls.append(("list_gateway_targets", gatewayIdentifier))
        if gatewayIdentifier == _SHARED_GW_ID:
            if self._shared_list_error:
                raise self._shared_list_error
            return {"items": list(self._shared_targets)}
        if not self._existing_target:
            return {"items": []}
        return {"items": [{"name": QUERY_BRIDGE_SYSTEM_ID,
                           "targetId": "tgt-bridge", "status": "READY"}]}

    def list_agent_runtimes(self):
        return {"agentRuntimes": []}

    def create_gateway_target(self, **kwargs):
        self.calls.append(("create_gateway_target", kwargs["gatewayIdentifier"]))
        if self._existing_target:
            raise _ConflictException("exists")
        return {"targetId": "tgt-bridge"}

    def synchronize_gateway_targets(self, gatewayIdentifier=None, targetIdList=None):  # noqa: N803
        self.calls.append(("synchronize_gateway_targets", gatewayIdentifier))
        return {}

    def update_gateway_target(self, **kwargs):
        # The conflict path's existing target carries no endpoint, so it is
        # brought in line with the runtime before the sync; the read-back then
        # shows what was sent (an update is accepted only once it does).
        self.calls.append(("update_gateway_target", kwargs["gatewayIdentifier"]))
        self.applied = kwargs["targetConfiguration"]
        return {}

    def delete_gateway_target(self, gatewayIdentifier=None, targetId=None):  # noqa: N803
        self.calls.append(("delete_gateway_target", gatewayIdentifier))
        self.deleted.append((gatewayIdentifier, targetId))
        return {}

    def get_gateway_target(self, gatewayIdentifier=None, targetId=None):  # noqa: N803
        # After delete the wait loop polls until this raises: deleted at once.
        if (gatewayIdentifier, targetId) in self.deleted:
            raise RuntimeError("ResourceNotFoundException")
        detail = {"targetId": targetId, "status": "READY"}
        if getattr(self, "applied", None):
            detail["targetConfiguration"] = self.applied
        return detail


def _mgr() -> AgentManager:
    return AgentManager(
        mode="cloud",
        gateway_id=_SHARED_GW_ID,
        gateway_url=_SHARED_GW_URL,
        ecr_image_uri="111122223333.dkr.ecr.us-east-1.amazonaws.com/adt-agents:hash",
        runtime_role_arn="arn:aws:iam::111122223333:role/adt-runtime",
    )


def _run_ensure_bridge(control: _FakeControl, env: dict[str, str]):
    """Run ensure_query_bridge_target with runtime provisioning stubbed out and
    the sleeps removed, under the given (complete) environment."""
    def _client(service, region_name=None):
        assert service == "bedrock-agentcore-control", service
        return control

    mgr = _mgr()
    with patch.dict("os.environ", env, clear=False), \
            patch("boto3.client", _client), \
            patch("agents.agent_manager.time.sleep", lambda *_: None), \
            patch.object(AgentManager, "_prepare_runtime_env", return_value={}), \
            patch.object(AgentManager, "_m2m_authorizer_config", return_value=None), \
            patch.object(AgentManager, "_find_reusable_runtime",
                         return_value=(_RUNTIME_ARN, "rt-bridge")), \
            patch.object(AgentManager, "_refresh_existing_runtime_image", return_value=None), \
            patch.object(AgentManager, "_warmup_runtime", return_value=None), \
            patch.object(AgentManager, "_ensure_credential_provider", return_value=""):
        url = mgr.ensure_query_bridge_target()
    return url, mgr


def _gateway_ids(control: _FakeControl, api: str) -> set[str]:
    return {gw for name, gw in control.calls if name == api}


def test_quick_gateway_env_routes_every_target_call_to_the_quick_gateway(caplog):
    control = _FakeControl()
    with caplog.at_level(logging.WARNING, logger="agents.agent_manager"):
        url, mgr = _run_ensure_bridge(control, {
            "QUICK_GATEWAY_ID": _QUICK_GW_ID, "QUICK_GATEWAY_URL": _QUICK_GW_URL,
        })

    assert url == _QUICK_GW_URL
    for api in ("list_gateway_targets", "create_gateway_target",
                "synchronize_gateway_targets"):
        assert _QUICK_GW_ID in _gateway_ids(control, api), (api, control.calls)
    # The shared gateway is only ever LISTED (the post-provision stale-target
    # sweep) — never created on, synced, or deleted from.
    shared_apis = {api for api, gw in control.calls if gw == _SHARED_GW_ID}
    assert shared_apis <= {"list_gateway_targets"}, control.calls
    assert control.deleted == []
    # The instance remembers its gateway so delete/reset address the right one.
    assert mgr._agents[QUERY_BRIDGE_SYSTEM_ID].gateway_id == _QUICK_GW_ID
    assert "QUICK_GATEWAY_ID is not set" not in caplog.text


def test_quick_gateway_conflict_path_lists_on_the_quick_gateway():
    control = _FakeControl(existing_target=True)
    url, _ = _run_ensure_bridge(control, {
        "QUICK_GATEWAY_ID": _QUICK_GW_ID, "QUICK_GATEWAY_URL": _QUICK_GW_URL,
    })
    assert url == _QUICK_GW_URL
    # fast-path probe (READY target but no runtime -> falls through) + reuse list
    # both address the Quick gateway; the only shared-gateway list is the sweep.
    assert _gateway_ids(control, "create_gateway_target") == {_QUICK_GW_ID}
    assert _gateway_ids(control, "update_gateway_target") == {_QUICK_GW_ID}
    quick_lists = [gw for api, gw in control.calls
                   if api == "list_gateway_targets" and gw == _QUICK_GW_ID]
    assert len(quick_lists) >= 2, control.calls


def test_missing_quick_gateway_env_refuses_to_provision_on_the_shared_gateway(caplog):
    """No silent fallback: with QUICK_API_ENABLED on but QUICK_GATEWAY_ID unset,
    ensure_query_bridge_target raises and makes NO control-plane call, so the
    bridge never lands on the shared gateway (where Quick would fan out over
    every system's tools — the drift ADR-0071 removes)."""
    control = _FakeControl()
    env = {"QUICK_GATEWAY_ID": "", "QUICK_GATEWAY_URL": ""}
    with pytest.raises(QuickGatewayNotConfiguredError) as excinfo:
        _run_ensure_bridge(control, env)

    assert "QUICK_GATEWAY_ID is not set" in str(excinfo.value)
    assert "adt-quick-gw" in str(excinfo.value)
    assert control.calls == [], control.calls


def test_stale_bridge_target_on_the_shared_gateway_is_removed_after_provisioning():
    """A deployment that predates the dedicated gateway left `query-bridge` on
    the shared gateway. After the dedicated target is up, that stale target is
    deleted (and only that one — other systems' targets are untouched)."""
    control = _FakeControl(shared_targets=[
        {"name": QUERY_BRIDGE_SYSTEM_ID, "targetId": "tgt-stale-bridge", "status": "READY"},
        {"name": "example_erp", "targetId": "tgt-erp", "status": "READY"},
    ])
    url, _ = _run_ensure_bridge(control, {
        "QUICK_GATEWAY_ID": _QUICK_GW_ID, "QUICK_GATEWAY_URL": _QUICK_GW_URL,
    })

    assert url == _QUICK_GW_URL
    assert control.deleted == [(_SHARED_GW_ID, "tgt-stale-bridge")]
    # Provisioning itself never touched the shared gateway.
    assert _gateway_ids(control, "create_gateway_target") == {_QUICK_GW_ID}
    assert _gateway_ids(control, "synchronize_gateway_targets") == {_QUICK_GW_ID}
    # The sweep is the ONLY shared-gateway call, and it came AFTER the bridge
    # was created on the dedicated gateway (a failed provision leaves the
    # environment untouched).
    shared_calls = [i for i, (_, gw) in enumerate(control.calls) if gw == _SHARED_GW_ID]
    create_idx = control.calls.index(("create_gateway_target", _QUICK_GW_ID))
    assert shared_calls and min(shared_calls) > create_idx, control.calls


def test_no_stale_bridge_target_means_nothing_is_deleted():
    control = _FakeControl(shared_targets=[
        {"name": "example_erp", "targetId": "tgt-erp", "status": "READY"},
    ])
    _run_ensure_bridge(control, {
        "QUICK_GATEWAY_ID": _QUICK_GW_ID, "QUICK_GATEWAY_URL": _QUICK_GW_URL,
    })
    assert control.deleted == []


def test_stale_target_sweep_failure_does_not_fail_provisioning(caplog):
    """The bridge is already serving on the dedicated gateway when the sweep
    runs; a control-plane error listing the shared gateway is logged, not raised."""
    control = _FakeControl(shared_list_error=RuntimeError("AccessDenied"))
    with caplog.at_level(logging.WARNING, logger="agents.agent_manager"):
        url, _ = _run_ensure_bridge(control, {
            "QUICK_GATEWAY_ID": _QUICK_GW_ID, "QUICK_GATEWAY_URL": _QUICK_GW_URL,
        })
    assert url == _QUICK_GW_URL
    assert control.deleted == []
    assert "stale query-bridge" in caplog.text


def test_deletion_gateway_for_bridge_without_quick_env_is_the_shared_gateway():
    """Deletion is cleanup, not provisioning: with no QUICK_GATEWAY_ID it must
    still be able to reach a bridge target a pre-dedicated deployment put on the
    shared gateway, rather than refusing."""
    mgr = _mgr()
    with patch.dict("os.environ", {"QUICK_GATEWAY_ID": ""}, clear=False):
        assert mgr._deletion_gateway_id(None, QUERY_BRIDGE_SYSTEM_ID) == _SHARED_GW_ID
    with patch.dict("os.environ", {"QUICK_GATEWAY_ID": _QUICK_GW_ID}, clear=False):
        assert mgr._deletion_gateway_id(None, QUERY_BRIDGE_SYSTEM_ID) == _QUICK_GW_ID


def test_onboarded_systems_still_use_the_shared_gateway_when_quick_env_is_set():
    """The override is scoped to the bridge: a normal system started while
    QUICK_GATEWAY_ID is set must NOT be provisioned on the Quick gateway."""
    control = _FakeControl()

    def _client(service, region_name=None):
        return control

    env = {"QUICK_GATEWAY_ID": _QUICK_GW_ID, "QUICK_GATEWAY_URL": _QUICK_GW_URL}
    with patch.dict("os.environ", env, clear=False), \
            patch("boto3.client", _client), \
            patch("agents.agent_manager.time.sleep", lambda *_: None), \
            patch.object(AgentManager, "_prepare_runtime_env", return_value={}), \
            patch.object(AgentManager, "_m2m_authorizer_config", return_value=None), \
            patch.object(AgentManager, "_apply_business_context",
                         side_effect=lambda _sid, cfg, _r: cfg), \
            patch.object(AgentManager, "_find_reusable_runtime",
                         return_value=(_RUNTIME_ARN, "rt-sys")), \
            patch.object(AgentManager, "_refresh_existing_runtime_image", return_value=None), \
            patch.object(AgentManager, "_warmup_runtime", return_value=None), \
            patch.object(AgentManager, "_ensure_credential_provider", return_value=""):
        url = _mgr().start_agent(
            system_id="example_erp", system_name="ERP", schema="", system_prompt="",
            tables_desc="", connection_type="PostgreSQL", connection_config={})

    assert url == _SHARED_GW_URL
    assert {gw for _, gw in control.calls} == {_SHARED_GW_ID}


if __name__ == "__main__":  # pragma: no cover - standalone runner
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
