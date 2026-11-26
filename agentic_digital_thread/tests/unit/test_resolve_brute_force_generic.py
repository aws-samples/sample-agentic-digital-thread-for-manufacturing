# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Threads brute-force resolution without demo literals.

Before: when the graph held no pattern rules the fallback stripped a
BUILT-IN list of demo prefixes, and every learn proposal claimed the source
field was ``batch_number``. Now the searchable core comes from the identifier
itself (graph-known prefixes, else its longest digit run) and the proposal's
source field is read from the graph — no field, no proposal.
"""
import os
import sys
from pathlib import Path

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))
os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.resolve as resolve  # noqa: E402


def test_identifier_core_prefers_a_graph_known_prefix():
    assert resolve._identifier_core("X-1234", {"X-", "LOT-"}) == "1234"
    assert resolve._identifier_core("lot-0099", {"LOT-"}) == "0099"  # prefix match is case-insensitive


def test_identifier_core_falls_back_to_the_longest_digit_run():
    assert resolve._identifier_core("PLANT-2026-01234", set()) == "1234"  # 01234 → 1234 (longer than 2026)
    assert resolve._identifier_core("X-1234", set()) == "1234"
    assert resolve._identifier_core("L3-M1", set()) == "L3-M1"  # nothing to strip


def test_known_pattern_prefixes_come_from_the_graph_only(monkeypatch):
    monkeypatch.setattr(resolve, "neptune_query", lambda *_a, **_k: [
        {"pat": "X-{id}"}, {"pat": "PLANT-2026-0{id}"}, {"pat": "*"}, {"pat": ""}, {"pat": None}])
    assert resolve._known_pattern_prefixes() == {"X-", "PLANT-2026-0"}
    monkeypatch.setattr(resolve, "neptune_query", lambda *_a, **_k: [])
    assert resolve._known_pattern_prefixes() == set()  # no built-in fallback list


def test_learn_proposal_reads_the_source_field_from_the_graph(monkeypatch):
    monkeypatch.setattr(resolve, "neptune_query", lambda *_a, **_k: [
        {"field_name": "runs.notes", "role": "correlate"},
        {"field_name": "runs.lot_ref", "role": "identifier"},
    ])
    p = resolve._learn_proposal("lot", "X-1234", "PLANT-2026-01234", "sys-a", "sys-b", "material_lot")
    assert p == {
        "source_pattern": "X-{id}", "target_pattern": "PLANT-2026-0{id}",
        "source_system": "sys-a", "source_field": "lot_ref",
        "target_system": "sys-b", "target_field": "material_lot",
        "concept": "lot", "confidence": 0.7,
        # The values it was learned from, for the learn route's reader check.
        "example_identifier": "X-1234", "example_matched_value": "PLANT-2026-01234",
    }


def test_learn_proposal_is_none_without_a_graph_field_or_pattern(monkeypatch):
    monkeypatch.setattr(resolve, "neptune_query", lambda *_a, **_k: [])
    assert resolve._learn_proposal("lot", "X-1234", "PLANT-2026-01234", "sys-a", "sys-b", "f") is None
    monkeypatch.setattr(resolve, "neptune_query", lambda *_a, **_k: [{"field_name": "f", "role": "identifier"}])
    assert resolve._learn_proposal("lot", "1234", "1234", "sys-a", "sys-b", "f") is None  # same format
    assert resolve._learn_proposal("lot", "X-1234", "Y-1234", "sys-a", "sys-a", "f") is None  # same system
