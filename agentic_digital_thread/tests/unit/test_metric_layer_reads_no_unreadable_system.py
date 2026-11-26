# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The metric layer composes no query for a system it has no reader for.

A system on a vendor REST connection (the REST vendor registry gives it no
``structured_reader``) is read only by its own agent at chat time. The
dashboard fan-out already leaves such systems out
(``test_dashboard_skips_unreadable_systems``), but a metric whose authority
lands on one (a single authority, or one site of a per-site partition) still
composed SQL for it. The catalog schema of such a system is empty and its
table names are vendor labels, so ``validate_identifier`` raised a bare
``ValueError``: the turn notice showed the site as ``UNAVAILABLE (ValueError)``,
a derived metric's stored read did not fall through, and the dashboard's
grouped route raised out of its per-site handler. With valid names the SQL
went to ``query_system``, which has no reader for these types.

Now the resolver refuses the system up front with a named
:class:`metric_resolver.UnreadableSystemError` (a ``MetricResolverError``), so
every caller's existing register applies and nothing is sent to the source.
Readable systems are unchanged. Fully offline; neutral ``widget``/``gadget``
vocabulary only.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

import agents.catalog as catalog_mod
import agents.heatmap_resolver as hr
import agents.metric_layer as ml
import agents.metric_resolver as mr
import agents.metric_turn_notice as mtn
import agents.metric_turn_sites as mts
import agents.rest_vendor_registry as reg
from agents import connection_types as ct
from agents.metric_layer import (Aggregation, CodeMetricLayer, DerivedMetric,
                                 DirectMetric, Formula, FormulaOp, MetricRef, Scalar)
from agents.models import ResolveMetricByDimensionRequest

NEWCOMER = "Newcomer (REST)"
READ_SITE, VENDOR_SITE = "wsys-read", "wsys-vendor"

# The vendor system's map step: vendor table labels (spaces) on an empty
# catalog schema, the shape a REST-family discovery produces.
VENDOR_BINDINGS = {
    "widget-score": ("Widget Summary", "score"),
    "widget-a": ("Widget Summary", "part_a"),
    "widget-b": ("Widget Summary", "part_b"),
    "widget-line": ("Widget Summary", "line"),
}
READ_BINDINGS = {
    "widget-score": ("widget_summary", "score"),
    "widget-a": ("widget_summary", "part_a"),
    "widget-b": ("widget_summary", "part_b"),
    "widget-line": ("widget_summary", "line"),
}
METRICS = [
    DirectMetric(id="widget-score-stored", concept="widget-score",
                 aggregation=Aggregation.AVG, label="Widget Score (Stored)", unit="ratio"),
    DirectMetric(id="widget-a", concept="widget-a", aggregation=Aggregation.AVG,
                 label="Widget A", unit="ratio"),
    DirectMetric(id="widget-b", concept="widget-b", aggregation=Aggregation.AVG,
                 label="Widget B", unit="ratio"),
    DerivedMetric(id="widget-score", label="Widget Score", unit="ratio",
                  formula=Formula(FormulaOp.SCALE, (
                      Formula(FormulaOp.PRODUCT, (MetricRef("widget-a"), MetricRef("widget-b"))),
                      Scalar(100.0))),
                  prefer_stored="widget-score-stored"),
]


def _system(sid, plant, conn, schema):
    return {"id": sid, "name": sid, "plant": plant, "status": "onboarded",
            "connection_type": conn, "schema": schema}


class _Backends:
    """Graph + backend seams. Records every backend call."""

    def __init__(self, bindings):
        self.bindings = bindings
        self.pg, self.system = [], []

    def neptune(self, cypher, params=None):
        params = params or {}
        if "AuthorityRule {concept" in cypher:
            concept = params.get("concept")
            return [{"sid": sid} for sid, b in self.bindings.items() if concept in b]
        sid, concept = params.get("sid"), params.get("concept")
        if sid and concept and "MAPS_TO" in cypher:
            b = self.bindings.get(sid, {}).get(concept)
            return [{"table": b[0], "column": b[1]}] if b else []
        return []

    def query_pg(self, schema, sql, params=None):
        self.pg.append((schema, sql))
        if "GROUP BY" in sql:
            return json.dumps({"columns": ["g", "v"], "rows": [["line1", 0.9], ["line2", 0.8]]})
        return json.dumps({"columns": ["v", "n"], "rows": [[0.75, 4]]})

    def query_system(self, system_id, sql, params=None):
        self.system.append((system_id, sql))
        return {"columns": ["v", "n"], "rows": [[0.5, 4]]}


