# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for ``agents/fleet_metrics.py`` — the pure, batched
``GetMetricData`` query builder + response parser that replaces the per-agent
Logs Insights scans on the Agents page.

No AWS, no graph, no DB, no LLM — pure functions over plain dicts. Verifies:
  • the batched query set is built correctly (one flat list, safe ``q{i}`` Ids);
  • per-system tool aggregation is GENERIC off the ``<system_id>___<tool>`` prefix
    (no dataset/system literals — arbitrary ids flow through);
  • runtime + model queries are emitted with the right native dims;
  • the parser folds the response back into per-system / per-runtime / per-model
    aggregates (sum for counts, average for latency, chronological sparkline);
  • the Phase-2 custom-namespace seam emits extra queries only when supplied.
"""
from __future__ import annotations

import agents.fleet_metrics as fm


# --- system-key derivation (generic, no literals) ----------------------------

def test_tool_system_key_from_prefix():
    assert fm.tool_system_key("lake-athena___run_query") == "lake-athena"
    assert fm.tool_system_key("some-new-sys___any_tool") == "some-new-sys"
    # underscore vs hyphen spelling collapses so runtime/tool correlate
    assert fm.tool_system_key("mes_mil___execute_sql") == "mes-mil"
    # no prefix ⇒ not attributable to a system
    assert fm.tool_system_key("explore_graph") is None
    assert fm.tool_system_key("") is None


def test_runtime_system_key_strips_adt_and_normalizes():
    assert fm.runtime_system_key("adt_orchestrator") == "orchestrator"
    assert fm.runtime_system_key("adt_lake_athena") == "lake-athena"
    assert fm.runtime_system_key("adt_mes_mil") == "mes-mil"


def test_tool_and_runtime_keys_correlate():
    # The whole point: a runtime and its gateway tools resolve to the SAME key.
    assert fm.runtime_system_key("adt_lake_athena") == fm.tool_system_key("lake-athena___run_query")


# --- query build -------------------------------------------------------------

_RUNTIMES = [
    {"agentRuntimeName": "adt_orchestrator", "agentRuntimeId": "adt_orchestrator-a",
     "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/adt_orchestrator-a"},
    {"agentRuntimeName": "adt_lake_athena", "agentRuntimeId": "adt_lake_athena-b",
     "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/adt_lake_athena-b"},
]
_TOOLS = ["lake-athena___run_query", "lake-athena___describe_schema",
          "telem-sitewise___connect", "explore_graph"]  # last is unprefixed
_MODELS = ["us.anthropic.claude-sonnet-4-6", "us.amazon.nova-2-lite-v1:0"]


def test_build_queries_ids_are_safe_and_sequential():
    queries, index = fm.build_fleet_queries(_RUNTIMES, _TOOLS, _MODELS, 24)
    assert len(queries) == len(index)
    assert [q["Id"] for q in queries] == [f"q{i}" for i in range(len(queries))]
    # Every query is a well-formed MetricStat with a numeric period + stat.
    for q in queries:
        ms = q["MetricStat"]
        assert ms["Metric"]["Namespace"] in (fm.NS_AGENTCORE, fm.NS_BEDROCK)
        assert isinstance(ms["Period"], int) and ms["Period"] % 60 == 0
        assert ms["Stat"] in ("Sum", "Average")


def test_build_queries_skips_unprefixed_tools_and_groups_by_system():
    _, index = fm.build_fleet_queries(_RUNTIMES, _TOOLS, _MODELS, 24)
    tool_systems = {d["system"] for d in index if d["kind"] == "tool"}
    # 'explore_graph' (no ___) contributes NO tool query.
    assert tool_systems == {"lake-athena", "telem-sitewise"}
    # Two lake-athena tool Names both attribute to the ONE system key (aggregation).
    athena_calls = [d for d in index
                    if d["kind"] == "tool" and d["system"] == "lake-athena" and d["metric"] == "calls"]
    assert len(athena_calls) == 2


def test_build_queries_emits_runtime_and_model_queries():
    _, index = fm.build_fleet_queries(_RUNTIMES, _TOOLS, _MODELS, 24)
    rt_metrics = {(d["runtime"], d["metric"]) for d in index if d["kind"] == "runtime"}
    for rt in ("adt_orchestrator", "adt_lake_athena"):
        for metric in ("invocations", "latency_ms", "errors", "hourly", "mem_gbh"):
            assert (rt, metric) in rt_metrics
    model_metrics = {(d["model"], d["metric"]) for d in index if d["kind"] == "model"}
    for mid in _MODELS:
        for metric in ("input", "output", "latency_ms"):
            assert (mid, metric) in model_metrics


def test_build_queries_custom_namespace_seam_off_by_default():
    _, index = fm.build_fleet_queries(_RUNTIMES, _TOOLS, _MODELS, 24)
    assert not any(d["kind"] == "custom" for d in index)
    _, index2 = fm.build_fleet_queries(_RUNTIMES, _TOOLS, _MODELS, 24, custom_namespace="ADT/Agents")
    custom = {(d["runtime"], d["metric"]) for d in index2 if d["kind"] == "custom"}
    assert ("adt_orchestrator", "sessions") in custom
    assert ("adt_orchestrator", "avg_events") in custom


def test_build_queries_hourly_uses_3600_period():
    queries, index = fm.build_fleet_queries(_RUNTIMES, _TOOLS, _MODELS, 24)
    for i, d in enumerate(index):
        if d.get("metric") == "hourly":
            assert queries[i]["MetricStat"]["Period"] == 3600


def test_clamp_period_caps_at_86400():
    # 1-week window is capped to the 86400s CloudWatch max (buckets summed in parse).
    queries, index = fm.build_fleet_queries(_RUNTIMES, _TOOLS, _MODELS, 168)
    non_hourly = [queries[i]["MetricStat"]["Period"]
                  for i, d in enumerate(index) if d.get("metric") != "hourly"]
    assert non_hourly and all(p <= 86400 for p in non_hourly)


# --- response parse ----------------------------------------------------------

def _results_from_index(index, value_for):
    """Fabricate a GetMetricData MetricDataResults list aligned to ``index``.

    ``value_for(i, desc)`` returns the Values list for query ``q{i}``."""
    out = []
    for i, desc in enumerate(index):
        vals = value_for(i, desc)
        out.append({"Id": f"q{i}", "Label": "x",
                    "Timestamps": [f"t{i}"] * len(vals), "Values": vals})
    return out


def test_parse_aggregates_per_system_tools():
    _, index = fm.build_fleet_queries(_RUNTIMES, _TOOLS, _MODELS, 24)

    def value_for(i, desc):
        if desc["kind"] == "tool" and desc["system"] == "lake-athena":
            if desc["metric"] == "calls":
                return [10.0]          # two tools → summed to 20
            if desc["metric"] == "latency_ms":
                return [100.0]         # two tools → averaged to 100
            if desc["metric"] == "errors":
                return [1.0]           # 3 error metrics × 2 tools → summed to 6
        return []

    parsed = fm.parse_fleet_metrics(_results_from_index(index, value_for), index)
    athena = parsed["tools"]["lake-athena"]
    assert athena["calls"] == 20       # 10 + 10 summed across the system's Names
    assert athena["latency_ms"] == 100.0
    assert athena["errors"] == 6       # (UserErrors+SystemErrors+Throttles) × 2 tools


def test_parse_maps_runtime_and_hourly_and_models():
    _, index = fm.build_fleet_queries(_RUNTIMES, _TOOLS, _MODELS, 24)

    def value_for(i, desc):
        if desc["kind"] == "runtime" and desc["runtime"] == "adt_orchestrator":
            return {"invocations": [743.0], "latency_ms": [2500.0], "errors": [2.0],
                    "mem_gbh": [515.0], "hourly": [3.0, 1.0, 2.0]}.get(desc["metric"], [])
        if desc["kind"] == "model" and desc["model"] == _MODELS[0]:
            return {"input": [1000.0], "output": [50.0], "latency_ms": [3000.0]}.get(desc["metric"], [])
        return []

    # Give the hourly result out-of-order timestamps to prove chronological sort.
    results = _results_from_index(index, value_for)
    for i, desc in enumerate(index):
        if desc.get("runtime") == "adt_orchestrator" and desc.get("metric") == "hourly":
            results[i]["Timestamps"] = ["2026-01-01T02:00", "2026-01-01T00:00", "2026-01-01T01:00"]
            results[i]["Values"] = [2.0, 3.0, 1.0]   # unsorted → expect [3,1,2]

    parsed = fm.parse_fleet_metrics(results, index)
    orch = parsed["runtimes"]["adt_orchestrator"]
    assert orch["invocations"] == 743
    assert orch["latency_ms"] == 2500.0
    assert orch["errors"] == 2
    assert orch["mem_gbh"] == 515.0
    assert orch["hourly"] == [3, 1, 2]   # sorted by timestamp ascending
    m = parsed["models"][_MODELS[0]]
    assert m["input"] == 1000 and m["output"] == 50 and m["latency_ms"] == 3000.0


def test_parse_custom_namespace_sessions():
    _, index = fm.build_fleet_queries(_RUNTIMES, _TOOLS, _MODELS, 24, custom_namespace="ADT/Agents")

    def value_for(i, desc):
        if desc["kind"] == "custom" and desc["runtime"] == "adt_orchestrator":
            return {"sessions": [42.0], "avg_events": [7.0]}.get(desc["metric"], [])
        return []

    parsed = fm.parse_fleet_metrics(_results_from_index(index, value_for), index)
    orch = parsed["runtimes"]["adt_orchestrator"]
    assert orch["sessions"] == 42
    assert orch["avg_events"] == 7.0


def test_parse_missing_results_are_zero_not_error():
    # An empty response yields empty aggregates (never raises); the read path uses
    # ``.get(sid, {}).get(metric, 0)`` so a missing system reads as zero.
    _, index = fm.build_fleet_queries(_RUNTIMES, _TOOLS, _MODELS, 24)
    parsed = fm.parse_fleet_metrics([], index)
    assert parsed == {"tools": {}, "runtimes": {}, "models": {}}
    assert parsed["tools"].get("lake-athena", {}).get("calls", 0) == 0
