# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""guidance_composition telemetry must not crowd routing rules out of the seed."""
from __future__ import annotations

import json

import agents.orchestrator as orch
from agents import query_intelligence

_EMPTY_COMPOSITION = {
    "scope_counts": {"answer_requirement": 0, "legacy_unscoped": 0, "query_hint": 0},
    "scope_digests": {"answer_requirement": [], "legacy_unscoped": [], "query_hint": []},
    "delivery_truncation": {
        "base_chars_dropped": 346, "chars_dropped": 346, "dropped_digests": [],
        "field_cap": 13, "fields": ["query_guidance"], "notes_dropped": 0,
        "notes_total": 0, "notes_truncated": 0, "truncated": True,
        "scope_notes_dropped": {"answer_requirement": 0, "legacy_unscoped": 0, "query_hint": 0},
    },
}


def test_compaction_keeps_only_informative_leaves():
    assert orch._compact_composition(_EMPTY_COMPOSITION) == {"delivery_truncation": {
        "base_chars_dropped": 346, "chars_dropped": 346, "field_cap": 13,
        "fields": ["query_guidance"], "truncated": True}}


def test_serialized_seed_compacts_composition_but_not_other_keys():
    payload = {"systems": [{"id": "a", "guidance_composition": _EMPTY_COMPOSITION,
                            "rules": {"join_paths": [{"id": "jp-1"}]},
                            "shared_concepts": [{"concept": "c", "peer_system": "b"}]}]}
    encoded = json.loads(orch._encode_route_seed(payload))
    system = encoded["systems"][0]
    # The only informative leaf was the drop report, which the serialized seed
    # carries in its ONE payload-level aggregate instead.
    assert "guidance_composition" not in system
    assert encoded["guidance_delivery_truncation"]["systems"] == {"a": [346, 0, 0, "q"]}
    assert system["rules"] == {"join_paths": [{"id": "jp-1"}]}  # untouched
    assert system["shared_concepts"] == [{"concept": "c", "peer_system": "b"}]


def test_compacted_composition_reads_back_the_same_telemetry():
    full = [{"id": "a", "guidance_composition": _EMPTY_COMPOSITION}]
    encoded = json.loads(orch._encode_route_seed({"systems": full}))
    compact = encoded["systems"]
    assert (query_intelligence.build_composed_context_from_metadata(
                compact, encoded.get("guidance_delivery_truncation"))
            == query_intelligence.build_composed_context_from_metadata(full))


def test_all_zero_scaffolding_shrinks_by_more_than_half():
    """Live 2026-09-27: 8 systems x ~465 chars of zero-valued scaffolding took
    3,724 of 7,300 seed chars and forced every routing rule out."""
    full = len(json.dumps(_EMPTY_COMPOSITION, separators=(",", ":")))
    compact = len(json.dumps(orch._compact_composition(_EMPTY_COMPOSITION),
                             separators=(",", ":")))
    assert compact * 2 < full