@pytest.fixture
def wire(monkeypatch):
    layer = CodeMetricLayer(METRICS)
    monkeypatch.setattr(mr, "get_metric_layer", lambda: layer)
    monkeypatch.setattr(ml, "get_metric_layer", lambda: layer)
    monkeypatch.setenv("METRIC_TURN_NOTICE", "on")

    def _wire(systems, bindings):
        backends = _Backends(bindings)
        monkeypatch.setattr(catalog_mod.catalog, "_systems", [dict(s) for s in systems])
        monkeypatch.setattr(mr, "neptune_query", backends.neptune)
        monkeypatch.setattr(mr, "query_pg", backends.query_pg)
        monkeypatch.setattr(mr, "query_system", backends.query_system)
        mtn.invalidate()
        return backends

    yield _wire
    mtn.invalidate()


@pytest.fixture
def newcomer_vendor(monkeypatch):
    """A vendor registered with no ``structured_reader``: the rule comes from
    the registry, not from a list of connection types."""
    template = next(a for a in reg.REST_VENDOR_REGISTRY if a.name == "tulip")
    newcomer = dataclasses.replace(
        template, name="newcomer",
        detect=lambda connection_type, base_url, entry=None: connection_type == NEWCOMER)
    monkeypatch.setattr(reg, "REST_VENDOR_REGISTRY", [newcomer, *reg.REST_VENDOR_REGISTRY])


UNREADABLE_TYPES = [ct.TULIP, ct.REST_API, NEWCOMER]


# --------------------------------------------------------------------------- #
# Scalar resolve
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("conn", UNREADABLE_TYPES)
def test_a_direct_metric_on_an_unreadable_system_is_refused_by_name(
        wire, newcomer_vendor, conn):
    backends = wire([_system(VENDOR_SITE, "Site V", conn, "")],
                    {VENDOR_SITE: VENDOR_BINDINGS})
    with pytest.raises(mr.UnreadableSystemError) as info:
        mr.resolve_metric("widget-a", authority=mr.PinnedAuthority(VENDOR_SITE))
    assert isinstance(info.value, mr.MetricResolverError)
    assert info.value.system_id == VENDOR_SITE and info.value.connection_type == conn
    assert f"system {VENDOR_SITE!r} ({conn})" in str(info.value)
    assert "no reader" in str(info.value)
    assert backends.pg == [] and backends.system == []


def test_valid_names_on_a_differently_named_schema_send_nothing_to_the_source(
        wire, newcomer_vendor):
    """Not an identifier problem: plain names on a non-empty schema are
    refused the same way, and the source gets no composed query."""
    bindings = {"gadget-src": {"widget-a": ("gadget_records", "gadget_value")}}
    backends = wire([_system("gadget-src", "Plant G", NEWCOMER, "vendor_ns")], bindings)
    with pytest.raises(mr.UnreadableSystemError):
        mr.resolve_metric("widget-a", authority=mr.PinnedAuthority("gadget-src"))
    assert backends.system == [] and backends.pg == []


def test_a_derived_metric_on_an_unreadable_system_fails_inside_the_error_contract(wire):
    """The stored read's refusal falls through to the formula, whose input is
    refused the same way: a MetricResolverError naming the system, never a
    bare ValueError that skips the derived metric's own handling."""
    backends = wire([_system(VENDOR_SITE, "Site V", ct.TULIP, "")],
                    {VENDOR_SITE: VENDOR_BINDINGS})
    with pytest.raises(mr.MetricResolverError) as info:
        mr.resolve_metric("widget-score", authority=mr.PinnedAuthority(VENDOR_SITE))
    assert "derived metric unavailable: input 'widget-a' unresolvable" in str(info.value)
    assert isinstance(info.value.__cause__, mr.UnreadableSystemError)
    assert backends.system == []


@pytest.mark.parametrize("conn", [ct.POSTGRESQL, ct.ATHENA, ct.SITEWISE_DIRECT,
                                  ct.SITEWISE_I3X, ct.MCP_SERVER, None])
def test_readable_types_pass_the_gate(wire, conn):
    system = _system(READ_SITE, "Site R", conn, "widgets")
    if conn is None:
        system.pop("connection_type")
    wire([system], {})
    mr.require_metric_reader(READ_SITE)  # no raise


def test_an_uncataloged_system_keeps_the_default_path(wire):
    wire([], {})
    mr.require_metric_reader("not-in-catalog")  # PostgreSQL default, as before


def test_a_readable_system_resolves_exactly_as_before(wire):
    backends = wire([_system(READ_SITE, "Site R", ct.POSTGRESQL, "widgets")],
                    {READ_SITE: READ_BINDINGS})
    resolved = mr.resolve_metric("widget-a", authority=mr.PinnedAuthority(READ_SITE))
    assert (resolved.value, resolved.sample_count) == (0.75, 4)
    assert backends.pg == [("widgets", "SELECT AVG(part_a), COUNT(part_a) "
                                       "FROM widgets.widget_summary")]


# --------------------------------------------------------------------------- #
# The turn notice: a per-site partition with one unreadable site
# --------------------------------------------------------------------------- #
def _partition(wire, vendor_conn=ct.TULIP):
    return wire([_system(READ_SITE, "Site R", ct.POSTGRESQL, "widgets"),
                 _system(VENDOR_SITE, "Site V", vendor_conn, "")],
                {READ_SITE: READ_BINDINGS, VENDOR_SITE: VENDOR_BINDINGS})


