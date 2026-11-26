# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Native CloudWatch fleet-metric plumbing for the Agents page (single batched
``GetMetricData`` round-trip) — the fast replacement for the per-agent Logs
Insights scans + serial ``get_metric_statistics`` token calls.

Everything here is PURE and generic (no dataset / system-id / model literals):

  * :func:`tool_system_key` derives a system key from a gateway tool ``Name``
    (``<system_id>___<tool>``) — the SAME ``___`` separator convention the
    runtime uses (``runtime_app`` / ``metrics_registry``).
  * :func:`runtime_system_key` derives the same key from a runtime name
    (``adt_<id>``) so tool activity and runtime activity for one system correlate.
  * :func:`build_fleet_queries` builds a batched ``MetricDataQueries`` list plus a
    parallel index describing each query (GetMetricData Ids must be short, safe
    tokens, so we emit ``q0..qN`` and keep the descriptor out-of-band).
  * :func:`parse_fleet_metrics` folds the ``GetMetricData`` response back into
    per-system tool aggregates, per-runtime aggregates, and per-model token
    aggregates — summing each system's tool ``Name``s generically.

The verified native sources (us-east-1 probe, 2026-07):
  * ``AWS/Bedrock-AgentCore`` gateway ``Invocations`` / ``Latency`` / ``UserErrors``
    / ``SystemErrors`` / ``Throttles`` with dims Operation=InvokeGateway,
    Method=tools/call, Protocol=MCP, Name=``<sid>___<tool>``.
  * ``AWS/Bedrock-AgentCore`` runtime ``Invocations`` / ``Duration`` / ``Errors``
    with dims Resource=runtime-ARN, Operation=InvokeAgentRuntime, Name=``adt_x::DEFAULT``.
  * ``AWS/Bedrock-AgentCore`` ``MemoryUsed-GBHours`` (Service=AgentCore.Runtime,
    per-runtime Resource dim).
  * ``AWS/Bedrock`` ``InputTokenCount`` / ``OutputTokenCount`` / ``InvocationLatency``
    by ModelId.

