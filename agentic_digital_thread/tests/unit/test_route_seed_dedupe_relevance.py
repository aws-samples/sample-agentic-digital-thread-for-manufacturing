# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Route seed: cross-system rules appear once; question words break tier ties."""
from __future__ import annotations

import json

import agents.orchestrator as orch


def _decode(seed):
    fragment = seed.split("ROUTE SEED RESULT:", 1)[1].lstrip()
    return json.JSONDecoder().raw_decode(fragment)[0]


def _query(rules_by_system, systems=("erp", "mes-mil")):
    def fake(cypher):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return [{"system_id": s, "system_name": s} for s in systems]
        if cypher == orch._ROUTE_SEED_AUTHORITY_CYPHER:
            return []
        return [{"system_id": s, **rules_by_system.get(s, {})} for s in systems]
    return fake


_SHARED = {"id": "jp-1", "from_system": "mes-mil", "from_field": "work_orders.work_order_number",
           "to_system": "erp", "to_field": "lot_mes_tracking.batch_id", "method": "user_confirmed"}
_VM = {"from_system": "mes-mil", "from_field": "a.item_id", "to_system": "erp",
       "to_field": "b.item_id", "concept": "material", "method": "user_confirmed",
       "value_pairs": [{"from": "I-1", "to": "I-1"}], "value_pair_count": 1}


def test_cross_system_rule_is_serialized_once():
    rules = {"join_paths": [_SHARED], "value_mappings": [_VM]}
    seed = orch.build_route_seed(query=_query({"erp": rules, "mes-mil": rules}))
    assert seed.count('"jp-1"') == 1
    payload = _decode(seed)
    assert sum(len(s.get("rules", {}).get("value_mappings", [])) for s in payload["systems"]) == 1


def test_question_terms_break_ties_within_a_tier():
    other = {**_SHARED, "id": "jp-2", "from_field": "work_orders.production_run_id",
             "to_field": "lot_production_usage.production_run_id", "confidence": 0.99}
    seed = orch.build_route_seed(
        query=_query({"erp": {"join_paths": [other, {**_SHARED, "confidence": 0.1}]}}),
        question="Which batch did each work order number trace to?")
    ids = [r["id"] for r in _decode(seed)["systems"][0]["rules"]["join_paths"]]
    assert ids == ["jp-1", "jp-2"]


def test_without_a_question_confidence_order_is_unchanged():
    other = {**_SHARED, "id": "jp-2", "confidence": 0.99}
    seed = orch.build_route_seed(
        query=_query({"erp": {"join_paths": [{**_SHARED, "confidence": 0.1}, other]}}))
    ids = [r["id"] for r in _decode(seed)["systems"][0]["rules"]["join_paths"]]
    assert ids == ["jp-2", "jp-1"]


def test_question_terms_are_stemmed_words_of_four_or_more_chars():
    assert orch._question_terms("Which lots and suppliers?") == {"which", "lot", "supplier"}
    assert orch._question_terms("the process") == {"process"}
