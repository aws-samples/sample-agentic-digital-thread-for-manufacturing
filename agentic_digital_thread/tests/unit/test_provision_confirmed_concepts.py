# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Checkpoint-confirmed concepts win at provision (workshop test event 13).

A map row carries the LLM's nested ``mappings`` role list AND the ``concepts``
list the Confirm Mappings checkpoint edits. Provision read ``mappings`` first,
so a steward's correction was silently written back as the LLM's concept
(``assets.asset_num -> work-order`` survived the attendee's fix to
``equipment-id``). Pins ``_confirmed_role_mappings`` and the ``_dp_field_row``
edge writes. Offline — ``neptune_write`` is recorded.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))
os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105 - offline fixture

import agents.onboard as onboard  # noqa: E402

_LLM_ROW = {
    "field": "assets.asset_num", "table": "assets",
    "concept": "work-order", "confidence": 0.82,
    "mappings": [{"concept": "work-order", "role": "identifier", "confidence": 0.82}],
    "concepts": [{"concept": "work-order", "confidence": 0.82}],
}


def test_untouched_row_reproduces_the_role_list_exactly():
    assert onboard._confirmed_role_mappings(dict(_LLM_ROW)) == _LLM_ROW["mappings"]


def test_corrected_row_writes_the_confirmed_concept_with_the_primary_role():
    row = {**_LLM_ROW, "concept": "equipment-id", "confidence": 0.5,
           "concepts": [{"concept": "equipment-id", "confidence": 0.5}]}
    assert onboard._confirmed_role_mappings(row) == [
        {"concept": "equipment-id", "confidence": 0.5, "role": "identifier"}]


def test_kept_concept_retains_its_own_role_and_added_one_inherits_primary():
    row = {"field": "t.c", "table": "t", "concept": "a", "confidence": 0.9,
           "mappings": [{"concept": "a", "role": "identifier", "confidence": 0.9},
                        {"concept": "b", "role": "measure", "confidence": 0.6}],
           "concepts": [{"concept": "b", "confidence": 0.6}, {"concept": "z", "confidence": 0.5}]}
    assert onboard._confirmed_role_mappings(row) == [
        {"concept": "b", "confidence": 0.6, "role": "measure"},
        {"concept": "z", "confidence": 0.5, "role": "identifier"}]


def test_skipped_row_writes_no_edge_but_legacy_row_keeps_role_list():
    skipped = {**_LLM_ROW, "concept": "needs_review", "confidence": 0, "concepts": []}
    assert onboard._confirmed_role_mappings(skipped) == []
    legacy = {k: v for k, v in _LLM_ROW.items() if k != "concepts"}
    assert onboard._confirmed_role_mappings(legacy) == _LLM_ROW["mappings"]


def _ctx(monkeypatch, writes):
    monkeypatch.setattr(onboard, "neptune_write", lambda q, p=None: writes.append((q, p)) or True)
    return onboard._ProvisionState(
        body=None, system_id="cmms-ind", system_name="CMMS", plant="P", schema="s",
        rules=[], mappings=[], connection_type="PostgreSQL", cm_items=[],
        committable={"work-order", "equipment-id"})


def _edges(writes):
    return [(p["cid"], p["conf"], p["role"]) for q, p in writes if "MERGE (f)-[r:MAPS_TO]->(c)" in q]


def test_dp_field_row_writes_the_corrected_concept_not_the_mappers(monkeypatch):
    writes = []
    ctx = _ctx(monkeypatch, writes)
    row = {**_LLM_ROW, "concept": "equipment-id", "confidence": 0.5,
           "concepts": [{"concept": "equipment-id", "confidence": 0.5}]}
    onboard._dp_field_row(ctx, row)
    assert _edges(writes) == [("equipment-id", 0.5, "identifier")]
    assert ctx.unmapped_columns == []


def test_dp_field_row_untouched_row_is_byte_identical(monkeypatch):
    writes = []
    onboard._dp_field_row(_ctx(monkeypatch, writes), dict(_LLM_ROW))
    assert _edges(writes) == [("work-order", 0.82, "identifier")]