Phase 2 seam: when a custom metric namespace is supplied (metric filters emitting
``SessionCount`` / ``ToolCall`` — see docs), :func:`build_fleet_queries` also emits
per-runtime queries against it so the read path can PREFER them over the
Insights-in-background session/event derivation. Absent ⇒ nothing extra is
emitted and the Insights fallback stands (filters are not retroactive).
"""
from __future__ import annotations

# Metric namespaces / names (constants, not dataset literals).
NS_AGENTCORE = "AWS/Bedrock-AgentCore"
NS_BEDROCK = "AWS/Bedrock"

_GATEWAY_SEP = "___"  # per-system tool prefix separator (runtime_app / metrics_registry)
_RUNTIME_PREFIX = "adt_"

# CloudWatch GetMetricData hard limit (queries per call).
MAX_QUERIES = 500


def _norm(key: str) -> str:
    """Normalize a system key so hyphen/underscore spelling differences collapse.

    Runtime names spell the id with underscores (``adt_lake_athena``); gateway tool
    ``Name``s and catalog ids spell it with hyphens (``lake-athena___run_query``).
    Normalizing both to hyphens lets tool activity and runtime activity for the
    same system correlate WITHOUT any hardcoded id."""
    return key.replace("_", "-").strip("-").lower()


def tool_system_key(tool_name: str) -> str | None:
    """System key for a gateway tool ``Name`` (``<system_id>___<tool>``).

    Returns ``None`` for a name with no ``___`` prefix (an unprefixed / built-in
    tool that is not attributable to an onboarded system)."""
    if not tool_name or _GATEWAY_SEP not in tool_name:
        return None
    return _norm(tool_name.split(_GATEWAY_SEP, 1)[0])


def runtime_system_key(runtime_name: str) -> str:
    """System key for a runtime name (``adt_<id>`` → ``<id>`` normalized).

    The orchestrator runtime (``adt_orchestrator``) normalizes to ``orchestrator``.
    """
    name = runtime_name or ""
    if name.startswith(_RUNTIME_PREFIX):
        name = name[len(_RUNTIME_PREFIX):]
    return _norm(name)


def _clamp_period(hours: float) -> int:
    """Aggregation period (s) for a windowed sum/avg: the whole window as ONE
    datapoint, capped at CloudWatch's 86400s max (longer windows sum the buckets
    in :func:`parse_fleet_metrics`). Always a multiple of 60."""
    window = max(60, int(hours * 3600))
    period = min(window, 86400)
    return period - (period % 60) or 60


def build_fleet_queries(
    runtimes: list[dict],
    tool_names: list[str],
    model_ids: list[str],
    hours: float,
    *,
    custom_namespace: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Build the batched ``MetricDataQueries`` + a parallel descriptor index.

    ``runtimes`` are AgentCore runtime dicts (need ``agentRuntimeName`` +
    ``agentRuntimeId``); ``tool_names`` are gateway tool ``Name`` dim values;
    ``model_ids`` are Bedrock ModelIds. One flat query list (≤ :data:`MAX_QUERIES`)
    covers the whole fleet in a single round-trip.

    Returns ``(queries, index)`` where ``index[i]`` describes ``queries[i]`` (its
    ``Id`` is ``q{i}``). Parsing is driven by the index, so query Ids stay short
    and safe regardless of arbitrary tool/model/ARN strings."""
    period = _clamp_period(hours)
    hourly_period = 3600
    queries: list[dict] = []
    index: list[dict] = []

    def _add(desc: dict, namespace: str, metric: str, dims: list[dict],
             stat: str, period_s: int) -> None:
        i = len(queries)
        queries.append({
            "Id": f"q{i}",
            "MetricStat": {
                "Metric": {"Namespace": namespace, "MetricName": metric,
                           "Dimensions": dims},
                "Period": period_s,
                "Stat": stat,
            },
            "ReturnData": True,
        })
        index.append(desc)

    # ── Per-tool gateway metrics (aggregated per-system in parse) ──────────
    for name in tool_names:
        sid = tool_system_key(name)
        if sid is None:
            continue
        base = [
            {"Name": "Operation", "Value": "InvokeGateway"},
            {"Name": "Method", "Value": "tools/call"},
            {"Name": "Protocol", "Value": "MCP"},
            {"Name": "Name", "Value": name},
        ]
        _add({"kind": "tool", "system": sid, "metric": "calls"},
             NS_AGENTCORE, "Invocations", base, "Sum", period)
        _add({"kind": "tool", "system": sid, "metric": "latency_ms"},
             NS_AGENTCORE, "Latency", base, "Average", period)
        for em in ("UserErrors", "SystemErrors", "Throttles"):
            _add({"kind": "tool", "system": sid, "metric": "errors"},
                 NS_AGENTCORE, em, base, "Sum", period)

    # ── Per-runtime metrics (invocations, latency, errors, memory, hourly) ─
    for rt in runtimes:
        rname = rt.get("agentRuntimeName", "")
        rid = rt.get("agentRuntimeId", "")
        if not rname or not rid:
            continue
        sid = runtime_system_key(rname)
        arn = rt.get("agentRuntimeArn") or _runtime_arn(rt)
        rt_dims = [
            {"Name": "Resource", "Value": arn},
            {"Name": "Operation", "Value": "InvokeAgentRuntime"},
            {"Name": "Name", "Value": f"{rname}::DEFAULT"},
        ]
        _add({"kind": "runtime", "system": sid, "runtime": rname, "metric": "invocations"},
             NS_AGENTCORE, "Invocations", rt_dims, "Sum", period)
        _add({"kind": "runtime", "system": sid, "runtime": rname, "metric": "latency_ms"},
             NS_AGENTCORE, "Duration", rt_dims, "Average", period)
        _add({"kind": "runtime", "system": sid, "runtime": rname, "metric": "errors"},
             NS_AGENTCORE, "Errors", rt_dims, "Sum", period)
        # hourly invocations → sparkline (chronological in parse)
        _add({"kind": "runtime", "system": sid, "runtime": rname, "metric": "hourly"},
             NS_AGENTCORE, "Invocations", rt_dims, "Sum", hourly_period)
        # per-runtime memory (Service dim + Resource dim)
        _add({"kind": "runtime", "system": sid, "runtime": rname, "metric": "mem_gbh"},
             NS_AGENTCORE, "MemoryUsed-GBHours",
             [{"Name": "Service", "Value": "AgentCore.Runtime"},
              {"Name": "Resource", "Value": arn}], "Sum", period)

        # Phase-2 seam: prefer custom-namespace native session metrics when the
        # metric filters exist (see docs/ + agents/fleet_metric_filters.py). The
        # ``ORCHESTRATOR COMPLETE`` marker is orchestrator-only and a metric filter
        # over unstructured logs cannot stamp a per-runtime dimension, so these are
        # DIMENSIONLESS and emitted for the orchestrator runtime only. Absent ⇒
        # Insights-in-background fallback stands (filters are not retroactive).
        if custom_namespace and "orchestrator" in rname.lower():
            _add({"kind": "custom", "system": sid, "runtime": rname, "metric": "sessions"},
                 custom_namespace, "SessionCount", [], "Sum", period)
            _add({"kind": "custom", "system": sid, "runtime": rname, "metric": "avg_events"},
                 custom_namespace, "SessionEvents", [], "Average", period)

    # ── Per-model token + latency (AWS/Bedrock) ───────────────────────────
    for mid in model_ids:
        if not mid:
            continue
        mdims = [{"Name": "ModelId", "Value": mid}]
        _add({"kind": "model", "model": mid, "metric": "input"},
             NS_BEDROCK, "InputTokenCount", mdims, "Sum", period)
        _add({"kind": "model", "model": mid, "metric": "output"},
             NS_BEDROCK, "OutputTokenCount", mdims, "Sum", period)
        _add({"kind": "model", "model": mid, "metric": "latency_ms"},
             NS_BEDROCK, "InvocationLatency", mdims, "Average", period)

    if len(queries) > MAX_QUERIES:
        # Defensive: never exceed the API cap. Callers should shard, but this keeps
        # a single call valid rather than erroring the whole page.
        queries = queries[:MAX_QUERIES]
        index = index[:MAX_QUERIES]
    return queries, index


