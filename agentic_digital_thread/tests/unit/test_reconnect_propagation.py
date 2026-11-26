# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for CLOUD/gateway-mode connection-change propagation (RW1 Task 1.3).

Background (the bug these tests pin down): a ``PUT /api/admin/connection/{id}``
reconnect in gateway mode used to call ``stop_agent`` -> ``start_agent`` -> 
``add_system_agent``. But ``start_agent`` fast-paths when a READY gateway target
already exists (the steady state after deploy) and returns the gateway URL WITHOUT
calling ``_start_cloud`` — so the ``adt/agent-config/<id>`` secret was
never rewritten. And even if it had been, a running AgentCore runtime resolves its
downstream connection ONCE at container start
(``runtime_app._run_system_agent`` -> ``system_agent.create_mcp_server``) and never
re-reads it. Net: the changed URL/token did NOT reach the running runtime until a
redeploy.

The fix routes the cloud branch through ``AgentManager.resync_cloud_connection``,
which (1) rewrites the agent-config secret with the freshly-resolved config and
(2) deploys a new runtime version so the replacement container re-reads it.

These tests assert:
  * the admin cloud branch calls ``resync_cloud_connection`` with the freshly
    RESOLVED ``connection_config`` (not the raw request body), and maps its
    bool to ``reconnected``;
  * ``resync_cloud_connection`` rewrites the secret with the fresh config AND
    issues ``update_agent_runtime`` (forcing pickup), never logging secret values.

Runnable two ways (matches ``test_admin_connection_test.py``):
  * pytest:      pytest agentic_digital_thread/tests/unit/test_reconnect_propagation.py
  * standalone:  python agentic_digital_thread/tests/unit/test_reconnect_propagation.py
