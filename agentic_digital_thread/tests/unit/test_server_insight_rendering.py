# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests: ``/api/proposed-mappings`` serves ``insight`` proposals as findings.

An ``insight`` is a NARRATIVE finding — informational context only, never a graph
rule. The route must serve it scored by the shared scorer (``ai_confidence ==
'low'``, a "Finding" recommendation) and must NEVER give it a schema verdict or
trigger a schema load for it (``insight`` is not in ``SCHEMA_CHECKED_TYPES``).

Conventions follow ``test_proposed_mappings_enrichment.py``: the endpoint
coroutine is called directly with ``self_learning.get_all_mappings`` stubbed to
in-memory rows and the graph/schema seams monkeypatched — no AWS, no Neptune,
no network.
"""
from __future__ import annotations

import asyncio

import agents.concept_layer as concept_layer
import agents.schema_check as schema_check
import agents.self_learning as self_learning
from agents.server import get_proposed_mappings_route


def _insight_row(**overrides):
    row = {
        "mapping_id": "map-insight-1",
        "proposed_type": "insight",
        "from_system": "erp",
        "to_system": "mes-mil",
        "from_field": "",
        "to_field": "",
        "concept": "correlation",
        "query_context": "Auto-analysis: Operator IDs in MES correlate with ERP work orders",
        "confidence": "0.5",
        "status": "pending",
    }
    row.update(overrides)
    return row


def _call(monkeypatch, rows):
    monkeypatch.setattr(self_learning, "get_all_mappings", lambda: rows)
    # Insight concepts (e.g. 'correlation') are non-core → would trigger a graph
    # read; stub the batch resolver so the test stays offline.
    monkeypatch.setattr(
        concept_layer, "concept_descriptions", lambda ids, query=None: {}
    )
    return asyncio.run(get_proposed_mappings_route())


def test_insight_served_with_low_confidence_finding_recommendation(monkeypatch):
    body = _call(monkeypatch, [_insight_row()])
    assert body["count"] == 1
    m = body["mappings"][0]
    assert m["proposed_type"] == "insight"
    assert m["ai_confidence"] == "low"
    assert "Finding" in m["ai_recommendation"]
    assert "never becomes a graph rule" in m["ai_recommendation"]


def test_insight_never_gets_a_schema_verdict(monkeypatch):
    body = _call(monkeypatch, [_insight_row()])
    m = body["mappings"][0]
    assert "schema_verdict" not in m
    assert "schema_checked" not in m


def test_insight_never_triggers_a_schema_load(monkeypatch):
    """Insights have no table.column fields — the route must not load their
    system's schema (only SCHEMA_CHECKED_TYPES with a from_field do)."""
    calls: list[str] = []

    def _spy_load(sys_id):
        calls.append(sys_id)
        return None

    monkeypatch.setattr(schema_check, "load_system_schema", _spy_load)
    _call(monkeypatch, [_insight_row()])
    assert calls == []


def test_insight_low_confidence_survives_alongside_schema_checked_types(monkeypatch):
    """A verified value_mapping gets a high-confidence verdict; the insight in
    the same batch stays low with NO verdict (enrichment never leaks across
    items)."""
    monkeypatch.setattr(
        schema_check, "load_system_schema",
        lambda sys_id: {"work_orders": {"wo_id"}},
    )
    monkeypatch.setattr(
        schema_check, "column_in_schema", lambda schema, field: True
    )
    rows = [
        _insight_row(),
        {
            "mapping_id": "map-vm-1",
            "proposed_type": "value_mapping",
            "from_system": "erp",
            "from_field": "work_orders.wo_id",
            "to_system": "mes-mil",
            "to_field": "orders.order_id",
            "concept": "",
            "confidence": "0.9",
            "status": "pending",
        },
    ]
    body = _call(monkeypatch, rows)
    by_id = {m["mapping_id"]: m for m in body["mappings"]}
    insight = by_id["map-insight-1"]
    vm = by_id["map-vm-1"]
    assert insight["ai_confidence"] == "low"
    assert "schema_verdict" not in insight
    assert vm["schema_verdict"] == "verified"
    assert vm["ai_confidence"] == "high"


def test_insight_route_docstring_documents_the_type():
    """Plan tripwire: the route contract documents insight explicitly."""
    assert "insight" in (get_proposed_mappings_route.__doc__ or "")
