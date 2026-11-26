# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline contract tests for the ADR-0066 composed metric layer (C5).

No AWS, no graph, no DB, no LLM. The graph half is reached through two lazy
seams — ``graph_metric_layer.load_definitions`` (this layer's view of the store)
and ``metric_definition_store.neptune_query`` (the store's view of the graph) —
and both arms are exercised: the seam-level tests pin the layer's own policy, the
store-level tests pin the composition end to end through the REAL parse /
canonical-bytes / shadow-report code.

Fixture ids are neutral placeholders (ADR-0044): no customer or domain literals.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess  # nosec B404 - fixed local interpreter command only
import sys

import pytest

import agents.graph_metric_layer as gml
import agents.metric_definition as md
import agents.metric_definition_store as store
import agents.metric_layer as ml
import agents.metric_resolver as mr
from agents.metric_layer import (
    Aggregation, CodeMetricLayer, DerivedMetric, DirectMetric, MetricLayer,
    validate_metric_layer,
)

SEED_METRIC_COUNT = 23


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


def _doc(metric_id: str = "parts-on-hand-total", **overrides) -> dict:
    """A neutral DIRECT definition over a core concept, canonicalized."""
    doc = {
        "id": metric_id,
        "kind": "direct",
        "label": "Parts On Hand Total",
        "synonyms": ["POHT"],
        "unit": "units",
        "concept": "mro-inventory",
        "aggregation": "sum",
    }
    doc.update(overrides)
    return md.parse_definition(doc)


def _row(doc: dict) -> dict:
    return {"id": doc["id"], "definition_json": md.canonical_json(doc)}


def _sentinel_rows() -> list[dict]:
    """The store's successful-but-empty OPTIONAL MATCH sentinel."""
    return [{"id": None, "definition_json": None}]


class _FakeAuthority(mr.AuthorityResolver):
    def __init__(self, system_id: str | None) -> None:
        self._system_id = system_id

    def authoritative_system(self, concept: str) -> str | None:
        return self._system_id


# --- (1) Byte-identity of the code-only case --------------------------------

def test_code_only_composition_is_the_seed_registry_byte_identical(monkeypatch):
    monkeypatch.setattr(gml, "load_definitions", lambda: {})
    layer = gml.ComposedMetricLayer()
    code = CodeMetricLayer()

    composed_metrics, code_metrics = layer.list_metrics(), code.list_metrics()
    assert len(composed_metrics) == SEED_METRIC_COUNT
    assert composed_metrics == code_metrics                      # same values …
    assert all(a is b for a, b in zip(composed_metrics, code_metrics))  # … same objects
    assert [m.id for m in composed_metrics] == [m.id for m in code_metrics]
    assert layer.graph_metric_ids() == frozenset()
    # The unchanged validator still passes over the composed layer.
    assert validate_metric_layer(layer) is None
    # Inherited walks are untouched by composition.
    assert layer.metrics_for_concept("mro-inventory") == \
        code.metrics_for_concept("mro-inventory")


def test_get_metric_layer_returns_a_composed_code_metric_layer(monkeypatch):
    monkeypatch.setattr(gml, "load_definitions", lambda: {})
    layer = ml.get_metric_layer()
    assert isinstance(layer, MetricLayer)
    assert isinstance(layer, CodeMetricLayer)          # the unmodified pin holds
    assert isinstance(layer, gml.ComposedMetricLayer)
    assert ml.get_metric_layer() is layer              # still lru_cached
    assert len(layer.list_metrics()) == SEED_METRIC_COUNT


# --- (2) A graph definition resolves through the UNCHANGED resolve path ------

def test_graph_definition_is_served_by_get_metric_and_list_metrics(monkeypatch):
    doc = _doc()
    monkeypatch.setattr(gml, "load_definitions", lambda: {doc["id"]: doc})
    layer = ml.get_metric_layer()

    metric = layer.get_metric("parts-on-hand-total")
    assert isinstance(metric, DirectMetric)
    assert metric.concept == "mro-inventory"
    assert metric.aggregation is Aggregation.SUM
    assert metric.label == "Parts On Hand Total"
    ids = [m.id for m in layer.list_metrics()]
    assert len(ids) == SEED_METRIC_COUNT + 1
    assert ids[:SEED_METRIC_COUNT] == [m.id for m in CodeMetricLayer().list_metrics()]
    assert ids[-1] == "parts-on-hand-total"
    # The inherited reverse index and the telemetry seam see it too.
    assert "parts-on-hand-total" in layer.metrics_for_concept("mro-inventory")
    assert layer.is_graph_defined("parts-on-hand-total") is True
    assert layer.is_graph_defined("oee") is False
    assert layer.is_satisfiable(metric, frozenset({"mro-inventory"})) is True
    assert layer.missing_concepts(metric, frozenset()) == frozenset({"mro-inventory"})


def test_graph_metric_resolves_through_the_unchanged_resolve_metric_path(monkeypatch):
    """ZERO resolver edits: ``resolve_metric`` reaches the graph metric through
    ``get_metric_layer()`` and builds a normal plan + lineage."""
    from agents.catalog import catalog as live_catalog
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _demo_catalog import DEMO_SYSTEMS

    live_catalog._systems = [dict(system) for system in DEMO_SYSTEMS]
    try:
        doc = _doc()
        monkeypatch.setattr(gml, "load_definitions", lambda: {doc["id"]: doc})
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

        resolved = mr.resolve_metric(
            "parts-on-hand-total", authority=_FakeAuthority("cmms"))

        assert resolved.metric_id == "parts-on-hand-total"
        assert resolved.value == 18
        assert resolved.unit == "units"
        assert resolved.label == "Parts On Hand Total"
        assert resolved.system_id == "cmms"
        assert resolved.query_plan.schema == "cmms_maintenance"
        assert resolved.query_plan.table == "spare_parts"
        assert resolved.query_plan.aggregation is Aggregation.SUM
        assert resolved.lineage.metric_id == "parts-on-hand-total"
        assert resolved.lineage.concept == "mro-inventory"
    finally:
        live_catalog._systems = []


# --- (3) Collisions: skipped, warned, reported ------------------------------

def test_code_id_collision_is_skipped_and_warned(monkeypatch, caplog):
    """The read-time arm of C5: code wins, and the skip is never silent."""
    colliding = _doc("oee")
    monkeypatch.setattr(gml, "load_definitions", lambda: {"oee": colliding})
    layer = ml.get_metric_layer()

    with caplog.at_level("WARNING", logger=gml.__name__):
        metrics = layer.list_metrics()

    assert len(metrics) == SEED_METRIC_COUNT
    assert isinstance(layer.get_metric("oee"), DerivedMetric)   # the code metric
    assert layer.is_graph_defined("oee") is False
    assert layer.graph_metric_ids() == frozenset()
    warnings = [r for r in caplog.records
                if "shadowed by a code metric id" in r.getMessage()]
    assert len(warnings) == 1
    assert "oee" in warnings[0].getMessage()


def test_collision_is_excluded_by_the_store_and_reported_as_shadowed(monkeypatch):
    """End to end through the REAL store: the colliding row never resolves and
    always shows up in ``shadowed_or_invalid()``."""
    served, colliding = _doc(), _doc("oee")
    monkeypatch.setattr(
        store, "neptune_query",
        lambda cypher, parameters=None: [_row(served), _row(colliding)],
    )
    layer = ml.get_metric_layer()

    ids = [m.id for m in layer.list_metrics()]
    assert ids.count("oee") == 1
    assert isinstance(layer.get_metric("oee"), DerivedMetric)
    assert ids[-1] == "parts-on-hand-total"
    reported = {row["id"]: row["reason"] for row in store.shadowed_or_invalid()}
    assert reported == {"oee": "code-id collision"}


def test_unusable_graph_document_is_skipped_not_raised(monkeypatch, caplog):
    monkeypatch.setattr(
        gml, "load_definitions",
        lambda: {"broken-rate": {"id": "broken-rate", "kind": "direct"}},
    )
    layer = ml.get_metric_layer()

    with caplog.at_level("WARNING", logger=gml.__name__):
        assert len(layer.list_metrics()) == SEED_METRIC_COUNT

    assert [r for r in caplog.records if "is unusable" in r.getMessage()]
    with pytest.raises(KeyError):
        layer.get_metric("broken-rate")


# --- (4) Unknown ids keep the exact KeyError shape --------------------------

def test_unknown_metric_id_keeps_the_code_layer_key_error_shape(monkeypatch):
    monkeypatch.setattr(gml, "load_definitions", lambda: {_doc()["id"]: _doc()})
    layer = ml.get_metric_layer()

    with pytest.raises(KeyError) as composed:
        layer.get_metric("no-such-metric")
    with pytest.raises(KeyError) as code:
        CodeMetricLayer().get_metric("no-such-metric")

    assert composed.value.args == code.value.args
    assert composed.value.args[0] == "unknown metric id: 'no-such-metric'"
    assert composed.value.__cause__ is None            # chained shape unchanged

    with pytest.raises(mr.MetricResolverError) as resolver:
        mr.resolve_metric("no-such-metric")
    assert str(resolver.value) == "unknown metric id: 'no-such-metric'"


# --- (5) Fail OPEN to code-only ---------------------------------------------

def test_store_cold_load_failure_serves_code_metrics_only(monkeypatch, caplog):
    """The store RAISES on a cold load (ADR-0066 D4); the LAYER must not — every
    card, coverage scan and resolve rides it. A graph-defined id simply stops
    resolving (the honest KeyError), and nothing claims a term is undefined."""
    calls = 0

    def query(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr(store, "neptune_query", query)
    layer = ml.get_metric_layer()

    with caplog.at_level("WARNING", logger=gml.__name__):
        assert len(layer.list_metrics()) == SEED_METRIC_COUNT
        assert len(layer.list_metrics()) == SEED_METRIC_COUNT

    assert validate_metric_layer(layer) is None
    with pytest.raises(KeyError, match="unknown metric id"):
        layer.get_metric("parts-on-hand-total")
    # One attempt, then the cooldown: a failing graph is not re-queried per caller.
    assert calls == 1
    warnings = [r for r in caplog.records
                if "serving code metrics only" in r.getMessage()]
    assert len(warnings) == 1


def test_absent_graph_half_disables_composition_for_the_process(monkeypatch, caplog):
    calls = 0

    def missing_module():
        nonlocal calls
        calls += 1
        raise ImportError("no module named agents.metric_definition_store")

    monkeypatch.setattr(gml, "load_definitions", missing_module)
    layer = ml.get_metric_layer()

    with caplog.at_level("WARNING", logger=gml.__name__):
        assert len(layer.list_metrics()) == SEED_METRIC_COUNT
        assert len(layer.list_metrics()) == SEED_METRIC_COUNT

    assert calls == 1                       # ImportError disables permanently
    assert [r for r in caplog.records if "graph half unavailable" in r.getMessage()]


def test_get_metric_layer_falls_back_when_the_graph_module_is_absent(monkeypatch):
    """``get_metric_layer`` itself fails OPEN: no graph half ⇒ the code layer."""
    monkeypatch.setitem(sys.modules, "agents.graph_metric_layer", None)
    ml.get_metric_layer.cache_clear()
    layer = ml.get_metric_layer()

    assert type(layer) is CodeMetricLayer
    assert len(layer.list_metrics()) == SEED_METRIC_COUNT
    ml.invalidate_metric_layer()            # no-op on a code-only layer
    assert len(ml.get_metric_layer().list_metrics()) == SEED_METRIC_COUNT


# --- (6) A warm snapshot costs ZERO graph calls ------------------------------

def test_warm_snapshot_makes_no_further_graph_calls(monkeypatch):
    calls = 0

    def query(cypher, parameters=None):
        nonlocal calls
        calls += 1
        return [_row(_doc())]

    monkeypatch.setenv("METRIC_DEFINITION_TTL_SECONDS", "300")
    monkeypatch.setattr(store, "neptune_query", query)
    monkeypatch.setattr(store.time, "monotonic", lambda: 1000.0)
    layer = ml.get_metric_layer()

    assert len(layer.list_metrics()) == SEED_METRIC_COUNT + 1
    assert layer.get_metric("parts-on-hand-total").id == "parts-on-hand-total"
    assert len(layer.list_metrics()) == SEED_METRIC_COUNT + 1
    assert layer.metrics_for_concept("mro-inventory")
    assert layer.is_graph_defined("parts-on-hand-total") is True
    assert calls == 1


def test_invalidate_metric_layer_reloads_and_clears_the_failure_cooldown(monkeypatch):
    rows: list[list[dict]] = [[]]
    calls = 0

    def query(cypher, parameters=None):
        nonlocal calls
        calls += 1
        if not rows[0]:
            raise RuntimeError("graph unavailable")
        return rows[0]

    monkeypatch.setattr(store, "neptune_query", query)
    layer = ml.get_metric_layer()
    assert len(layer.list_metrics()) == SEED_METRIC_COUNT       # cold failure
    assert calls == 1

    rows[0] = [_row(_doc())]
    ml.invalidate_metric_layer()                                # re-arms both halves

    assert len(layer.list_metrics()) == SEED_METRIC_COUNT + 1
    assert calls == 2
    assert layer.graph_metric_ids() == frozenset({"parts-on-hand-total"})

    # A superseding definition becomes visible after the next invalidate, and the
    # per-snapshot concept memo does not carry the old closure.
    rows[0] = [_row(_doc(concept="quality-metric"))]
    ml.invalidate_metric_layer()
    assert layer.get_metric("parts-on-hand-total").concept == "quality-metric"
    assert "parts-on-hand-total" not in layer.metrics_for_concept("mro-inventory")
    assert "parts-on-hand-total" in layer.metrics_for_concept("quality-metric")


# --- (7) Import purity ------------------------------------------------------

def test_metric_layer_has_no_module_level_graph_import():
    source = Path(ml.__file__).read_text(encoding="utf-8")
    module_level = [
        line for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "graph_metric_layer" in line
    ]
    assert module_level == []
    assert "from .graph_metric_layer import ComposedMetricLayer" in source


def test_modules_import_without_graph_or_aws_dependencies():
    code = (
        "import sys\n"
        "for name in ('pg8000', 'agents.pgdriver', 'boto3', 'botocore'):\n"
        "    sys.modules[name] = None\n"
        "import agents.graph_metric_layer as gml\n"
        "import agents.metric_layer as ml\n"
        "layer = ml.get_metric_layer()\n"
        "assert isinstance(layer, gml.ComposedMetricLayer)\n"
        "assert len(layer.list_metrics()) == 23\n"
        "print('IMPORT-PURE-OK')\n"
    )
    adt_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(adt_root))
    result = subprocess.run(  # nosec B603 - fixed argv, no shell, local inputs only
        [sys.executable, "-c", code], cwd=adt_root, env=env,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "IMPORT-PURE-OK" in result.stdout


# --- ADR-0075: a graph definition with a filter composes into Predicates ----

def test_graph_definition_filter_composes_into_predicates(monkeypatch):
    from agents.metric_layer import Predicate, PredicateOp
    doc = _doc("filtered-parts-total", filter=[
        {"concept": "criticality", "op": "in", "values": ["HIGH", "CRITICAL"]}])
    monkeypatch.setattr(gml, "load_definitions", lambda: {doc["id"]: doc})
    layer = ml.get_metric_layer()
    metric = layer.get_metric("filtered-parts-total")
    assert metric.filter == (
        Predicate("criticality", PredicateOp.IN, ("CRITICAL", "HIGH")),)
    assert "filtered-parts-total" in layer.metrics_for_concept("criticality")
    assert layer.missing_concepts(metric, frozenset({"mro-inventory"})) == \
        frozenset({"criticality"})
