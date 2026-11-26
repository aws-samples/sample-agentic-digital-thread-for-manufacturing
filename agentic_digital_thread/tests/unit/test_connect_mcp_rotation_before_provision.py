# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""MCP token rotation of a user registration before its first provision.

Before the first provision a user MCP registration has no graph node, so the
admin Edit's current connection (``resolve_connection`` + the catalog config)
carried no URL. A token-only rotation (``{"token": ...}``, the API shape) was
refused with "MCP Server requires a 'url'". The Edit now reads the current
connection from the registration (its URL and its own bound
``adt/conn/<id>`` secret), as it already does for a PostgreSQL registration:
the rotation updates the registration's own secret, keeps its binding and
nothing else, and discover / provision then use the new token. An own secret
that cannot be read changes nothing. Offline.
"""
from __future__ import annotations

import os

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, SID, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_credential_source import (  # noqa: E402,F401 - fixtures
    MCP_URL, USER_TOKEN, _mcp_discover, _provision_mcp, _register_mcp, highbyte, httpx_spy)
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401 - fixture
from tests.unit.test_connect_new_system_env_credential_fill import _admin_update  # noqa: E402
from tests.unit.test_connect_new_system_checked_read_seam import _graph_unavailable  # noqa: E402
from tests.unit.test_connect_new_system_ui_pg_edit import route_env  # noqa: E402,F401

NEW_TOKEN = "rotated-mcp-token-987"  # nosec B105 - fixture
BOUND = {"connection_origin": "user", "system_id": SID}


def _status(res) -> int:
    return getattr(res, "status_code", 200)


def test_a_token_only_rotation_before_the_first_provision_updates_its_own_secret(
        cat, secrets, graph, route_env, httpx_spy, monkeypatch):
    _register_mcp()
    res = _admin_update(SID, {"token": NEW_TOKEN})
    assert _status(res) == 200, getattr(res, "body", res)
    assert secrets[CONN_ARN] == {"token": NEW_TOKEN, **BOUND}, secrets[CONN_ARN]
    assert list(secrets) == [CONN_ARN], "no other secret written"
    _mcp_discover(SID, "")
    assert httpx_spy.calls and httpx_spy.calls[0][0] == MCP_URL, httpx_spy.calls
    assert NEW_TOKEN in httpx_spy.calls[0][1], httpx_spy.calls
    result, calls, started = _provision_mcp(cat, monkeypatch)
    assert result["status"] == "provisioned", result
    assert started[-1]["connection_config"]["token"] == NEW_TOKEN, started[-1]
    assert started[-1]["connection_config"]["url"] == MCP_URL


def test_a_token_only_rotation_with_an_unreadable_own_secret_changes_nothing(
        cat, secrets, graph, route_env, monkeypatch):
    _register_mcp()
    secrets.clear()
    res = _admin_update(SID, {"token": NEW_TOKEN})
    assert _status(res) == 500, getattr(res, "body", res)
    assert b"nothing was changed" in res.body
    assert secrets == {} and SID not in graph


def test_a_token_only_rotation_during_a_graph_outage_changes_nothing(
        cat, secrets, graph, route_env, monkeypatch):
    _register_mcp()
    before = dict(secrets[CONN_ARN])
    _graph_unavailable(monkeypatch)
    res = _admin_update(SID, {"token": NEW_TOKEN})
    assert _status(res) == 500, getattr(res, "body", res)
    assert secrets[CONN_ARN] == before


def test_guard_the_ui_edit_body_url_and_token_still_rotates(
        cat, secrets, graph, route_env, monkeypatch):
    _register_mcp()
    res = _admin_update(SID, {"url": MCP_URL, "token": NEW_TOKEN})
    assert _status(res) == 200, getattr(res, "body", res)
    assert secrets[CONN_ARN] == {"token": NEW_TOKEN, **BOUND}


def test_a_tokenless_registration_can_get_its_first_token_before_provision(
        cat, secrets, graph, route_env, monkeypatch):
    _register_mcp(token=None)
    assert secrets == {}
    res = _admin_update(SID, {"token": NEW_TOKEN})
    assert _status(res) == 200, getattr(res, "body", res)
    assert secrets[CONN_ARN]["token"] == NEW_TOKEN and list(secrets) == [CONN_ARN]
    result, calls, started = _provision_mcp(cat, monkeypatch)
    assert started[-1]["connection_config"]["token"] == NEW_TOKEN, started[-1]


def test_guard_a_provisioned_user_mcp_rotation_is_unchanged(
        cat, secrets, graph, route_env, monkeypatch):
    from tests.unit.test_connect_new_system_reonboard_source import _node_row
    _register_mcp()
    graph[SID] = _node_row({"url": MCP_URL, "auth_scheme": "bearer"}, CONN_ARN, "MCP Server")
    res = _admin_update(SID, {"token": NEW_TOKEN})
    assert _status(res) == 200, getattr(res, "body", res)
    assert secrets[CONN_ARN] == {"token": NEW_TOKEN, **BOUND}
