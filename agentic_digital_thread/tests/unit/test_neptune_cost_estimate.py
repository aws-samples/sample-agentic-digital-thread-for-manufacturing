# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Neptune Analytics cost estimate on the Agents tab.

Neptune Analytics bills provisioned m-NCU-hours (1 m-NCU = 1 GB of graph
memory) at the AWS Price List rate for the region, with no storage or I/O
charge. us-east-1: 16 m-NCU = $0.48/hour (SKU NBECQJKR9S6A9SYY).

Offline: boto3 is faked; no AWS, no network.
"""
from __future__ import annotations

import pytest

from agents import agents_routes


class _FakeGraph:
    def __init__(self, memory_gb: int, replicas: int = 0, status: str = "AVAILABLE"):
        self._graph = {"status": status, "provisionedMemory": memory_gb,
                       "replicaCount": replicas}

    def get_graph(self, graphIdentifier):  # noqa: N803 - boto3 casing
        return self._graph


class _FakeCloudWatch:
    def get_metric_statistics(self, **kwargs):
        return {"Datapoints": []}


def _metrics(monkeypatch, region: str, memory_gb: int = 16, replicas: int = 0,
             status: str = "AVAILABLE") -> dict:
    import boto3
    clients = {"neptune-graph": _FakeGraph(memory_gb, replicas, status),
               "cloudwatch": _FakeCloudWatch()}
    monkeypatch.setattr(boto3, "client", lambda name, **kwargs: clients[name])
    monkeypatch.setenv("AWS_REGION", region)
    monkeypatch.setenv("GRAPH_IDENTIFIER", "g-test")
    return agents_routes.get_neptune_metrics(hours=24.0)


def test_16_mncu_for_24_hours_in_us_east_1_is_11_52(monkeypatch):
    d = _metrics(monkeypatch, "us-east-1")
    assert d["ncu_count"] == 16           # m-NCUs: 1 per GB, not GB / 8
    assert d["mncu_price_per_hour"] == 0.03
    assert d["cost_usd"] == pytest.approx(11.52)
    assert d["cost_ncu"] == d["cost_usd"]


def test_no_storage_or_io_charge_is_reported(monkeypatch):
    d = _metrics(monkeypatch, "us-east-1")
    for key in ("cost_storage", "cost_io", "total_requests"):
        assert key not in d


def test_replicas_are_billed_like_the_primary(monkeypatch):
    d = _metrics(monkeypatch, "us-east-1", memory_gb=16, replicas=1)
    assert d["cost_usd"] == pytest.approx(23.04)


def test_regional_rate_applies(monkeypatch):
    d = _metrics(monkeypatch, "eu-central-1")
    assert d["cost_usd"] == pytest.approx(16 * 0.0363125 * 24)


def test_unknown_region_shows_no_cost_rather_than_a_guess(monkeypatch):
    d = _metrics(monkeypatch, "sa-east-1")
    assert d["ncu_count"] == 16
    assert d["mncu_price_per_hour"] is None
    assert d["cost_usd"] is None


def test_a_stopped_graph_is_estimated_at_the_stopped_rate(monkeypatch):
    """Price List: a stopped graph costs 10% of the running rate in every listed
    region (us-east-1 16 m-NCU: $0.48 running, $0.048 stopped)."""
    d = _metrics(monkeypatch, "us-east-1", status="STOPPED")
    assert d["billing_state"] == "stopped"
    assert d["mncu_price_per_hour"] == pytest.approx(0.003)
    assert d["cost_usd"] == pytest.approx(16 * 0.003 * 24)


def test_a_running_graph_says_it_was_priced_as_running(monkeypatch):
    assert _metrics(monkeypatch, "us-east-1")["billing_state"] == "running"
