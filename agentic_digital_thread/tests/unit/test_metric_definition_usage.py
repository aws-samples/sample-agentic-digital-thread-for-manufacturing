# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0066 D6 delivery telemetry for graph metric definitions (Task 3C).

This file IS the ADR-0052 gate evidence: a learning writer (the
``metric_definition`` lifecycle accept) ships with its read paths (the composed
metric layer, the pre-check) PLUS usage telemetry — and the telemetry proves
DELIVERY/COMPOSITION, never obedience (ADR-0063 D7).

Pins:
- a GRAPH-DEFINED metric resolve bumps the usage ledger EXACTLY once with
  evidence ``"metric-resolve"``;
- a code-metric resolve bumps NOTHING (no ledger interaction at all);
- a pre-check candidate resolved BY a graph definition bumps with evidence
  ``"term-precheck-resolved"``; baseline-resolved candidates bump nothing;
- a ledger failure NEVER propagates into a resolve or a turn;
- no evidence string claims obedience ("followed"/"obeyed"/"used"/...).

Offline: no AWS, no graph, no DB, no LLM. Fixture ids are neutral placeholders
(ADR-0044). The graph half is reached through the same monkeypatchable seams
``tests/unit/test_graph_metric_layer.py`` uses.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import agents.artifact_usage as artifact_usage
import agents.graph_metric_layer as gml
import agents.metric_definition as md
import agents.metric_definition_store as store
import agents.metric_layer as ml
import agents.metric_resolver as mr
import agents.runtime_app as runtime_app


# --- Fixtures / helpers -----------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_layer():
    """The composed layer is an ``lru_cache``d process singleton over a
    process-global store snapshot — reset BOTH around every test."""
    ml.get_metric_layer.cache_clear()
    store.invalidate()
    yield
    ml.get_metric_layer.cache_clear()
    store.invalidate()


@pytest.fixture(autouse=True)
def _seed_live_catalog_with_demo_systems():
    """Resolve paths exercise demo schemas by name (the ``test_metric_resolver``
    idiom): seed the shared catalog from the test-local fixture, then restore
    the empty base-mode catalog."""
    from agents.catalog import catalog as _cat
    try:
        from unit._demo_catalog import DEMO_SYSTEMS
    except ImportError:  # pragma: no cover - alternate invocation path
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _demo_catalog import DEMO_SYSTEMS
    _cat._systems = [dict(s) for s in DEMO_SYSTEMS]
    try:
        yield
    finally:
        _cat._systems = []


def _doc() -> dict:
    """A neutral DIRECT graph definition over a core concept (ADR-0044)."""
    return md.parse_definition({
        "id": "line-utilization",
        "kind": "direct",
        "label": "Line Utilization",
        "synonyms": ["LU"],
        "unit": "percent",
        "concept": "mro-inventory",
        "aggregation": "sum",
    })


class _FakeAuthority(mr.AuthorityResolver):
    def __init__(self, system_id: str | None) -> None:
        self._system_id = system_id

    def authoritative_system(self, concept: str) -> str | None:
        return self._system_id


class _BumpRecorder:
    """Fake ``artifact_usage.bump_artifact_usage`` capturing (refs, evidence)."""

    def __init__(self, raise_error: bool = False) -> None:
        self.calls: list[tuple[set, str]] = []
        self._raise = raise_error

    def __call__(self, refs, evidence: str = "") -> int:
        if self._raise:
            raise RuntimeError("ledger unavailable")
        refs = set(refs)
        self.calls.append((refs, evidence))
        return len(refs)


def _install_resolve_seams(monkeypatch) -> None:
    """Canned graph-traversal + Postgres seams for a successful direct resolve."""
    monkeypatch.setattr(
        mr, "neptune_query",
        lambda cypher, params=None: [
            {"table": "spare_parts", "column": "quantity_on_hand"}],
    )
    monkeypatch.setattr(
        mr, "query_pg",
        lambda schema, sql, params=None: json.dumps(
            {"columns": ["sum"], "rows": [[18]], "row_count": 1}),
    )


