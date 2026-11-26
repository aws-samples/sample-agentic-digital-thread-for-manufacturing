# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Knowledge Graph trace step shows what the routing lookup FOUND.

Walkthrough feedback: the trace showed that the graph was checked but not why a
system was asked or how an identifier was matched. The step now renders the
seed's own findings for THIS question (connected systems, shared concepts and
every identifier translation applied, with how the rule was learned), read
back from the built seed rather than from canned text. The seed header also
tells the model in plain words where the block came from, so it can describe
the lookup accurately instead of calling it part of its standing instructions.
"""
from agents import identifier_translation as it
from agents import orchestrator as orch
from agents import runtime_app

_REAL_BUILD_ROUTE_SEED = orch.build_route_seed


def _rule(**over):
    base = {"id": "ir-1", "concept": "batch", "source_system": "mes-x", "source_field": "batch_number",
            "source_pattern": "X-{id}", "target_system": "erp-x", "target_field": "material_lot",
            "target_pattern": "PLANT-2026-0{id}", "confidence": 0.28, "method": "value_core_match",
            "provenance": "50 of 50 sampled value pairs"}
    base.update(over)
    return base


def _system_row(system_id, name, **over):
    row = {"system_id": system_id, "system_name": name, "plants": [], "capabilities": None,
           "connection_type": None, "query_guidance": None}
    row.update(over)
    return row


def _fake_query(rules=None, shared=None):
    inventory = [_system_row("mes-x", "Line MES"), _system_row("erp-x", "Plant ERP")]
    rows = [_system_row("mes-x", "Line MES", mapped_concepts=["batch"],
                        shared_concepts=shared if shared is not None else
                        [{"concept": "material_lot", "peer_system": "erp-x"}],
                        join_paths=[], value_mappings=[],
                        identity_rules=rules if rules is not None else [_rule()])]

    def query(cypher, params=None):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        if cypher == orch._ROUTE_SEED_CYPHER:
            return rows
        return []
    return query


def _step_text(monkeypatch, question, **fake):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    q = _fake_query(**fake)
    monkeypatch.setattr(orch, "build_route_seed",
                        lambda candidate=None, question=None: _REAL_BUILD_ROUTE_SEED(
                            query=q, question=question))
    _augmented, events, _planned = runtime_app._graph_first_prelude(question)
    return events[1]["message"]["content"][0]["toolResult"]["content"][0]["text"]


def test_trace_step_names_systems_concepts_and_the_translation_applied(monkeypatch):
    text = _step_text(monkeypatch, "Which orders shipped from batch X-2847?")
    assert not text.startswith("Checked the knowledge graph")
    assert "Line MES" in text and "Plant ERP" in text
    assert "material lot" in text
    assert ("X-2847 in Line MES (batch_number) matches PLANT-2026-02847 in "
            "Plant ERP (material_lot)") in text
    assert "learned at onboarding by matching identifier values" in text
    # Display names, never rule ids or raw seed JSON.
    assert "ir-1" not in text and "{" not in text


def test_trace_step_says_plainly_when_nothing_needed_translating(monkeypatch):
    text = _step_text(monkeypatch, "What is line L3 doing?")
    assert "No identifier in this question needed translating" in text
    assert "matches" not in text


def test_trace_step_method_wording_follows_the_rule_not_a_constant(monkeypatch):
    text = _step_text(monkeypatch, "batch X-2847",
                      rules=[_rule(method="user_confirmed")])
    assert "confirmed by a reviewer" in text
    text = _step_text(monkeypatch, "batch X-2847", rules=[_rule(method="something_new")])
    assert "recorded in the knowledge graph" in text


def test_trace_step_falls_back_for_non_route_seeds():
    from agents import route_seed_trace as rst
    assert rst.seed_step_text(None) == rst.FALLBACK_TEXT
    assert rst.seed_step_text("GRAPH-FIRST ROUTING ...") == rst.FALLBACK_TEXT
    assert rst.seed_step_text(orch._ROUTE_SEED_HEADER + "not json") == rst.FALLBACK_TEXT


def test_parse_rendered_round_trips_render():
    found = it.find_translations("batch X-2847", [_rule()])
    back = it.parse_rendered("prefix\n" + it.render(found) + "\nafter the block")
    assert [(b["token"], b["translated"], b["from_system"], b["from_field"],
             b["to_system"], b["to_field"], b["method"]) for b in back] == [
        (f["token"], f["translated"], f["from_system"], f["from_field"],
         f["to_system"], f["to_field"], f["method"]) for f in found]
    assert it.parse_rendered("no block here") == []


def test_seed_header_tells_the_model_where_the_block_came_from():
    for header in (orch._ROUTE_SEED_HEADER, orch._ROUTE_SEED_PARTIAL_HEADER):
        assert "knowledge graph" in header.lower()
        assert "not part of your standing instructions" in header
        assert header.endswith("ROUTE SEED RESULT:\n")


def test_characterization_exact_translation_line_is_byte_identical(monkeypatch):
    text = _step_text(monkeypatch, "Which orders shipped from batch X-2847?")
    assert ("- Identifier match: X-2847 in Line MES (batch_number) matches "
            "PLANT-2026-02847 in Plant ERP (material_lot). Rule learned at onboarding "
            "by matching identifier values.") in text.splitlines()


def test_trace_step_renders_a_wildcard_rule_as_a_candidate_set(monkeypatch):
    wild = _rule(id="ir-w", source_pattern="ITM-{id}", target_pattern="PRT-{id}-R-{*}",
                 source_field="code", target_field="item_id", confidence=0.85)
    text = _step_text(monkeypatch, "status of ITM-5101", rules=[wild])
    assert ("- Identifier match: ITM-5101 in Line MES (code) matches every identifier "
            "beginning PRT-5101-R- in Plant ERP (item_id); several records share it, so the "
            "answer must choose one. Rule learned at onboarding by matching identifier "
            "values.") in text.splitlines()
    assert "{" not in text
