# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0078 D2 — the DEFINED-metric matcher (``defined_metric_terms``).

Fully offline. The pre-check in ``metric_term_resolution`` fires only for
terms that do NOT resolve; a DEFINED metric was therefore the worst case (the
term resolves, nothing warns, the model computes a proxy). This file pins the
deterministic matcher that tells the app which defined metrics a question
names, so the metric layer's result can ride the turn:

* multiword forms admit outright; single words and initialisms need the
  existing metric-request gate;
* forms under 3 characters never match; the longest overlapping span wins;
  a span naming 2+ ids is ambiguous; first-appearance order, capped at 3;
* graph synonyms credit ONLY graph-served ids; a store failure degrades to
  the layer's own forms — never to silence.

Fixture vocabulary is neutral ``widget_*`` placeholders only.
"""
from __future__ import annotations

import time

import pytest

import agents.metric_definition_store as mds
import agents.metric_layer as ml
import agents.metric_term_resolution as mtr
from agents.metric_layer import Aggregation, DirectMetric


def _direct(metric_id: str, label: str) -> DirectMetric:
    return DirectMetric(id=metric_id, concept="widget-count",
                        aggregation=Aggregation.SUM, label=label, unit="units")


class _FakeLayer:
    """A composed-layer stand-in: ``list_metrics`` + the graph seam."""

    def __init__(self, metrics, graph_ids=()):
        self._metrics = list(metrics)
        self._graph_ids = frozenset(graph_ids)

    def list_metrics(self):
        return list(self._metrics)

    def is_graph_defined(self, metric_id):
        return metric_id in self._graph_ids


@pytest.fixture
def install(monkeypatch):
    """Install a fake layer and store docs; returns the store call log."""
    calls: list[str] = []

    def _install(metrics, graph_ids=(), docs=None, store_error=None):
        layer = _FakeLayer(metrics, graph_ids)
        monkeypatch.setattr(ml, "get_metric_layer", lambda: layer)

        def _load():
            calls.append("load")
            if store_error is not None:
                raise store_error
            return dict(docs or {})

        monkeypatch.setattr(mds, "load_definitions", _load)
        monkeypatch.setattr(mds, "neptune_query", _forbidden_graph_read)
        return calls

    return _install


def _forbidden_graph_read(*_a, **_k):
    raise AssertionError("the matcher must never read the graph per turn")


def _ids(matches):
    return [m.metric_ids for m in matches]


def test_multiword_label_matches_without_context_word(install):
    install([_direct("widget-defect-rate", "Widget Defect Rate")])
    matches = mtr.defined_metric_terms("How did Widget Defect Rate move lately")
    assert _ids(matches) == [("widget-defect-rate",)]
    assert matches[0].term == "widget defect rate"
    assert matches[0].label == "Widget Defect Rate"
    assert not matches[0].ambiguous


def test_single_word_label_requires_metric_context(install):
    install([_direct("widget-throughput", "Throughput")])
    assert mtr.defined_metric_terms("throughput of ideas at the offsite") == []
    assert _ids(mtr.defined_metric_terms("what is the throughput")) == [
        ("widget-throughput",)]
    assert _ids(mtr.defined_metric_terms("show throughput rate by week")) == [
        ("widget-throughput",)]


def test_initialism_of_label_matches_under_what_is_shape(install):
    install([_direct("widget-defect-rate", "Widget Defect Rate")])
    assert _ids(mtr.defined_metric_terms("What's the WDR?")) == [
        ("widget-defect-rate",)]
    assert mtr.defined_metric_terms("WDR team meeting notes") == []


def test_graph_synonym_matches_and_credits_the_graph_id(install):
    install([_direct("widget-defect-rate", "Widget Defect Rate"),
             _direct("widget-units", "Widget Units")],
            graph_ids={"widget-defect-rate"},
            docs={"widget-defect-rate": {"id": "widget-defect-rate",
                                         "synonyms": ["scrap share"]},
                  "widget-units": {"id": "widget-units",
                                   "synonyms": ["output tally"]}})
    assert _ids(mtr.defined_metric_terms("trend the scrap share please")) == [
        ("widget-defect-rate",)]
    # A code-shadowed doc (not graph-served) contributes no synonym.
    assert mtr.defined_metric_terms("trend the output tally please") == []


def test_longest_span_wins_over_nested_match(install):
    install([_direct("widget-defect-rate", "Widget Defect Rate"),
             _direct("defect-rate", "Defect Rate")])
    assert _ids(mtr.defined_metric_terms("the widget defect rate today")) == [
        ("widget-defect-rate",)]


def test_two_ids_on_one_span_is_ambiguous(install):
    install([_direct("widget-yield-a", "Widget Yield"),
             _direct("widget-yield-b", "Widget Yield")])
    matches = mtr.defined_metric_terms("report widget yield for the week")
    assert _ids(matches) == [("widget-yield-a", "widget-yield-b")]
    assert matches[0].ambiguous
    assert matches[0].label == ""


def test_cap_and_first_appearance_order(install):
    install([_direct(f"widget-m{i}", f"Widget Gauge {w}")
             for i, w in enumerate(("Alpha", "Bravo", "Charlie", "Delta"))])
    matches = mtr.defined_metric_terms(
        "compare widget gauge delta, widget gauge bravo, widget gauge alpha "
        "and widget gauge charlie")
    assert len(matches) == mtr.MAX_METRIC_TERMS == 3
    assert _ids(matches) == [("widget-m3",), ("widget-m1",), ("widget-m0",)]


def test_overflow_scan_detects_exactly_one_over_cap_match(install):
    """The caller can DETECT overflow (cap + 1), and the scan
    stops there — a fifth metric is never indexed into the result."""
    install([_direct(f"widget-m{i}", f"Widget Gauge {w}")
             for i, w in enumerate(("Alpha", "Bravo", "Charlie", "Delta", "Echo"))])
    question = ("compare widget gauge delta, widget gauge bravo, widget gauge alpha, "
                "widget gauge charlie and widget gauge echo")
    matches = mtr.defined_metric_terms(question, limit=mtr.MAX_METRIC_TERMS + 1)
    assert _ids(matches) == [("widget-m3",), ("widget-m1",), ("widget-m0",),
                             ("widget-m2",)]


def test_same_metric_named_twice_is_one_match(install):
    install([_direct("widget-defect-rate", "Widget Defect Rate")])
    matches = mtr.defined_metric_terms(
        "widget defect rate vs last week's widget defect rate")
    assert _ids(matches) == [("widget-defect-rate",)]


def test_short_forms_never_match(install):
    install([_direct("wd", "WD")])
    assert mtr.MIN_FORM_CHARS == 3
    assert mtr.defined_metric_terms("what is the wd rate") == []


def test_store_failure_degrades_to_layer_forms_not_silence(install):
    install([_direct("widget-defect-rate", "Widget Defect Rate")],
            graph_ids={"widget-defect-rate"},
            store_error=RuntimeError("graph down"))
    assert _ids(mtr.defined_metric_terms("widget defect rate now")) == [
        ("widget-defect-rate",)]


def test_matcher_is_pure_no_graph_round_trip_beyond_snapshot(install):
    calls = install([_direct("widget-defect-rate", "Widget Defect Rate")],
                    graph_ids={"widget-defect-rate"}, docs={})
    mtr.defined_metric_terms("widget defect rate now")
    assert calls == ["load"]           # one snapshot read, zero graph queries


def test_empty_question_and_unmatched_question_yield_nothing(install):
    install([_direct("widget-defect-rate", "Widget Defect Rate")])
    assert mtr.defined_metric_terms("") == []
    assert mtr.defined_metric_terms("which machines are down?") == []


# --------------------------------------------------------------------------- #
# Imperative requests admit exact single-word forms
# --------------------------------------------------------------------------- #
def _single_word_fixture(install):
    install([_direct("widget-throughput", "Throughput"),
             _direct("widgetyield", "Widget Yield Gauge"),
             _direct("widget-defect-rate", "Widget Defect Rate")],
            graph_ids={"widget-defect-rate"},
            docs={"widget-defect-rate": {"id": "widget-defect-rate",
                                         "synonyms": ["scrapshare"]}})


@pytest.mark.parametrize("question, expected", [
    ("Show throughput", "widget-throughput"),                 # single-word label
    ("show me the throughput for line 3", "widget-throughput"),
    ("Report widgetyield for the week", "widgetyield"),       # single-word id
    ("give me our WDR", "widget-defect-rate"),                # initialism
    ("Compare scrapshare across shifts", "widget-defect-rate"),  # graph synonym
    ("please list the throughput", "widget-throughput"),
    ("can you tell me the WYG", "widgetyield"),               # initialism of label
])
def test_imperative_request_admits_exact_single_word_forms(install, question, expected):
    _single_word_fixture(install)
    assert _ids(mtr.defined_metric_terms(question)) == [(expected,)]


def test_imperative_list_chains_every_named_single_word_metric(install):
    _single_word_fixture(install)
    assert _ids(mtr.defined_metric_terms("compare throughput and WDR")) == [
        ("widget-throughput",), ("widget-defect-rate",)]
    assert _ids(mtr.defined_metric_terms("report throughput, WDR, widgetyield")) == [
        ("widget-throughput",), ("widget-defect-rate",), ("widgetyield",)]


@pytest.mark.parametrize("question", [
    "throughput of ideas at the offsite was great",
    "show me machines where throughput dropped",
    "tell the team WDR notes are due friday",
    "the report on throughput culture is attached",
    "notes: show and tell about throughput culture",
])
def test_casual_mentions_stay_unadmitted(install, question):
    _single_word_fixture(install)
    assert mtr.defined_metric_terms(question) == []


@pytest.mark.parametrize("question,expected", [
    ("throughput?", [("widget-throughput",)]),
    ("Throughput", [("widget-throughput",)]),
    ("How is throughput?", [("widget-throughput",)]),
    ("how was WDR", [("widget-defect-rate",)]),
    ("Did throughput improve?", [("widget-throughput",)]),
    ("is WDR up?", [("widget-defect-rate",)]),
    ("throughput improved this week?", [("widget-throughput",)]),
    ("WDR over the last week", [("widget-defect-rate",)]),
    ("so, how about throughput", [("widget-throughput",)]),
    ("what about the WDR", [("widget-defect-rate",)]),
])
def test_bare_single_word_metric_as_question_subject_is_admitted(install, question, expected):
    """A bare id/label/initialism that IS the question's
    subject must carry a notice — the earlier gates covered only imperatives,
    lists and measurement words."""
    _single_word_fixture(install)
    assert _ids(mtr.defined_metric_terms(question)) == expected


@pytest.mark.parametrize("question", [
    "throughput of ideas at the offsite was great",
    "did the team enjoy the throughput culture talk?",
    "how is the weather near the throughput poster",
    "throughput culture matters a lot to us here honestly",
    "WDR team meeting notes",
    "throughput poster draft",
])
def test_bare_form_inside_prose_stays_unadmitted(install, question):
    """The subject gate is anchored: a mention inside a longer sentence, or a
    long predicate, does not fire."""
    _single_word_fixture(install)
    assert mtr.defined_metric_terms(question) == []


@pytest.mark.parametrize("question", [
    "If the glue defect had been eliminated last month, what would the WDR impact have been?",
    "Given the shutdown, how much did WDR at the north cell move, and why?",
    "which cell had the worst WDR last week and what drove it",
    "Is the WDR meeting on friday?",  # declared acronym in a question: a notice is harmless
])
def test_declared_acronym_anywhere_in_a_question_is_admitted(install, question):
    """Measured live: a declared metric written as an acronym inside
    an interrogative question is the user naming the metric — mid-sentence,
    after a conditional clause, or as the object of 'impact'."""
    _single_word_fixture(install)
    assert _ids(mtr.defined_metric_terms(question)) == [("widget-defect-rate",)]


@pytest.mark.parametrize("question", [
    "WDR team meeting notes",
    "the WDR slide deck is attached, review by friday",
    "what would the wdr impact have been",  # lower-case: not written as an acronym
])
def test_acronym_admission_needs_question_shape_and_original_casing(install, question):
    _single_word_fixture(install)
    assert mtr.defined_metric_terms(question) == []


def test_gap_precheck_gate_is_unchanged_by_the_imperative_admission(install):
    """The imperative gate is DEFINED-metric admission only: the unresolved
    pre-check keeps its own gate, so an unknown acronym after a request verb
    is not newly claimed as an undefined metric."""
    assert not mtr._in_metric_context("show ABC", 5, 8)


# --------------------------------------------------------------------------- #
# Matching work is bounded; over-bound fails CLOSED
# --------------------------------------------------------------------------- #
def test_oversized_question_raises_the_matcher_bound_not_a_silent_miss(install):
    _single_word_fixture(install)
    question = ("widget defect rate " * (mtr.MAX_MATCH_INPUT_CHARS // 10)
                + "and widget yield gauge")
    assert len(question) > mtr.MAX_MATCH_INPUT_CHARS
    with pytest.raises(mtr.MatcherInputTooLong):
        mtr.defined_metric_terms(question)


def test_repeated_form_under_the_bound_never_hides_a_later_term(install):
    _single_word_fixture(install)
    repeated = "widget defect rate " * ((mtr.MAX_MATCH_INPUT_CHARS - 200) // 19)
    question = repeated + "and widget yield gauge"
    assert len(question) <= mtr.MAX_MATCH_INPUT_CHARS
    started = time.perf_counter()
    matches = mtr.defined_metric_terms(question)
    assert time.perf_counter() - started < 2.0
    assert _ids(matches) == [("widget-defect-rate",), ("widgetyield",)]


def test_scan_is_one_pass_one_span_per_word_start(install):
    forms = {"widget defect rate": {}, "defect rate": {}, "widget": {}}
    spans = mtr._scan_defined_forms("widget defect rate widget", forms)
    assert spans == [(0, 18, "widget defect rate"), (7, 18, "defect rate"),
                     (19, 25, "widget")]


# --------------------------------------------------------------------------- #
# A plain-noun multiword label ("Work Orders") once fired on
# every question that merely contained the words, so asset-, line- and
# status-scoped questions got the deployment-wide count narrated at them.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question", [
    "Which work orders are open on L3-M1 right now?",
    "List the work orders for CMX-441 and their status.",
    "Is there a running work orders entry for line 3 with failed checks?",
    "Show me the work orders raised for the bearing on L3-M1 this shift.",
    "What maintenance work orders did the CMMS log against the spindle?",
])
def test_plain_noun_label_stays_silent_on_scoped_record_questions(install, question):
    install([_direct("work-order-count", "Work Orders")])
    assert mtr.defined_metric_terms(question) == []


@pytest.mark.parametrize("question", [
    "How many work orders are there?",
    "How many open work orders exist across the deployment?",
    "What is the total number of work orders?",
    "Give me the work orders count by status.",
    "What are the work orders?",
    "work orders",
    "Work orders total for the plant, please.",
])
def test_plain_noun_label_admits_under_a_count_or_metric_request(install, question):
    install([_direct("work-order-count", "Work Orders")])
    assert _ids(mtr.defined_metric_terms(question)) == [("work-order-count",)]


def test_measurement_word_label_still_admits_anywhere(install):
    install([_direct("widget-defect-rate", "Widget Defect Rate"),
             _direct("good-count", "Good Count")])
    assert _ids(mtr.defined_metric_terms(
        "Did widget defect rate on L3-M1 change after the bearing swap?")) == [
            ("widget-defect-rate",)]
    assert _ids(mtr.defined_metric_terms(
        "Compare good count on line 3 with yesterday.")) == [("good-count",)]
