# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure in-process unit tests for the multi-site resolution engine (Task 1B).

No AWS, no graph, no DB — every impure seam (``neptune_query``, the per-system
``resolve_metric``, the catalog read) is injected through the module's default
args (``query=`` / ``resolver=`` / ``systems=`` / ``metric_layer=``), the same
convention as ``coverage.py``. All ids/names below are fabricated generics —
no dataset/system/plant literals (owner directive).
"""
from __future__ import annotations

import pytest

import agents.metric_multisite as mms
import agents.metric_resolver as mr
from agents.metric_layer import (
    Aggregation,
    ConceptRef,
    DirectMetric,
    DerivedMetric,
    Formula,
    FormulaOp,
    MetricLayer,
)
from agents.metric_resolver import MetricResolverError, ResolvedMetric


# --- Fabricated fixtures (generic ids only) ---

CONCEPT_A = "concept-a"
CONCEPT_B = "concept-b"

SUM_METRIC = DirectMetric(id="m-sum", concept=CONCEPT_A,
                          aggregation=Aggregation.SUM, label="Sum Metric", unit="count")
AVG_METRIC = DirectMetric(id="m-avg", concept=CONCEPT_A,
                          aggregation=Aggregation.AVG, label="Avg Metric", unit="percent")
MAX_METRIC = DirectMetric(id="m-max", concept=CONCEPT_A,
                          aggregation=Aggregation.MAX, label="Max Metric", unit="count")
MIN_METRIC = DirectMetric(id="m-min", concept=CONCEPT_A,
                          aggregation=Aggregation.MIN, label="Min Metric", unit="count")
LATEST_METRIC = DirectMetric(id="m-latest", concept=CONCEPT_A,
                             aggregation=Aggregation.LATEST, label="Latest Metric", unit="state")
DERIVED_METRIC = DerivedMetric(
    id="m-derived", label="Derived Metric", unit="percent",
    formula=Formula(op=FormulaOp.RATIO,
                    inputs=(ConceptRef(CONCEPT_A), ConceptRef(CONCEPT_B))),
)


class _FakeLayer(MetricLayer):
    def __init__(self, metrics):
        self._metrics = {m.id: m for m in metrics}

    def get_metric(self, metric_id):
        return self._metrics[metric_id]          # KeyError on miss (contract)

    def list_metrics(self):
        return list(self._metrics.values())


LAYER = _FakeLayer([SUM_METRIC, AVG_METRIC, MAX_METRIC, MIN_METRIC,
                    LATEST_METRIC, DERIVED_METRIC])

# 3 onboarded systems / 2 sites + 1 wildcard-plant system.
SYS_1 = {"id": "sys-1", "name": "System One", "plant": "site-x", "status": "onboarded"}
SYS_2 = {"id": "sys-2", "name": "System Two", "plant": "site-y", "status": "onboarded"}
SYS_3 = {"id": "sys-3", "name": "System Three", "plant": "site-x", "status": "onboarded"}
SYS_WILD = {"id": "sys-w", "name": "Wildcard System", "plant": "all", "status": "onboarded"}


def test_site_of_wildcard_spellings_are_unsited():
    """'all' / 'All Sites' / 'All Plants' (any case/whitespace) are wildcard
    spellings seen in real catalog entries — never a literal site label (the
    dashboard was rendering 'All Sites' chips next to real sites)."""
    from agents.metric_multisite import site_of
    for plant in ("all", "All", "ALL", "All Sites", "all sites", " All Plants ", "", None):
        assert site_of({"plant": plant}) == "", repr(plant)
    assert site_of({"plant": "Milwaukee"}) == "Milwaukee"
SYSTEMS = [SYS_1, SYS_2, SYS_3]


def _pairs_query(pairs):
    """Fake ``neptune_query`` returning (system, concept) coverage pairs."""
    rows = [{"system_id": sid, "concept_id": cid} for sid, cid in pairs]

    def query(cypher, params=None):  # noqa: ARG001 - signature parity
        return rows
    return query


ALL_COVER_A = _pairs_query([("sys-1", CONCEPT_A), ("sys-2", CONCEPT_A),
                            ("sys-3", CONCEPT_A)])


def _resolver(values_by_system, errors_by_system=None):
    """Fake ``resolve_metric``: per-system canned value or raised error.
    Asserts authority pinning by reading the pinned id off the authority."""
    errors_by_system = errors_by_system or {}

    def resolve(metric_id, *, authority):
        sid = authority.authoritative_system("any-concept")
        if sid in errors_by_system:
            raise errors_by_system[sid]
        return ResolvedMetric(metric_id=metric_id, value=values_by_system[sid],
                              unit="u", label="L", query_plan=None, system_id=sid)
    return resolve


def _multi(metric_id, *, values=None, errors=None, query=ALL_COVER_A,
           systems=SYSTEMS, layer=LAYER):
    return mms.resolve_metric_multi(
        metric_id, resolver=_resolver(values or {}, errors),
        query=query, metric_layer=layer, systems=systems)


# --- systems_covering ---

class TestSystemsCovering:
    def test_concept_target_membership(self):
        out = mms.systems_covering(CONCEPT_A, query=ALL_COVER_A,
                                   metric_layer=LAYER, systems=SYSTEMS)
        assert [e["system_id"] for e in out] == ["sys-1", "sys-2", "sys-3"]
        assert out[0] == {"system_id": "sys-1", "name": "System One", "site": "site-x"}

    def test_metric_target_uses_satisfiability(self):
        # Derived RATIO(A, B): only the system mapping BOTH concepts covers it.
        query = _pairs_query([("sys-1", CONCEPT_A), ("sys-1", CONCEPT_B),
                              ("sys-2", CONCEPT_A)])
        out = mms.systems_covering("m-derived", query=query,
                                   metric_layer=LAYER, systems=SYSTEMS)
        assert [e["system_id"] for e in out] == ["sys-1"]

    def test_wildcard_plant_maps_to_empty_site(self):
        query = _pairs_query([("sys-w", CONCEPT_A)])
        out = mms.systems_covering(CONCEPT_A, query=query,
                                   metric_layer=LAYER, systems=[SYS_WILD])
        assert out == [{"system_id": "sys-w", "name": "Wildcard System", "site": ""}]

    def test_non_onboarded_or_unmapped_systems_excluded(self):
        query = _pairs_query([("sys-1", CONCEPT_A), ("sys-ghost", CONCEPT_A)])
        out = mms.systems_covering(CONCEPT_A, query=query,
                                   metric_layer=LAYER, systems=SYSTEMS)
        # sys-ghost not in the onboarded list; sys-2/3 map nothing → excluded.
        assert [e["system_id"] for e in out] == ["sys-1"]

    def test_empty_graph_read_yields_empty(self):
        out = mms.systems_covering(CONCEPT_A, query=lambda *a, **k: [],
                                   metric_layer=LAYER, systems=SYSTEMS)
        assert out == []


# --- resolve_metric_multi: combination semantics ---

class TestCombination:
    def test_sum_over_three_systems_two_sites(self):
        out = _multi("m-sum", values={"sys-1": 10, "sys-2": 20, "sys-3": 5})
        assert out["aggregate"] == {"value": 35, "unit": "count",
                                    "label": "Sum Metric", "method": "sum"}
        assert out["contributing"] == 3
        assert {(s["site"], s["system_id"], s["value"], s["status"]) for s in out["sites"]} == {
            ("site-x", "sys-1", 10, "ok"),
            ("site-x", "sys-3", 5, "ok"),
            ("site-y", "sys-2", 20, "ok"),
        }

    def test_avg_is_simple_mean(self):
        out = _multi("m-avg", values={"sys-1": 90.0, "sys-2": 60.0, "sys-3": 30.0})
        assert out["aggregate"]["value"] == pytest.approx(60.0)
        assert out["aggregate"]["method"] == "mean"

    def test_max_and_min(self):
        vals = {"sys-1": 3, "sys-2": 9, "sys-3": 6}
        assert _multi("m-max", values=vals)["aggregate"] == {
            "value": 9, "unit": "count", "label": "Max Metric", "method": "max"}
        assert _multi("m-min", values=vals)["aggregate"] == {
            "value": 3, "unit": "count", "label": "Min Metric", "method": "min"}

    def test_latest_skips_aggregate_with_reason(self):
        out = _multi("m-latest", values={"sys-1": 1, "sys-2": 2, "sys-3": 3})
        assert out["aggregate"]["value"] is None
        assert out["aggregate"]["method"] == "latest-not-combined"
        # Per-site values still ride in sites.
        assert out["contributing"] == 3
        assert all(s["status"] == "ok" for s in out["sites"])

    def test_derived_combines_per_site_results_with_mean(self):
        query = _pairs_query([(s, c) for s in ("sys-1", "sys-2")
                              for c in (CONCEPT_A, CONCEPT_B)])
        out = _multi("m-derived", values={"sys-1": 80.0, "sys-2": 60.0}, query=query)
        assert out["aggregate"]["value"] == pytest.approx(70.0)
        assert out["aggregate"]["method"] == "mean"
        assert out["aggregate"]["unit"] == "percent"

    def test_sites_sorted_by_site_then_name(self):
        out = _multi("m-sum", values={"sys-1": 1, "sys-2": 1, "sys-3": 1})
        assert [(s["site"], s["system_name"]) for s in out["sites"]] == [
            ("site-x", "System One"), ("site-x", "System Three"),
            ("site-y", "System Two")]


# --- resolve_metric_multi: fail-soft / fail-loud ---

class TestFailure:
    def test_skip_on_error_leaves_remaining_sites(self):
        out = _multi("m-sum", values={"sys-1": 10, "sys-3": 5},
                     errors={"sys-2": MetricResolverError("boundary")})
        assert out["aggregate"]["value"] == 15
        assert out["contributing"] == 2
        errored = [s for s in out["sites"] if s["status"] == "error"]
        assert [(s["system_id"], s["value"]) for s in errored] == [("sys-2", None)]

    def test_unexpected_exception_is_skipped_not_propagated(self):
        out = _multi("m-sum", values={"sys-1": 10, "sys-3": 5},
                     errors={"sys-2": RuntimeError("boom")})
        assert out["aggregate"]["value"] == 15
        assert [s["status"] for s in out["sites"]
                if s["system_id"] == "sys-2"] == ["error"]

    def test_none_value_is_no_data_and_excluded(self):
        out = _multi("m-avg", values={"sys-1": 50.0, "sys-2": None, "sys-3": 100.0})
        assert out["aggregate"]["value"] == pytest.approx(75.0)
        assert out["contributing"] == 2
        assert [s["status"] for s in out["sites"]
                if s["system_id"] == "sys-2"] == ["no-data"]

    def test_wildcard_plant_contributes_with_empty_site(self):
        query = _pairs_query([("sys-1", CONCEPT_A), ("sys-w", CONCEPT_A)])
        out = mms.resolve_metric_multi(
            "m-sum", resolver=_resolver({"sys-1": 10, "sys-w": 7}),
            query=query, metric_layer=LAYER, systems=[SYS_1, SYS_WILD])
        assert out["aggregate"]["value"] == 17
        wild = next(s for s in out["sites"] if s["system_id"] == "sys-w")
        assert wild["site"] == ""
        assert wild["status"] == "ok"
        # Empty site sorts first (site "" < "site-x").
        assert out["sites"][0]["system_id"] == "sys-w"

    def test_zero_covering_systems_raises(self):
        with pytest.raises(MetricResolverError, match="no onboarded system covers"):
            _multi("m-sum", values={}, query=lambda *a, **k: [])

    def test_all_systems_failing_raises(self):
        errs = {sid: MetricResolverError("down") for sid in ("sys-1", "sys-2", "sys-3")}
        with pytest.raises(MetricResolverError, match="resolved no value"):
            _multi("m-sum", values={}, errors=errs)

    def test_unknown_metric_id_raises(self):
        with pytest.raises(MetricResolverError, match="unknown metric id"):
            _multi("m-nope", values={})


# --- authority pinning contract ---

def test_resolver_receives_pinned_authority_per_system():
    seen: list[str] = []

    def resolve(metric_id, *, authority):  # noqa: ARG001
        sid = authority.authoritative_system("anything")
        seen.append(sid)
        return ResolvedMetric(metric_id=metric_id, value=1, unit="u", label="L",
                              query_plan=None, system_id=sid)

    mms.resolve_metric_multi("m-sum", resolver=resolve, query=ALL_COVER_A,
                             metric_layer=LAYER, systems=SYSTEMS)
    assert seen == ["sys-1", "sys-2", "sys-3"]


# --- ADR-0075: an unbound predicate is NOT no-data ---

def _predicate_resolver(values_by_system, unresolved):
    def resolve(metric_id, *, authority):
        sid = authority.authoritative_system("any-concept")
        if sid in unresolved:
            return ResolvedMetric(
                metric_id=metric_id, value=None, unit="u", label="L",
                query_plan=None, system_id=sid, status="unresolved_predicate",
                detail="predicate concept 'concept-b' has no field bound")
        return ResolvedMetric(metric_id=metric_id, value=values_by_system[sid],
                              unit="u", label="L", query_plan=None, system_id=sid)
    return resolve


def test_unresolved_predicate_site_is_distinct_from_no_data():
    out = mms.resolve_metric_multi(
        "m-sum", resolver=_predicate_resolver({"sys-1": 10, "sys-3": 5}, {"sys-2"}),
        query=ALL_COVER_A, metric_layer=LAYER, systems=SYSTEMS)
    assert out["aggregate"]["value"] == 15 and out["contributing"] == 2
    site = next(s for s in out["sites"] if s["system_id"] == "sys-2")
    assert site["status"] == "unresolved-predicate" and site["value"] is None


def test_all_sites_unresolved_predicate_is_a_withheld_answer_not_no_data():
    """Every plant was read and withheld the value: an answer in its own
    register (the card names the reason), never a raise that sends the caller
    to the single-authority path (which cannot start on a per-plant partition)."""
    out = mms.resolve_metric_multi(
        "m-sum", resolver=_predicate_resolver({}, {"sys-1", "sys-2", "sys-3"}),
        query=ALL_COVER_A, metric_layer=LAYER, systems=SYSTEMS)
    assert out["aggregate"]["value"] is None and out["contributing"] == 0
    assert {s["status"] for s in out["sites"]} == {"unresolved-predicate"}


# --- ADR-0076: an ambiguous binding is NOT no-data (and nothing was read) ---

def _ambiguous_resolver(values_by_system, ambiguous):
    def resolve(metric_id, *, authority):
        sid = authority.authoritative_system("any-concept")
        if sid in ambiguous:
            return ResolvedMetric(
                metric_id=metric_id, value=None, unit="u", label="L",
                query_plan=None, system_id=sid, status=mr.AMBIGUOUS_BINDING_STATUS,
                detail="concept 'concept-a' is bound to 2 distinct columns")
        return ResolvedMetric(metric_id=metric_id, value=values_by_system[sid],
                              unit="u", label="L", query_plan=None, system_id=sid)
    return resolve


def test_ambiguous_binding_site_is_distinct_from_no_data():
    out = mms.resolve_metric_multi(
        "m-sum", resolver=_ambiguous_resolver({"sys-1": 10, "sys-3": 5}, {"sys-2"}),
        query=ALL_COVER_A, metric_layer=LAYER, systems=SYSTEMS)
    assert out["aggregate"]["value"] == 15 and out["contributing"] == 2
    site = next(s for s in out["sites"] if s["system_id"] == "sys-2")
    assert site["status"] == "ambiguous-binding" and site["value"] is None
    assert site["detail"] == "concept 'concept-a' is bound to 2 distinct columns"
    assert "ambiguous-binding" in mms._READ_SITE_STATUSES


def test_all_sites_ambiguous_is_a_withheld_answer_not_no_data():
    out = mms.resolve_metric_multi(
        "m-sum", resolver=_ambiguous_resolver({}, {"sys-1", "sys-2", "sys-3"}),
        query=ALL_COVER_A, metric_layer=LAYER, systems=SYSTEMS)
    assert out["aggregate"]["value"] is None and out["contributing"] == 0
    assert {s["status"] for s in out["sites"]} == {"ambiguous-binding"}
