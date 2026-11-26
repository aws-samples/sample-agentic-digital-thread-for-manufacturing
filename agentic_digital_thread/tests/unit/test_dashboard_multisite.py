# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the 2A multi-site card fan-out (dashboard._build_card
consuming metric_multisite.resolve_metric_multi — the Wave-1B contract).

No AWS, no graph, no DB. The dashboard imports the fan-out module as a module
object (``from . import metric_multisite``), so monkeypatching attributes on
``agents.metric_multisite`` drives exactly what ``_build_card`` calls. The
catalog singleton's ``get_onboarded`` gates the fan-out (>=2 onboarded systems
required BEFORE any graph read).

Key disciplines proven here (the exact-key-set snapshot contract):
  * ``sites`` is CONDITIONAL — present only when >1 onboarded system covers the
    card's primary concept; a 1-system deployment emits NO ``sites`` key and the
    whole card payload is byte-identical to the single-authority build.
  * The fan-out gate performs ZERO graph reads for a single-system deployment.
  * Fan-out failures fall back to the single-authority path (fail-soft).
  * The gauge attaches per-site values ONLY to the primary (composite) metric.
"""
from __future__ import annotations

import json
from unittest.mock import Mock

import agents.dashboard as dashboard
import agents.metric_multisite as mms
import agents.metric_resolver as mr
from agents.metric_layer import Aggregation
from agents.metric_resolver import (
    AuthoritySource,
    Lineage,
    MappingProvenance,
    MetricResolverError,
    QueryPlan,
    ResolvedMetric,
)


# --- Canned shapes ------------------------------------------------------------

_TWO_SYSTEMS = [
    {"id": "sys-a", "name": "Site A MES", "plant": "Plant A",
     "system_type": "MES", "schema": "sa"},
    {"id": "sys-b", "name": "Site B MES", "plant": "Plant B",
     "system_type": "MES", "schema": "sb"},
]

_COVERING = [
    {"system_id": "sys-a", "name": "Site A MES", "site": "Plant A"},
    {"system_id": "sys-b", "name": "Site B MES", "site": "Plant B"},
]


def _multi_result(value=12, unit="units", label="Spare Parts On Hand",
                  method="sum", sites=None, contributing=2) -> dict:
    return {
        "aggregate": {"value": value, "unit": unit, "label": label,
                      "method": method},
        "sites": sites if sites is not None else [
            {"site": "Plant A", "system_id": "sys-a",
             "system_name": "Site A MES", "value": 5, "status": "ok"},
            {"site": "Plant B", "system_id": "sys-b",
             "system_name": "Site B MES", "value": 7, "status": "ok"},
        ],
        "contributing": contributing,
    }


def _single_resolved(metric_id="spare-parts-on-hand", value=18,
                     system_id="cmms") -> ResolvedMetric:
    plan = QueryPlan(system_id=system_id, schema="s", table="t", column="c",
                     aggregation=Aggregation.SUM)
    lineage = Lineage(
        metric_id=metric_id, kind="direct", concept="mro-inventory",
        authority=AuthoritySource(system_id=system_id, source="authority_rule"),
        mapping=MappingProvenance(table="t", column="c", role="measure",
                                  confidence=0.9, source="agent_proposed"),
        query_plan=plan, sql="SELECT SUM(c), COUNT(c) FROM s.t",
        executed_at="2026-07-15T00:00:00+00:00",
    )
    return ResolvedMetric(metric_id=metric_id, value=value, unit="units",
                          label=metric_id, query_plan=plan,
                          system_id=system_id, lineage=lineage)


def _gate_open(monkeypatch, covering=None):
    """Open the fan-out gate: 2 onboarded systems + a covering list."""
    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: _TWO_SYSTEMS)
    monkeypatch.setattr(
        mms, "systems_covering",
        lambda target, **kwargs: list(_COVERING if covering is None else covering))


# --- >1 covering system ⇒ conditional `sites` + aggregate as value ------------

def test_two_site_fanout_attaches_sites_and_aggregate(monkeypatch):
    _gate_open(monkeypatch)
    monkeypatch.setattr(mms, "resolve_metric_multi",
                        lambda mid, **k: _multi_result())

    card = dashboard._build_card("spare-parts-on-hand")
    metric = card["metrics"][0]

    assert metric["status"] == "ok"
    assert metric["value"] == 12                       # the combined aggregate
    assert metric["unit"] == "units"
    assert metric["system_id"] is None                 # no SINGLE system behind it
    assert metric["lineage"] is None
    assert [s["site"] for s in metric["sites"]] == ["Plant A", "Plant B"]
    assert [s["value"] for s in metric["sites"]] == [5, 7]
    assert all(s["status"] == "ok" for s in metric["sites"])
    # No single resolving system ⇒ no drill hint and no auto-derived context.
    assert "drill" not in card
    assert "context" not in card
    assert json.dumps(card)                            # JSON-safe payload


def test_fanout_aggregate_math_matches_1b_semantics(monkeypatch):
    # REAL metric_multisite.resolve_metric_multi (only its impure seams mocked):
    # spare-parts-on-hand is a SUM metric ⇒ per-site 5 + 7 combine to 12 with
    # per-site breakdown intact — proving _build_card consumes the genuine 1B
    # combination semantics, not a canned dict.
    _gate_open(monkeypatch)                            # patched systems_covering is
    #                                                    also what the REAL
    #                                                    resolve_metric_multi calls
    values = {"sys-a": 5, "sys-b": 7}

    def _pinned_resolve(metric_id, *, authority=None):
        sid = authority.authoritative_system("any")
        return _single_resolved(metric_id=metric_id, value=values[sid],
                                system_id=sid)

    monkeypatch.setattr(mr, "resolve_metric", _pinned_resolve)

    card = dashboard._build_card("spare-parts-on-hand")
    metric = card["metrics"][0]

    assert metric["value"] == 12                       # SUM across sites (1B)
    assert {(s["site"], s["value"]) for s in metric["sites"]} == {
        ("Plant A", 5), ("Plant B", 7)}
    assert all(s["status"] == "ok" for s in metric["sites"])


# --- Byte-identical single-system discipline -----------------------------------

def test_single_onboarded_system_skips_gate_and_graph_reads(monkeypatch):
    # <2 onboarded systems ⇒ the gate closes BEFORE any graph read:
    # systems_covering / resolve_metric_multi are never called and the payload
    # is byte-identical to the single-authority build.
    monkeypatch.setattr(dashboard.catalog, "get_onboarded",
                        lambda: [_TWO_SYSTEMS[0]])
    covering_spy = Mock(name="systems_covering")
    multi_spy = Mock(name="resolve_metric_multi")
    monkeypatch.setattr(mms, "systems_covering", covering_spy)
    monkeypatch.setattr(mms, "resolve_metric_multi", multi_spy)
    monkeypatch.setattr(mr, "resolve_metric", lambda *a, **k: _single_resolved())

    card = dashboard._build_card("spare-parts-on-hand")

    covering_spy.assert_not_called()
    multi_spy.assert_not_called()
    metric = card["metrics"][0]
    assert "sites" not in metric
    assert set(metric) == {"metric_id", "label", "value", "unit", "system_id",
                           "status", "lineage"}
    assert metric["value"] == 18
    assert metric["system_id"] == "cmms"
    assert card["drill"] == {"system_id": "cmms", "table": "t",
                             "entity_label": "Spare Parts On Hand"}


def test_one_covering_system_uses_single_path_no_sites(monkeypatch):
    # 2 onboarded but only ONE covers the concept ⇒ single-authority path,
    # regression-snapshot equal to a closed-gate build (byte-identical).
    monkeypatch.setattr(mr, "resolve_metric", lambda *a, **k: _single_resolved())
    multi_spy = Mock(name="resolve_metric_multi")
    monkeypatch.setattr(mms, "resolve_metric_multi", multi_spy)

    _gate_open(monkeypatch, covering=[_COVERING[0]])   # one covering system
    with_gate = dashboard._build_card("spare-parts-on-hand")

    monkeypatch.setattr(dashboard.catalog, "get_onboarded",
                        lambda: [_TWO_SYSTEMS[0]])      # gate closed entirely
    without_gate = dashboard._build_card("spare-parts-on-hand")

    multi_spy.assert_not_called()
    assert with_gate == without_gate                   # byte-identical payloads
    assert "sites" not in with_gate["metrics"][0]


# --- Fail-soft paths ------------------------------------------------------------

def test_multi_resolve_error_falls_back_to_single_authority(monkeypatch):
    _gate_open(monkeypatch)

    def _boom(mid, **k):
        raise MetricResolverError("no value on any covering system")

    monkeypatch.setattr(mms, "resolve_metric_multi", _boom)
    monkeypatch.setattr(mr, "resolve_metric", lambda *a, **k: _single_resolved())

    metric = dashboard._build_card("spare-parts-on-hand")["metrics"][0]
    assert metric["status"] == "ok"                    # single path took over
    assert metric["value"] == 18
    assert "sites" not in metric


def test_gate_failure_falls_back_to_single_authority(monkeypatch):
    # Any error in the coverage gate (e.g. graph/catalog hiccup) means the card
    # simply builds single-authority — enrichment never breaks a card.
    monkeypatch.setattr(dashboard.catalog, "get_onboarded", lambda: _TWO_SYSTEMS)

    def _boom(target, **kwargs):
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr(mms, "systems_covering", _boom)
    monkeypatch.setattr(mr, "resolve_metric", lambda *a, **k: _single_resolved())

    metric = dashboard._build_card("spare-parts-on-hand")["metrics"][0]
    assert metric["status"] == "ok"
    assert "sites" not in metric


# --- Gauge: per-site values ride ONLY on the primary (composite) metric ---------

def test_gauge_sites_only_on_primary_metric(monkeypatch):
    _gate_open(monkeypatch)
    monkeypatch.setattr(
        mms, "resolve_metric_multi",
        lambda mid, **k: _multi_result(value=71.1, unit="ratio", label=mid,
                                       method="mean"))

    card = dashboard._build_card("oee")

    assert [m["metric_id"] for m in card["metrics"]] == [
        "oee", "availability", "performance", "oee-quality"]
    assert "sites" in card["metrics"][0]               # composite carries sites
    for sub in card["metrics"][1:]:
        assert "sites" not in sub                      # sub-readings aggregate-level
        assert sub["value"] == 71.1
        assert sub["status"] == "ok"


# --- Honest aggregate gaps -------------------------------------------------------

def test_uncombinable_aggregate_classified_no_data(monkeypatch):
    # LATEST across systems has no honest combined value (1B method
    # 'latest-not-combined', aggregate value None) ⇒ status 'no-data', with the
    # per-site values still riding in `sites`.
    _gate_open(monkeypatch)
    monkeypatch.setattr(
        mms, "resolve_metric_multi",
        lambda mid, **k: _multi_result(value=None, method="latest-not-combined"))

    metric = dashboard._build_card("spare-parts-on-hand")["metrics"][0]
    assert metric["status"] == "no-data"
    assert metric["value"] is None
    assert len(metric["sites"]) == 2


# --- The fan-out stays inside the TTL-cached _build_cards path -------------------

def test_fanout_flows_through_build_cards_scalar_snapshot(monkeypatch):
    _gate_open(monkeypatch)
    monkeypatch.setattr(mms, "resolve_metric_multi",
                        lambda mid, **k: _multi_result(label=mid))

    cards = dashboard._build_cards()

    by_id = {c["card_id"]: c for c in cards}
    # Every scalar card fanned out (all seed refs share the mocked coverage).
    assert "sites" in by_id["spare-parts-on-hand"]["metrics"][0]
    assert "sites" in by_id["oee"]["metrics"][0]
    # By-dimension cards stay descriptors — never scalar/fan-out resolved.
    assert "scrap-by-line" not in by_id
    assert "throughput-by-line" not in by_id
    assert json.dumps(cards)
