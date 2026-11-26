# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the Agents-page fast path in ``agents/agents_routes.py``:

  1. The SNAPSHOT TTL CACHE (mirrors the dashboard cards cache): warm reads never
     recompute, a cold miss computes exactly once, and a present-but-expired
     snapshot is served slightly-stale while the background warmer is active
     (``AGENTS_FLEET_POLL``) instead of blocking the request.
  2. The COMPUTE mapping: ``_compute_cloud_fleet`` issues ONE batched
     ``GetMetricData`` (mocked CloudWatch) and maps the native metrics back onto
     the same per-agent dict contract the frontend consumes — with per-system tool
     calls aggregated generically off the ``<system_id>___<tool>`` prefix, tokens/
     cost from ``AWS/Bedrock``, and the hourly sparkline from runtime invocations.

No AWS, no graph, no LLM: the CloudWatch client, runtime list, per-system model
graph read, and the small orchestrator-only Insights enrichment are all stubbed.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import Mock

import agents.agents_routes as ar


# ────────────────────────────────────────────────────────────────────────────
# 1. Snapshot TTL cache
# ────────────────────────────────────────────────────────────────────────────

def test_refresh_fleet_cache_populates_and_warm_read_does_not_recompute(monkeypatch):
    monkeypatch.setenv("AGENTS_FLEET_TTL", "9999")
    ar.invalidate_fleet_cache()
    calls = {"n": 0}

    def _compute(hours):
        calls["n"] += 1
        return [{"name": "adt_orchestrator", "hours": hours}]

    monkeypatch.setattr(ar, "_compute_cloud_fleet", _compute)

    out = ar.refresh_fleet_cache(24)
    assert out == [{"name": "adt_orchestrator", "hours": 24}]
    assert calls["n"] == 1

    # Warm read for the SAME window serves cache without a second compute.
    served = ar._cached_fleet(24)
    assert served == out
    assert calls["n"] == 1


def test_cached_fleet_cold_miss_computes_once(monkeypatch):
    monkeypatch.setenv("AGENTS_FLEET_TTL", "9999")
    ar.invalidate_fleet_cache()
    calls = {"n": 0}
    monkeypatch.setattr(ar, "_compute_cloud_fleet",
                        lambda hours: (calls.__setitem__("n", calls["n"] + 1), [{"h": hours}])[1])

    first = ar._cached_fleet(8)
    second = ar._cached_fleet(8)
    assert first == [{"h": 8}] == second
    assert calls["n"] == 1          # computed once, then served warm


def test_cached_fleet_keyed_by_hours(monkeypatch):
    monkeypatch.setenv("AGENTS_FLEET_TTL", "9999")
    ar.invalidate_fleet_cache()
    monkeypatch.setattr(ar, "_compute_cloud_fleet", lambda hours: [{"h": hours}])

    assert ar._cached_fleet(24) == [{"h": 24}]
    assert ar._cached_fleet(168) == [{"h": 168}]   # distinct bucket, own entry
    assert set(ar._FLEET_CACHE.keys()) == {"24", "168"}


def test_cached_fleet_serves_stale_while_warming(monkeypatch):
    # AGENTS_FLEET_POLL set ⇒ a background warmer keeps it fresh, so an EXPIRED but
    # present snapshot is served as-is rather than blocking on a fresh compute.
    monkeypatch.setenv("AGENTS_FLEET_TTL", "90")
    monkeypatch.setenv("AGENTS_FLEET_POLL", "1")
    ar.invalidate_fleet_cache()
    stale = [{"name": "stale"}]
    ar._FLEET_CACHE["24"] = {"ts": time.monotonic() - 100_000, "agents": stale}
    boom = Mock(side_effect=AssertionError("must not compute while warming"))
    monkeypatch.setattr(ar, "_compute_cloud_fleet", boom)

    assert ar._cached_fleet(24) is stale
    boom.assert_not_called()


def test_cached_fleet_reresolves_on_expiry_when_not_warming(monkeypatch):
    # Poll OFF ⇒ an expired snapshot recomputes inline (no serve-stale-forever).
    monkeypatch.setenv("AGENTS_FLEET_TTL", "90")
    monkeypatch.delenv("AGENTS_FLEET_POLL", raising=False)
    ar.invalidate_fleet_cache()
    ar._FLEET_CACHE["24"] = {"ts": time.monotonic() - 100_000, "agents": [{"old": 1}]}
    monkeypatch.setattr(ar, "_compute_cloud_fleet", lambda hours: [{"fresh": 1}])

    assert ar._cached_fleet(24) == [{"fresh": 1}]


