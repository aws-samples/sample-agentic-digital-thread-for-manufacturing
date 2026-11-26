# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Regression pins: ``update_agent_runtime`` must round-trip ``protocolConfiguration``.

Background (root-caused live, 2026-07-28): ``update_agent_runtime`` is a
FULL-REPLACEMENT API — any field omitted from the call is reset to its service
default. Every ADT update path omitted ``protocolConfiguration``, so an MCP
runtime silently reverted to the default ``HTTP`` server protocol. The runtime
still reports READY, but the platform never forwards the MCP payload: MCP
``initialize`` hangs until client timeout, with ZERO inbound requests in the
runtime logs (it presents as cold-start churn, not as a config bug). Observed
on ``adt_lake_athena`` / ``adt_telem_sitewise`` after ``refresh_business_context``
rolled them; restoring the field fixed both immediately.

The fix ROUND-TRIPS the field from the ``get_agent_runtime`` details — never
hardcodes MCP, because the orchestrator runtime is legitimately an HTTP agent.

These tests pin the shared kwargs builder
(``agents.runtime_provision_helpers.runtime_update_kwargs``) AND every update
call site, so a future edit to any one site cannot silently regress:

  1. ``AgentManager._start_cloud`` reuse/image-refresh path
  2. ``AgentManager.resync_cloud_connection`` (via ``_resync_force_runtime_pickup``)
  3. ``AgentManager._bump_runtime_version`` (business-context refresh, ADR-0054,
     and the live agent-model swap, ADR-0055 — via ``refresh_agent_model``)
  4. ``admin_routes._update_orchestrator_runtime_model`` (HTTP round-trip proof)
  5. ``admin_routes.restore_runtime_auth`` (authorizer repair)
  6. ``scripts/_query_mode_lib.build_runtime_update_kwargs`` (query-mode CLI)

Fully offline: boto3 is stubbed; no real AWS calls.

Runnable two ways:
  * pytest:      pytest agentic_digital_thread/tests/unit/test_runtime_update_protocol_roundtrip.py
  * standalone:  python agentic_digital_thread/tests/unit/test_runtime_update_protocol_roundtrip.py
