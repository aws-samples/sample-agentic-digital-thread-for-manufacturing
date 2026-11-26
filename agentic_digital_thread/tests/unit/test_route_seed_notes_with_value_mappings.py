# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A question that matches a concept note AND names an identifier with a
stored value mapping keeps its whole route seed.

The concept notes ride on top of the prompt budget (``_reinsert_concept_notes``)
and a seed whose tail carries the IDENTIFIER VALUE MAPPINGS block swaps in the
longer mapping-aware header (``_with_value_mapping_header``). The header swap
re-checks the seed's length; that check must allow the notes the same room
``_with_asset_coverage`` gave them, or a full seed with both is thrown away for
the emergency seed (no systems at all), the Knowledge Graph trace step shows
its generic sentence, and the model plans with no connected system listed.

Also covered here: the trace step for the emergency seed says what that seed
carried (how many connected systems it left out), and the ``planned_with``
footer counts the stored value mappings the seed carries in its block, not
only the per-system summaries. Synthetic systems, fields, values and concepts
only.
"""
from __future__ import annotations

import json

import pytest

import agents.concept_notes as cn
import agents.identifier_translation as it
import agents.orchestrator as orch
from agents import route_seed_trace as rst
from agents import runtime_app
from agents.catalog import REMOVING_SYSTEMS_READ

IDS = tuple(f"sys-{c}" for c in "abcdefgh")
QUESTION = "Was unit QX-101 serviced inside its upkeep window before run RX-20260101-0001 ran?"
CONCEPTS = (
    cn.Concept(id="upkeep-window", name="upkeep window",
               description=("The interval in which a planned upkeep task is due. A task done "
                            "after its window closes counts as late, and a task moved to a "
                            "later window still counts against the window it was first due "
                            "in. ") * 3,
               systems=("sys-a",)),
    cn.Concept(id="service-run", name="service run",
               description="One pass of a unit through its line, from start to finish.",
               systems=("sys-b",)),
)
_VM_READ = "\nMATCH (vm:ValueMapping)\nWHERE "


def _vm(vid, fs, fv, ts, tv, ff="units.code", tf="units.tag"):
    return {"id": f"vm-equip-{vid}", "from_system": fs, "from_field": ff, "from_value": fv,
            "to_system": ts, "to_field": tf, "to_value": tv, "concept": "equipment",
            "method": "value_format_match", "confidence": 0.95, "provenance": None}


MAPPINGS = [_vm("1", "sys-a", "QX-101", "sys-b", "77-QX-101"),
            _vm("2", "sys-a", "QX-101", "sys-c", "Q-101", tf="assets.name")]


def _inventory(prose: str = "Routing note. " * 30) -> list:
    return [{"system_id": sid, "system_name": f"System {sid[-1].upper()}", "plants": ["site-1"],
             "capabilities": f"{sid} records. {prose}", "connection_type": "PostgreSQL",
             "query_guidance": prose} for sid in IDS]


def _rule_rows(extra: int, summaries=()) -> list:
    rows = []
    for sid in IDS:
        rows.append({
            "system_id": sid, "mapped_concepts": ["equipment-id"],
            "shared_concepts": [{"concept": f"c-{k}", "peer_system": p}
                                for k in range(extra) for p in IDS if p != sid],
            "join_paths": [{"id": f"jp-{sid}-{k}", "from_system": sid, "from_field": f"t{k}.ref_id",
                            "to_system": sid, "to_field": f"t{k + 1}.id", "transform": "direct",
                            "method": "foreign_key_match", "confidence": 0.9} for k in range(extra)],
            "value_mappings": [dict(s) for s in summaries if s["from_system"] == sid],
            "identity_rules": []})
    return rows


class Graph:
    """The seed's reads. The value-mapping read returns the rows whose stored
    value equals one of ``$exact``/``$linked`` or ends with a ``$s<n>`` suffix."""

    def __init__(self, mappings=(), extra: int = 20, summaries=(), prose=None):
        self.mappings = list(mappings)
        self.inventory = _inventory() if prose is None else _inventory(prose)
        self.rules = _rule_rows(extra, summaries)

    def __call__(self, cypher, params=None):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return self.inventory
        if cypher == orch._ROUTE_SEED_CYPHER:
            return self.rules
        if cypher in (orch._ROUTE_SEED_AUTHORITY_CYPHER, REMOVING_SYSTEMS_READ):
            return []
        if _VM_READ in cypher:
            params = params or {}
            exact = params.get("exact") or params.get("linked") or []
            suffixes = [v for k, v in params.items() if k[:1] == "s" and k[1:].isdigit()]
            return [dict(vm) for vm in self.mappings
                    if any(v in exact or any(v.endswith(s) for s in suffixes)
                           for v in (vm["from_value"].lower(), vm["to_value"].lower()))]
        return []


def _split(seed: str) -> tuple[str, dict, str]:
    """``(header, payload, tail)`` of a composed seed."""
    marker = "ROUTE SEED RESULT:\n"
    head, body = seed.split(marker, 1)
    payload, end = json.JSONDecoder().raw_decode(body)
    return head + marker, payload, body[end:]


def _build(graph, concepts=CONCEPTS, question=QUESTION):
    return orch.build_route_seed(query=graph, question=question, concepts=concepts)


# ------------------------------------------------- notes + value mappings keep the seed

@pytest.mark.parametrize("budget", [14_000, 16_000])
@pytest.mark.parametrize("extra", [8, 40])
def test_notes_and_the_value_mapping_block_together_keep_every_system(monkeypatch, budget, extra):
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", budget)
    seed = _build(Graph(MAPPINGS, extra))
    header, payload, tail = _split(seed)
    assert not payload.get("inventory_compact")
    assert [s["id"] for s in payload["systems"]] == list(IDS)
    assert payload["concept_notes"]
    assert it.carries_value_mappings(tail)
    assert header == orch._ROUTE_SEED_VALUE_MAPPING_HEADERS[orch._ROUTE_SEED_HEADER]


@pytest.mark.parametrize("budget", [10_000, 14_000, 16_000])
@pytest.mark.parametrize("extra", [8, 40])
def test_the_notes_add_only_their_own_key(monkeypatch, budget, extra):
    """The seed with notes is the seed without them plus the ``concept_notes``
    key: same header, same fitted payload, same identifier tail. At 10,000 the
    block has no room, so no header is swapped."""
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", budget)
    without = _split(_build(Graph(MAPPINGS, extra), concepts=()))
    header, payload, tail = _split(_build(Graph(MAPPINGS, extra)))
    notes = payload.pop("concept_notes")
    assert notes.count("upkeep window") == 1
    assert (header, payload, tail) == without


@pytest.mark.parametrize("budget", [14_000, 16_000])
def test_the_trace_step_lists_the_systems_and_the_stored_mappings(monkeypatch, budget):
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", budget)
    text = rst.seed_step_text(_build(Graph(MAPPINGS, 20)))
    assert text != rst.FALLBACK_TEXT
    assert "- Connected systems: System A, System B, System C" in text
    assert ("- Stored value mapping: QX-101 in System A (units.code) is 77-QX-101 in System B "
            "(units.tag).") in text
    assert ("- Stored value mapping: QX-101 in System A (units.code) is Q-101 in System C "
            "(assets.name).") in text


def test_the_runtime_trace_event_and_footer_for_such_a_question(monkeypatch):
    """End to end through the prelude: the Knowledge Graph step and the
    ``planned_with`` footer both describe the seed the model got."""
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    graph = Graph(MAPPINGS, 20)
    real = orch.build_route_seed
    monkeypatch.setattr(orch, "build_route_seed", lambda candidate=None, question=None: real(
        query=graph, question=question, concepts=CONCEPTS))
    augmented, events, planned = runtime_app._graph_first_prelude(QUESTION)
    text = events[1]["message"]["content"][0]["toolResult"]["content"][0]["text"]
    assert "- Connected systems:" in text and "- Stored value mapping:" in text
    assert "upkeep window" in augmented
    assert planned["systems"] == len(IDS)
    assert planned["value_mappings"] == 2


def test_the_header_swap_still_refuses_a_seed_past_every_allowance(monkeypatch):
    plain = orch._ROUTE_SEED_HEADER
    final = plain + '{"systems":[]}' + it.VALUE_MAPPINGS_COMPACT_HEADER
    growth = orch._value_mapping_header_growth(final)
    payload = {"_asset_coverage_allowance": 3, "_concept_notes_allowance": 5}
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", len(final) + growth - 8)
    assert orch._with_value_mapping_header(payload, final).startswith(
        orch._ROUTE_SEED_VALUE_MAPPING_HEADERS[plain])
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", len(final) + growth - 9)
    with pytest.raises(orch._RouteSeedSizeInvariantError):
        orch._with_value_mapping_header(payload, final)


# ------------------------------------------------- the emergency seed's trace step

def _emergency(omitted: int) -> str:
    return orch._ROUTE_SEED_PARTIAL_HEADER + json.dumps(
        {"inventory_compact": True, "systems": [], "systems_omitted": omitted, "truncated": True})


def test_the_trace_step_for_the_emergency_seed_says_what_it_left_out():
    text = rst.seed_step_text(_emergency(8))
    assert text != rst.FALLBACK_TEXT
    assert text.splitlines() == [
        "Looked up this question in the knowledge graph before asking any system.",
        "- Connected systems: none listed. The lookup result left out all "
        "8 connected systems, so the answer has to find them itself."]
    one = rst.seed_step_text(_emergency(1))
    assert "left out all 1 connected system, so the answer has to find it itself." in one


def test_the_emergency_step_does_not_claim_the_systems_did_not_fit():
    """The emergency seed drops every system after any size check fails, even
    systems that would fit. The step says they were left out, never that the
    seed had no room for them."""
    payload = {"systems": [{"system_id": "sys-a"}, {"system_id": "sys-b"}],
               "systems_omitted": 0}
    seed = orch._emergency_bounded_route_seed(payload)
    assert seed is not None and len(seed) < orch._ROUTE_SEED_SERIALIZED_BUDGET // 10
    text = rst.seed_step_text(seed)
    assert "left out all 2 connected systems" in text
    assert "room" not in text and "fit" not in text


def test_an_empty_seed_with_nothing_left_out_keeps_the_fallback():
    assert rst.seed_step_text(_emergency(0)) == rst.FALLBACK_TEXT
    assert rst.seed_step_text(orch._ROUTE_SEED_HEADER + '{"systems": []}') == rst.FALLBACK_TEXT


# ------------------------------------------------- the footer counts the block

def test_the_footer_counts_stored_mappings_the_block_carries():
    """No per-system summary survived, but the block hands the model two
    stored mappings: the footer says 2, not 0."""
    seed = _build(Graph(MAPPINGS, 0), concepts=())
    _header, payload, tail = _split(seed)
    assert not any((s.get("rules") or {}).get("value_mappings") for s in payload["systems"])
    assert len(it.parse_value_mappings(tail)) == 2
    assert runtime_app._planned_with_from_seed(seed)["value_mappings"] == 2


def test_a_field_pair_in_both_the_summaries_and_the_block_counts_once():
    summary = {"from_system": "sys-a", "from_field": "units.code", "to_system": "sys-b",
               "to_field": "units.tag", "concept": "equipment", "method": "value_format_match",
               "confidence": 0.95, "value_pairs": [{"from": "QX-101", "to": "77-QX-101"}],
               "value_pair_count": 1}
    seed = _build(Graph(MAPPINGS, 0, summaries=[summary]), concepts=())
    _header, payload, tail = _split(seed)
    kept = [vm for s in payload["systems"] for vm in (s.get("rules") or {}).get("value_mappings", [])]
    assert len(kept) == 1 and len(it.parse_value_mappings(tail)) == 2
    assert runtime_app._planned_with_from_seed(seed)["value_mappings"] == 2


def test_the_footer_without_a_block_counts_as_before():
    seed = _build(Graph([], 0), concepts=())
    assert not it.carries_value_mappings(_split(seed)[2])
    assert runtime_app._planned_with_from_seed(seed)["value_mappings"] == 0
