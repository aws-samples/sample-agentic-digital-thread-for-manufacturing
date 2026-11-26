# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Provision-time guidance precedence (base-mode generic connectors).

At provision the System node's query_guidance/capabilities/tool_prefix are
resolved with a durability-aware precedence so a connect-registered system
carries correct orchestrator guidance without relying solely on per-replica
in-memory catalog state:

  1. explicit registration guidance on the catalog entry;
  2. guidance already persisted on the durable System node (survives a catalog
     rehydrate / re-provision — plan risk #4);
  3. the generic per-connection-TYPE default (never a hardcoded demo id).

Pure helper — no I/O, no AWS, no graph.
"""
from __future__ import annotations

from agents.onboard import _resolve_provision_guidance
from agents.connection_types import default_query_guidance, SITEWISE_DIRECT, ATHENA


def test_explicit_entry_guidance_wins():
    qg, caps, tp = _resolve_provision_guidance(
        {"query_guidance": "EXPLICIT", "capabilities": "CAPS", "tool_prefix": "tp1"},
        {"query_guidance": "NODE", "capabilities": "NC", "tool_prefix": "tp2"},
        SITEWISE_DIRECT, "sys-a",
    )
    assert qg == "EXPLICIT"
    assert caps == "CAPS"
    assert tp == "tp1"


def test_durable_node_guidance_used_when_entry_blank():
    """No catalog guidance (e.g. rehydrated/other-replica) -> durable node guidance."""
    qg, caps, tp = _resolve_provision_guidance(
        {},  # empty catalog entry
        {"query_guidance": "NODE_G", "capabilities": "NODE_C", "tool_prefix": "node-tp"},
        SITEWISE_DIRECT, "sys-b",
    )
    assert qg == "NODE_G"
    assert caps == "NODE_C"
    assert tp == "node-tp"


def test_generic_default_when_nothing_registered():
    """Neither catalog nor node guidance -> generic per-connection-type default."""
    qg, caps, tp = _resolve_provision_guidance({}, {}, SITEWISE_DIRECT, "sys-c")
    assert qg == default_query_guidance(SITEWISE_DIRECT)
    assert "sitewise_list_asset_models" in qg  # correct usage guidance
    assert caps  # generic capabilities present
    assert tp == "sys-c"  # tool_prefix defaults to system_id


def test_athena_generic_default_applies_generically():
    qg, _caps, tp = _resolve_provision_guidance({}, {}, ATHENA, "example-athena")
    assert qg == default_query_guidance(ATHENA)
    assert tp == "example-athena"
    # Generic — no demo/dataset literals.
    assert "example-workgroup" not in qg and "example_records_raw" not in qg


def test_refreshed_base_preserves_all_steward_notes_and_reports_overage(caplog):
    from agents.guidance_scope import parse_document, serialize_document

    long_hint = "keep query hint " + "h" * 4000
    durable = serialize_document({
        "kind": "adt.guidance", "version": 1, "base": "old base",
        "query_hints": [long_hint],
        "answer_requirements": ["keep answer requirement"],
        "legacy_unscoped": ["keep legacy note"],
    })
    refreshed_base = "B" * 3990

    with caplog.at_level("WARNING"):
        guidance, _caps, _prefix = _resolve_provision_guidance(
            {"query_guidance": refreshed_base},
            {"query_guidance": durable},
            SITEWISE_DIRECT, "sys-over-budget",
        )

    document = parse_document(guidance)
    assert document["base"] == refreshed_base
    assert document["query_hints"] == [long_hint]
    assert document["answer_requirements"] == ["keep answer requirement"]
    assert document["legacy_unscoped"] == ["keep legacy note"]
    assert len(guidance) > 8000  # report-only: never truncate the base or notes
    assert "GUIDANCE_BUDGET_OVERAGE" in caplog.text
    assert "notes_preserved=true" in caplog.text
    assert "keep query hint" not in caplog.text  # payload-safe telemetry only


def test_a_long_refreshed_base_alone_is_not_an_overage(caplog):
    """The stored cap counts steward notes, never the generic base."""
    from agents.guidance_scope import parse_document, serialize_document

    durable = serialize_document({
        "kind": "adt.guidance", "version": 1, "base": "old base",
        "query_hints": ["keep query hint"], "answer_requirements": [],
        "legacy_unscoped": [],
    })
    with caplog.at_level("WARNING"):
        guidance, _caps, _prefix = _resolve_provision_guidance(
            {"query_guidance": "B" * 3990}, {"query_guidance": durable},
            SITEWISE_DIRECT, "sys-long-base",
        )
    assert parse_document(guidance)["query_hints"] == ["keep query hint"]
    assert len(guidance) > 4000
    assert "GUIDANCE_BUDGET_OVERAGE" not in caplog.text


def test_base_refresh_never_resurrects_a_pruned_note():
    """A steward-PRUNED note stays pruned across a base refresh.

    The catalog entry carries a per-replica MIRROR of the notes. Unioning it back
    in meant the next re-provision RESURRECTED a note the steward had pruned on
    the durable node — silently undoing a steward decision. Notes come from the
    durable document alone; surviving notes are preserved byte-for-byte.
    """
    from agents.guidance_scope import parse_document, serialize_document

    pruned = "pruned by the steward"
    survivor = "surviving steward hint"
    answer_survivor = "surviving answer requirement"
    # The catalog mirror still carries the pruned note (and the survivors).
    entry_guidance = serialize_document({
        "kind": "adt.guidance", "version": 1, "base": "new base",
        "query_hints": [survivor, pruned],
        "answer_requirements": [answer_survivor],
        "legacy_unscoped": ["pruned legacy note"],
    })
    # The durable node is the authority: the steward removed two notes there.
    durable = serialize_document({
        "kind": "adt.guidance", "version": 1, "base": "old base",
        "query_hints": [survivor],
        "answer_requirements": [answer_survivor],
        "legacy_unscoped": [],
    })

    guidance, _caps, _prefix = _resolve_provision_guidance(
        {"query_guidance": entry_guidance}, {"query_guidance": durable},
        SITEWISE_DIRECT, "sys-pruned")

    document = parse_document(guidance)
    assert pruned not in guidance          # never resurrected, anywhere
    assert "pruned legacy note" not in guidance
    assert document["query_hints"] == [survivor]          # byte-for-byte
    assert document["answer_requirements"] == [answer_survivor]
    assert document["legacy_unscoped"] == []
    assert document["base"] == "new base"  # only the code-derived base refreshes


def test_base_refresh_drops_mirror_notes_when_the_steward_pruned_them_all():
    """The all-pruned case must not fall back to the mirror either."""
    from agents.guidance_scope import parse_document, serialize_document

    entry_guidance = serialize_document({
        "kind": "adt.guidance", "version": 1, "base": "new base",
        "query_hints": ["pruned note"], "answer_requirements": [],
        "legacy_unscoped": [],
    })
    durable = serialize_document({
        "kind": "adt.guidance", "version": 1, "base": "old base",
        "query_hints": [], "answer_requirements": [], "legacy_unscoped": [],
    })

    guidance, _caps, _prefix = _resolve_provision_guidance(
        {"query_guidance": entry_guidance}, {"query_guidance": durable},
        SITEWISE_DIRECT, "sys-all-pruned")

    document = parse_document(guidance)
    assert "pruned note" not in guidance
    assert document["query_hints"] == []
    assert document["base"] == "new base"


def test_plain_text_base_refresh_stays_byte_identical():
    """No notes on either side ⇒ the entry is returned verbatim (no v1 rewrite)."""
    guidance, _caps, _prefix = _resolve_provision_guidance(
        {"query_guidance": "PLAIN BASE"}, {"query_guidance": "OLD PLAIN BASE"},
        SITEWISE_DIRECT, "sys-plain")
    assert guidance == "PLAIN BASE"