"""
import asyncio
import types
from pathlib import Path
import sys
from unittest.mock import patch

# Make `agents...` importable (agentic_digital_thread/ is two levels up), and
# repo-root scripts/ for the query-mode lib (mirrors test_set_query_mode_lib).
_ADT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ADT_ROOT.parent / "scripts"
for _p in (str(_ADT_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents.agent_manager import AgentManager  # noqa: E402

_MCP = {"serverProtocol": "MCP"}
_HTTP = {"serverProtocol": "HTTP"}


def _details(protocol=None, **overrides):
    """A get_agent_runtime response shape; protocol=None omits the field."""
    d = {
        "status": "READY",
        "environmentVariables": {"SYSTEM_ID": "erp", "AWS_REGION": "us-east-1"},
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "uri:1"}},
        "roleArn": "arn:role",
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": {"subnets": ["s-1"], "securityGroups": ["sg-1"],
                                  "requireServiceS3Endpoint": True},
        },
        "authorizerConfiguration": {
            "customJWTAuthorizer": {"discoveryUrl": "https://d/",
                                    "allowedClients": ["c"]}},
    }
    if protocol is not None:
        d["protocolConfiguration"] = dict(protocol)
    d.update(overrides)
    return d


class _FakeControl:
    """bedrock-agentcore-control stand-in with one READY runtime."""

    def __init__(self, details, runtime_name="adt_erp"):
        self._details = details
        self._runtime_name = runtime_name
        self.update_calls = []

    def list_agent_runtimes(self):
        return {"agentRuntimes": [{
            "agentRuntimeName": self._runtime_name, "agentRuntimeId": "rt-1",
            "agentRuntimeArn":
                "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/rt-1",
            "status": "READY",
        }]}

    def get_agent_runtime(self, agentRuntimeId=None):  # noqa: N803 - boto3 kwarg
        return dict(self._details)

    def update_agent_runtime(self, **kwargs):
        self.update_calls.append(kwargs)
        return {"agentRuntimeId": kwargs.get("agentRuntimeId")}


class _FakeSecrets:
    """Secrets Manager stand-in — create-or-update returns an ARN."""

    class ResourceExistsException(Exception):
        pass

    class ResourceNotFoundException(Exception):
        pass

    def __init__(self, current: dict | None = None):
        import json as _json
        self._current = _json.dumps(current) if current is not None else None
        self.exceptions = types.SimpleNamespace(
            ResourceExistsException=self.ResourceExistsException,
            ResourceNotFoundException=self.ResourceNotFoundException)

    def get_secret_value(self, SecretId):  # noqa: N803 - boto3 kwarg
        if self._current is None:
            raise self.ResourceNotFoundException()
        return {"SecretString": self._current}

    def create_secret(self, **kwargs):
        return {"ARN": "arn:aws:secretsmanager:::secret:adt/agent-config/erp"}

    def put_secret_value(self, **kwargs):
        return {"ARN": "arn:aws:secretsmanager:::secret:adt/agent-config/erp"}

    def describe_secret(self, **kwargs):
        return {"ARN": "arn:aws:secretsmanager:::secret:adt/agent-config/erp"}


def _fake_boto3(control, secrets=None):
    def _client(service, region_name=None, **kw):
        if service == "bedrock-agentcore-control":
            return control
        if service == "secretsmanager" and secrets is not None:
            return secrets
        raise AssertionError(f"unexpected boto3 client: {service}")
    return types.SimpleNamespace(client=_client)


def _mgr(gateway_id=""):
    return AgentManager(mode="cloud", gateway_id=gateway_id,
                        gateway_url="https://gw/mcp",
                        ecr_image_uri="uri:1", runtime_role_arn="arn:role")


# --------------------------------------------------------------------------- #
# Shared builder: agents.runtime_provision_helpers.runtime_update_kwargs
# (imported inside each test so the call-site pins below still run — and fail
# on the missing field — even before the builder exists.)
# --------------------------------------------------------------------------- #

def test_builder_roundtrips_mcp_protocol():
    """MCP in details ⇒ the update kwargs carry it UNCHANGED (the brick fix)."""
    from agents.runtime_provision_helpers import runtime_update_kwargs
    kwargs = runtime_update_kwargs("rt-1", _details(_MCP), {"K": "v"})
    assert kwargs["protocolConfiguration"] == _MCP, kwargs


def test_builder_roundtrips_http_protocol_never_hardcodes_mcp():
    """HTTP in details (the orchestrator shape) ⇒ HTTP out — proves the fix
    round-trips instead of hardcoding MCP."""
    from agents.runtime_provision_helpers import runtime_update_kwargs
    kwargs = runtime_update_kwargs("rt-1", _details(_HTTP), {"K": "v"})
    assert kwargs["protocolConfiguration"] == _HTTP, kwargs


def test_builder_omits_key_when_details_have_none():
    """No protocolConfiguration in details ⇒ the key is OMITTED (no None/empty
    value the API would reject)."""
    from agents.runtime_provision_helpers import runtime_update_kwargs
    kwargs = runtime_update_kwargs("rt-1", _details(None), {"K": "v"})
    assert "protocolConfiguration" not in kwargs, kwargs
    # An empty dict in details is also not echoed (tagged member required).
    kwargs = runtime_update_kwargs("rt-1", _details({}), {"K": "v"})
    assert "protocolConfiguration" not in kwargs, kwargs


def test_builder_keeps_existing_behaviours_pinned():
    """requireServiceS3Endpoint is DROPPED; authorizerConfiguration appears only
    when a customJWTAuthorizer is set (tagged union — {} fails the call)."""
    from agents.runtime_provision_helpers import runtime_update_kwargs
    kwargs = runtime_update_kwargs("rt-1", _details(_MCP), {"K": "v"})
    assert "requireServiceS3Endpoint" not in \
        kwargs["networkConfiguration"]["networkModeConfig"], kwargs
    assert kwargs["authorizerConfiguration"]["customJWTAuthorizer"][
        "allowedClients"] == ["c"]
    # No authorizer in details ⇒ key omitted.
    no_auth = _details(_MCP)
    no_auth.pop("authorizerConfiguration")
    kwargs = runtime_update_kwargs("rt-1", no_auth, {"K": "v"})
    assert "authorizerConfiguration" not in kwargs, kwargs


def test_builder_overrides_artifact_role_and_authorizer():
    """The _start_cloud reuse path supplies a FRESH artifact/role/authorizer;
    an explicit empty authorizer override means 'omit' (never falls back to
    details), preserving that path's existing semantics."""
    from agents.runtime_provision_helpers import runtime_update_kwargs
    fresh_auth = {"customJWTAuthorizer": {"discoveryUrl": "https://new/",
                                          "allowedClients": ["n"]}}
    kwargs = runtime_update_kwargs(
        "rt-1", _details(_MCP), {"K": "v"},
        artifact={"containerConfiguration": {"containerUri": "uri:2"}},
        role_arn="arn:new-role", authorizer_config=fresh_auth)
    assert kwargs["agentRuntimeArtifact"]["containerConfiguration"][
        "containerUri"] == "uri:2"
    assert kwargs["roleArn"] == "arn:new-role"
    assert kwargs["authorizerConfiguration"] == fresh_auth
    assert kwargs["protocolConfiguration"] == _MCP
    kwargs = runtime_update_kwargs("rt-1", _details(_MCP), {"K": "v"},
                                   authorizer_config={})
    assert "authorizerConfiguration" not in kwargs, kwargs