"""
import asyncio
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.connection_resolver as cr  # noqa: E402
import agents.config as appconfig  # noqa: E402
import agents.admin_routes as admin_routes  # noqa: E402
from agents.admin_routes import update_connection as update_connection_route  # noqa: E402
from agents.agent_manager import AgentManager  # noqa: E402


def _make_request(is_admin: bool, email: str = "admin@example.com"):
    req = types.SimpleNamespace()
    req.state = types.SimpleNamespace(
        is_admin=is_admin,
        user=types.SimpleNamespace(email=email),
    )
    return req


class _UpdateCatalog:
    def __init__(self, known: dict):
        self._known = known

    def get(self, system_id):
        return self._known.get(system_id)


def _patch_update_catalog(known: dict):
    return patch("agents.catalog.catalog", _UpdateCatalog(known))


def _gateway_config():
    """Patch get_config() (only consulted inside _reconnect_system) to gateway mode."""
    return patch.object(appconfig, "get_config",
                        lambda: types.SimpleNamespace(has_gateway=True))


# --------------------------------------------------------------------------- #
# admin_routes cloud branch — wiring + propagation contract
# --------------------------------------------------------------------------- #

def test_cloud_reconnect_calls_resync_with_resolved_config():
    """The gateway-mode branch must call resync_cloud_connection with the FRESHLY
    RESOLVED connection_config (URL+token), NOT the raw request body, and map a
    truthy result to reconnected:true. It must NOT fall back to start_agent."""
    resolved_cfg = {"connection_type": "MCP Server",
                    "url": "https://new.example.com/mcp", "token": "tok-NEW"}
    pass_result = {"status": "pass", "latency_ms": 4, "connection_type": "MCP Server",
                   "reason": "ok", "detail": "ok"}
    calls = {"resync": [], "start_agent": []}

    def _resync(**kwargs):
        calls["resync"].append(kwargs)
        return True

    def _start_agent(**kwargs):
        calls["start_agent"].append(kwargs)
        return "https://gateway.example.com/mcp"

    fake_mgr = types.SimpleNamespace(
        resync_cloud_connection=_resync, start_agent=_start_agent)
    admin_routes.set_agent_manager(fake_mgr)
    try:
        with _patch_update_catalog({"hb": {"name": "HB", "schema": "",
                                           "connection_type": "MCP Server"}}), \
                _gateway_config(), \
                patch.object(cr, "resolve_connection", lambda sid: dict(resolved_cfg)), \
                patch.object(cr, "probe_connection", lambda sid, resolved=None: pass_result), \
                patch.object(cr, "write_connection", lambda sid, **k: "arn:x"), \
                patch.object(cr, "audit_connection_event", lambda *a, **k: None):
            res = asyncio.run(update_connection_route(
                "hb", _make_request(is_admin=True),
                {"connection_type": "MCP Server",
                 "url": "https://new.example.com/mcp", "token": "tok-NEW"}))
    finally:
        admin_routes.set_agent_manager(None)

    # resync_cloud_connection was called exactly once with the resolved config.
    assert len(calls["resync"]) == 1, calls
    kw = calls["resync"][0]
    assert kw["system_id"] == "hb", kw
    assert kw["connection_type"] == "MCP Server", kw
    assert kw["connection_config"] == resolved_cfg, kw
    # The no-op fast-path primitive was NOT used.
    assert calls["start_agent"] == [], calls
    # Endpoint result.
    assert isinstance(res, dict) and res["status"] == "ok", res
    assert res["reconnected"] is True, res
    # The masked echo never leaks the token.
    assert "tok-new" not in json.dumps(res).lower(), res


def test_cloud_reconnect_failure_returns_200_reconnected_false():
    """When resync_cloud_connection returns False (e.g. runtime update failed), the
    endpoint still returns 200 with reconnected:false — the config write already
    succeeded and is persisted — NEVER a 500."""
    pass_result = {"status": "pass", "latency_ms": 2, "connection_type": "MCP Server",
                   "reason": "ok", "detail": "ok"}
    fake_mgr = types.SimpleNamespace(
        resync_cloud_connection=lambda **k: False,
        start_agent=lambda **k: "https://gateway.example.com/mcp")
    admin_routes.set_agent_manager(fake_mgr)
    try:
        with _patch_update_catalog({"hb": {"name": "HB", "connection_type": "MCP Server"}}), \
                _gateway_config(), \
                patch.object(cr, "resolve_connection",
                             lambda sid: {"connection_type": "MCP Server",
                                          "url": "https://new.example.com/mcp"}), \
                patch.object(cr, "probe_connection", lambda sid, resolved=None: pass_result), \
                patch.object(cr, "write_connection", lambda sid, **k: "arn:x"), \
                patch.object(cr, "audit_connection_event", lambda *a, **k: None):
            res = asyncio.run(update_connection_route(
                "hb", _make_request(is_admin=True),
                {"connection_type": "MCP Server", "url": "https://new.example.com/mcp"}))
    finally:
        admin_routes.set_agent_manager(None)

    assert isinstance(res, dict), res
    assert res["status"] == "ok", res
    assert res["reconnected"] is False, res


def test_cloud_reconnect_no_agent_manager_returns_false():
    """Gateway mode with no agent_manager wired → reconnected:false, still 200."""
    pass_result = {"status": "pass", "latency_ms": 1, "connection_type": "MCP Server",
                   "reason": "ok", "detail": "ok"}
    admin_routes.set_agent_manager(None)
    with _patch_update_catalog({"hb": {"name": "HB", "connection_type": "MCP Server"}}), \
            _gateway_config(), \
            patch.object(cr, "resolve_connection",
                         lambda sid: {"connection_type": "MCP Server",
                                      "url": "https://new.example.com/mcp"}), \
            patch.object(cr, "probe_connection", lambda sid, resolved=None: pass_result), \
            patch.object(cr, "write_connection", lambda sid, **k: "arn:x"), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: None):
        res = asyncio.run(update_connection_route(
            "hb", _make_request(is_admin=True),
            {"connection_type": "MCP Server", "url": "https://new.example.com/mcp"}))

    assert isinstance(res, dict) and res["status"] == "ok", res
    assert res["reconnected"] is False, res


# --------------------------------------------------------------------------- #
# AgentManager.resync_cloud_connection — secret rewrite + forced runtime pickup
# --------------------------------------------------------------------------- #

class _FakeSecrets:
    """Secrets Manager stand-in recording create/put/describe."""

    class ResourceExistsException(Exception):
        pass

    def __init__(self, exists: bool = False):
        self._exists = exists
        self.create_calls = []
        self.put_calls = []
        self.exceptions = types.SimpleNamespace(
            ResourceExistsException=self.ResourceExistsException)

    def create_secret(self, **kwargs):
        self.create_calls.append(kwargs)
        if self._exists:
            raise self.ResourceExistsException()
        return {"ARN": "arn:aws:secretsmanager:::secret:adt/agent-config/hb-new"}

    def put_secret_value(self, **kwargs):
        self.put_calls.append(kwargs)
        return {"ARN": "arn:aws:secretsmanager:::secret:adt/agent-config/hb-existing"}

    def describe_secret(self, **kwargs):
        return {"ARN": "arn:aws:secretsmanager:::secret:adt/agent-config/hb-existing"}


class _FakeControl:
    """bedrock-agentcore-control stand-in."""

    def __init__(self, runtime_found: bool = True, details: dict | None = None,
                 runtime_name: str = "adt_hb"):
        self._runtime_found = runtime_found
        self._runtime_name = runtime_name
        self._details = details if details is not None else {
            "environmentVariables": {"SYSTEM_ID": "hb", "AWS_REGION": "us-east-1"},
            "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "uri:1"}},
            "roleArn": "arn:role",
            "networkConfiguration": {
                "networkMode": "VPC",
                "networkModeConfig": {"subnets": ["s-1"], "securityGroups": ["sg-1"],
                                      "requireServiceS3Endpoint": True},
            },
            "authorizerConfiguration": {
                "customJWTAuthorizer": {"discoveryUrl": "https://d/", "allowedClients": ["c"]}},
        }
        self.update_calls = []

    def list_agent_runtimes(self):
        if not self._runtime_found:
            return {"agentRuntimes": []}
        return {"agentRuntimes": [{"agentRuntimeName": self._runtime_name,
                                   "agentRuntimeId": "rt-1"}]}

    def get_agent_runtime(self, agentRuntimeId=None):  # noqa: N803 - boto3 kwarg name
        return dict(self._details)

    def update_agent_runtime(self, **kwargs):
        self.update_calls.append(kwargs)
        return {"agentRuntimeId": kwargs.get("agentRuntimeId")}


def _fake_boto3(secrets, control):
    def _client(service, region_name=None):
        if service == "secretsmanager":
            return secrets
        if service == "bedrock-agentcore-control":
            return control
        raise AssertionError(f"unexpected boto3 client: {service}")
    return types.SimpleNamespace(client=_client)


def _mgr():
    return AgentManager(mode="cloud", gateway_id="gw", gateway_url="https://gw/mcp",
                        ecr_image_uri="uri:1", runtime_role_arn="arn:role")


def test_resync_rewrites_secret_and_updates_runtime():
    """Happy path: new secret created with the fresh config JSON, then
    update_agent_runtime is called with the new CONNECTION_SECRET_ARN env and the
    requireServiceS3Endpoint key stripped; returns True."""
    secrets = _FakeSecrets(exists=False)
    control = _FakeControl(runtime_found=True)
    fresh = {"connection_type": "MCP Server", "url": "https://new.example.com/mcp",
             "token": "tok-NEW"}
    with patch("agents.agent_manager.boto3", _fake_boto3(secrets, control)):
        ok = _mgr().resync_cloud_connection(
            system_id="hb", connection_type="MCP Server", connection_config=fresh)

    assert ok is True
    # Secret rewritten with the EXACT fresh config.
    assert len(secrets.create_calls) == 1, secrets.create_calls
    assert secrets.create_calls[0]["Name"] == "adt/agent-config/hb"
    assert json.loads(secrets.create_calls[0]["SecretString"]) == fresh
    # Runtime update forced (pickup), with the new secret ARN injected into env.
    assert len(control.update_calls) == 1, control.update_calls
    upd = control.update_calls[0]
    assert upd["agentRuntimeId"] == "rt-1", upd
    assert upd["environmentVariables"]["CONNECTION_SECRET_ARN"].endswith("agent-config/hb-new")
    # Pre-existing env preserved.
    assert upd["environmentVariables"]["SYSTEM_ID"] == "hb", upd
    # requireServiceS3Endpoint stripped from the network config.
    assert "requireServiceS3Endpoint" not in upd["networkConfiguration"]["networkModeConfig"]
    # Authorizer preserved (tagged union present).
    assert upd["authorizerConfiguration"]["customJWTAuthorizer"]["allowedClients"] == ["c"]


def test_resync_existing_secret_uses_put_secret_value():
    """When the secret already exists, the put_secret_value path runs and the
    fresh config still lands; runtime is still updated."""
    secrets = _FakeSecrets(exists=True)
    control = _FakeControl(runtime_found=True)
    fresh = {"connection_type": "REST API", "base_url": "https://api.example.com/"}
    with patch("agents.agent_manager.boto3", _fake_boto3(secrets, control)):
        ok = _mgr().resync_cloud_connection(
            system_id="hb", connection_type="REST API", connection_config=fresh)

    assert ok is True
    assert len(secrets.put_calls) == 1, secrets.put_calls
    assert json.loads(secrets.put_calls[0]["SecretString"]) == fresh
    assert len(control.update_calls) == 1, control.update_calls


def test_resync_returns_false_when_runtime_missing():
    """Secret is still rewritten, but with no matching runtime the method cannot
    force pickup → returns False (no update_agent_runtime call)."""
    secrets = _FakeSecrets(exists=False)
    control = _FakeControl(runtime_found=False)
    with patch("agents.agent_manager.boto3", _fake_boto3(secrets, control)):
        ok = _mgr().resync_cloud_connection(
            system_id="hb", connection_type="MCP Server",
            connection_config={"url": "https://new/mcp"})

    assert ok is False
    assert len(secrets.create_calls) == 1, secrets.create_calls  # secret WAS rewritten
    assert control.update_calls == [], control.update_calls       # but no pickup


def test_resync_postgres_merges_conn_params_into_secret():
    """For PostgreSQL, conn_params (host/port/dbname/user/password) are merged into
    the persisted effective_config (mirrors _start_cloud)."""
    secrets = _FakeSecrets(exists=False)
    control = _FakeControl(runtime_found=True, runtime_name="adt_erp")
    base = {"connection_type": "PostgreSQL", "connection_string": "postgresql://h/db"}
    conn_params = {"host": "h", "port": 5432, "dbname": "db", "user": "u", "password": "pw"}
    with patch("agents.agent_manager.boto3", _fake_boto3(secrets, control)):
        ok = _mgr().resync_cloud_connection(
            system_id="erp", connection_type="PostgreSQL",
            connection_config=base, conn_params=conn_params)

    assert ok is True
    persisted = json.loads(secrets.create_calls[0]["SecretString"])
    assert persisted["host"] == "h" and persisted["password"] == "pw", persisted
    assert persisted["connection_string"] == "postgresql://h/db", persisted


def test_resync_never_logs_secret_values():
    """Secret VALUES (token/password) must never reach the logs — only the ARN and
    exception type are logged."""
    secrets = _FakeSecrets(exists=False)
    control = _FakeControl(runtime_found=True)
    fake_logger = MagicMock()
    fresh = {"url": "https://new/mcp", "token": "tok-SECRET-XYZ",
             "password": "pw-SECRET-XYZ"}
    with patch("agents.agent_manager.boto3", _fake_boto3(secrets, control)), \
            patch("agents.agent_manager.logger", fake_logger):
        ok = _mgr().resync_cloud_connection(
            system_id="hb", connection_type="MCP Server", connection_config=fresh)

    assert ok is True
    rendered = []
    for level in (fake_logger.info, fake_logger.warning, fake_logger.error):
        for call in level.call_args_list:
            args = call.args
            if not args:
                continue
            try:
                rendered.append(str(args[0]) % tuple(args[1:]))
            except Exception:
                rendered.append(" ".join(str(a) for a in args))
    blob = " ".join(rendered)
    assert "tok-SECRET-XYZ" not in blob, blob
    assert "pw-SECRET-XYZ" not in blob, blob


_TESTS = [
    test_cloud_reconnect_calls_resync_with_resolved_config,
    test_cloud_reconnect_failure_returns_200_reconnected_false,
    test_cloud_reconnect_no_agent_manager_returns_false,
    test_resync_rewrites_secret_and_updates_runtime,
    test_resync_existing_secret_uses_put_secret_value,
    test_resync_returns_false_when_runtime_missing,
    test_resync_postgres_merges_conn_params_into_secret,
    test_resync_never_logs_secret_values,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} cloud reconnect propagation tests passed "
          "(no real AWS/DB/HTTP)")
