# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Fleet provider abstraction — separates local from cloud for the Agents page.

The Agents page needs per-agent status + metrics. The two runtimes source that
data completely differently:

  * cloud  → AgentCore ``list_agent_runtimes`` + CloudWatch Logs Insights + Bedrock
             token metrics (see ``agents_routes._build_cloud_fleet``).
  * local  → the in-process ``AgentManager`` roster + the orchestrator + per-system
             models from the local knowledge graph + the in-process MetricsRegistry.

Both produce the SAME per-agent dict shape, so the frontend (agents-shared.js)
renders either without change. The factory keys on ``AgentManager.mode`` — the
same local/cloud signal the chat-client factory uses.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod

from .metrics_registry import get_metrics_registry

# Canonical display names + ordering for the fleet (single source of truth;
# agents_routes imports these).
AGENT_DISPLAY_NAMES = {
    "adt_orchestrator": "Orchestrator",
    # The graph runtime: the SAME thing the architecture/investigation views
    # call the "Knowledge Graph" hub. Carry both terms so the Agents page and
    # the graph views visibly tie together (it was title-cased "Digital Thread"
    # before, reading as an unrelated component).
    "adt_digital_thread": "Knowledge Graph (Digital Thread)",
    "adt_erp":          "ERP",
    "adt_cmms":         "CMMS",
    "adt_plm":          "PLM",
    "adt_mes_mil":      "MES - Milwaukee",
    "adt_mes_det":      "MES - Detroit",
    "adt_mes_cr":       "MES - Cedar Rapids",
}

AGENT_ORDER = ["adt_orchestrator", "adt_erp", "adt_cmms", "adt_plm", "adt_mes_mil", "adt_mes_det", "adt_mes_cr"]


def _region_prefix() -> str:
    region = os.environ.get("AWS_REGION", "us-east-1")
    return "eu" if region.startswith("eu") else "ap" if region.startswith("ap") else "us"


def model_price_per_1k(model_id: str, prefix: str) -> dict:
    """USD per 1K tokens — delegates to the curated catalog (single source)."""
    from .model_catalog import pricing
    return pricing(model_id)


class FleetProvider(ABC):
    """Returns the agent fleet as a list of per-agent dicts (frontend shape)."""

    @abstractmethod
    async def get_fleet(self, hours: float) -> list[dict]:
        ...


class CloudFleetProvider(FleetProvider):
    """AgentCore + CloudWatch + Bedrock metrics (the original implementation)."""

    async def get_fleet(self, hours: float) -> list[dict]:
        # Lazy import avoids a module-load cycle (agents_routes imports this module).
        from . import agents_routes
        return await agents_routes._build_cloud_fleet(hours)


class LocalFleetProvider(FleetProvider):
    """In-process roster + metrics. Mirrors the cloud dict shape field-for-field.

    Note: local metrics are cumulative since app start (not windowed), so ``hours``
    is accepted for interface parity but does not filter.
    """

    def __init__(self, manager):
        self.manager = manager

    async def get_fleet(self, hours: float) -> list[dict]:
        prefix = _region_prefix()
        reg = get_metrics_registry().snapshot()

        # Single source for the configured models (mirrors actual runtime env).
        from .model_config import get_configured_models
        _models = get_configured_models()
        orch_model = _models["orchestrator"]
        agent_model_default = _models["agent"]
        # NOTE: the per-system `s.agent_model` graph property (see _system_models)
        # is a snapshot baked at onboard time and drifts when the deployment model
        # is changed later. The fleet display sources the LIVE configured agent
        # model instead (below), so it matches what the runtimes actually run and
        # what /api/admin/llm-settings reports, and can't drift.

        agents: list[dict] = [
            self._row("adt_orchestrator", "Orchestrator", "orchestrator",
                      orch_model, prefix, reg, healthy=True)
        ]

        roster = self.manager.list_agents()
        for info in roster:
            sid = info["system_id"]
            name = f"adt_{sid.replace('-', '_')}"
            display = AGENT_DISPLAY_NAMES.get(name, info.get("system_name", sid))
            model = agent_model_default
            healthy = self._is_healthy(sid)
            agents.append(self._row(name, display, sid, model, prefix, reg, healthy=healthy))

        agents.sort(key=lambda a: AGENT_ORDER.index(a["name"]) if a["name"] in AGENT_ORDER else 99)
        return agents

    # ── helpers ────────────────────────────────────────────────────────────
    def _system_models(self, default: str) -> dict:
        """Per-system agent model from the local knowledge graph (mode-agnostic)."""
        models: dict[str, str] = {}
        try:
            from .db import neptune_query
            rows = neptune_query(
                "MATCH (s:System) WHERE s.agent_model IS NOT NULL "
                "RETURN s.id AS id, s.agent_model AS model"
            )
            for r in (rows or []):
                if r.get("id"):
                    models[r["id"]] = r.get("model") or default
        except Exception:  # nosec B110 - best-effort path; failure is deliberately non-fatal
            pass
        return models

    def _is_healthy(self, system_id: str) -> bool:
        inst = self.manager._agents.get(system_id)
        if inst is None:
            return False
        if getattr(inst, "server", None) is not None:
            return inst.server.is_running()
        return bool(getattr(inst, "mcp_url", ""))

    def _row(self, name, display, reg_key, model_id, prefix, reg, *, healthy) -> dict:
        m = reg.get(reg_key, {})
        inp = m.get("input_tokens", 0)
        out = m.get("output_tokens", 0)
        inv = m.get("invocations", 0)
        price = model_price_per_1k(model_id, prefix)
        cost = round(inp / 1000 * price["input"] + out / 1000 * price["output"], 4)
        avg_latency = m.get("avg_latency_s")
        from .console_links import agentcore_console_links
        region = os.environ.get("AWS_REGION", "us-east-1")
        return {
            "name": name,
            "runtime_name": name,
            "display_name": display,
            "runtime_id": f"local-{reg_key}",
            "status": "READY" if healthy else "STOPPED",
            "last_updated": m.get("last_active", ""),
            "has_eval": False,
            "model_id": model_id,
            "invocations_24h": inv,
            "avg_latency_s": round(avg_latency, 2) if avg_latency else None,
            "tool_calls_24h": m.get("tool_calls", 0),
            "avg_events_per_session": None,
            "hourly_sessions": [],
            "log_group": "",
            "input_tokens_24h": inp,
            "output_tokens_24h": out,
            "cost_usd_24h": cost,
            "cost_per_request": round(cost / inv, 6) if inv else 0.0,
            "eval_scores": [],
            **agentcore_console_links(region, ""),
        }


def get_fleet_provider(manager) -> FleetProvider:
    """Pick a provider from the agent-manager mode (same signal as the chat client)."""
    if manager is not None and getattr(manager, "mode", "") == "local":
        return LocalFleetProvider(manager)
    return CloudFleetProvider()
