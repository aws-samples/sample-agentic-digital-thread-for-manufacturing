# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Honest no-data must not cost a SECOND full resolve pass (P0-C).

Incident evidence (2026-08-24 Management full-e2e): one app task at cpu=512
pinned ~99.9-100% CPU for ~45 minutes with ALB averages of 3-6 s. One
contributor to that amplification is the multi-site fan-out's "fail loud on
zero contributors" rule: an environment where every covering system honestly
returns NO ROWS raised ``MetricResolverError``, which
``dashboard._build_card_metric_multi`` caught and answered by re-running the
WHOLE single-authority resolve — every card, every TTL rebuild — and logging a
full ``exc_info`` traceback for a completely EXPECTED state.

The honest-data invariant (ADR-0045) already names this state: ``value None``
with no samples is ``"no-data"``, distinct from ``unavailable``. So a fan-out in
which at least one covering system was READ SUCCESSFULLY and reported no rows is
a no-data ANSWER, not a failure. Raising stays reserved for the genuinely
unanswerable cases: no coverage at all, or every covering system errored.

Pins here (all offline; every impure seam injected, generic ids only):
1. ALL covering systems no-data ⇒ the existing aggregate shape with
   ``value None``, ``contributing 0``, site statuses/unit/label preserved, and a
   ``method`` from the EXISTING vocabulary.
2. MIXED no-data + error ⇒ no-data aggregate (at least one source was read).
3. ALL covering systems errored ⇒ still raises (nothing was read).
4. No coverage at all ⇒ still raises (unchanged).
5. ``dashboard._build_card_metric_multi`` consumes the no-data aggregate
   DIRECTLY: no second resolve, and no traceback in the log for the expected
   state (unexpected exceptions keep their traceback).
