# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

""": a computed metric this turn, bound to a concept the route
seed lists in ``unrecorded_terms``, drops that concept from the unrecorded
line (engine fact must not contradict a computed result). Pure, offline."""
from __future__ import annotations

import agents.concept_notes as cn


# --- the pure line editor --------------------------------------------------- #

def test_drop_one_named_concept_from_the_line():
    line = cn.unrecorded_terms_line(cn.UnrecordedTerms(("cycle-time", "ink-colour")))
    assert "cycle-time" in line and "ink-colour" in line
    dropped = cn.drop_terms_from_unrecorded_line(line, {"cycle-time"})
    assert "cycle-time" not in dropped
    assert "ink-colour" in dropped


def test_dropping_the_only_concept_empties_the_line():
    line = cn.unrecorded_terms_line(cn.UnrecordedTerms(("cycle-time",)))
    assert cn.drop_terms_from_unrecorded_line(line, {"cycle-time"}) == ""


def test_dropping_nothing_is_a_no_op():
    line = cn.unrecorded_terms_line(cn.UnrecordedTerms(("cycle-time",)))
    assert cn.drop_terms_from_unrecorded_line(line, set()) == line
    assert cn.drop_terms_from_unrecorded_line(line, {"other"}) == line


def test_empty_line_stays_empty():
    assert cn.drop_terms_from_unrecorded_line("", {"cycle-time"}) == ""


def test_counted_hidden_terms_are_preserved():
    line = cn.unrecorded_terms_line(cn.UnrecordedTerms(("cycle-time",), hidden=1))
    dropped = cn.drop_terms_from_unrecorded_line(line, {"cycle-time"})
    # the hidden term is still reported even though the named one is gone
    assert dropped and "cycle-time" not in dropped


# --- the runtime seam: computed results -> concepts to drop ----------------- #

def test_computed_concepts_maps_results_to_bound_concepts(monkeypatch):
    import agents.metric_turn_notice as mtn
    from agents.metric_layer import Aggregation, DirectMetric

    metric = DirectMetric(id="cycle-tp", concept="cycle-time",
                          aggregation=Aggregation.COUNT, label="X", unit="h")

    class _Layer:
        def get_metric(self, mid):
            assert mid == "cycle-tp"
            return metric

    monkeypatch.setattr("agents.metric_layer.get_metric_layer", lambda: _Layer())
    results = [{"outcome": "computed", "metric_id": "cycle-tp"},
               {"outcome": "withheld", "metric_id": "other"}]
    assert mtn.computed_concepts(results) == {"cycle-time"}


def test_computed_concepts_is_fail_soft(monkeypatch):
    import agents.metric_turn_notice as mtn

    def _boom():
        raise RuntimeError("no layer")

    monkeypatch.setattr("agents.metric_layer.get_metric_layer", _boom)
    assert mtn.computed_concepts([{"outcome": "computed", "metric_id": "x"}]) == set()


# --- percentile result classifies as computed + renders ------------------- #

def test_percentile_result_classifies_computed():
    import agents.metric_turn_notice as mtn
    from agents.metric_resolver import ResolvedMetric

    resolved = ResolvedMetric(
        metric_id="cycle-tp", value=None, unit="hours", label="Cycle Time",
        query_plan=None, system_id="sys-a", lineage=None, sample_count=1000,
        percentiles=((0.9, 61.8), (0.99, 121.4)))
    out = mtn._classify(resolved)
    assert out["outcome"] == "computed"
    assert out["percentiles"] == [{"p": 0.9, "value": 61.8},
                                  {"p": 0.99, "value": 121.4}]
    assert out["sample_count"] == 1000


def test_percentile_no_samples_is_no_data():
    import agents.metric_turn_notice as mtn
    from agents.metric_resolver import ResolvedMetric

    resolved = ResolvedMetric(
        metric_id="cycle-tp", value=None, unit="hours", label="X",
        query_plan=None, system_id="sys-a", lineage=None, sample_count=0,
        percentiles=((0.9, None),))
    assert mtn._classify(resolved)["status"] == "no-data"


def test_percentile_block_renders_each_p():
    from agents.metric_turn_render import render_metric_layer_notice
    result = {
        "outcome": "computed", "term": "cycle time", "metric_id": "cycle-tp",
        "label": "Cycle Time", "source": "graph", "unit": "hours",
        "kind": "direct", "shape": "direct", "system_id": "sys-a",
        "lineage": "sys-a.segments.segment_id (count)", "query_count": 1,
        "systems": ["sys-a"], "sample_count": 1000,
        "percentiles": [{"p": 0.9, "value": 61.8}, {"p": 0.99, "value": 121.4}],
    }
    notice = render_metric_layer_notice([result])
    assert "p90=61.8 hours" in notice and "p99=121.4 hours" in notice
    assert "COMPUTED" in notice
