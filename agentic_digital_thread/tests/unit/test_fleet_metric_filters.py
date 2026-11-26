# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the Phase-2 metric-filter bootstrap
(``agents/fleet_metric_filters.py``). No AWS — the CloudWatch Logs client is a
mock. Verifies: generic runtime-log-group discovery, idempotent per-group
``put_metric_filter`` with the right markers/namespace, orchestrator-only session
filter, flag-gated no-op, and fail-soft behavior."""
from __future__ import annotations

from unittest.mock import Mock

import agents.fleet_metric_filters as fmf


class _FakeLogs:
    def __init__(self, groups):
        self._groups = groups
        self.puts = []

    def get_paginator(self, op):
        assert op == "describe_log_groups"
        groups = self._groups

        class _P:
            def paginate(self, **kwargs):
                prefix = kwargs.get("logGroupNamePrefix", "")
                yield {"logGroups": [{"logGroupName": g} for g in groups
                                     if g.startswith(prefix)]}
        return _P()

    def put_metric_filter(self, **kwargs):
        self.puts.append(kwargs)


_GROUPS = [
    "/aws/bedrock-agentcore/runtimes/adt_orchestrator-a-DEFAULT",
    "/aws/bedrock-agentcore/runtimes/adt_lake_athena-b-DEFAULT",
    "/aws/bedrock-agentcore/runtimes/adt_telem_sitewise-c-DEFAULT",
]


def test_discover_runtime_log_groups_generic():
    logs = _FakeLogs(_GROUPS + ["/aws/other/thing"])
    found = fmf.discover_runtime_log_groups(logs)
    assert set(found) == set(_GROUPS)   # only the runtimes/ prefix, no literals


def test_ensure_metric_filters_orchestrator_only_session():
    logs = _FakeLogs(_GROUPS)
    report = fmf.ensure_metric_filters(logs, namespace="ADT/Agents")

    # ToolCall on every runtime; SessionCount ONLY on the orchestrator group.
    assert report["tool_filters"] == 3
    assert report["session_filters"] == 1
    assert report["namespace"] == "ADT/Agents"

    session_puts = [p for p in logs.puts if p["filterName"] == fmf.SESSION_FILTER_NAME]
    assert len(session_puts) == 1
    sp = session_puts[0]
    assert "orchestrator" in sp["logGroupName"]
    assert sp["filterPattern"] == '"ORCHESTRATOR COMPLETE"'
    mt = sp["metricTransformations"][0]
    assert mt["metricName"] == "SessionCount"
    assert mt["metricNamespace"] == "ADT/Agents"

    tool_puts = [p for p in logs.puts if p["filterName"] == fmf.TOOL_FILTER_NAME]
    assert all(p["filterPattern"] == '"Tool #"' for p in tool_puts)
    assert all(p["metricTransformations"][0]["metricName"] == "ToolCall" for p in tool_puts)


def test_ensure_metric_filters_is_idempotent_upsert():
    # put_metric_filter is an upsert; re-running produces the SAME set of puts.
    logs = _FakeLogs(_GROUPS)
    fmf.ensure_metric_filters(logs, namespace="ADT/Agents")
    first = list(logs.puts)
    fmf.ensure_metric_filters(logs, namespace="ADT/Agents")
    assert logs.puts[len(first):] == first   # identical calls the second time


def test_ensure_metric_filters_failsoft_per_group():
    logs = _FakeLogs(_GROUPS)
    logs.put_metric_filter = Mock(side_effect=RuntimeError("access denied"))
    report = fmf.ensure_metric_filters(logs, namespace="ADT/Agents")  # must not raise
    assert report["tool_filters"] == 0 and report["session_filters"] == 0


def test_bootstrap_noop_when_flag_off(monkeypatch):
    monkeypatch.delenv("AGENTS_METRIC_FILTERS", raising=False)
    # Must NOT touch boto/session when disabled.
    assert fmf.bootstrap_metric_filters(session=Mock(side_effect=AssertionError)) is None


def test_bootstrap_runs_when_flag_on(monkeypatch):
    monkeypatch.setenv("AGENTS_METRIC_FILTERS", "1")
    monkeypatch.setenv("AGENTS_CUSTOM_METRIC_NAMESPACE", "ADT/Agents")
    logs = _FakeLogs(_GROUPS)

    class _Sess:
        def client(self, name, **kw):
            assert name == "logs"
            return logs

    report = fmf.bootstrap_metric_filters(session=_Sess())
    assert report is not None
    assert report["session_filters"] == 1 and report["tool_filters"] == 3