# --------------------------------------------------------------------------- #
# Call site 1: _start_cloud reuse/image-refresh path
# --------------------------------------------------------------------------- #

def test_start_cloud_reuse_path_roundtrips_protocol():
    control = _FakeControl(_details(_MCP), runtime_name="adt_erp")
    secrets = _FakeSecrets()
    with patch("boto3.client", _fake_boto3(control, secrets).client), \
            patch.object(AgentManager, "_warmup_runtime",
                         lambda self, url, region: None), \
            patch.object(AgentManager, "_ensure_credential_provider",
                         lambda self, control, region: ""):
        _mgr()._start_cloud(
            system_id="erp", system_name="ERP", schema="",
            system_prompt="p", tables_desc="t",
            connection_type="PostgreSQL",
            connection_config={"host": "h", "database": "d"})
    assert len(control.update_calls) == 1, control.update_calls
    assert control.update_calls[0].get("protocolConfiguration") == _MCP, \
        control.update_calls[0]


# --------------------------------------------------------------------------- #
# Call site 2: resync_cloud_connection -> _resync_force_runtime_pickup
# --------------------------------------------------------------------------- #

def test_resync_pickup_roundtrips_protocol():
    control = _FakeControl(_details(_MCP), runtime_name="adt_erp")
    secrets = _FakeSecrets()
    with patch("agents.agent_manager.boto3", _fake_boto3(control, secrets)):
        ok = _mgr().resync_cloud_connection(
            system_id="erp", connection_type="REST API",
            connection_config={"base_url": "https://api.example.com/"})
    assert ok is True
    assert len(control.update_calls) == 1, control.update_calls
    assert control.update_calls[0].get("protocolConfiguration") == _MCP, \
        control.update_calls[0]


# --------------------------------------------------------------------------- #
# Call site 3: _bump_runtime_version (business-context refresh ADR-0054 and the
# live agent-model swap ADR-0055 — exercised through refresh_agent_model)
# --------------------------------------------------------------------------- #

def test_bump_runtime_version_roundtrips_protocol():
    control = _FakeControl(_details(_MCP), runtime_name="adt_erp")
    with patch("agents.agent_manager.boto3", _fake_boto3(control)):
        outcome = _mgr().refresh_agent_model("erp", "us.amazon.nova-pro-v1:0")
    assert outcome == "ok"
    assert len(control.update_calls) == 1, control.update_calls
    upd = control.update_calls[0]
    assert upd.get("protocolConfiguration") == _MCP, upd
    # The model override still lands and existing env is preserved.
    assert upd["environmentVariables"]["SYSTEM_AGENT_MODEL"] == \
        "us.amazon.nova-pro-v1:0"
    assert upd["environmentVariables"]["SYSTEM_ID"] == "erp"


# --------------------------------------------------------------------------- #
# Call site 4: admin_routes._update_orchestrator_runtime_model — the
# orchestrator is legitimately HTTP, so this pins the HTTP round-trip.
# --------------------------------------------------------------------------- #

def test_orchestrator_model_update_roundtrips_http_protocol():
    from agents import admin_routes
    control = _FakeControl(_details(_HTTP), runtime_name="adt_orchestrator")
    with patch("boto3.client", _fake_boto3(control).client):
        err = admin_routes._update_orchestrator_runtime_model(
            "us-east-1", "us.anthropic.claude-sonnet-5")
    assert err == "", err
    assert len(control.update_calls) == 1, control.update_calls
    upd = control.update_calls[0]
    assert upd.get("protocolConfiguration") == _HTTP, upd
    assert upd["environmentVariables"]["BEDROCK_MODEL"] == \
        "us.anthropic.claude-sonnet-5"


# --------------------------------------------------------------------------- #
# Call site 5: admin_routes.restore_runtime_auth (authorizer repair) — must
# read the runtime details and round-trip protocol + env, not send a partial
# payload built from a list entry.
# --------------------------------------------------------------------------- #

def test_restore_runtime_auth_roundtrips_protocol_and_env(monkeypatch):
    from agents import admin_routes
    monkeypatch.setenv("M2M_DISCOVERY_URL", "https://disc/")
    monkeypatch.setenv("M2M_CLIENT_ID", "client-1")
    control = _FakeControl(_details(_MCP), runtime_name="adt_erp")
    req = types.SimpleNamespace(state=types.SimpleNamespace(is_admin=True))
    with patch("boto3.client", _fake_boto3(control).client):
        result = asyncio.run(admin_routes.restore_runtime_auth_route(req))
    assert result.get("results"), result
    assert len(control.update_calls) == 1, control.update_calls
    upd = control.update_calls[0]
    assert upd.get("protocolConfiguration") == _MCP, upd
    # Full-replacement API: env must be round-tripped too, or the repair
    # wipes every environment variable on the runtime.
    assert upd.get("environmentVariables", {}).get("SYSTEM_ID") == "erp", upd
    # And the repair still applies the fresh authorizer from the env config.
    assert upd["authorizerConfiguration"]["customJWTAuthorizer"][
        "allowedClients"] == ["client-1"]


