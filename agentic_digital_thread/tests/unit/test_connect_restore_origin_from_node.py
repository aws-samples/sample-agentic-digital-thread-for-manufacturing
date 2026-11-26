# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""An unauthenticated user MCP comes back user-registered after a restart.

After a restart ``server._restore`` re-marked a user registration only from the
binding in its own ``adt/conn/<id>`` secret. A user MCP registered without a
token has no secret, so it came back unmarked; on a ``--highbyte`` deploy its
no-URL discover was then answered by HighByte and its own URL refused. Now
provision records the registration's origin on the System node
(``s.connection_origin``) and the restore re-marks from it as well. A node
marker only ever says "user" for a system the user registered: HighByte and
demo systems (marker ``demo`` or none) are never marked, and HighByte's token
still only goes to HighByte. Offline.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.server as server  # noqa: E402 - pytest blocks sockets; no AWS call
from agents.models import DiscoverRequest  # noqa: E402
from tests.unit import test_restore_isolation as iso  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    SID, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_credential_source import (  # noqa: E402,F401
    HB_TOKEN, HB_URL, MCP_URL, _register_mcp, highbyte, httpx_spy)
from tests.unit.test_connect_new_system_reonboard_source import _restart  # noqa: E402

TOKENLESS_RESOLVED = {"system_id": SID, "connection_type": "MCP Server", "url": MCP_URL,
                      "secret_arn": "", "_secret_unresolved": False}


def _restore_direct(node_row: dict, resolved: dict) -> list:
    cat_mock = MagicMock()
    cat_mock.get_all.return_value = [{"id": node_row["id"]}]
    mgr = MagicMock()
    mgr.start_agent = iso._Starts(set(), RuntimeError("unused"))
    with patch.object(server, "_agent_manager", mgr), \
            patch.object(server, "neptune_query", MagicMock(side_effect=[[], [node_row]])), \
            patch.object(server, "catalog", cat_mock), \
            patch.object(server, "add_system_agent", MagicMock()), \
            patch.object(cr, "resolve_connection", MagicMock(return_value=resolved)), \
            patch.object(server, "_reconcile_restored_sql_provenance", MagicMock()):
        server._restore()
    return [c.args for c in cat_mock.set_connection_origin.call_args_list]


def test_a_tokenless_user_mcp_is_re_marked_from_its_node():
    row = {"id": SID, "name": "Q", "plant": "", "connection_type": "MCP Server",
           "connection_origin": "user", "status": "onboarded"}
    assert _restore_direct(row, TOKENLESS_RESOLVED) == [(SID, "user")]


def test_the_restore_query_reads_the_node_origin():
    seen: list = []

    def _query(cypher, params=None):
        seen.append(cypher)
        return []
    with patch.object(server, "neptune_query", _query), \
            patch.object(server, "_reconcile_restored_sql_provenance", MagicMock()):
        server._restore()
    assert seen and all("s.connection_origin AS connection_origin" in q
                        for q in seen if "MATCH (s:System" in q), seen


def test_guard_highbyte_and_demo_nodes_are_never_marked():
    hb = {"id": "highbyte", "name": "HighByte", "plant": "", "connection_type": "MCP Server",
          "connection_origin": "demo", "status": "onboarded"}
    resolved = {**TOKENLESS_RESOLVED, "system_id": "highbyte", "url": HB_URL}
    decided: list = []
    real = server._restore_mark_user_origin
    with patch.object(server, "_restore_mark_user_origin",
                      lambda sid, *a, **k: decided.append(sid) or real(sid, *a, **k)):
        assert _restore_direct(hb, resolved) == []
        legacy = {**hb, "connection_origin": None}
        assert _restore_direct(legacy, resolved) == []
    assert decided == ["highbyte", "highbyte"], "the origin was never decided"


def test_provision_records_the_origin_on_the_system_node(cat, secrets, monkeypatch):
    _register_mcp(token=None)
    writes: list = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: writes.append((c, p)) or True)
    monkeypatch.setattr(onboard, "_existing_node_guidance", lambda sid: {})
    ctx = onboard._ProvisionState(body=MagicMock(), system_id=SID, system_name="Q", plant="",
                                  schema="", rules=[], mappings=[], connection_type="MCP Server",
                                  cm_items=[])
    onboard._dp_system_node(ctx)
    node_write = [p for c, p in writes if "MERGE (s:System" in c]
    assert node_write and node_write[0].get("origin") == "user", node_write


def test_after_the_restart_its_discover_uses_its_own_url_not_highbyte(
        cat, secrets, highbyte, httpx_spy, monkeypatch):
    """The end state the marker restores: a user-marked tokenless MCP on a
    HighByte deploy discovers at its own URL with no token."""
    _register_mcp(token=None)
    _restart(cat, {"connection_type": "MCP Server"})
    monkeypatch.setattr(cr, "_fetch_node_config", lambda sid: {
        "connection_config": '{"url": "%s"}' % MCP_URL, "secret_arn": "",
        "connection_type": "MCP Server"} if sid == SID else None)
    cat.set_connection_origin(SID, "user")             # what the restore now does
    body = DiscoverRequest(system_id=SID, connection_type="MCP Server", connection_string="")
    asyncio.run(onboard._discover_mcp_server(body, SID, "", "", "MCP Server"))
    assert httpx_spy.calls and httpx_spy.calls[0][0] == MCP_URL, httpx_spy.calls
    assert all(HB_TOKEN not in auth for _, auth in httpx_spy.calls), httpx_spy.calls
