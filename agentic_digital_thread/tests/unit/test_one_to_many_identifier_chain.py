# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""One-to-many identifier cores, end to end across modules (ADR-0086).

Each module has its own unit tests; this pins the CHAIN so the contract
between them cannot drift apart silently:

    identity_affinity.affinity_rule (neutral one-to-many sample)
      -> the IdentityRule as the graph returns it to the route seed
      -> orchestrator.build_route_seed (JSON pattern + the ONE wildcard note)
      -> identifier_translation find_translations/render/parse_rendered
      -> route_seed_trace.seed_step_text (the KG step line)
      -> resolve.transform_id (SQL side: ``{*}`` -> LIKE ``%``)
      -> runtime_app._planned_with_from_seed (telemetry survives the note)

Offline: fake graph query, no AWS, no network.
"""
from __future__ import annotations

from agents import identifier_translation as it
from agents import orchestrator as orch
from agents import route_seed_trace as trace
from agents import runtime_app
from agents.identity_affinity import SampledField, affinity_rule
from agents.resolve import transform_id

# Same neutral fixture as test_identity_affinity (mirrors a 3-revision item).
_CODES = SampledField("sys-a", "runs", "product_code", "product", ("5101", "5202", "5303"))
_ITEMS = SampledField("sys-b", "items", "item_id", "part",
                      ("PRT-5101-R-A", "PRT-5101-R-B", "PRT-5101-R-C",
                       "PRT-5202-R-A", "PRT-5202-R-B", "PRT-5303-R-A",
                       *[f"PRT-{n}-R-A" for n in range(6001, 6024)]))


def _learned_rule() -> dict:
    rule = affinity_rule(_CODES, _ITEMS)
    assert rule is not None
    assert (rule["source_pattern"], rule["target_pattern"]) == ("{id}", "PRT-{id}-R-{*}")
    return rule


def _graph_rule(rule: dict, **over) -> dict:
    """The IdentityRule shape the route-seed Cypher returns (seed row)."""
    graph = {"id": "ir-chain", "concept": rule["concept"],
             "source_system": "sys-a", "source_field": "product_code",
             "target_system": "sys-b", "target_field": "item_id",
             "source_pattern": rule["source_pattern"],
             "target_pattern": rule["target_pattern"], "transform": None,
             "confidence": rule["confidence"], "method": rule["method"],
             "provenance": rule["reasoning"]}
    graph.update(over)
    return graph


def _query(rules):
    inventory = [{"system_id": sid, "system_name": sid.upper(), "plants": [],
                  "capabilities": None, "connection_type": None, "query_guidance": None}
                 for sid in ("sys-a", "sys-b")]
    rows = [{**inventory[0], "mapped_concepts": [], "shared_concepts": [], "join_paths": [],
             "value_mappings": [], "identity_rules": list(rules)}]

    def fake_query(cypher, params=None):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        if cypher == orch._ROUTE_SEED_CYPHER:
            return rows
        return []
    return fake_query


def test_learned_wildcard_rule_reaches_the_seed_json_and_the_note():
    rule = _graph_rule(_learned_rule())
    seed = orch.build_route_seed(query=_query([rule]))
    payload = trace._payload(seed)
    seeded = [ir for s in payload["systems"] for ir in s.get("rules", {}).get("identity_rules", [])]
    assert [ir["target_pattern"] for ir in seeded] == ["PRT-{id}-R-{*}"]
    assert seed.endswith(it.WILDCARD_SEED_NOTE)
    assert seed.count(it.WILDCARD_SEED_NOTE) == 1
    # The note must not cost the turn its routing telemetry.
    assert runtime_app._planned_with_from_seed(seed)["identity_rules"] == 1


def test_a_revision_in_the_question_reverse_translates_exactly():
    rule = _graph_rule(_learned_rule())
    question = "what changed on PRT-5101-R-C?"
    seed = orch.build_route_seed(query=_query([rule]), question=question)
    assert "- PRT-5101-R-C (sys-b.item_id) → 5101 (sys-a.product_code) [" in seed
    parsed = it.parse_rendered(seed)
    assert [(t["token"], t["translated"]) for t in parsed] == [("PRT-5101-R-C", "5101")]
    assert "candidate_set" not in parsed[0]
    step = trace.seed_step_text(seed)
    assert "Identifier match: PRT-5101-R-C in SYS-B (item_id) matches 5101 in SYS-A" in step
    assert seed.endswith(it.WILDCARD_SEED_NOTE)
    assert runtime_app._planned_with_from_seed(seed) is not None


def test_a_literal_source_code_renders_a_candidate_set_through_every_layer():
    rule = _graph_rule(_learned_rule(), source_pattern="ITM-{id}")
    seed = orch.build_route_seed(query=_query([rule]), question="status of ITM-5101")
    parsed = it.parse_rendered(seed)
    assert [(t["token"], t["translated"]) for t in parsed] == [("ITM-5101", "PRT-5101-R-{*}")]
    assert parsed[0]["candidate_set"] is True
    assert it.CANDIDATE_SUFFIX in seed
    step = trace.seed_step_text(seed)
    assert "matches every identifier beginning PRT-5101-R- in SYS-B (item_id)" in step
    assert "several records share it, so the answer must choose one." in step


def test_the_sql_side_transform_renders_the_wildcard_as_like():
    rule = _learned_rule()
    assert transform_id(rule["source_pattern"], rule["target_pattern"], "5101") == "PRT-5101-R-%"
    assert transform_id(rule["target_pattern"], rule["source_pattern"], "PRT-5101-R-C") == "5101"
