# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The workshop's one-click HighByte onboarding, end to end, offline.

The workshop registers the built-in ``highbyte`` system (ADR-0031) as a
register-only DEMO-origin entry with no connection of its own; its URL and token
come from the deployment (``/adt/highbyte-url`` + ``/mcp``, the setup Lambda's
``adt/conn/highbyte`` key). These tests drive the wizard's own sequence
(preflight, discover, provision, the provisioned runtime's tool loading, plus
the onboarding agent's discover) against a fake 4.4.2 hub that accepts ONLY
``Authorization: Bearer <key>``, the form the setup Lambda verifies and the
HighByte guide documents. A local 4.4.2 hub accepts the raw key too; Bearer is
pinned because it is the documented form.

They also pin the read-only tool rule: the built-in HighByte system never
exposes the hub's Configuration Tools (``config_*``, which create and edit
pipelines under the administrator key) to discovery or to its runtime, whatever
the hub's own setting, while any other MCP system keeps every tool.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

from agents import catalog as catalog_mod  # noqa: E402
from agents import connection_probes, onboard, onboard_agent, onboard_preflight, server  # noqa: E402
from agents import connection_resolver as cr  # noqa: E402
from agents import system_agent  # noqa: E402
from agents.catalog import SystemCatalog  # noqa: E402
from agents.demo import workshop  # noqa: E402
from agents.models import DiscoverRequest, ProvisionRequest  # noqa: E402
from tests.unit import _fake_mcp_hub as fh  # noqa: E402

HB_BASE = "https://hb.example.com"
HB_MCP = HB_BASE + "/mcp"
HB_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/highbyte-AbCdEf"
REPO = Path(__file__).resolve().parents[3]
SETTINGS = REPO / "highbyte" / "components" / "highbyte-settings" / "intelligencehub-settings.json"


@pytest.fixture
def hub(monkeypatch):
    hub = fh.FakeHub(accept="bearer")
    fh.install(monkeypatch, hub)
    return hub


@pytest.fixture
def workshop_hb(monkeypatch, hub):
    """A fresh catalog holding the workshop's register-only HighByte entry,
    registered the way the startup hook registers it, with the deployment's URL
    and key in the app's env and no graph."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("HIGHBYTE_URL", HB_BASE)
    monkeypatch.setenv("HIGHBYTE_I3X_TOKEN", fh.KEY)
    fresh = SystemCatalog([])
    monkeypatch.setattr(fresh, "_graph_rows", lambda: [])
    for mod in (catalog_mod, onboard, onboard_agent):
        monkeypatch.setattr(mod, "catalog", fresh)
    monkeypatch.setattr(cr, "_fetch_node_config", lambda _sid: None)
    monkeypatch.setattr(cr, "validate_connection_target", lambda *_a, **_k: "203.0.113.10")
    monkeypatch.setattr(connection_probes, "validate_connection_target",
                        lambda *_a, **_k: "203.0.113.10")
    entry = next(s for s in workshop.registered_systems("live", highbyte_shipped=True)
                 if s["id"] == "highbyte")
    from agents.demo.db_routing import demo_pg_dsn
    assert server._register_one_demo_system(
        entry, onboard_agent.register_demo_system, demo_pg_dsn) == "registered"
    return fresh


def _catalog_conn_str(cat) -> str:
    return (cat.get("highbyte") or {}).get("connection_string") or ""


def _discovered(cat) -> dict:
    """The wizard's discover call (the route, which records the schema
    provenance provision checks the mappings against)."""
    body = DiscoverRequest(system_id="highbyte", connection_string=_catalog_conn_str(cat),
                           connection_type="MCP Server")
    return asyncio.run(onboard._onboard_discover_integrity(body))


def _provision_body(cat) -> ProvisionRequest:
    """The wizard's provision body: the catalog URL (empty for this entry) and a
    mapping on a discovered column, carrying discover's schema revision."""
    found = _discovered(cat)
    assert "error" not in found, found
    (table,) = found["tables"]
    column = next(c["name"] for c in table["columns"] if not c["name"].startswith("_"))
    return ProvisionRequest(
        system_id="highbyte", system_name="HighByte Intelligence Hub",
        connection_type="MCP Server", connection_string=_catalog_conn_str(cat),
        mappings=[{"field": f"{table['name']}.{column}", "concept": "sensor-reading",
                   "table": table["name"], "schema_revision": table["schema_revision"]}])


