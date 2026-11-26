# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bug H1 (regression of the inline "API key and secret" option): an INLINE
Tulip system carries its OWN ``adt/conn/<id>`` secret_arn, whose payload is a
token (NOT a seat contract). The runtime Tulip client must still authenticate
on that path - snapshot token when the agent-config has one, else the token
read from the system's own secret - instead of treating any ``secret_arn`` as a
seat-secret pointer and sending nothing.

The config is built the way provision builds it (``onboard._dp_agent_tulip``),
so the test exercises the REAL registration/provision wiring the hand-built
unit tests skip. Only the network and Secrets Manager are faked; no AWS.

Fails on ``ddd8ec29`` (``_TulipClient._refresh_from_secret`` runs the seat
contract for the own ``adt/conn/<id>`` secret, ``request_tuple`` rejects the
token-only payload, and ``get`` raises "seat secret unavailable" so nothing is
sent) and passes after the fix.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import types
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
import agents.system_agent as sa  # noqa: E402
from agents.connection_types import TULIP  # noqa: E402

OWN = "111122223333"
# The inline path's OWN secret: adt/conn/<system_id>-<suffix>.
SID = "inline-tulip"
OWN_CONN_ARN = f"arn:aws:secretsmanager:us-east-1:{OWN}:secret:adt/conn/{SID}-AbCdEf"
BASE_URL = "https://acme.tulip.co"
# base64("apiKey:secret") - a Tulip HTTP Basic token (snapshot or in-secret).
TOKEN = base64.b64encode(b"apiKey:secret").decode()  # nosec B105


# --------------------------------------------------------------------------- #
# Build the provisioned agent-config through the REAL provision code path.
# --------------------------------------------------------------------------- #
def _ctx(system_id):
    return types.SimpleNamespace(system_id=system_id, system_name="Tulip MES",
                                 schema="", cm_items=[], field_descriptions=[])


def _provisioned_config(monkeypatch, system_id, secret_arn, token):
    """The ``connection_config`` ``_dp_agent_tulip`` hands to start_agent (the
    exact dict serialised into ``adt/agent-config/<id>`` and later read by the
    runtime)."""
    seen = {}

    class _FakeMgr:
        _agents: dict = {}

        def start_agent(self, **kwargs):
            seen.update(kwargs)
            return "https://runtime.example/mcp"

    monkeypatch.setattr(onboard, "_agent_manager", _FakeMgr())
    monkeypatch.setattr(onboard, "add_system_agent", lambda *a, **k: None)
    monkeypatch.setattr(onboard, "_tulip_known_tables_desc", lambda ctx, cfg: "")
    monkeypatch.setattr(onboard, "_with_concept_mappings", lambda cfg, items, fields=None: cfg)
    monkeypatch.setattr(onboard, "_dp_tulip_workspace_id", lambda ctx: "")
    monkeypatch.setattr(onboard, "_dp_tulip_key_scope", lambda ctx: "")
    monkeypatch.setattr(onboard, "_persist_node_connection", lambda *a, **k: None)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    onboard._dp_agent_tulip(_ctx(system_id), BASE_URL, token, secret_arn)
    return seen.get("connection_config", {})


# --------------------------------------------------------------------------- #
# Drive the runtime client the way the runtime builds it from that config.
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, sink, body):
        self._sink = sink
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def stream(self, method, url, headers=None, params=None):
        self._sink.append({"method": method, "url": url,
                           "headers": dict(headers or {}), "params": dict(params or {})})
        return _FakeResponse(self._body)


def _runtime_client(cfg, system_id):
    """Build the runtime Tulip tools the way the runtime does:
    ``system_agent._direct_system_tools`` dispatches the REST family and injects
    the system id into the non-secret config, then
    ``rest_vendor_registry._tulip_runtime_tools`` -> ``_make_tulip_tools``."""
    tools, _desc = sa._direct_system_tools(system_id, TULIP, "", dict(cfg))
    return {t.tool_name: t for t in tools}


def _drive(monkeypatch, cfg, system_id, own_secret):
    """Run tulip_list_tables through the fake HTTP + Secrets Manager; return the
    captured request list."""
    sink = []
    import agents.pinned_http as pinned_http
    monkeypatch.setattr(pinned_http, "client", lambda **kw: _FakeClient(sink, "[]"))
    monkeypatch.setattr("agents.response_integrity_query.read_http_body_capped",
                        lambda response, limit: (response._body, False))
    monkeypatch.setattr(sa, "_fetch_credentials", lambda arn: dict(own_secret))
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    tools = _runtime_client(cfg, system_id)
    out = tools["tulip_list_tables"]._tool_func()
    return sink, out


def _basic(token: str) -> str:
    return f"Basic {token}"


def test_inline_tulip_sends_snapshot_token_header(monkeypatch):
    """The inline agent-config carries secret_arn=adt/conn/<id> AND a snapshot
    token. The runtime must send that token's Basic header to the snapshot URL,
    NOT treat the own secret as a seat pointer and send nothing. Live symptom on
    member: all three Tulip tool calls returned a tool error, so the tool result
    must NOT be the fixed-safe {error_type: tool_error} envelope either."""
    cfg = _provisioned_config(monkeypatch, SID, OWN_CONN_ARN, TOKEN)
    # Sanity: provision keeps the inline path's token in the config.
    assert cfg.get("secret_arn") == OWN_CONN_ARN and cfg.get("token") == TOKEN
    # The own secret holds a token, never a seat contract.
    sink, out = _drive(monkeypatch, cfg, SID, own_secret={"token": TOKEN})
    assert len(sink) == 1, sink
    req = sink[0]
    assert req["url"] == f"{BASE_URL}/api/v3/tables"
    assert req["headers"].get("Authorization") == _basic(TOKEN)
    # The tool answered (not the "tool_unavailable / tool_error" envelope the
    # inline runtime returned on a rehearsal account before the fix).
    parsed = json.loads(out)
    assert parsed.get("error_type") != "tool_error", parsed
    assert "tables" in parsed


def test_inline_tulip_reads_token_from_its_own_secret_when_not_snapshotted(monkeypatch):
    """Even with NO snapshot token in the config, an own ``adt/conn/<id>``
    secret's token is read and sent (pre-branch behaviour) - the client never
    runs the seat contract against an own secret."""
    # Config as provision writes it, then simulate an agent-config that carries
    # only the pointer (no inline token): the token lives in the own secret.
    cfg = _provisioned_config(monkeypatch, SID, OWN_CONN_ARN, TOKEN)
    cfg.pop("token", None)
    sink, out = _drive(monkeypatch, cfg, SID, own_secret={"token": TOKEN})
    assert len(sink) == 1, sink
    assert sink[0]["headers"].get("Authorization") == _basic(TOKEN)
    parsed = json.loads(out)
    assert parsed.get("error_type") != "tool_error", parsed