def test_refresh_fleet_cache_failsoft_keeps_prior(monkeypatch):
    monkeypatch.setenv("AGENTS_FLEET_TTL", "9999")
    ar.invalidate_fleet_cache()
    monkeypatch.setattr(ar, "_compute_cloud_fleet", lambda hours: [{"good": 1}])
    ar.refresh_fleet_cache(24)

    monkeypatch.setattr(ar, "_compute_cloud_fleet",
                        Mock(side_effect=RuntimeError("boom")))
    out = ar.refresh_fleet_cache(24)          # must NOT raise
    assert out == [{"good": 1}]               # prior snapshot preserved
    assert ar._FLEET_CACHE["24"]["agents"] == [{"good": 1}]


def test_ttl_zero_disables_cache(monkeypatch):
    monkeypatch.setenv("AGENTS_FLEET_TTL", "0")
    ar.invalidate_fleet_cache()
    calls = {"n": 0}
    monkeypatch.setattr(ar, "_compute_cloud_fleet",
                        lambda hours: (calls.__setitem__("n", calls["n"] + 1), [{"h": hours}])[1])

    ar._cached_fleet(24)
    ar._cached_fleet(24)
    assert calls["n"] == 2                     # always fresh, nothing cached
    assert ar._FLEET_CACHE == {}


def test_build_cloud_fleet_serves_cache_via_executor(monkeypatch):
    monkeypatch.setenv("AGENTS_FLEET_TTL", "9999")
    ar.invalidate_fleet_cache()
    monkeypatch.setattr(ar, "_compute_cloud_fleet", lambda hours: [{"h": hours}])

    out = asyncio.run(ar._build_cloud_fleet(24))
    assert out == [{"h": 24}]


# ────────────────────────────────────────────────────────────────────────────
# 2. Compute mapping — ONE batched GetMetricData → agent dict contract
# ────────────────────────────────────────────────────────────────────────────

class _FakeCloudWatch:
    """Deterministic CloudWatch double: list_metrics enumerates tool Names,
    get_metric_data returns a canned value per query keyed on its dims."""

    def __init__(self, tool_names):
        self._tool_names = tool_names
        self.get_metric_data_calls = 0

    def get_paginator(self, op):
        assert op == "list_metrics"
        names = self._tool_names

        class _P:
            def paginate(self, **kwargs):
                yield {"Metrics": [
                    {"Dimensions": [
                        {"Name": "Method", "Value": "tools/call"},
                        {"Name": "Name", "Value": n},
                    ]} for n in names
                ]}
        return _P()

    def get_metric_data(self, MetricDataQueries, **kwargs):
        self.get_metric_data_calls += 1
        results = []
        for q in MetricDataQueries:
            m = q["MetricStat"]["Metric"]
            mn = m["MetricName"]
            dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
            v = self._value(mn, dims)
            results.append({"Id": q["Id"], "Label": "x",
                            "Timestamps": ["t"] if v is not None else [],
                            "Values": [v] if v is not None else []})
        return {"MetricDataResults": results}

    @staticmethod
    def _value(mn, dims):
        if dims.get("Method") == "tools/call":            # gateway tool
            return {"Invocations": 5.0, "Latency": 100.0}.get(mn, 1.0)
        if dims.get("Operation") == "InvokeAgentRuntime":  # runtime
            return {"Invocations": 100.0, "Duration": 2000.0,
                    "Errors": 0.0, "MemoryUsed-GBHours": 10.0}.get(mn)
        if "ModelId" in dims:                              # bedrock tokens
            return {"InputTokenCount": 1000.0, "OutputTokenCount": 100.0,
                    "InvocationLatency": 3000.0}.get(mn)
        return None


class _FakeControl:
    def __init__(self, runtimes):
        self._runtimes = runtimes

    def list_agent_runtimes(self):
        return {"agentRuntimes": self._runtimes}


class _FakeSession:
    def __init__(self, runtimes, tool_names):
        self._cw = _FakeCloudWatch(tool_names)
        self._control = _FakeControl(runtimes)

    def client(self, name, **kwargs):
        if name == "cloudwatch":
            return self._cw
        if name == "bedrock-agentcore-control":
            return self._control
        raise AssertionError(f"unexpected client requested: {name}")


