# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The derived Basic token is never persisted in the per-agent
config for an ARN-sourced Tulip system.

``_dp_agent_tulip`` builds the ``connection_config`` handed to
``AgentManager.start_agent``; ``_upsert_agent_config_secret`` serialises that
verbatim into ``adt/agent-config/<id>``. For an ARN-sourced system the config
must carry ONLY the seat-secret ARN and non-secret routing metadata, never the
derived ``token`` (nor a raw ``api_key``/``api_secret``) - the runtime derives
the header from a fresh seat read. The inline API-key path keeps its own token.

Offline: the agent manager and graph write are stubbed; no AWS, no network.
Fails on ``faa43934`` (the token was put into the config) and passes after.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402

OWN = "111122223333"
SEAT_ARN = f"arn:aws:secretsmanager:us-east-1:{OWN}:secret:customer/tulip/tulip-connection-AbCdEf"
OWN_CONN_ARN = f"arn:aws:secretsmanager:us-east-1:{OWN}:secret:adt/conn/inline-tulip-AbCdEf"
TOKEN = "ZGVyaXZlZA=="  # a base64 'token'  # nosec B105


def _ctx(system_id):
    return types.SimpleNamespace(system_id=system_id, system_name="Tulip MES",
                                 schema="", cm_items=[], field_descriptions=[])


def _captured_config(monkeypatch, system_id, secret_arn, token):
    """Run _dp_agent_tulip with the agent manager stubbed; return the
    connection_config it handed to start_agent."""
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
    monkeypatch.setattr(onboard, "_dp_tulip_workspace_id", lambda ctx: "wsX")
    monkeypatch.setattr(onboard, "_dp_tulip_key_scope", lambda ctx: "instance")
    monkeypatch.setattr(onboard, "_persist_node_connection", lambda *a, **k: None)
    onboard._dp_agent_tulip(_ctx(system_id), "https://acme.tulip.co", token, secret_arn)
    return seen.get("connection_config", {})


def test_arn_sourced_config_has_no_token_or_raw_credentials(monkeypatch):
    cfg = _captured_config(monkeypatch, "tulip-mes", SEAT_ARN, TOKEN)
    # The seat ARN and the non-secret routing survive; the token does not.
    assert cfg.get("secret_arn") == SEAT_ARN
    assert cfg.get("base_url") == "https://acme.tulip.co"
    assert cfg.get("key_scope") == "instance"
    assert "token" not in cfg, cfg
    assert "api_key" not in cfg and "api_secret" not in cfg, cfg


def test_serialized_agent_config_payload_has_no_token(monkeypatch):
    """Belt and braces: the exact value agent_manager would serialise carries
    no token/api_key/api_secret for an ARN system."""
    import json
    cfg = _captured_config(monkeypatch, "tulip-mes", SEAT_ARN, TOKEN)
    blob = json.dumps(cfg)
    assert TOKEN not in blob
    for leak in ('"token"', '"api_key"', '"api_secret"'):
        assert leak not in blob, blob


def test_inline_api_key_path_keeps_its_token(monkeypatch):
    """The inline 'API key and secret' path (its own adt/conn/<id> secret) keeps
    its token in the config, as before - only the ARN-sourced path drops it."""
    cfg = _captured_config(monkeypatch, "inline-tulip", OWN_CONN_ARN, TOKEN)
    assert cfg.get("token") == TOKEN, cfg
