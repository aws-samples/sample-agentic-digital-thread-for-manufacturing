# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the ``/api/proposed-mappings`` concept-description enrichment.

Concept descriptions first-class everywhere (ADR 0021): every item in the endpoint's
``mappings[]`` now carries ``concept_description: str`` and ``concept_layer: str``. The
endpoint coroutine is called directly with ``self_learning.get_all_mappings`` stubbed to
in-memory rows — no AWS, no Neptune, no network. The cases chosen never require a graph
read (a minted proposal short-circuits on its ``to_description``; a core concept resolves
from ``CORE_CONCEPT_DEFINITIONS``).
"""
from __future__ import annotations

import asyncio

import agents.self_learning as self_learning
from agents.catalog import CORE_CONCEPT_DEFINITIONS
from agents.server import get_proposed_mappings_route


def _call(monkeypatch, rows):
    monkeypatch.setattr(self_learning, "get_all_mappings", lambda: rows)
    return asyncio.run(get_proposed_mappings_route())


def test_field_concept_mint_uses_its_authored_to_description(monkeypatch):
    rows = [{
        "proposed_type": "field_concept_mint",
        "concept": "lead-time-days",
        "to_field": "spare_parts.lead_time_days",
        "to_value": "Lead Time Days",
        "to_description": "Days between ordering a spare part and its receipt.",
        "confidence": "0.6",
    }]
    body = _call(monkeypatch, rows)
    m = body["mappings"][0]
    assert m["concept_description"] == "Days between ordering a spare part and its receipt."
    # Minted/dynamic concept renders as a field-layer concept.
    assert m["concept_layer"] == "field"


def test_concept_mapping_to_core_concept_uses_core_definition(monkeypatch):
    rows = [{
        "proposed_type": "concept_mapping",
        "concept": "mro-inventory",
        "to_field": "spare_parts.qty_on_hand",
        "confidence": "1.0",
    }]
    body = _call(monkeypatch, rows)
    m = body["mappings"][0]
    assert m["concept_description"] == CORE_CONCEPT_DEFINITIONS["mro-inventory"]
    assert m["concept_layer"] == "field"


def test_core_entity_concept_layer_is_entity(monkeypatch):
    rows = [{
        "proposed_type": "concept_mapping",
        "concept": "asset",
        "to_field": "assets.asset_id",
        "confidence": "1.0",
    }]
    body = _call(monkeypatch, rows)
    m = body["mappings"][0]
    assert m["concept_description"] == CORE_CONCEPT_DEFINITIONS["asset"]
    assert m["concept_layer"] == "entity"


def test_every_item_carries_the_new_keys(monkeypatch):
    rows = [
        {"proposed_type": "value_mapping", "concept": "", "confidence": "0.9",
         "from_value": "x", "to_value": "y"},
        {"proposed_type": "field_concept_mint", "concept": "lead-time-days",
         "to_description": "A definition.", "confidence": "0.6"},
    ]
    body = _call(monkeypatch, rows)
    for m in body["mappings"]:
        assert "concept_description" in m
        assert "concept_layer" in m
    # Empty concept → empty description + empty layer (no graph read).
    vm = body["mappings"][0]
    assert vm["concept_description"] == ""
    assert vm["concept_layer"] == ""