def _wire_compute(monkeypatch):
    runtimes = [
        {"agentRuntimeName": "adt_orchestrator", "agentRuntimeId": "adt_orchestrator-a",
         "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/adt_orchestrator-a",
         "status": "READY"},
        {"agentRuntimeName": "adt_lake_athena", "agentRuntimeId": "adt_lake_athena-b",
         "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/adt_lake_athena-b",
         "status": "READY"},
    ]
    tool_names = ["lake-athena___run_query", "lake-athena___describe_schema",
                  "telem-sitewise___connect"]
    session = _FakeSession(runtimes, tool_names)
    monkeypatch.setattr(ar, "_boto_session", lambda: session)
    # Neptune per-system model read → none (use defaults); Insights enrichment off.
    import agents.db as db
    monkeypatch.setattr(db, "neptune_query", lambda *a, **k: [])
    monkeypatch.setattr(ar, "_get_metrics", lambda *a, **k: {})       # no Logs Insights
    monkeypatch.setattr(ar, "_get_eval_scores", lambda *a, **k: {})   # no Logs Insights
    return session


def test_compute_cloud_fleet_maps_native_metrics_to_contract(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    session = _wire_compute(monkeypatch)

    agents = ar._compute_cloud_fleet(24)

    # ONE batched GetMetricData round-trip for the whole fleet (not per-agent).
    assert session._cw.get_metric_data_calls == 1

    by = {a["name"]: a for a in agents}
    assert set(by) == {"adt_orchestrator", "adt_lake_athena"}

    # Contract fields the frontend (agents-shared.js) consumes are all present.
    for a in agents:
        for field in ("invocations_24h", "tool_calls_24h", "avg_latency_s",
                      "input_tokens_24h", "output_tokens_24h", "cost_usd_24h",
                      "hourly_sessions", "eval_scores", "status", "model_id"):
            assert field in a

    orch = by["adt_orchestrator"]
    athena = by["adt_lake_athena"]

    # invocations from native runtime Invocations (100); hourly sparkline populated.
    assert orch["invocations_24h"] == 100
    assert athena["invocations_24h"] == 100
    assert orch["hourly_sessions"] == [100]

    # Per-system tool calls aggregated off the ``___`` prefix:
    #   lake-athena has TWO tool Names × 5 = 10; orchestrator = total across systems
    #   (lake-athena 10 + telem-sitewise 5 = 15).
    assert athena["tool_calls_24h"] == 10
    assert orch["tool_calls_24h"] == 15

    # Latency from native Duration/InvocationLatency (ms → s).
    assert orch["avg_latency_s"] is not None

    # Orchestrator tokens/cost from AWS/Bedrock (sonnet); positive.
    assert orch["input_tokens_24h"] > 0
    assert orch["output_tokens_24h"] > 0
    assert orch["cost_usd_24h"] > 0

    # System-agent tokens distributed by invocation share (both share equally here).
    assert athena["cost_usd_24h"] >= 0


def test_compute_does_not_prefer_custom_namespace_sessions(monkeypatch):
    # The custom SessionCount metric filter adds one point per COMPLETE log
    # line, and a deployed runtime writes each line twice, so the orchestrator
    # session count keeps the Insights count even when that metric is populated.
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AGENTS_CUSTOM_METRIC_NAMESPACE", "ADT/Agents")
    session = _wire_compute(monkeypatch)

    # Extend the fake to answer the custom-namespace SessionCount query.
    orig = session._cw.get_metric_data

    def _patched(MetricDataQueries, **kwargs):
        resp = orig(MetricDataQueries, **kwargs)
        by_id = {q["Id"]: q for q in MetricDataQueries}
        for r in resp["MetricDataResults"]:
            m = by_id[r["Id"]]["MetricStat"]["Metric"]
            if m["Namespace"] == "ADT/Agents" and m["MetricName"] == "SessionCount":
                r["Values"], r["Timestamps"] = [42.0], ["t"]
        return resp

    session._cw.get_metric_data = _patched
    insights_count = Mock(return_value={"invocations_24h": 21, "sessions_measured": True,
                                        "hourly_sessions": [21], "hourly_measured": True})
    monkeypatch.setattr(ar, "_get_metrics", insights_count)

    agents = ar._compute_cloud_fleet(24)
    orch = next(a for a in agents if a["name"] == "adt_orchestrator")
    assert insights_count.called
    assert orch["invocations_24h"] == 21


def _wire_compute_models(monkeypatch, runtimes, agent_model, orch_model):
    """Wire _compute_cloud_fleet with a controllable set of runtimes and a LIVE
    configured-model result — plus a STALE per-system graph value that must be
    ignored for the displayed model."""
    tool_names = ["erp___run_query"]
    session = _FakeSession(runtimes, tool_names)
    monkeypatch.setattr(ar, "_boto_session", lambda: session)
    monkeypatch.setattr(
        ar, "_get_configured_models",
        lambda _s: {"orchestrator": orch_model, "agent": agent_model},
    )
    # Stale per-system snapshot in the graph — must NOT surface on the Agents page.
    import agents.db as db
    monkeypatch.setattr(
        db, "neptune_query",
        lambda *a, **k: [{"id": "erp", "model": "us.amazon.nova-2-lite-v1:0"}],
    )
    monkeypatch.setattr(ar, "_get_metrics", lambda *a, **k: {})
    monkeypatch.setattr(ar, "_get_eval_scores", lambda *a, **k: {})
    return session


def test_cloud_fleet_system_agent_uses_live_model_not_stale_graph(monkeypatch):
    """Regression: the cloud Agents page must show the LIVE configured system-agent
    model (Claude Haiku 4.5), never the stale per-system ``s.agent_model`` snapshot
    baked at onboard time (which showed "Nova Lite v2" after the model switch)."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    live_agent = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    live_orch = "us.anthropic.claude-sonnet-4-6"
    runtimes = [
        {"agentRuntimeName": "adt_orchestrator", "agentRuntimeId": "adt_orchestrator-a",
         "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/adt_orchestrator-a",
         "status": "READY"},
        {"agentRuntimeName": "adt_erp", "agentRuntimeId": "adt_erp-b",
         "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/adt_erp-b",
         "status": "READY"},
    ]
    _wire_compute_models(monkeypatch, runtimes, live_agent, live_orch)

    agents = ar._compute_cloud_fleet(24)
    by = {a["name"]: a for a in agents}

    assert by["adt_orchestrator"]["model_id"] == live_orch
    # System agent reports the LIVE model, never the stale nova graph value.
    assert by["adt_erp"]["model_id"] == live_agent
    assert "nova" not in by["adt_erp"]["model_id"].lower()
    assert by["adt_erp"].get("no_llm") is False


def test_cloud_fleet_digital_thread_is_tools_no_llm(monkeypatch):
    """Regression: adt_digital_thread is a knowledge-graph TOOLS runtime (no LLM) —
    it must report no model and a no_llm flag, while adt_onboarding (which DOES run
    an LLM for field mapping) keeps the live agent model."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    live_agent = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    live_orch = "us.anthropic.claude-sonnet-4-6"
    runtimes = [
        {"agentRuntimeName": "adt_digital_thread", "agentRuntimeId": "adt_digital_thread-a",
         "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/adt_digital_thread-a",
         "status": "READY"},
        {"agentRuntimeName": "adt_onboarding", "agentRuntimeId": "adt_onboarding-b",
         "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/adt_onboarding-b",
         "status": "READY"},
    ]
    _wire_compute_models(monkeypatch, runtimes, live_agent, live_orch)

    agents = ar._compute_cloud_fleet(24)
    by = {a["name"]: a for a in agents}

    # Tools runtime: no LLM, no model.
    assert by["adt_digital_thread"]["no_llm"] is True
    assert by["adt_digital_thread"]["model_id"] == ""
    # Onboarding DOES use an LLM — keeps the live agent model, not flagged no_llm.
    assert by["adt_onboarding"]["no_llm"] is False
    assert by["adt_onboarding"]["model_id"] == live_agent


def test_cloud_fleet_counts_tokens_under_the_global_profile_ids(monkeypatch):
    """The orchestrator bucket also sums the older Sonnet models' tokens. Bedrock
    reports tokens under the id the model was called with, which for every
    model with a global profile is ``global.<family>``; a hand-built ``us.`` key
    missed them (and ``ap.`` is not a Bedrock prefix at all)."""
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    runtimes = [{"agentRuntimeName": "adt_orchestrator", "agentRuntimeId": "adt_orchestrator-a",
                 "agentRuntimeArn": "arn:aws:bedrock-agentcore:ap-southeast-2:1:runtime/adt_orchestrator-a",
                 "status": "READY"}]
    session = _wire_compute_models(monkeypatch, runtimes,
                                   agent_model="global.anthropic.claude-haiku-5-5",
                                   orch_model="global.anthropic.claude-sonnet-5-5")
    tokens_only_for = "global.anthropic.claude-sonnet-4-6"  # used before the switch

    def _value(mn, dims):
        if "ModelId" in dims:
            if dims["ModelId"] != tokens_only_for:
                return None
            return {"InputTokenCount": 1000.0, "OutputTokenCount": 100.0}.get(mn)
        return _FakeCloudWatch._value(mn, dims)
    monkeypatch.setattr(session._cw, "_value", _value)

    orch = next(a for a in ar._compute_cloud_fleet(24) if a["name"] == "adt_orchestrator")
    assert orch["input_tokens_24h"] == 1000
    assert orch["output_tokens_24h"] == 100
