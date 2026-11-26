# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The dashboard leaves out systems it has no reader for.

A system on a vendor REST connection (Tulip, generic OpenAPI) is answered
only through its per-system agent at chat time. The dashboard's section
renderer and the metric fan-out compose SQL, so before this rule every such
system showed up as a failed site on the metric cards and as one
"Section unavailable" row per table. Generic ids only.
"""
from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

import agents.dashboard as dashboard
import agents.entity_render as er
import agents.metric_multisite as mms
from agents import connection_types as ct
from agents.metric_layer import Aggregation, DirectMetric, MetricLayer

CONCEPT = "concept-a"
METRIC = DirectMetric(id="m-avg", concept=CONCEPT, aggregation=Aggregation.AVG,
                      label="Avg Metric", unit="percent")


class _Layer(MetricLayer):
    def get_metric(self, metric_id):
        if metric_id != METRIC.id:
            raise KeyError(metric_id)
        return METRIC

    def list_metrics(self):
        return [METRIC]


SQL_A = {"id": "sys-a", "name": "A", "plant": "site-x", "connection_type": ct.POSTGRESQL}
SQL_B = {"id": "sys-b", "name": "B", "plant": "site-y"}  # no type = PostgreSQL
VENDOR = {"id": "sys-v", "name": "V", "plant": "site-x", "connection_type": ct.TULIP}
TULIP = {"id": "sys-t", "name": "T", "plant": "site-y", "connection_type": ct.TULIP}
OPENAPI = {"id": "sys-o", "name": "O", "plant": "site-y", "connection_type": ct.REST_API}
ATHENA = {"id": "sys-l", "name": "L", "plant": "site-x", "connection_type": ct.ATHENA}
SITEWISE = {"id": "sys-s", "name": "S", "plant": "", "connection_type": ct.SITEWISE_DIRECT}
I3X = {"id": "sys-i", "name": "I", "plant": "", "connection_type": ct.SITEWISE_I3X}
MCP = {"id": "sys-m", "name": "M", "plant": "", "connection_type": ct.MCP_SERVER}

EVERY = [SQL_A, SQL_B, VENDOR, TULIP, OPENAPI, ATHENA, SITEWISE, I3X, MCP]


@pytest.mark.parametrize("system,expected", [
    (SQL_A, True), (SQL_B, True), (ATHENA, True), (SITEWISE, True), (I3X, True),
    (MCP, True),  # unchanged: the metric path still hands MCP its own query
    (VENDOR, False), (TULIP, False), (OPENAPI, False),
])
def test_structured_read_supported(system, expected):
    assert ct.structured_read_supported(system) is expected


def test_a_new_rest_vendor_is_left_out_without_any_extra_declaration(monkeypatch):
    """The registry owns the answer: a vendor registered without
    ``structured_reader`` is off the dashboard automatically."""
    import dataclasses
    import agents.rest_vendor_registry as reg
    template = next(a for a in reg.REST_VENDOR_REGISTRY if a.name == "tulip")
    newcomer = dataclasses.replace(
        template, name="newcomer",
        detect=lambda connection_type, base_url, entry=None: connection_type == "Newcomer (REST)")
    monkeypatch.setattr(reg, "REST_VENDOR_REGISTRY", [newcomer, *reg.REST_VENDOR_REGISTRY])
    assert ct.structured_read_supported({"connection_type": "Newcomer (REST)"}) is False
    assert ct.structured_read_supported(SQL_A) is True


def test_metric_fan_out_skips_systems_with_no_structured_reader():
    covered = [{"system_id": s["id"], "concept_id": CONCEPT} for s in EVERY]
    out = mms.systems_covering(METRIC.id, query=lambda *a, **k: covered,
                               metric_layer=_Layer(), systems=EVERY)
    assert [e["system_id"] for e in out] == [
        "sys-a", "sys-b", "sys-l", "sys-s", "sys-i", "sys-m"]


def test_section_fan_out_skips_systems_with_no_structured_reader(monkeypatch):
    rendered = []

    def fake_render(system, **_kwargs):
        rendered.append(system["id"])
        return [{"system_id": system["id"], "status": "ok"}]

    monkeypatch.setattr(er, "_render_system_sections_safe", fake_render)
    out = er.render_all_sections(EVERY)
    assert sorted(rendered) == sorted(["sys-a", "sys-b", "sys-l", "sys-s", "sys-i", "sys-m"])
    assert [s["system_id"] for s in out] == [
        "sys-a", "sys-b", "sys-l", "sys-s", "sys-i", "sys-m"]


def test_section_fan_out_with_only_unreadable_systems_is_empty(monkeypatch):
    render = Mock(side_effect=AssertionError("must not render a vendor REST system"))
    monkeypatch.setattr(er, "_render_system_sections_safe", render)
    assert er.render_all_sections([VENDOR, TULIP]) == []
    render.assert_not_called()


@pytest.fixture
def _fresh_read_cache(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PANEL_READ_TTL", "0")
    dashboard.invalidate_dashboard_read_cache()
    yield
    dashboard.invalidate_dashboard_read_cache()


def test_targeted_section_drill_on_unreadable_system_queries_nothing(
        monkeypatch, _fresh_read_cache):
    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: [SQL_A, VENDOR])
    render = Mock(side_effect=AssertionError("must not render a vendor REST system"))
    monkeypatch.setattr(dashboard.entity_render, "_render_system_sections_safe", render)

    assert asyncio.run(
        dashboard.dashboard_sections("all", "sys-v", "assets")) == {"sections": []}
    render.assert_not_called()


def test_connected_types_still_count_every_onboarded_system(monkeypatch):
    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: [
        {**SQL_A, "system_type": "MES"}, {**VENDOR, "system_type": "CMMS"}])
    monkeypatch.setattr(dashboard, "_cached_cards", lambda: [])
    assert dashboard.fetch_dashboard_data()["connected_types"] == ["CMMS", "MES"]


# --- Mixed coverage: one readable system and one REST system ----------------
# The gates used to count only readable
# systems, so one PostgreSQL + one vendor-API mapping the same metric took the
# single-authority path, which still sees both mappings and fails as ambiguous.
# The gates must count every covering system; the fan-out then reads only the
# readable ones.

def _mixed(monkeypatch):
    pairs = {"sys-a": frozenset({CONCEPT}), "sys-v": frozenset({CONCEPT})}
    monkeypatch.setattr(mms, "_covered_by_system", lambda query: pairs)
    monkeypatch.setattr(mms, "get_metric_layer", lambda: _Layer())
    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: [SQL_A, VENDOR])


def test_gate_counts_every_covering_system(monkeypatch):
    _mixed(monkeypatch)
    every = mms.systems_covering(METRIC.id, readable_only=False, systems=[SQL_A, VENDOR])
    assert [e["system_id"] for e in every] == ["sys-a", "sys-v"]
    card = type("Card", (), {"metric_refs": [METRIC.id], "id": "card-x"})()
    assert dashboard._multi_site_covering(card) is not None


def test_preview_gate_counts_every_covering_system(monkeypatch):
    import agents.metric_preview as mp
    _mixed(monkeypatch)
    assert mp._fan_out_systems(METRIC.id, _Layer()) == [SQL_A, VENDOR]


def test_mixed_fan_out_reads_only_the_readable_system(monkeypatch):
    from agents.metric_resolver import ResolvedMetric
    _mixed(monkeypatch)
    pinned = []

    def resolve(metric_id, *, authority):
        sid = authority.authoritative_system("any")
        pinned.append(sid)
        return ResolvedMetric(metric_id=metric_id, value=42.0, unit="percent",
                              label="Avg Metric", query_plan=None, system_id=sid)

    out = mms.resolve_metric_multi(METRIC.id, resolver=resolve, systems=[SQL_A, VENDOR])
    assert pinned == ["sys-a"]
    assert out["contributing"] == 1
    assert [s["system_id"] for s in out["sites"]] == ["sys-a"]
    assert out["aggregate"]["value"] == 42.0