# --------------------------------------------------------------------------- #
# Call site 6 (beyond the brief's five, same defect class): the query-mode CLI
# lib's update-kwargs builder.
# --------------------------------------------------------------------------- #

def test_query_mode_update_kwargs_roundtrips_protocol():
    from _query_mode_lib import build_runtime_update_kwargs
    details = _details(_MCP, agentRuntimeId="rt-1")
    kwargs = build_runtime_update_kwargs(details, "agent")
    assert kwargs.get("protocolConfiguration") == _MCP, kwargs
    # And omitted when absent (no None/empty echo).
    details = _details(None, agentRuntimeId="rt-1")
    assert "protocolConfiguration" not in \
        build_runtime_update_kwargs(details, "agent")


_TESTS = [
    test_builder_roundtrips_mcp_protocol,
    test_builder_roundtrips_http_protocol_never_hardcodes_mcp,
    test_builder_omits_key_when_details_have_none,
    test_builder_keeps_existing_behaviours_pinned,
    test_builder_overrides_artifact_role_and_authorizer,
    test_start_cloud_reuse_path_roundtrips_protocol,
    test_resync_pickup_roundtrips_protocol,
    test_bump_runtime_version_roundtrips_protocol,
    test_orchestrator_model_update_roundtrips_http_protocol,
    test_query_mode_update_kwargs_roundtrips_protocol,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} protocol round-trip tests passed "
          "(no real AWS)")


class TestProtocolRepairOverride:
    """Round-tripping PREVENTS the ADR-0056 downgrade but cannot REPAIR one.

    A runtime already reset to HTTP round-trips as HTTP forever, so callers that
    know the correct protocol by construction must be able to assert it.
    """

    def _details(self, proto):
        d = {"agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "x"}},
             "roleArn": "arn:aws:iam::111122223333:role/r",
             "networkConfiguration": {"networkMode": "PUBLIC"},
             "environmentVariables": {"SYSTEM_ID": "erp"}}
        if proto is not None:
            d["protocolConfiguration"] = proto
        return d

    def test_override_repairs_a_downgraded_runtime(self):
        """The bug's signature: details say HTTP; the caller knows it must be MCP."""
        from agents.runtime_provision_helpers import runtime_update_kwargs
        kw = runtime_update_kwargs(
            "r1", self._details({"serverProtocol": "HTTP"}), {"SYSTEM_ID": "erp"},
            protocol_override={"serverProtocol": "MCP"})
        assert kw["protocolConfiguration"] == {"serverProtocol": "MCP"}

    def test_override_also_repairs_a_wholly_absent_protocol(self):
        from agents.runtime_provision_helpers import runtime_update_kwargs
        kw = runtime_update_kwargs(
            "r1", self._details(None), {"SYSTEM_ID": "erp"},
            protocol_override={"serverProtocol": "MCP"})
        assert kw["protocolConfiguration"] == {"serverProtocol": "MCP"}

    def test_without_override_the_roundtrip_is_unchanged(self):
        """Default behavior must stay byte-identical — the orchestrator is
        legitimately HTTP and must never be forced to MCP."""
        from agents.runtime_provision_helpers import runtime_update_kwargs
        kw = runtime_update_kwargs(
            "r1", self._details({"serverProtocol": "HTTP"}), {"SYSTEM_ID": "orch"})
        assert kw["protocolConfiguration"] == {"serverProtocol": "HTTP"}

    def test_override_does_not_disturb_other_fields(self, monkeypatch):
        from agents.runtime_provision_helpers import (
            ONBOARDING_SESSIONS_TABLE_DEFAULT,
            runtime_update_kwargs,
        )
        monkeypatch.delenv("ONBOARDING_SESSIONS_TABLE", raising=False)
        kw = runtime_update_kwargs(
            "r1", self._details({"serverProtocol": "HTTP"}), {"SYSTEM_ID": "erp"},
            protocol_override={"serverProtocol": "MCP"})
        # Caller env passes through PLUS the provenance-store fill (the
        # full-replacement builder must never strip ONBOARDING_SESSIONS_TABLE
        # — see test_provenance_env_always_present.py).
        assert kw["environmentVariables"] == {
            "SYSTEM_ID": "erp",
            "ONBOARDING_SESSIONS_TABLE": ONBOARDING_SESSIONS_TABLE_DEFAULT,
        }
        assert kw["roleArn"] == "arn:aws:iam::111122223333:role/r"
        assert kw["networkConfiguration"] == {"networkMode": "PUBLIC"}
