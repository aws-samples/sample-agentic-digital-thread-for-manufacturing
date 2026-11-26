# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Edit form's Authorization header choice changes only user MCP systems.

The scheme select is shown only for a user-registered MCP system, but its
backend half was global: ``_describe_resolved`` added ``auth_scheme`` to
EVERY connection summary (HighByte and demo responses that never had it),
and the new three-value check refused an ``auth_scheme`` on EVERY MCP
system's admin update. Both are now limited to the
``is_user_registration`` + ``MCP Server`` branch: HighByte and demo MCP
systems answer and accept exactly what they did before. Offline.
"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.admin_routes as admin_routes  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    cat, no_dns, secrets)
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401
from tests.unit.test_connect_new_system_env_credential_fill import (  # noqa: E402,F401
    _admin_update, deployed)

URL = "https://mcp.example.com/mcp"
SYSTEMS = {
    "highbyte": {"id": "highbyte", "name": "HighByte", "connection_type": "MCP Server",
                 "schema": "", "status": "registered", "connection_origin": "demo"},
    "demo-mcp": {"id": "demo-mcp", "name": "Demo MCP", "connection_type": "MCP Server",
                 "schema": "", "status": "registered", "connection_origin": "demo"},
}
#: GET /api/admin/connection/{id} for these nodes before the Edit-form change.
BEFORE = {
    "connection_type": "MCP Server", "host": None, "port": None,
    "scheme": None, "base_url": None,
    "dbname": None, "search_path": None, "secret_arn": "",
    "has_secret": False, "secret_fields": [],
    # Non-secret routing/credential-source markers (empty for a non-ARN MCP).
    "workspace_id": "", "key_scope": "", "credential_source": "",
}


def _node(graph, sid: str, cfg: dict):
    graph[sid] = {"connection_config": json.dumps(cfg), "secret_arn": "",
                  "connection_type": "MCP Server"}


@pytest.mark.parametrize("sid", sorted(SYSTEMS))
@pytest.mark.parametrize("cfg", [{"url": URL}, {"url": URL, "auth_scheme": "bearer_literal"}],
                         ids=["no-scheme", "scheme-on-node"])
def test_a_non_user_mcp_summary_is_unchanged(cat, secrets, graph, sid, cfg):
    cat.add(dict(SYSTEMS[sid]))
    _node(graph, sid, cfg)
    out = admin_routes._describe_connection_for(sid, cat.get(sid))
    assert out == {"system_id": sid, **BEFORE}, out


@pytest.mark.parametrize("sid", sorted(SYSTEMS))
@pytest.mark.parametrize("scheme", ["digest", "basic"])
def test_a_non_user_mcp_update_accepts_what_it_accepted_before(
        cat, secrets, graph, deployed, sid, scheme):
    cat.add(dict(SYSTEMS[sid]))
    _node(graph, sid, {"url": URL})
    res = _admin_update(sid, {"auth_scheme": scheme})
    body = json.loads(res.body) if hasattr(res, "body") else res
    assert body.get("status") == "ok", body
    assert json.loads(graph[sid]["connection_config"])["auth_scheme"] == scheme