_LU_QUESTION = "What is the LU rate for the plant?"


# --- Resolver arm: evidence="metric-resolve" --------------------------------

def test_graph_metric_resolve_bumps_exactly_once_with_metric_resolve(monkeypatch):
    doc = _doc()
    monkeypatch.setattr(gml, "load_definitions", lambda: {doc["id"]: doc})
    _install_resolve_seams(monkeypatch)
    recorder = _BumpRecorder()
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage", recorder)

    resolved = mr.resolve_metric(
        "line-utilization", authority=_FakeAuthority("cmms"))

    assert resolved.value == 18
    assert resolved.metric_id == "line-utilization"
    assert recorder.calls == [
        ({("metric_definition", "line-utilization")}, "metric-resolve")]


def test_code_metric_resolve_bumps_nothing(monkeypatch):
    """Byte-identity for code metrics: NO ledger interaction of any kind."""
    monkeypatch.setattr(gml, "load_definitions", lambda: {})
    _install_resolve_seams(monkeypatch)
    recorder = _BumpRecorder()
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage", recorder)

    resolved = mr.resolve_metric(
        "spare-parts-on-hand", authority=_FakeAuthority("cmms"))

    assert resolved.value == 18
    assert recorder.calls == []


def test_ledger_failure_never_fails_a_graph_metric_resolve(monkeypatch):
    doc = _doc()
    monkeypatch.setattr(gml, "load_definitions", lambda: {doc["id"]: doc})
    _install_resolve_seams(monkeypatch)
    monkeypatch.setattr(
        artifact_usage, "bump_artifact_usage", _BumpRecorder(raise_error=True))

    resolved = mr.resolve_metric(
        "line-utilization", authority=_FakeAuthority("cmms"))

    assert resolved.value == 18          # the raise was swallowed, never seen