def _ctx(body: ProvisionRequest):
    return onboard._ProvisionState(
        body=body, system_id=body.system_id, system_name=body.system_name, plant="",
        schema="", rules=[], mappings=body.mappings, connection_type=body.connection_type,
        cm_items=[])


def _tool_names(tools) -> list[str]:
    return sorted(getattr(t, "tool_name", None) or t.get("name") for t in tools)


# ---------------------------------------------------------------- the click

def test_preflight_reaches_the_hub_with_the_deployment_key(workshop_hb, hub):
    result = onboard_preflight.run_preflight("highbyte")
    assert result["connection"]["status"] == "pass", result["connection"]
    assert result["verdict"] != "blocked"
    assert "bearer" in hub.auth_seen and "raw" not in hub.auth_seen


def test_wizard_discover_sends_bearer_and_lists_only_the_namespace_tool(workshop_hb, hub):
    result = _discovered(workshop_hb)
    assert "error" not in result, result
    assert [t["name"] for t in result["tables"]] == ["NameSpace_MCP"]
    assert set(hub.auth_seen) == {"bearer"}
    assert not set(hub.calls) & fh.CONFIG_TOOL_NAMES


def test_agent_discover_sends_bearer_and_lists_only_the_namespace_tool(workshop_hb, hub):
    entry = workshop_hb.get("highbyte")
    result = onboard_agent._discover_backend_result("highbyte", entry, "MCP Server", {})
    assert "error" not in result, result
    assert [t["name"] for t in result["tables"]] == ["NameSpace_MCP"]
    assert set(hub.auth_seen) == {"bearer"}


def test_provision_accepts_the_click_without_a_url_in_the_request(workshop_hb):
    trusted = onboard._trusted_provision_body(_provision_body(workshop_hb))
    assert trusted.connection_string == HB_MCP
    assert onboard._plan_provision(trusted)["plan"]["system_id"] == "highbyte"


def test_provision_gives_the_runtime_the_deployment_key_as_bearer(workshop_hb):
    trusted = onboard._trusted_provision_body(_provision_body(workshop_hb))
    source = onboard._dp_mcp_source(_ctx(trusted))
    assert not isinstance(source, dict), source
    url, cfg, _own = source
    assert url == HB_MCP
    assert cfg["token"] == fh.KEY
    assert cfg["auth_scheme"] == "bearer"


def test_provision_points_the_node_at_the_setup_lambda_secret(workshop_hb, monkeypatch):
    """With the key read from ``adt/conn/highbyte`` (no env token), the node
    keeps a pointer to that secret: provision never rewrites it, so the setup
    Lambda's own fields (storedKey, friendlyName) stay."""
    monkeypatch.delenv("HIGHBYTE_I3X_TOKEN")

    class _SM:
        def get_secret_value(self, SecretId):
            assert SecretId == "adt/conn/highbyte"
            return {"ARN": HB_ARN, "SecretString": json.dumps(
                {"token": fh.KEY, "storedKey": "k", "friendlyName": "adt-mcp"})}

    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: _SM())
    persisted = {}
    monkeypatch.setattr(onboard, "_persist_node_connection",
                        lambda sid, **kw: persisted.update(kw))

    class _Mgr:
        _agents: dict = {}

        def start_agent(self, **kw):
            persisted["start_agent_config"] = kw["connection_config"]
            return "https://gateway.example.com/mcp"

    monkeypatch.setattr(onboard, "_agent_manager", _Mgr())
    monkeypatch.setattr(onboard, "add_system_agent", lambda *a, **k: None)
    trusted = onboard._trusted_provision_body(_provision_body(workshop_hb))
    assert onboard._dp_agent_mcp(_ctx(trusted)) is None
    assert persisted["non_secret"]["auth_scheme"] == "bearer"
    assert persisted["secret"] is None
    assert persisted["secret_arn"] == HB_ARN
    assert persisted["start_agent_config"]["token"] == fh.KEY


class _RecordingSecrets:
    """Secrets Manager stand-in holding ``adt/conn/highbyte`` as the setup Lambda
    writes it, recording every write call."""

    class exceptions:
        class ResourceExistsException(Exception):
            pass

        class InvalidRequestException(Exception):
            pass

    def __init__(self, payload: dict):
        self.payload = payload
        self.writes: list = []

    def get_secret_value(self, SecretId):
        if SecretId not in ("adt/conn/highbyte", HB_ARN):
            raise RuntimeError("ResourceNotFoundException")
        return {"ARN": HB_ARN, "SecretString": json.dumps(self.payload)}

    def create_secret(self, Name, SecretString):
        self.writes.append(("create_secret", Name))
        raise self.exceptions.ResourceExistsException()

    def put_secret_value(self, SecretId, SecretString):
        self.writes.append(("put_secret_value", SecretId))
        return {"ARN": HB_ARN}


