# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""In-process agent metrics for local mode.

The cloud Agents page sources fleet activity from AgentCore + CloudWatch Logs
Insights. Locally there is no AgentCore runtime and no CloudWatch, so this module
provides the *semi-equivalent*: a thread-safe, in-process registry that the local
query path increments as the orchestrator runs and calls tools.

Scope / honesty:
  - Counters are cumulative since app start (not time-windowed). The Agents-page
    time filter does not change local numbers.
  - Orchestrator token usage is accurate (read from the Strands AgentResult).
  - System-agent (Nova) token usage is NOT measurable in-process — those models
    run inside separate MCP subprocesses — so system agents report tokens=0.
    Their invocation and tool-call counts ARE accurate.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _AgentCounters:
    invocations: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_sum_s: float = 0.0
    latency_count: int = 0
    last_active: str = ""


class MetricsRegistry:
    """Thread-safe per-agent counters. Agent keys: 'orchestrator' or a system_id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._agents: dict[str, _AgentCounters] = {}

    def _get(self, key: str) -> _AgentCounters:
        c = self._agents.get(key)
        if c is None:
            c = _AgentCounters()
            self._agents[key] = c
        return c

    def record_invocation(
        self,
        key: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_s: float | None = None,
    ) -> None:
        with self._lock:
            c = self._get(key)
            c.invocations += 1
            c.input_tokens += max(0, int(input_tokens or 0))
            c.output_tokens += max(0, int(output_tokens or 0))
            if latency_s is not None and latency_s >= 0:
                c.latency_sum_s += float(latency_s)
                c.latency_count += 1
            c.last_active = _now_iso()

    def record_tool_call(self, key: str, n: int = 1) -> None:
        with self._lock:
            c = self._get(key)
            c.tool_calls += max(0, int(n))
            c.last_active = _now_iso()

    def snapshot(self) -> dict[str, dict]:
        """Return a plain-dict copy with derived avg_latency_s."""
        with self._lock:
            out: dict[str, dict] = {}
            for key, c in self._agents.items():
                avg = (c.latency_sum_s / c.latency_count) if c.latency_count else None
                out[key] = {
                    "invocations": c.invocations,
                    "tool_calls": c.tool_calls,
                    "input_tokens": c.input_tokens,
                    "output_tokens": c.output_tokens,
                    "avg_latency_s": avg,
                    "last_active": c.last_active,
                }
            return out


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ── Module singleton ──────────────────────────────────────────────────────────

_registry: MetricsRegistry | None = None
_registry_lock = threading.Lock()


def get_metrics_registry() -> MetricsRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = MetricsRegistry()
    return _registry


# ── Helpers ───────────────────────────────────────────────────────────────────

def tool_to_agent_key(tool_name: str) -> str:
    """Map an orchestrator tool name to the agent that backs it.

    Local system-agent tools are gateway-prefixed as ``<id>___execute_sql_<id>``
    (the id uses underscores for hyphens, e.g. ``mes_mil___...``). The orchestrator's
    own graph/schema tools have no prefix and are attributed to the orchestrator.
    """
    if not tool_name:
        return "orchestrator"
    if "___" in tool_name:
        prefix = tool_name.split("___", 1)[0]
        return prefix.replace("_", "-")
    return "orchestrator"


def systems_from_tool_names(tool_names) -> "list[str]":
    """PURE: distinct owning-system ids from gateway-prefixed tool names
    (``<system>___<tool>`` — the :func:`tool_to_agent_key` convention; ids
    use ``_`` for ``-``). Non-prefixed tools (orchestrator-own) don't count.

    Public home for the derivation (it was ``learning_routes.
    _systems_from_tool_names``; cross-module consumers import it from HERE —
    the tool-name → agent-key convention already lives in this module).
    """
    systems = {tool_to_agent_key(name)
               for name in tool_names or ()
               if isinstance(name, str) and "___" in name}
    return sorted(system for system in systems if system)


def extract_strands_usage(result) -> tuple[int, int]:
    """Best-effort (input, output) token counts from a Strands AgentResult.

    Returns (0, 0) if the metrics shape is unavailable — counters still work.
    """
    try:
        metrics = getattr(result, "metrics", None)
        usage = getattr(metrics, "accumulated_usage", None) or {}
        return (
            int(usage.get("inputTokens", 0) or 0),
            int(usage.get("outputTokens", 0) or 0),
        )
    except Exception:
        return (0, 0)
