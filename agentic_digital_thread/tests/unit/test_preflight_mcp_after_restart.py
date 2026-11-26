# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Preflight on a USER MCP system after an ECS restart (seen on a live run,
2026-10-08: verdict ``degraded`` — probe "3 tool(s) discovered", sample read
"failed for table 'namespace' (DiscoveryError)"). Offline: no AWS, no Neptune,
no network.

Provision writes an MCP node's URL under ``url`` only (``_dp_agent_mcp``), so
``resolve_connection`` carries no ``connection_string`` for it. Before the
restart the preflight bootstrap filled ``connection_string`` from the catalog
entry; the restart rebuilds the entry without it, and ``_pf_mcp`` passed
``connection_string=""`` to ``_discover_mcp``, which then answered "MCP Server
requires a server URL". The probe already used ``url`` (or
``connection_string``). The sample read now selects its URL the same way, and
both the URL and the token come from the one resolved connection.
"""
from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard_agent as onboard_agent  # noqa: E402
import agents.onboard_preflight as op  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, SID, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_credential_source import (  # noqa: E402
    MCP_URL, USER_TOKEN, _register_mcp)
from tests.unit.test_connect_new_system_reonboard_source import _node_row, _restart  # noqa: E402
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401 - fixture

HB_MCP = "https://hb.example.com/mcp"
HB_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/highbyte-AbCdEf"
TABLES = {"tables": [{"name": "namespace", "columns": [{"name": "Site", "data_type": "str"}]}]}


@pytest.fixture
def mcp_sample(monkeypatch):
    """_discover_mcp with the network stubbed: the REAL function answers an
    entry without a URL (its own guard); an entry with one is recorded and
    gets a namespace listing."""
    real = onboard_agent._discover_mcp
    seen: list = []

    def _discover(entry, system_id=""):
        if not entry.get("connection_string"):
            return real(entry, system_id=system_id)
        seen.append(entry)
        return TABLES
    monkeypatch.setattr(onboard_agent, "_discover_mcp", _discover)
    return seen


@pytest.fixture
def probe_pass(monkeypatch):
    probed: list = []
    monkeypatch.setattr(cr, "probe_connection",
                        lambda sid, resolved=None: probed.append(dict(resolved or {})) or {
                            "status": "pass", "reason": "ok", "latency_ms": 5,
                            "detail": "3 tool(s) discovered"})
    return probed


def _restarted_user_mcp(cat, graph):
    """Registered with a token, provisioned (node: url + own pointer), then an
    ECS restart (entry rebuilt with no connection string; the restore re-marks
    the origin from the own bound secret)."""
    _register_mcp()
    graph[SID] = _node_row({"url": MCP_URL, "auth_scheme": "bearer"}, CONN_ARN, "MCP Server")
    _restart(cat, {"connection_type": "MCP Server", "connection_origin": "user"})


def test_preflight_after_a_restart_reads_the_namespace(cat, secrets, graph, probe_pass,
                                                       mcp_sample):
    _restarted_user_mcp(cat, graph)
    resolved = cr.resolve_connection(SID)
    assert resolved.get("url") == MCP_URL and not resolved.get("connection_string"), resolved
    result = op.run_preflight(SID)
    assert result["verdict"] == "ready", result
    assert result["tables"] == [{"name": "namespace", "sample_read": "ok", "detail": ""}], result
    entry = mcp_sample[-1]
    assert entry["connection_string"] == MCP_URL
    assert entry["connection_config"]["url"] == MCP_URL
    assert entry["connection_config"]["token"] == USER_TOKEN       # its own, from the node pointer
    assert probe_pass[-1].get("url") == MCP_URL                    # the probe saw the same URL


def test_the_sample_read_takes_url_and_token_from_one_resolved_connection(mcp_sample):
    """A resolved dict shaped like a restored user MCP node."""
    resolved = {"system_id": SID, "connection_type": "MCP Server", "connection_string": "",
                "url": MCP_URL, "auth_scheme": "bearer", "secret_arn": CONN_ARN,
                "token": USER_TOKEN, "_secret_unresolved": False}
    tables, _ = op._pf_mcp(resolved, SID, time.monotonic() + 30)
    assert tables == [{"name": "namespace", "sample_read": "ok", "detail": ""}], tables
    assert mcp_sample[-1] == {"connection_string": MCP_URL,
                              "connection_config": {"url": MCP_URL, "token": USER_TOKEN,
                                                    "auth_scheme": "bearer"}}


@pytest.mark.parametrize("cs", [HB_MCP, ""], ids=["bootstrap-connection-string", "url-only"])
def test_guard_highbyte_sample_read_uses_its_own_url_and_token(mcp_sample, cs):
    resolved = {"system_id": "highbyte", "connection_type": "MCP Server",
                "connection_string": cs, "url": HB_MCP, "auth_scheme": "bearer",
                "secret_arn": HB_ARN, "token": "hb-token", "_secret_unresolved": False}
    tables, _ = op._pf_mcp(resolved, "highbyte", time.monotonic() + 30)
    assert tables[0]["sample_read"] == "ok", tables
    assert mcp_sample[-1] == {"connection_string": HB_MCP,
                              "connection_config": {"url": HB_MCP, "token": "hb-token",
                                                    "auth_scheme": "bearer"}}


def test_guard_no_url_anywhere_is_still_a_failed_sample(mcp_sample):
    resolved = {"system_id": SID, "connection_type": "MCP Server", "connection_string": "",
                "token": USER_TOKEN}
    tables, _ = op._pf_mcp(resolved, SID, time.monotonic() + 30)
    assert tables == [{"name": "namespace", "sample_read": "failed",
                       "detail": "DiscoveryError"}], tables
    assert mcp_sample == []