"""
from __future__ import annotations

import logging

import pytest

import agents.dashboard as dash
import agents.metric_multisite as mms
import agents.metric_resolver as mr
from agents.metric_layer import (
    Aggregation,
    ConceptRef,
    DerivedMetric,
    DirectMetric,
    Formula,
    FormulaOp,
    MetricLayer,
)
from agents.metric_resolver import MetricResolverError, ResolvedMetric

CONCEPT_A = "concept-a"
CONCEPT_B = "concept-b"

SUM_METRIC = DirectMetric(id="m-sum", concept=CONCEPT_A, aggregation=Aggregation.SUM,
                          label="Sum Metric", unit="count")
AVG_METRIC = DirectMetric(id="m-avg", concept=CONCEPT_A, aggregation=Aggregation.AVG,
                          label="Avg Metric", unit="percent")
MAX_METRIC = DirectMetric(id="m-max", concept=CONCEPT_A, aggregation=Aggregation.MAX,
                          label="Max Metric", unit="count")
MIN_METRIC = DirectMetric(id="m-min", concept=CONCEPT_A, aggregation=Aggregation.MIN,
                          label="Min Metric", unit="count")
LATEST_METRIC = DirectMetric(id="m-latest", concept=CONCEPT_A,
                             aggregation=Aggregation.LATEST, label="Latest Metric",
                             unit="state")
DERIVED_METRIC = DerivedMetric(
    id="m-derived", label="Derived Metric", unit="percent",
    formula=Formula(op=FormulaOp.RATIO,
                    inputs=(ConceptRef(CONCEPT_A), ConceptRef(CONCEPT_B))),
)

# The EXISTING method vocabulary — a no-data aggregate must reuse it, never
# invent a new token the frontend/card layer has never seen.
EXISTING_METHODS = {"sum", "mean", "max", "min", "latest-not-combined"}


class _FakeLayer(MetricLayer):
    def __init__(self, metrics):
        self._metrics = {m.id: m for m in metrics}

    def get_metric(self, metric_id):
        return self._metrics[metric_id]

    def list_metrics(self):
        return list(self._metrics.values())


LAYER = _FakeLayer([SUM_METRIC, AVG_METRIC, MAX_METRIC, MIN_METRIC,
                    LATEST_METRIC, DERIVED_METRIC])

SYS_1 = {"id": "sys-1", "name": "System One", "plant": "site-x", "status": "onboarded"}
SYS_2 = {"id": "sys-2", "name": "System Two", "plant": "site-y", "status": "onboarded"}
SYSTEMS = [SYS_1, SYS_2]


def _query_all_cover(cypher):
    """Graph seam: both systems map a field to both concepts."""
    return [{"system_id": s["id"], "concept_id": c}
            for s in SYSTEMS for c in (CONCEPT_A, CONCEPT_B)]


def _no_data_resolver(metric_id, *, authority):
    """Every covering system is read successfully and honestly has no rows."""
    sid = authority.authoritative_system("any-concept")
    return ResolvedMetric(metric_id=metric_id, value=None, unit="count",
                          label="L", query_plan=None, system_id=sid,
                          sample_count=0)


def _error_resolver(metric_id, *, authority):  # noqa: ARG001
    raise MetricResolverError("boundary failure")


def _multi(metric_id, resolver):
    return mms.resolve_metric_multi(metric_id, resolver=resolver,
                                    query=_query_all_cover, metric_layer=LAYER,
                                    systems=SYSTEMS)


class TestAllNoDataIsAnHonestAggregate:
    def test_shape_is_preserved_with_value_none_and_zero_contributors(self):
        out = _multi("m-sum", _no_data_resolver)
        assert out["contributing"] == 0
        assert out["aggregate"]["value"] is None
        assert out["aggregate"]["unit"] == "count"
        assert out["aggregate"]["label"] == "Sum Metric"
        assert out["aggregate"]["method"] in EXISTING_METHODS
        assert [s["status"] for s in out["sites"]] == ["no-data", "no-data"]
        assert {s["system_id"] for s in out["sites"]} == {"sys-1", "sys-2"}
        assert all(s["value"] is None for s in out["sites"])

    @pytest.mark.parametrize("metric_id", ["m-sum", "m-avg", "m-max", "m-min",
                                           "m-latest", "m-derived"])
    def test_every_aggregation_yields_an_existing_method(self, metric_id):
        out = _multi(metric_id, _no_data_resolver)
        assert out["aggregate"]["method"] in EXISTING_METHODS
        assert out["aggregate"]["value"] is None

    def test_sites_stay_sorted_by_the_shared_rule(self):
        out = _multi("m-sum", _no_data_resolver)
        assert [s["site"] for s in out["sites"]] == ["site-x", "site-y"]


class TestZeroSampleCountIsNoData:
    """``value is None`` is only HALF the honest-data invariant (ADR-0045).

    The rule is ``value None`` **or** ``sample_count == 0`` ⇒ ``no-data``, and
    ``agents/dashboard.py`` already applies both halves. A degenerate aggregate
    that returns ``0`` over ZERO rows must therefore not contribute — publishing
    it as a real ``0`` measurement fabricates a reading from no samples.
    """

    @staticmethod
    def _zero_sample_resolver(metric_id, *, authority):
        sid = authority.authoritative_system("any-concept")
        return ResolvedMetric(metric_id=metric_id, value=0, unit="count",
                              label="L", query_plan=None, system_id=sid,
                              sample_count=0)

    @staticmethod
    def _real_zero_resolver(metric_id, *, authority):
        """A genuine measured zero: zero value over REAL samples."""
        sid = authority.authoritative_system("any-concept")
        return ResolvedMetric(metric_id=metric_id, value=0, unit="count",
                              label="L", query_plan=None, system_id=sid,
                              sample_count=7)

    def test_zero_value_with_zero_samples_is_a_no_data_aggregate(self):
        out = _multi("m-sum", self._zero_sample_resolver)
        assert out["contributing"] == 0
        assert out["aggregate"]["value"] is None
        assert [s["status"] for s in out["sites"]] == ["no-data", "no-data"]
        assert all(s["value"] is None for s in out["sites"])

    def test_zero_value_with_samples_still_contributes(self):
        """Zeros WITH samples stay visible — the other half of the invariant."""
        out = _multi("m-sum", self._real_zero_resolver)
        assert out["contributing"] == 2
        assert out["aggregate"]["value"] == 0
        assert [s["status"] for s in out["sites"]] == ["ok", "ok"]
        assert [s["value"] for s in out["sites"]] == [0, 0]

    def test_absent_sample_count_still_contributes(self):
        """``sample_count is None`` means the resolver did not report one — that
        is NOT zero samples, so a real value must still contribute."""
        def _resolver(metric_id, *, authority):
            sid = authority.authoritative_system("any-concept")
            return ResolvedMetric(metric_id=metric_id, value=5, unit="count",
                                  label="L", query_plan=None, system_id=sid)

        out = _multi("m-sum", _resolver)
        assert out["contributing"] == 2
        assert out["aggregate"]["value"] == 10

    def test_mixed_zero_sample_and_error_is_no_data(self):
        calls: list[str] = []

        def _resolver(metric_id, *, authority):
            sid = authority.authoritative_system("any-concept")
            calls.append(sid)
            if sid == "sys-1":
                raise MetricResolverError("boundary failure")
            return ResolvedMetric(metric_id=metric_id, value=0, unit="count",
                                  label="L", query_plan=None, system_id=sid,
                                  sample_count=0)

        out = _multi("m-sum", _resolver)
        assert out["contributing"] == 0
        assert out["aggregate"]["value"] is None
        assert sorted(s["status"] for s in out["sites"]) == ["error", "no-data"]
        assert len(calls) == 2

    def test_all_errors_still_raise_with_zero_sample_rule_in_place(self):
        with pytest.raises(MetricResolverError):
            _multi("m-sum", _error_resolver)


class TestMixedAndAllErrorBoundaries:
    def test_mixed_no_data_and_error_is_no_data(self):
        """At least one source was READ successfully ⇒ honest no-data."""

        def _mixed(metric_id, *, authority):
            if authority.authoritative_system("any-concept") == "sys-1":
                raise MetricResolverError("boundary failure")
            return _no_data_resolver(metric_id, authority=authority)

        out = _multi("m-sum", _mixed)
        assert out["contributing"] == 0
        assert out["aggregate"]["value"] is None
        assert sorted(s["status"] for s in out["sites"]) == ["error", "no-data"]

    def test_all_covering_systems_errored_still_raises(self):
        with pytest.raises(MetricResolverError):
            _multi("m-sum", _error_resolver)

    def test_no_coverage_at_all_still_raises(self):
        with pytest.raises(MetricResolverError):
            mms.resolve_metric_multi("m-sum", resolver=_no_data_resolver,
                                     query=lambda c: [], metric_layer=LAYER,
                                     systems=SYSTEMS)

    def test_unexpected_exception_per_system_is_still_error_status(self):
        def _boom(metric_id, *, authority):  # noqa: ARG001
            raise ZeroDivisionError("unexpected")

        with pytest.raises(MetricResolverError):
            _multi("m-sum", _boom)


class TestDashboardConsumesNoDataDirectly:
    """The consumer half: no fallback resolve, no traceback storm."""

    def test_no_data_aggregate_needs_no_fallback_resolve(self, monkeypatch, caplog):
        no_data = {
            "aggregate": {"value": None, "unit": "count", "label": "Sum Metric",
                          "method": "sum"},
            "sites": [{"site": "site-x", "system_id": "sys-1",
                       "system_name": "System One", "value": None,
                       "status": "no-data"}],
            "contributing": 0,
        }
        monkeypatch.setattr(dash.metric_multisite, "resolve_metric_multi",
                            lambda ref: no_data)

        def _must_not_run(ref):
            raise AssertionError("fallback single-authority resolve must not run")

        monkeypatch.setattr(dash, "_build_card_metric", _must_not_run)
        with caplog.at_level(logging.DEBUG, logger="agents.dashboard"):
            entry = dash._build_card_metric_multi("m-sum", attach_sites=True)

        assert entry["status"] == "no-data"
        assert entry["value"] is None
        assert entry["label"] == "Sum Metric"
        assert entry["sites"] == no_data["sites"]
        assert not any(r.exc_info for r in caplog.records), \
            "an expected no-data state must not log a traceback"

    def test_expected_boundary_logs_concisely_and_falls_back(
            self, monkeypatch, caplog):
        """``MetricResolverError`` is the fan-out's NAMED boundary class — its
        message carries the reason, so it needs no traceback storm."""

        def _raise(ref):
            raise MetricResolverError("no onboarded system covers metric")

        monkeypatch.setattr(dash.metric_multisite, "resolve_metric_multi", _raise)
        monkeypatch.setattr(dash, "_build_card_metric",
                            lambda ref: {"metric_id": ref, "status": "unavailable"})
        with caplog.at_level(logging.INFO, logger="agents.dashboard"):
            entry = dash._build_card_metric_multi("m-sum", attach_sites=False)

        assert entry["status"] == "unavailable"
        assert not any(r.exc_info for r in caplog.records)
        assert any("no onboarded system covers metric" in r.getMessage()
                   for r in caplog.records)

    def test_code_defect_class_keeps_the_traceback_and_falls_back(
            self, monkeypatch, caplog):
        def _raise(ref):
            raise KeyError("unexpected shape")

        monkeypatch.setattr(dash.metric_multisite, "resolve_metric_multi", _raise)
        monkeypatch.setattr(dash, "_build_card_metric",
                            lambda ref: {"metric_id": ref, "status": "unavailable"})
        with caplog.at_level(logging.WARNING, logger="agents.dashboard"):
            entry = dash._build_card_metric_multi("m-sum", attach_sites=False)

        assert entry["status"] == "unavailable"
        assert any(r.exc_info for r in caplog.records)


def _invariant_resolver(metric_id, *, authority):
    """Every covering system computed a value its declared invariant withheld."""
    sid = authority.authoritative_system("any-concept")
    return ResolvedMetric(metric_id=metric_id, value=None, unit="count",
                          label="L", query_plan=None, system_id=sid,
                          sample_count=4, status="invariant_violation",
                          detail="value outside the declared range")


class TestInvariantViolationIsNeverRelabelledNoData:
    """ADR-0045 / ADR-0080: a declaration-withheld value keeps its OWN status
    through the fan-out — per site AND on the aggregate card entry."""

    def test_sites_keep_invariant_violation(self):
        out = _multi("m-sum", _invariant_resolver)
        assert out["aggregate"]["value"] is None
        assert out["contributing"] == 0
        assert [s["status"] for s in out["sites"]] == ["invariant-violation"] * 2

    def test_card_entry_is_invariant_violation_naming_the_systems(self):
        entry = dash.multi_card_entry("m-sum", _multi("m-sum", _invariant_resolver),
                                      attach_sites=True)
        assert entry["status"] == "invariant-violation"
        assert entry["value"] is None
        assert "sys-1" in entry["detail"] and "sys-2" in entry["detail"]

    def test_a_contributing_site_keeps_the_aggregate_ok(self):
        def mixed(metric_id, *, authority):
            if authority.authoritative_system("c") == "sys-1":
                return _invariant_resolver(metric_id, authority=authority)
            return ResolvedMetric(metric_id=metric_id, value=5, unit="count",
                                  label="L", query_plan=None, system_id="sys-2",
                                  sample_count=5)
        entry = dash.multi_card_entry("m-sum", _multi("m-sum", mixed),
                                      attach_sites=True)
        assert entry["status"] == "ok" and entry["value"] == 5
        assert "detail" not in entry
        assert {s["status"] for s in entry["sites"]} == {"ok", "invariant-violation"}

    def test_all_no_data_card_entry_is_unchanged(self):
        entry = dash.multi_card_entry("m-sum", _multi("m-sum", _no_data_resolver),
                                      attach_sites=False)
        assert entry["status"] == "no-data" and "detail" not in entry


def _binding_resolver(metric_id, *, authority):
    """Every covering plant binds the concept to two columns (ADR-0076)."""
    sid = authority.authoritative_system("any-concept")
    return ResolvedMetric(metric_id=metric_id, value=None, unit="count",
                          label="L", query_plan=None, system_id=sid,
                          status=mr.AMBIGUOUS_BINDING_STATUS,
                          detail="concept 'speed' is bound to 2 distinct columns")


class TestAWithheldValueOnEveryPlantIsAnAnswer:
    """Event 26 (2026-10-04, four-plant account): ``machine-speed`` binds to
    two columns on every plant's MES. The fan-out counted the four withheld
    sites as "nothing read", raised, and the card fell back to the
    single-authority path, which cannot even start on a four-authority
    deployment and rendered "ambiguous authority ... 4 active AuthorityRules"
    with a Retry button. The plants WERE read; the value is withheld for a
    reason a steward can act on, and that reason is what the card says."""

    def test_all_sites_ambiguous_binding_does_not_raise(self):
        out = _multi("m-sum", _binding_resolver)
        assert out["aggregate"]["value"] is None and out["contributing"] == 0
        assert [s["status"] for s in out["sites"]] == ["ambiguous-binding"] * 2
        assert "ambiguous-binding" in mms._READ_SITE_STATUSES
        assert "unresolved-predicate" in mms._READ_SITE_STATUSES

    def test_card_entry_is_ambiguous_binding_naming_the_systems(self):
        entry = dash.multi_card_entry("m-sum", _multi("m-sum", _binding_resolver),
                                      attach_sites=True)
        assert entry["status"] == "ambiguous-binding"
        assert entry["value"] is None
        assert "sys-1" in entry["detail"] and "sys-2" in entry["detail"]
        assert "2 distinct columns" in entry["detail"]

    def test_mixed_withheld_reasons_name_each(self):
        def mixed(metric_id, *, authority):
            if authority.authoritative_system("c") == "sys-1":
                return _invariant_resolver(metric_id, authority=authority)
            return _binding_resolver(metric_id, authority=authority)
        entry = dash.multi_card_entry("m-sum", _multi("m-sum", mixed), attach_sites=False)
        assert entry["status"] in {"invariant-violation", "ambiguous-binding"}
        assert "sys-1" in entry["detail"] and "sys-2" in entry["detail"]

    def test_every_site_erroring_still_raises(self):
        with pytest.raises(MetricResolverError):
            _multi("m-sum", _error_resolver)
