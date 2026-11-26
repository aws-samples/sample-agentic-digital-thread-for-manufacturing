# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Phase-2 (SCAFFOLD) — turn the app-semantic Logs Insights markers into NATIVE
CloudWatch metrics via idempotent metric filters, so the Agents page can read
sessions from the SAME batched ``GetMetricData`` call and drop the last Insights
query even from the background snapshot compute.

WHY startup ``put_metric_filter`` and not CDK MetricFilter constructs
--------------------------------------------------------------------
The AgentCore runtimes are created at DEPLOY TIME by the provisioner
(``bedrock-agentcore:CreateAgentRuntime`` — see ``stacks/onboarding_stack.py``),
NOT as CDK resources. Their log groups (``/aws/bedrock-agentcore/runtimes/
{runtimeId}-DEFAULT``) are therefore created implicitly by the AgentCore service
and DO NOT EXIST at CDK synth time — and new ones appear every time a system is
(re)onboarded. A static CDK ``MetricFilter`` cannot target a log group that does
not yet exist. So the correct fit for THIS repo is a small, generic, idempotent
``put_metric_filter`` bootstrap that runs at app startup (and can be re-run) over
the runtime log groups it DISCOVERS. ``put_metric_filter`` is an upsert keyed by
(logGroupName, filterName), so re-running is safe.

What it emits (custom namespace, default ``ADT/Agents``)
-------------------------------------------------------
  * ``SessionCount`` — 1 per ``ORCHESTRATOR COMPLETE`` log line, on the
    ORCHESTRATOR runtime log group(s) only (that marker is orchestrator-only).
    Dimensionless: a metric filter over unstructured text cannot stamp a static
    per-runtime dimension, and sessions are an orchestrator-level concept anyway.
  * ``ToolCall`` — 1 per ``Tool #`` log line, on every runtime log group.

Both metrics are unused scaffolding for now. With
``AGENTS_CUSTOM_METRIC_NAMESPACE`` set, ``fleet_metrics.build_fleet_queries``
still requests them, but the Agents tab ignores ``SessionCount`` for the
orchestrator and nothing reads ``ToolCall`` (see below). They become usable once
the application emits one metric point per turn itself. Filters are NOT
retroactive — they only score log events written after creation.

Everything here is generic — no dataset / system-id literals. ``SESSION_MARKER``
is the marker the Insights session count matches (``agents_routes._get_metrics``).
The Agents tab does not read ``SessionCount`` for now: a deployed runtime writes
each line twice (its runtime stream and the shared ``otel-rt-logs`` stream), and
a metric filter counts every copy, where the Insights count folds the copies of
a turn by its trace id. ``TOOL_MARKER`` matches no line the runtimes write today
(the Insights tool count reads ``tool_calls=`` on the COMPLETE line, or each
turn's ``DETERMINISM`` line).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Custom namespace + metric names (constants, not dataset literals).
DEFAULT_NAMESPACE = "ADT/Agents"
SESSION_METRIC = "SessionCount"
TOOL_METRIC = "ToolCall"

# Log markers — mirror the existing Insights query patterns (single source of the
# app-semantic markers; keep in sync with agents_routes._get_metrics).
SESSION_MARKER = "ORCHESTRATOR COMPLETE"
TOOL_MARKER = "Tool #"

# Filter names (put_metric_filter upserts by (logGroupName, filterName)).
SESSION_FILTER_NAME = "adt-agents-session-count"
TOOL_FILTER_NAME = "adt-agents-tool-call"

_RUNTIME_LOG_PREFIX = "/aws/bedrock-agentcore/runtimes/"


def _enabled() -> bool:
    """True iff the ``AGENTS_METRIC_FILTERS`` flag is set. Default OFF — creating
    filters needs ``logs:PutMetricFilter`` and only takes effect after a deploy."""
    return bool(os.environ.get("AGENTS_METRIC_FILTERS", "").strip())


def _namespace() -> str:
    return os.environ.get("AGENTS_CUSTOM_METRIC_NAMESPACE", "").strip() or DEFAULT_NAMESPACE


def discover_runtime_log_groups(logs_client) -> list[str]:
    """Every AgentCore runtime log group currently present (generic discovery).

    Returns the full ``/aws/bedrock-agentcore/runtimes/{id}-DEFAULT`` names so the
    bootstrap can attach filters per-runtime without any hardcoded runtime id."""
    groups: list[str] = []
    try:
        paginator = logs_client.get_paginator("describe_log_groups")
        for page in paginator.paginate(logGroupNamePrefix=_RUNTIME_LOG_PREFIX):
            for lg in page.get("logGroups", []):
                name = lg.get("logGroupName", "")
                if name:
                    groups.append(name)
    except Exception as e:
        logger.warning("discover_runtime_log_groups failed: %s", e)
    return groups


def _is_orchestrator_group(log_group: str) -> bool:
    """The orchestrator runtime log group (``...runtimes/adt_orchestrator-*-DEFAULT``)."""
    return "orchestrator" in log_group.lower()


def _put(logs_client, log_group: str, filter_name: str, pattern: str,
         metric_name: str, namespace: str) -> bool:
    """Idempotent single ``put_metric_filter`` (upsert). Fail-soft: returns False
    on error rather than raising into startup."""
    try:
        logs_client.put_metric_filter(
            logGroupName=log_group,
            filterName=filter_name,
            filterPattern=pattern,
            metricTransformations=[{
                "metricName": metric_name,
                "metricNamespace": namespace,
                "metricValue": "1",
                "defaultValue": 0.0,
            }],
        )
        return True
    except Exception as e:
        logger.warning("put_metric_filter(%s on %s) failed: %s",
                       filter_name, log_group, e)
        return False


def ensure_metric_filters(logs_client, *, namespace: str | None = None,
                          log_groups: list[str] | None = None) -> dict:
    """Idempotently ensure the session/tool metric filters exist on the runtime
    log groups. Discovers the groups when not supplied. Returns a small report
    (created counts per metric) for logging/tests. Never raises."""
    ns = namespace or _namespace()
    groups = log_groups if log_groups is not None else discover_runtime_log_groups(logs_client)
    report = {"namespace": ns, "log_groups": len(groups),
              "session_filters": 0, "tool_filters": 0}
    for lg in groups:
        # ToolCall on every runtime; SessionCount on the orchestrator only.
        if _put(logs_client, lg, TOOL_FILTER_NAME,
                f'"{TOOL_MARKER}"', TOOL_METRIC, ns):
            report["tool_filters"] += 1
        if _is_orchestrator_group(lg) and _put(
                logs_client, lg, SESSION_FILTER_NAME,
                f'"{SESSION_MARKER}"', SESSION_METRIC, ns):
            report["session_filters"] += 1
    return report


def bootstrap_metric_filters(session=None) -> dict | None:
    """Startup entry point (flag-gated ``AGENTS_METRIC_FILTERS``). NO-OP + returns
    ``None`` when the flag is off, so the runtime is byte-identical by default.

    Fail-soft end to end: a missing permission / transient error is logged and
    swallowed — the read path simply keeps using the Insights-in-background
    fallback until the filters exist and start scoring new log events."""
    if not _enabled():
        return None
    try:
        if session is None:
            import boto3
            session = boto3.Session(region_name=os.environ.get("AWS_REGION", "us-east-1"))
        logs_client = session.client("logs")
        report = ensure_metric_filters(logs_client)
        logger.info("Agents metric filters ensured: %s", report)
        return report
    except Exception as e:  # nosec B110 - fail-soft bootstrap
        logger.warning("bootstrap_metric_filters failed (non-fatal): %s", e)
        return None