def test_layer_probe_failure_never_fails_a_resolve(monkeypatch):
    """Even the ``is_graph_defined`` probe itself is inside the swallow."""
    monkeypatch.setattr(gml, "load_definitions", lambda: {})
    _install_resolve_seams(monkeypatch)
    monkeypatch.setattr(
        gml.ComposedMetricLayer, "is_graph_defined",
        lambda self, metric_id: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    resolved = mr.resolve_metric(
        "spare-parts-on-hand", authority=_FakeAuthority("cmms"))

    assert resolved.value == 18


def test_plain_code_layer_without_the_seam_is_a_noop(monkeypatch):
    """A layer with no ``is_graph_defined`` attribute degrades to no bump."""
    _install_resolve_seams(monkeypatch)
    recorder = _BumpRecorder()
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage", recorder)
    plain = ml.CodeMetricLayer()
    monkeypatch.setattr(mr, "get_metric_layer", lambda: plain)
    assert not hasattr(plain, "is_graph_defined")

    resolved = mr.resolve_metric(
        "spare-parts-on-hand", authority=_FakeAuthority("cmms"))

    assert resolved.value == 18
    assert recorder.calls == []


# --- Pre-check arm: evidence="term-precheck-resolved" -----------------------

def test_precheck_resolution_by_a_graph_definition_bumps(monkeypatch):
    doc = _doc()
    monkeypatch.setattr(store, "load_definitions", lambda: {doc["id"]: doc})
    recorder = _BumpRecorder()
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage", recorder)

    bumped = runtime_app._bump_metric_definition_precheck_usage(
        _LU_QUESTION, "")

    assert bumped == 1
    assert recorder.calls == [
        ({("metric_definition", "line-utilization")},
         "term-precheck-resolved")]


def test_precheck_baseline_resolution_bumps_nothing(monkeypatch):
    """A candidate the CODE vocabulary resolves is never credited to a graph
    definition — the definition was not what allowed it to resolve."""
    doc = _doc()
    monkeypatch.setattr(store, "load_definitions", lambda: {doc["id"]: doc})
    recorder = _BumpRecorder()
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage", recorder)

    bumped = runtime_app._bump_metric_definition_precheck_usage(
        "What is the OEE rate for the plant?", "")

    assert bumped == 0
    assert recorder.calls == []


def test_precheck_unresolved_candidate_bumps_nothing(monkeypatch):
    """A term NO source resolves (the notice case) credits no definition."""
    doc = _doc()
    monkeypatch.setattr(store, "load_definitions", lambda: {doc["id"]: doc})
    recorder = _BumpRecorder()
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage", recorder)

    bumped = runtime_app._bump_metric_definition_precheck_usage(
        "What is the QZX rate for the plant?", "")

    assert bumped == 0
    assert recorder.calls == []


def test_precheck_without_candidates_does_no_store_work(monkeypatch):
    """An ordinary turn (nothing metric-shaped) never touches the store."""
    loads = {"count": 0}

    def _counting_load():
        loads["count"] += 1
        return {}

    monkeypatch.setattr(store, "load_definitions", _counting_load)
    recorder = _BumpRecorder()
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage", recorder)

    bumped = runtime_app._bump_metric_definition_precheck_usage(
        "Good morning, how are you?", "")

    assert bumped == 0
    assert loads["count"] == 0
    assert recorder.calls == []


def test_precheck_ledger_failure_never_raises(monkeypatch):
    doc = _doc()
    monkeypatch.setattr(store, "load_definitions", lambda: {doc["id"]: doc})
    monkeypatch.setattr(
        artifact_usage, "bump_artifact_usage", _BumpRecorder(raise_error=True))

    assert runtime_app._bump_metric_definition_precheck_usage(
        _LU_QUESTION, "") == 0


def test_precheck_store_failure_never_raises(monkeypatch):
    monkeypatch.setattr(
        store, "load_definitions",
        lambda: (_ for _ in ()).throw(RuntimeError("graph outage")))
    recorder = _BumpRecorder()
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage", recorder)

    assert runtime_app._bump_metric_definition_precheck_usage(
        _LU_QUESTION, "") == 0
    assert recorder.calls == []


def test_compose_seeded_turn_stays_byte_identical_while_bumping(monkeypatch):
    """The bump is a ledger side-effect ONLY: the resolved term suppresses the
    notice and the composition is the pre-telemetry byte form."""
    doc = _doc()
    monkeypatch.setattr(store, "load_definitions", lambda: {doc["id"]: doc})
    recorder = _BumpRecorder()
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage", recorder)

    composed = runtime_app._compose_seeded_turn("ROUTE-SEED", _LU_QUESTION)

    assert composed == f"ROUTE-SEED\n\n---\n\n{_LU_QUESTION}"
    assert recorder.calls == [
        ({("metric_definition", "line-utilization")},
         "term-precheck-resolved")]


# --- Never-obedience (ADR-0063 D7 / ADR-0066 D6) -----------------------------

def test_evidence_values_are_exact_and_never_claim_obedience():
    """Evidence names DELIVERY events only. No value may imply the model
    followed, obeyed, or applied a definition — that claim is unprovable."""
    assert mr._GRAPH_DEFINITION_RESOLVE_EVIDENCE == "metric-resolve"
    assert runtime_app._TERM_PRECHECK_RESOLVED_EVIDENCE == "term-precheck-resolved"
    assert runtime_app._METRIC_TURN_COMPOSED_EVIDENCE == "metric-turn-composed"
    forbidden = ("follow", "obey", "used", "adhere", "compli", "honor",
                 "applied", "respect")
    for evidence in (mr._GRAPH_DEFINITION_RESOLVE_EVIDENCE,
                     runtime_app._TERM_PRECHECK_RESOLVED_EVIDENCE,
                     runtime_app._METRIC_TURN_COMPOSED_EVIDENCE):
        lowered = evidence.lower()
        assert not any(word in lowered for word in forbidden), evidence
