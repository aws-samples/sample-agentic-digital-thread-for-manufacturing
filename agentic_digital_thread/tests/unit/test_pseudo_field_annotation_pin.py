# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pseudo-field ``field_annotation`` behavior — PINNED, not changed (offline).

Found 2026-07-13: the Demo env accepted field_annotations
whose "field" was a TOOL PARAMETER, not a table.column — e.g.
``athena_run_query.byte_limit`` and
``sitewise_list_associated_assets.hierarchy_id``. These are structurally
indistinguishable from a real ``table.column`` reference (one identifier, one
dot, identifier segments), so TODAY's deterministic gates deliberately do NOT
reject them; the deployed schema-existence gate (3c) is the layer that can
definitively drop them (the "table" does not exist in any onboarded schema),
and scoring keeps them at review-tier. This file PINS that behavior so any
future change to it is a conscious decision:

  * ``is_table_column_shaped`` accepts the pseudo-field shape (True);
  * ``score_proposal`` gives such an annotation MEDIUM confidence
    ("verify column exists") — never high, so no auto-accept path admits it;
  * ingestion with an UNAVAILABLE schema (unit env / system without an
    onboarded schema) STORES the proposal pending — uncertainty never drops;
  * ingestion with an AVAILABLE schema that lacks the pseudo-table DROPS it
    definitively (gate 3c) — the deployed-env defense.

No AWS / Neptune / LLM / network.
"""
from __future__ import annotations

import json

import pytest

import agents.catalog as catalog_mod
import agents.graph_tools as graph_tools
import agents.self_learning as self_learning
from agents.proposal_scoring import is_table_column_shaped, score_proposal

try:
    from unit._demo_catalog import DEMO_SYSTEMS
except ImportError:  # pragma: no cover - depends on pytest rootdir
    from _demo_catalog import DEMO_SYSTEMS


PSEUDO_FIELDS = [
    "athena_run_query.byte_limit",
    "sitewise_list_associated_assets.hierarchy_id",
]


# ===========================================================================
# Shape predicate: pseudo-fields ARE table.column-shaped (structural limit)
# ===========================================================================
@pytest.mark.parametrize("field", PSEUDO_FIELDS)
def test_pseudo_field_is_table_column_shaped(field):
    # A tool-param reference is structurally one dotted identifier pair — the
    # pure shape predicate CANNOT tell it from a real column. Pinned True.
    assert is_table_column_shaped(field) is True


# ===========================================================================
# Scoring: review-tier (medium), never high — no auto path admits these
# ===========================================================================
@pytest.mark.parametrize("field", PSEUDO_FIELDS)
def test_pseudo_field_annotation_scores_medium_review(field):
    verdict = score_proposal({
        "proposed_type": "field_annotation",
        "from_field": field,
        "confidence": 0.9,
    })
    assert verdict["ai_confidence"] == "medium"
    assert "verify column exists" in verdict["ai_recommendation"]


# ===========================================================================
# Ingestion: schema unavailable → stored pending; schema present → 3c drops
# ===========================================================================
class FakeTable:
    def __init__(self) -> None:
        self._items: list[dict] = []

    def put_item(self, Item: dict) -> None:
        self._items.append(dict(Item))

    def query(self, **kwargs) -> dict:
        vals = kwargs.get("ExpressionAttributeValues", {})
        idx = kwargs.get("IndexName")
        if idx == "subject-key-index":
            return {"Items": [i for i in self._items
                              if i.get("subject_key") == vals.get(":sk")]}
        if idx == "status-index":
            return {"Items": [i for i in self._items
                              if i.get("status") == vals.get(":status")]}
        return {"Items": [i for i in self._items
                          if i.get("mapping_id") == vals.get(":mid")]}

    def update_item(self, **kwargs) -> None:
        pass

    def all_items(self) -> list[dict]:
        return [dict(i) for i in self._items]


@pytest.fixture
def fake_table(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    return fake


@pytest.fixture
def seeded_catalog(monkeypatch):
    monkeypatch.setattr(catalog_mod.catalog, "_systems",
                        [dict(s) for s in DEMO_SYSTEMS])
    return catalog_mod.catalog


def _log_pseudo_annotation(field):
    return self_learning.log_proposed_mapping(
        from_system="erp", from_field=field, from_value="",
        to_system="erp", to_field=field, to_value="",
        concept="schema-annotation", confidence=0.9,
        query_context="Auto-analysis: caps the bytes scanned per query run",
        proposed_type="field_annotation",
    )


@pytest.mark.parametrize("field", PSEUDO_FIELDS)
def test_pseudo_field_stored_pending_when_schema_unavailable(fake_table,
                                                             seeded_catalog,
                                                             field, monkeypatch):
    # Schema lookup unavailable (unit env) → indeterminate → never drop on
    # uncertainty. The proposal lands PENDING for human review.
    monkeypatch.setattr(graph_tools, "get_system_schema",
                        lambda sid: json.dumps({"error": "not onboarded"}))
    mid = _log_pseudo_annotation(field)
    assert mid is not None
    item = fake_table.all_items()[0]
    assert item["status"] == "pending"
    assert item["from_field"] == field


@pytest.mark.parametrize("field", PSEUDO_FIELDS)
def test_pseudo_field_dropped_by_schema_gate_when_schema_known(fake_table,
                                                               seeded_catalog,
                                                               field, monkeypatch):
    # With erp's onboarded schema available (and no pseudo-table in it), gate
    # 3c resolves the single-dot reference DEFINITIVELY missing → dropped.
    monkeypatch.setattr(graph_tools, "get_system_schema",
                        lambda sid: json.dumps(
                            {"work_orders": {"columns": ["id", "status"]}}))
    mid = _log_pseudo_annotation(field)
    assert mid is None
    assert fake_table.all_items() == []
