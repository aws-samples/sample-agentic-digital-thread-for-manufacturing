# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Cross-module boundary pins for graph-backed metric definitions (ADR-0066).

These tests intentionally overlap narrower unit suites at the architecture seams:
structured definitions never become prompt text, the steward lifecycle cannot be
bypassed, code definitions keep precedence, and deployment vocabulary never
leaks into the neutral engine modules.
"""
from __future__ import annotations

from collections.abc import Mapping
import dataclasses
import enum
import hashlib
import json
from pathlib import Path
import re

import pytest

import agents.graph_metric_layer as graph_metric_layer
import agents.metric_definition as metric_definition
import agents.metric_definition_store as metric_definition_store
import agents.metric_layer as metric_layer
import agents.self_learning as self_learning
import agents.vocabulary_routes as vocabulary_routes
from tests.unit.test_engine_domain_neutrality import DENYLIST


_NEW_MODULES = (
    metric_definition,
    metric_definition_store,
    graph_metric_layer,
)
_CODE_REGISTRY_COUNT = 23
_CODE_REGISTRY_SHA256 = (
    "1bdd44952cb95bdae951c5dc8ed434fd0a14b65d6d285a39d48dfedb9ab1cbc1"
)


def _direct_doc(metric_id: str = "neutral-rate") -> dict:
    return metric_definition.parse_definition({
        "id": metric_id,
        "kind": "direct",
        "label": "Neutral Rate",
        "synonyms": ["NR"],
        "unit": "units",
        "concept": "mro-inventory",
        "aggregation": "sum",
    })


def _json_value(value):
    if dataclasses.is_dataclass(value):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _registry_bytes(layer) -> bytes:
    return json.dumps(
        [_json_value(metric) for metric in layer.list_metrics()],
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class _AcceptTable:
    def __init__(self, item: dict) -> None:
        self.item = item

    def query(self, **_kwargs) -> dict:
        return {"Items": [self.item]}


@pytest.fixture(autouse=True)
def _reset_metric_definition_state():
    metric_definition_store.invalidate()
    metric_layer.get_metric_layer.cache_clear()
    yield
    metric_definition_store.invalidate()
    metric_layer.get_metric_layer.cache_clear()


def test_accept_writes_structured_node_and_zero_query_guidance(monkeypatch):
    doc = _direct_doc()
    canonical = metric_definition.canonical_json(doc)
    item = {
        "mapping_id": "metric-proposal-neutral",
        "discovered_at": "2026-09-04T00:00:00+00:00",
        "status": "pending",
        "proposed_type": "metric_definition",
        "subject_key": "metric_definition|neutral-rate",
        "version": "1",
        "query_context": canonical,
    }
    writes: list[tuple[str, dict]] = []
    statuses: list[tuple] = []
    code_ids = frozenset(
        metric.id for metric in metric_layer.CodeMetricLayer().list_metrics())

    monkeypatch.setattr(self_learning, "_get_table", lambda: _AcceptTable(item))
    monkeypatch.setattr(
        self_learning, "_metric_definition_context",
        lambda: (frozenset({"mro-inventory"}), code_ids, code_ids, {}),
    )
    monkeypatch.setattr(
        metric_definition_store, "neptune_write",
        lambda cypher, parameters=None: writes.append(
            (cypher, dict(parameters or {}))) or True,
    )
    monkeypatch.setattr(
        metric_definition_store, "neptune_query",
        lambda *_args, **_kwargs: [{"definition_json": canonical}],
    )
    monkeypatch.setattr(
        self_learning, "_neptune_write",
        lambda *_args, **_kwargs: pytest.fail(
            "metric-definition accept reached a query-guidance writer"),
    )
    monkeypatch.setattr(
        self_learning, "_update_status",
        lambda *args, **kwargs: statuses.append((args, kwargs)),
    )
    monkeypatch.setattr(self_learning, "_reset_accepted_coverage_cache", lambda: None)
    monkeypatch.setattr(metric_layer, "invalidate_metric_layer", lambda: None)

    result = self_learning.accept_mapping(
        "metric-proposal-neutral", actor="steward")

    assert result["status"] == "accepted"
    assert statuses and statuses[0][0][3] == "accepted"
    assert len(writes) == 1
    cypher, parameters = writes[0]
    assert "MERGE (m:MetricDefinition" in cypher
    assert parameters["definition_json"] == canonical
    assert "query_guidance" not in (cypher + json.dumps(parameters))


def test_new_modules_never_import_the_guidance_budget():
    for module in _NEW_MODULES:
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "MAX_GUIDANCE_LEN" not in source, module.__name__


def test_metric_definitions_and_conversation_are_tuple_deny_members():
    assert isinstance(self_learning._NEVER_AUTO_ACCEPT_TYPES, tuple)
    assert isinstance(self_learning._NEVER_AUTO_ACCEPT_SOURCES, tuple)
    assert "metric_definition" in self_learning._NEVER_AUTO_ACCEPT_TYPES
    assert "conversation" in self_learning._NEVER_AUTO_ACCEPT_SOURCES


def test_no_graph_store_keeps_exact_23_metric_code_registry(monkeypatch):
    def absent_store():
        raise ImportError("graph metric-definition store absent")

    monkeypatch.setattr(graph_metric_layer, "load_definitions", absent_store)
    composed = graph_metric_layer.ComposedMetricLayer()
    code = metric_layer.CodeMetricLayer()

    assert len(composed.list_metrics()) == _CODE_REGISTRY_COUNT
    assert [metric.id for metric in composed.list_metrics()] == [
        metric.id for metric in code.list_metrics()]
    assert hashlib.sha256(_registry_bytes(composed)).hexdigest() == \
        _CODE_REGISTRY_SHA256
    assert _registry_bytes(composed) == _registry_bytes(code)


def test_code_id_collision_is_rejected_at_propose_and_accept(monkeypatch):
    colliding = _direct_doc("oee")
    code_ids = frozenset(
        metric.id for metric in metric_layer.CodeMetricLayer().list_metrics())
    context = (frozenset({"mro-inventory"}), code_ids, code_ids, {})
    monkeypatch.setattr(self_learning, "_metric_definition_context", lambda: context)
    monkeypatch.setattr(
        self_learning, "log_proposed_mapping",
        lambda **_kwargs: pytest.fail("colliding proposal was filed"),
    )

    with pytest.raises(metric_definition.MetricDefinitionError, match="id"):
        vocabulary_routes._parse_validate_and_file_metric(colliding)

    item = {
        "mapping_id": "metric-proposal-collision",
        "discovered_at": "2026-09-04T00:00:00+00:00",
        "status": "pending",
        "proposed_type": "metric_definition",
        "subject_key": "metric_definition|oee",
        "version": "1",
        "query_context": metric_definition.canonical_json(colliding),
    }
    monkeypatch.setattr(self_learning, "_get_table", lambda: _AcceptTable(item))
    monkeypatch.setattr(
        metric_definition_store, "write_definition",
        lambda *_args, **_kwargs: pytest.fail("colliding definition was written"),
    )
    monkeypatch.setattr(
        self_learning, "_update_status",
        lambda *_args, **_kwargs: pytest.fail("colliding proposal was accepted"),
    )

    with pytest.raises(ValueError, match="id"):
        self_learning.accept_mapping("metric-proposal-collision", actor="steward")
    assert item["status"] == "pending"


def test_code_id_collision_is_shadowed_never_served_or_listed_accepted(
        monkeypatch):
    colliding = _direct_doc("oee")
    row = {
        "id": "oee",
        "definition_json": metric_definition.canonical_json(colliding),
    }
    monkeypatch.setattr(
        metric_definition_store, "neptune_query",
        lambda *_args, **_kwargs: [row],
    )
    monkeypatch.setattr(self_learning, "_query_by_status", lambda *_args, **_kwargs: [])

    layer = graph_metric_layer.ComposedMetricLayer()
    listed_ids = [metric.id for metric in layer.list_metrics()]
    rows = vocabulary_routes._read_metric_rows()
    collision_rows = [entry for entry in rows if entry["id"] == "oee"]

    assert listed_ids.count("oee") == 1
    assert layer.get_metric("oee") is metric_layer.CodeMetricLayer().get_metric("oee")
    assert layer.is_graph_defined("oee") is False
    assert {entry["status"] for entry in collision_rows} == {"code", "shadowed"}
    assert not [entry for entry in collision_rows if entry["status"] == "accepted"]
    shadowed = [entry for entry in collision_rows if entry["status"] == "shadowed"]
    assert len(shadowed) == 1
    assert shadowed[0]["reason"] == "code-id collision"


def test_new_metric_definition_modules_are_engine_neutral():
    violations: list[str] = []
    for module in _NEW_MODULES:
        path = Path(module.__file__)
        source = path.read_text(encoding="utf-8")
        for family, pattern in DENYLIST.items():
            for match in re.finditer(pattern, source):
                violations.append(
                    f"{path.name}:{family}:{match.group(0)!r}")
    assert violations == []
