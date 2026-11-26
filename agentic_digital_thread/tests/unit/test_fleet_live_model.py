# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit test: the LOCAL fleet reports the LIVE configured system-agent
model (the value ``/api/admin/llm-settings`` reports and the runtimes actually
run via ``SYSTEM_AGENT_MODEL``), NOT the per-system ``s.agent_model`` snapshot
baked into the knowledge graph at onboard time.

Regression guard for the Agents page showing a stale model (e.g. "Nova Lite v2")
after the deployment model was switched to Claude Haiku 4.5.
"""
from __future__ import annotations

import asyncio

import agents.fleet as fleet


class _Inst:
    def __init__(self):
        self.server = None
        self.mcp_url = "mcp://local"


class _FakeManager:
    mode = "local"

    def __init__(self, system_ids):
        self._agents = {sid: _Inst() for sid in system_ids}

    def list_agents(self):
        return [{"system_id": sid, "system_name": sid.upper()} for sid in self._agents]


class _FakeRegistry:
    def snapshot(self):
        return {}


def test_local_fleet_uses_live_agent_model_not_stale_graph(monkeypatch):
    live_agent = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    live_orch = "us.anthropic.claude-sonnet-4-6"

    # Live configured models (what llm-settings reports).
    import agents.model_config as mc
    monkeypatch.setattr(
        mc, "get_configured_models",
        lambda region=None: {"orchestrator": live_orch, "agent": live_agent, "region": "us-east-1"},
    )
    # Stale per-system value in the graph — must be IGNORED for the display.
    import agents.db as db
    monkeypatch.setattr(
        db, "neptune_query",
        lambda *a, **k: [{"id": "erp", "model": "us.amazon.nova-2-lite-v1:0"}],
    )
    monkeypatch.setattr(fleet, "get_metrics_registry", lambda: _FakeRegistry())

    provider = fleet.LocalFleetProvider(_FakeManager(["erp", "cmms"]))
    agents = asyncio.run(provider.get_fleet(24))

    by = {a["name"]: a for a in agents}
    # Orchestrator reports the live orchestrator model.
    assert by["adt_orchestrator"]["model_id"] == live_orch
    # Every SYSTEM agent reports the live agent model, never the stale nova value.
    system_rows = [a for a in agents if a["name"] != "adt_orchestrator"]
    assert system_rows, "expected at least one system agent row"
    for a in system_rows:
        assert a["model_id"] == live_agent, a
        assert "nova" not in a["model_id"].lower()
