# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for the query-gateway reconcile helper
(scripts/_reconcile_query_gateway_lib.py and scripts/reconcile_query_gateway.py).

Everything here is fully offline: the AgentCore control-plane client is a stub
(no boto3, no live AWS). Mirrors the stub style in
``tests/unit/test_reonboard_lib.py``.

Coverage (task deliverable):
  * ``mcp_url_for_arn`` matches ``AgentManager._runtime_mcp_url`` byte-for-byte
    and round-trips through ``extract_runtime_arn`` (no drift in the URL format);
  * ``reconcile_query_gateway`` heals ONLY drifted targets, is idempotent (a
    second run is a no-op), preserves name/description/credential config on
    update, and skips non-runtime (builtin) targets;
  * ``--dry-run`` detects drift without writing;
  * a target whose backing runtime is gone raises RuntimeMissingForTargetError
    (fail-loud).

Run:
  * pytest:     pytest agentic_digital_thread/tests/unit/test_reconcile_query_gateway.py -q
  * standalone: python agentic_digital_thread/tests/unit/test_reconcile_query_gateway.py
"""
import sys
from pathlib import Path

# agentic_digital_thread/ (for agents.*) and repo-root scripts/ (for the helpers under test).
_ADT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ADT_ROOT.parent / "scripts"
for _p in (str(_ADT_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

from _reconcile_query_gateway_lib import (  # noqa: E402
    RuntimeMissingForTargetError,
    extract_runtime_arn,
    mcp_url_for_arn,
    reconcile_query_gateway,
)

REGION = "us-east-1"
ACCT = "111122223333"


def _arn(name: str, suffix: str) -> str:
    return f"arn:aws:bedrock-agentcore:{REGION}:{ACCT}:runtime/{name}-{suffix}"


# --------------------------------------------------------------------------- #
# Fake AgentCore control-plane client
# --------------------------------------------------------------------------- #
class _FakeControl:
    """Stub for the reconcile client.

    * ``runtimes``: {runtimeName: arn}  — what list_agent_runtimes reports now.
    * ``targets``:  {targetId: detail}  — full get_gateway_target detail.
    ``update_gateway_target`` records each call and mutates the stored detail so
    a follow-up reconcile observes the healed state (idempotence test)."""

    def __init__(self):
        self.runtimes: dict[str, str] = {}
        self.targets: dict[str, dict] = {}
        self.updates: list[dict] = []
        self.list_runtime_calls = 0

    # -- scripting helpers (test-only) --
    def add_runtime(self, name, arn):
        self.runtimes[name] = arn

    def add_target(self, target_id, name, endpoint, *, description="", cred=None, extra_cfg=None):
        cfg = extra_cfg if extra_cfg is not None else {"mcp": {"mcpServer": {"endpoint": endpoint}}}
        self.targets[target_id] = {
            "targetId": target_id,
            "name": name,
            "description": description,
            "targetConfiguration": cfg,
            "credentialProviderConfigurations": cred or [],
        }

    # -- control-plane surface --
    def list_agent_runtimes(self):
        self.list_runtime_calls += 1
        return {
            "agentRuntimeSummaries": [
                {"agentRuntimeName": n, "agentRuntimeArn": a} for n, a in self.runtimes.items()
            ]
        }

    def list_gateway_targets(self, gatewayIdentifier, nextToken=None):
        return {
            "items": [
                {"targetId": tid, "name": d["name"], "status": "READY"}
                for tid, d in self.targets.items()
            ]
        }

    def get_gateway_target(self, gatewayIdentifier, targetId):
        return dict(self.targets[targetId])

    def update_gateway_target(self, **kwargs):
        self.updates.append(kwargs)
        # Reflect the change so a second reconcile sees it as in-sync.
        tid = kwargs["targetId"]
        self.targets[tid]["targetConfiguration"] = kwargs["targetConfiguration"]
        if "credentialProviderConfigurations" in kwargs:
            self.targets[tid]["credentialProviderConfigurations"] = kwargs[
                "credentialProviderConfigurations"
            ]
        return {"targetId": tid, "status": "READY"}


# --------------------------------------------------------------------------- #
# URL format parity + round-trip
# --------------------------------------------------------------------------- #
def test_mcp_url_matches_agent_manager_format():
    from agents.agent_manager import AgentManager

    arn = _arn("adt_mes_mil", "PIQcGBBxlq")
    assert mcp_url_for_arn(arn, REGION) == AgentManager._runtime_mcp_url(arn, REGION)


def test_extract_runtime_arn_round_trips():
    arn = _arn("adt_erp", "gQVJBRAu68")
    url = mcp_url_for_arn(arn, REGION)
    assert extract_runtime_arn(url) == arn


def test_extract_runtime_arn_none_for_non_runtime_endpoint():
    assert extract_runtime_arn("") is None
    assert extract_runtime_arn(None) is None
    assert extract_runtime_arn("https://example.com/lambda/tool") is None
    # A runtimes-looking URL that doesn't decode to a runtime ARN.
    assert extract_runtime_arn("https://x/runtimes/not-an-arn/invocations") is None


# --------------------------------------------------------------------------- #
# reconcile: heal drift, idempotent, preserve config
# --------------------------------------------------------------------------- #
def _seed_drifted_and_current():
    """One drifted target (mes-mil) + one already-current target (cmms)."""
    c = _FakeControl()
    # Current runtimes.
    c.add_runtime("adt_mes_mil", _arn("adt_mes_mil", "PIQcGBBxlq"))
    c.add_runtime("adt_cmms", _arn("adt_cmms", "fs8ro38N9x"))
    # mes-mil target points at a STALE arn; cmms target is current.
    c.add_target(
        "t-mes",
        "mes-mil",
        mcp_url_for_arn(_arn("adt_mes_mil", "AWLkNW7aOLD"), REGION),
        description="System agent for MES Milwaukee",
        cred=[{"credentialProviderType": "OAUTH"}],
    )
    c.add_target(
        "t-cmms",
        "cmms",
        mcp_url_for_arn(_arn("adt_cmms", "fs8ro38N9x"), REGION),
        description="System agent for CMMS",
    )
    return c


def test_reconcile_heals_only_drifted_target():
    c = _seed_drifted_and_current()
    report = reconcile_query_gateway(c, "gw-1", REGION)

    assert report["checked"] == 2
    assert report["in_sync"] == 1
    assert report["drifted"] == 1
    assert report["updated"] == 1
    assert len(c.updates) == 1

    upd = c.updates[0]
    assert upd["targetId"] == "t-mes"
    assert upd["name"] == "mes-mil"
    # repointed to the current arn
    new_ep = upd["targetConfiguration"]["mcp"]["mcpServer"]["endpoint"]
    assert extract_runtime_arn(new_ep) == _arn("adt_mes_mil", "PIQcGBBxlq")
    # preserved description + credential config
    assert upd["description"] == "System agent for MES Milwaukee"
    assert upd["credentialProviderConfigurations"] == [{"credentialProviderType": "OAUTH"}]


def test_reconcile_is_idempotent():
    c = _seed_drifted_and_current()
    reconcile_query_gateway(c, "gw-1", REGION)
    # Second pass: everything now current → no further writes.
    report2 = reconcile_query_gateway(c, "gw-1", REGION)
    assert report2["drifted"] == 0
    assert report2["updated"] == 0
    assert len(c.updates) == 1  # unchanged from the first pass


def test_dry_run_detects_without_writing():
    c = _seed_drifted_and_current()
    report = reconcile_query_gateway(c, "gw-1", REGION, apply=False)
    assert report["drifted"] == 1
    assert report["updated"] == 0
    assert c.updates == []


def test_reconcile_skips_non_runtime_targets():
    c = _FakeControl()
    c.add_runtime("adt_mes_mil", _arn("adt_mes_mil", "PIQcGBBxlq"))
    # A builtin (lambda/openapi) target with no MCP runtime endpoint.
    c.add_target("t-builtin", "guidance-tools", "", extra_cfg={"lambda": {"functionArn": "x"}})
    c.add_target(
        "t-mes", "mes-mil", mcp_url_for_arn(_arn("adt_mes_mil", "PIQcGBBxlq"), REGION)
    )
    report = reconcile_query_gateway(c, "gw-1", REGION)
    assert report["skipped"] == 1
    assert report["checked"] == 1
    assert report["in_sync"] == 1
    assert c.updates == []


def test_reconcile_fail_loud_when_runtime_missing():
    c = _FakeControl()
    # No live runtime named adt_plm, but a target still points at one.
    c.add_target(
        "t-plm", "plm", mcp_url_for_arn(_arn("adt_plm", "RLGS068RCc"), REGION)
    )
    with pytest.raises(RuntimeMissingForTargetError) as ei:
        reconcile_query_gateway(c, "gw-1", REGION)
    assert ei.value.runtime_name == "adt_plm"
    assert c.updates == []


def test_reconcile_requires_gateway_id():
    from _reconcile_query_gateway_lib import QueryGatewayReconcileError

    with pytest.raises(QueryGatewayReconcileError):
        reconcile_query_gateway(_FakeControl(), "", REGION)


# --------------------------------------------------------------------------- #
# Naming parity with AgentManager (DRY — the runtime we look up is the one it makes)
# --------------------------------------------------------------------------- #
def test_target_name_maps_to_agent_manager_runtime_name():
    from agents.agent_manager import AgentManager

    # target name is system_id with '_'->'-'; the runtime we reconcile against
    # must be exactly AgentManager._runtime_name(system_id).
    from _reonboard_lib import runtime_name

    for system_id in ("mes-mil", "erp", "plm", "mes_cr"):
        target_nm = system_id.replace("_", "-")
        assert runtime_name(target_nm) == AgentManager._runtime_name(system_id)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