def _runtime_arn(rt: dict) -> str:
    """Best-effort runtime ARN when the dict lacks ``agentRuntimeArn``.

    ``list_agent_runtimes`` returns ``agentRuntimeArn``; if a caller passed a
    trimmed dict we fall back to the raw id (the dim match then simply misses,
    yielding zeros — never an error)."""
    return rt.get("agentRuntimeArn", "") or rt.get("agentRuntimeId", "")


def _sum(values: list) -> float:
    return float(sum(v for v in values if v is not None))


def _avg(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def parse_fleet_metrics(results: list[dict], index: list[dict]) -> dict:
    """Fold a ``GetMetricData`` response into per-system / per-runtime / per-model
    aggregates. ``results`` is ``MetricDataResults``; ``index`` is the descriptor
    list from :func:`build_fleet_queries` (correlated by query ``Id`` ``q{i}``).

    Returns::

        {
          "tools":   {sid: {"calls": N, "latency_ms": x|None, "errors": N}},
          "runtimes":{rname: {"system": sid, "invocations": N, "latency_ms": x|None,
                              "errors": N, "mem_gbh": x, "hourly": [..], "sessions": N|None,
                              "avg_events": x|None}},
          "models":  {mid: {"input": N, "output": N, "latency_ms": x|None}},
        }

    Each system's tool ``Name`` datapoints are summed generically (calls/errors)
    or averaged (latency), so tool-calls-per-system needs no per-tool literals."""
    by_id: dict[str, dict] = {}
    for r in results:
        rid = r.get("Id")
        if rid:
            by_id[rid] = r

    tools: dict[str, dict] = {}
    runtimes: dict[str, dict] = {}
    models: dict[str, dict] = {}

    # Collect raw per-descriptor values first so per-system latency can average.
    tool_lat: dict[str, list] = {}
    for i, desc in enumerate(index):
        res = by_id.get(f"q{i}")
        if res is None:
            continue
        values = res.get("Values", []) or []
        kind = desc["kind"]

        if kind == "tool":
            sid = desc["system"]
            t = tools.setdefault(sid, {"calls": 0.0, "latency_ms": None, "errors": 0.0})
            metric = desc["metric"]
            if metric == "calls":
                t["calls"] += _sum(values)
            elif metric == "errors":
                t["errors"] += _sum(values)
            elif metric == "latency_ms":
                a = _avg(values)
                if a is not None:
                    tool_lat.setdefault(sid, []).append(a)

        elif kind == "runtime":
            rname = desc["runtime"]
            rt = runtimes.setdefault(rname, {
                "system": desc["system"], "invocations": 0.0, "latency_ms": None,
                "errors": 0.0, "mem_gbh": 0.0, "hourly": [], "sessions": None,
                "avg_events": None,
            })
            metric = desc["metric"]
            if metric == "invocations":
                rt["invocations"] += _sum(values)
            elif metric == "errors":
                rt["errors"] += _sum(values)
            elif metric == "mem_gbh":
                rt["mem_gbh"] += _sum(values)
            elif metric == "latency_ms":
                rt["latency_ms"] = _avg(values)
            elif metric == "hourly":
                rt["hourly"] = _hourly_series(res)

        elif kind == "custom":
            rname = desc["runtime"]
            rt = runtimes.setdefault(rname, {
                "system": desc["system"], "invocations": 0.0, "latency_ms": None,
                "errors": 0.0, "mem_gbh": 0.0, "hourly": [], "sessions": None,
                "avg_events": None,
            })
            if desc["metric"] == "sessions":
                rt["sessions"] = _sum(values)
            elif desc["metric"] == "avg_events":
                rt["avg_events"] = _avg(values)

        elif kind == "model":
            mid = desc["model"]
            m = models.setdefault(mid, {"input": 0.0, "output": 0.0, "latency_ms": None})
            metric = desc["metric"]
            if metric == "input":
                m["input"] += _sum(values)
            elif metric == "output":
                m["output"] += _sum(values)
            elif metric == "latency_ms":
                m["latency_ms"] = _avg(values)

    # Finalize per-system tool latency (average across the system's tool Names).
    for sid, lats in tool_lat.items():
        tools[sid]["latency_ms"] = _avg(lats)

    # Cast counts to ints (they are CloudWatch Sums of integer counters).
    for t in tools.values():
        t["calls"] = int(t["calls"])
        t["errors"] = int(t["errors"])
    for rt in runtimes.values():
        rt["invocations"] = int(rt["invocations"])
        rt["errors"] = int(rt["errors"])
        if rt["sessions"] is not None:
            rt["sessions"] = int(rt["sessions"])
    for m in models.values():
        m["input"] = int(m["input"])
        m["output"] = int(m["output"])

    return {"tools": tools, "runtimes": runtimes, "models": models}


def _hourly_series(result: dict) -> list[int]:
    """Chronological hourly counts from one ``GetMetricData`` result.

    GetMetricData returns Timestamps/Values paired; order depends on ``ScanBy``.
    We sort by timestamp ascending so the sparkline reads oldest→newest
    regardless of the scan direction the caller used."""
    ts = result.get("Timestamps", []) or []
    vals = result.get("Values", []) or []
    paired = sorted(zip(ts, vals), key=lambda p: p[0])
    return [int(v or 0) for _, v in paired]