def test_the_unreadable_site_is_withheld_with_its_reason_not_an_exception_class(wire):
    backends = _partition(wire)
    result = mtn._resolve_one("widget-score")
    assert result["outcome"] == mts.SITES_OUTCOME
    read, vendor = result["sites"]
    assert (read["site"], read["outcome"], read["value"]) == ("Site R", "computed", 0.75)
    assert (vendor["site"], vendor["outcome"], vendor["status"]) == (
        "Site V", "withheld", "unavailable")
    assert "reason" not in vendor
    assert f"system {VENDOR_SITE!r} ({ct.TULIP})" in vendor["detail"]
    assert all(sid != VENDOR_SITE for sid, _sql in backends.system)


def test_the_rendered_block_names_why_the_site_has_no_value(wire):
    _partition(wire)
    notice = mtn.render_metric_layer_notice(
        mtn.sanitize_results([{**mtn._resolve_one("widget-score"),
                               "metric_id": "widget-score", "term": "widget score",
                               "label": "Widget Score"}]))
    line = next(x for x in notice.splitlines() if x.startswith("  Site V"))
    assert "WITHHELD — status 'unavailable'" in line
    assert "UNAVAILABLE (ValueError)" not in notice
    assert f"({ct.TULIP})" in line


def test_a_direct_metric_per_site_withholds_the_unreadable_site(wire):
    _partition(wire, vendor_conn=ct.REST_API)
    sites = mtn._resolve_one("widget-a")["sites"]
    assert [(s["site"], s["outcome"]) for s in sites] == [
        ("Site R", "computed"), ("Site V", "withheld")]
    assert f"({ct.REST_API})" in sites[1]["detail"]


# --------------------------------------------------------------------------- #
# The dashboard's grouped route and the heatmap
# --------------------------------------------------------------------------- #
def test_the_grouped_route_answers_the_readable_site_and_marks_the_other(wire):
    backends = _partition(wire)
    out = mr._resolve_by_dimension_payload(ResolveMetricByDimensionRequest(
        metric_id="widget-a", dimension_concept="widget-line"))
    assert out["status"] == "ok"
    assert [(s["site"], s["status"]) for s in out["sites"]] == [
        ("Site R", "ok"), ("Site V", "error")]
    assert [r[0] for r in out["rows"]] == ["Site R · line1", "Site R · line2"]
    assert backends.system == []


def test_the_grouped_route_names_why_the_unreadable_site_has_no_bars(wire):
    _partition(wire)
    out = mr._resolve_by_dimension_payload(ResolveMetricByDimensionRequest(
        metric_id="widget-a", dimension_concept="widget-line"))
    vendor = next(s for s in out["sites"] if s["site"] == "Site V")
    assert vendor["status"] == "error"
    assert VENDOR_SITE in vendor["detail"] and ct.TULIP in vendor["detail"]
    read = next(s for s in out["sites"] if s["site"] == "Site R")
    assert "detail" not in read


def test_a_grouped_route_with_no_readable_site_names_every_unreadable_system(wire):
    other = "wsys-vendor-2"
    backends = wire([_system(VENDOR_SITE, "Site V", ct.TULIP, ""),
                     _system(other, "Site W", ct.REST_API, "")],
                    {VENDOR_SITE: VENDOR_BINDINGS, other: VENDOR_BINDINGS})
    out = mr._resolve_by_dimension_payload(ResolveMetricByDimensionRequest(
        metric_id="widget-a", dimension_concept="widget-line"))
    assert out["status"] == "error" and out.get("structural") is True
    for sid, conn in ((VENDOR_SITE, ct.TULIP), (other, ct.REST_API)):
        assert sid in out["error"] and conn in out["error"]
    assert "authority" not in out["error"].lower()
    assert backends.system == [] and backends.pg == []


def test_a_grouped_resolve_pinned_to_an_unreadable_system_is_refused(wire):
    import agents.metric_dimension as md
    backends = _partition(wire)
    with pytest.raises(mr.UnreadableSystemError):
        md.resolve_metric_by_dimension("widget-a", "widget-line",
                                       authority=mr.PinnedAuthority(VENDOR_SITE))
    assert backends.system == []


def test_a_heatmap_on_an_unreadable_system_is_refused(wire):
    backends = _partition(wire)

    def query(cypher, params=None):
        if params is None:  # the coverage read: the metric's concept is covered
            return [{"id": "widget-a"}]
        return backends.neptune(cypher, params)

    with pytest.raises(mr.UnreadableSystemError):
        hr.resolve_heatmap("widget-a", "widget-line", "widget-line",
                           authority=mr.PinnedAuthority(VENDOR_SITE), query=query)
    assert backends.system == []