def test_provision_with_the_env_key_never_rewrites_a_rich_canonical_secret(workshop_hb,
                                                                           monkeypatch):
    """The demo's app task can carry the key in ``HIGHBYTE_I3X_TOKEN`` too (the
    fixture sets it). The setup Lambda's ``adt/conn/highbyte`` still wins: the
    runtime gets its key and the node points at it, and provision makes no
    Secrets Manager write, so ``storedKey`` and ``friendlyName`` stay."""
    assert os.environ.get("HIGHBYTE_I3X_TOKEN")
    sm = _RecordingSecrets({"token": fh.KEY, "storedKey": "k", "friendlyName": "adt-mcp"})
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: sm)
    monkeypatch.setattr(cr, "_secrets_client", lambda: sm)
    node_writes: list = []
    monkeypatch.setattr(cr, "_neptune_write",
                        lambda query, params=None, **kw: node_writes.append(params) or True)
    started: dict = {}

    class _Mgr:
        _agents: dict = {}

        def start_agent(self, **kw):
            started.update(kw["connection_config"])
            return "https://gateway.example.com/mcp"

    monkeypatch.setattr(onboard, "_agent_manager", _Mgr())
    monkeypatch.setattr(onboard, "add_system_agent", lambda *a, **k: None)
    trusted = onboard._trusted_provision_body(_provision_body(workshop_hb))
    assert onboard._dp_agent_mcp(_ctx(trusted)) is None
    assert sm.writes == [], sm.writes
    assert started["token"] == fh.KEY and started["auth_scheme"] == "bearer"
    (node,) = [p for p in node_writes if p and p.get("sid") == "highbyte"]
    assert node["arn"] == HB_ARN


def test_the_provisioned_runtime_loads_only_the_namespace_tool_with_bearer(workshop_hb, hub):
    trusted = onboard._trusted_provision_body(_provision_body(workshop_hb))
    _url, cfg, _own = onboard._dp_mcp_source(_ctx(trusted))
    tools, _desc = system_agent._system_tools("highbyte", "mcp", "", None, "MCP Server", cfg)
    try:
        assert _tool_names(tools) == ["NameSpace_MCP"]
        assert set(hub.auth_seen) == {"bearer"}
    finally:
        tools[0].mcp_client.stop(None, None, None)


# ---------------------------------------------------------------- read-only rule

def test_an_existing_hub_with_configuration_tools_still_gives_only_the_read_tool(hub):
    """The rule holds for a hub whose settings still enable the tools (an
    existing EFS-backed hub keeps its first-boot settings file)."""
    assert hub.config_tools
    cfg = {"url": HB_MCP, "token": fh.KEY, "auth_scheme": "bearer"}
    tools, _ = system_agent._system_tools("highbyte", "mcp", "", None, "MCP Server", cfg)
    try:
        assert _tool_names(tools) == ["NameSpace_MCP"]
    finally:
        tools[0].mcp_client.stop(None, None, None)
    found = onboard_agent._discover_mcp(onboard._mcp_entry(HB_MCP, fh.KEY, "bearer"),
                                        system_id="highbyte")
    assert [t["name"] for t in found["tables"]] == ["NameSpace_MCP"]


def test_any_other_mcp_system_keeps_every_tool(hub):
    cfg = {"url": HB_MCP, "token": fh.KEY, "auth_scheme": "bearer"}
    tools, _ = system_agent._system_tools("plant-uns", "mcp", "", None, "MCP Server", cfg)
    try:
        assert _tool_names(tools) == sorted(["NameSpace_MCP", *fh.CONFIG_TOOL_NAMES])
    finally:
        tools[0].mcp_client.stop(None, None, None)
    found = onboard_agent._discover_mcp(onboard._mcp_entry(HB_MCP, fh.KEY, "bearer"),
                                        system_id="plant-uns")
    assert {t["name"] for t in found["tables"]} == {"NameSpace_MCP", *fh.CONFIG_TOOL_NAMES}


def test_a_fresh_hub_ships_with_configuration_tools_off():
    settings = json.loads(SETTINGS.read_text())["settings"]["restDataServer"]
    assert settings["mcpServerEnabled"] is True
    assert settings["mcpConfigTools"] == "disabled"
