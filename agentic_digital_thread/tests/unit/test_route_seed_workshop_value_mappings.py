# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The IDENTIFIER VALUE MAPPINGS block on the workshop's own route seed.

The workshop fixture (``_route_seed_workshop_fixture``: the module 6 systems,
registration text and exact joins from the workshop manifests) plus the two
equipment ValueMappings a module 4 provision leaves in the graph once the
plant CMMS's registry is found: the CMMS tag to the sensor asset (the
existing ``value_format_match`` post-pass) and the plant MES machine id to the
CMMS tag (shaped like the cross-registry name match the plan's MR 2 writes).
Values come from the story constants, never literals here. Module 5 Q1 names
the MES machine id; module 6's main question names the machine's designation.
Each is built at the budget on this branch and at the two budgets of the
route-seed budget branch (14,000 and 16,000). At every budget the block costs
no system's own text: it gets the room left after it, or is left out.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import agents.identifier_translation as it
import agents.orchestrator as orch
from tests.unit import _route_seed_workshop_fixture as wf

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from erp_simulation import condition_monitor as cm  # noqa: E402

(CMMS,) = wf.MODULE_4_ONBOARDED
MES, SENSOR = wf.PLANT_MES_ID, wf.SENSOR_ID
#: The machine's designation: the CMMS tag after its site prefix, which the
#: sensor asset name ends with.
DESIGNATION = cm.STORY_CMMS_ASSET_NUM.split("-", 1)[1]
MODULE_6_QUESTION = (f"Is vibration on the {DESIGNATION} trending up over the last hour, and which\n"
                     "current work orders and open shipments are at risk if it fails?")
MAPPINGS = [
    {"id": "vm-equip-sensor", "from_system": CMMS, "from_field": "assets.asset_num",
     "from_value": cm.STORY_CMMS_ASSET_NUM, "to_system": SENSOR, "to_field": "asset_name",
     "to_value": cm.STORY_SITEWISE_ASSET, "concept": "equipment",
     "method": "value_format_match", "confidence": 0.95, "provenance": None},
    {"id": "vm-equip-mes", "from_system": CMMS, "from_field": "assets.asset_num",
     "from_value": cm.STORY_CMMS_ASSET_NUM, "to_system": MES,
     "to_field": "equipment.equipment_id", "to_value": cm.STORY_MES_EQUIPMENT_ID,
     "concept": "equipment", "method": "equipment_name_match", "confidence": 0.95,
     "provenance": "the same machine name in both registries"},
]


def _graph(mappings, reads=None):
    base = wf.graph(wf.MODULE_6)

    def query(cypher, params=None):
        if "\nMATCH (vm:ValueMapping)\nWHERE " not in cypher:
            return base(cypher)
        if reads is not None:
            reads.append(params)
        params = params or {}
        exact = params.get("exact") or params.get("linked") or []
        suffixes = [v for k, v in params.items() if k[:1] == "s" and k[1:].isdigit()]
        return [dict(vm) for vm in mappings
                if any(v in exact or any(v.endswith(s) for s in suffixes)
                       for v in (vm["from_value"].lower(), vm["to_value"].lower()))]
    return query


def _mappings(seed: str) -> list[tuple]:
    _payload, tail = wf.decode(seed)
    return [(m["from_system"], m["from_value"], m["to_system"], m["to_value"], m.get("match"))
            for m in it.parse_value_mappings(tail)]


def test_the_story_designation_is_the_end_of_both_other_names():
    assert it.value_match(cm.STORY_CMMS_ASSET_NUM, DESIGNATION) == it.MATCH_SEGMENT
    assert it.value_match(cm.STORY_SITEWISE_ASSET, DESIGNATION) == it.MATCH_SEGMENT
    assert it.value_match(cm.STORY_MES_EQUIPMENT_ID, DESIGNATION) is None


@pytest.mark.parametrize("budget", [10_000, 14_000, 16_000])
def test_module_5_q1_carries_the_maintenance_tag_of_the_machine_it_names(monkeypatch, budget):
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", budget)
    seed = orch.build_route_seed(query=_graph(MAPPINGS), question=wf.IDENTIFIER_QUESTION)
    assert len(seed) <= budget
    found = _mappings(seed)
    assert (CMMS, cm.STORY_CMMS_ASSET_NUM, MES, cm.STORY_MES_EQUIPMENT_ID) == found[0][:4]
    if budget > 10_000:  # the full form: the sensor asset one link further
        assert found[0][4] == it.MATCH_EXACT
        assert found[1] == (CMMS, cm.STORY_CMMS_ASSET_NUM, SENSOR, cm.STORY_SITEWISE_ASSET,
                            it.MATCH_HOP)


@pytest.mark.parametrize("budget", [10_000, 14_000, 16_000])
def test_module_6_carries_every_name_of_the_machine(monkeypatch, budget):
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", budget)
    seed = orch.build_route_seed(query=_graph(MAPPINGS), question=MODULE_6_QUESTION)
    assert len(seed) <= budget
    assert {m[:4] for m in _mappings(seed)} == {
        (CMMS, cm.STORY_CMMS_ASSET_NUM, MES, cm.STORY_MES_EQUIPMENT_ID),
        (CMMS, cm.STORY_CMMS_ASSET_NUM, SENSOR, cm.STORY_SITEWISE_ASSET)}


@pytest.mark.parametrize("budget", [10_000, 14_000, 16_000])
@pytest.mark.parametrize("question", [wf.IDENTIFIER_QUESTION, MODULE_6_QUESTION])
def test_the_block_costs_no_system_and_no_tier01_rule(monkeypatch, budget, question):
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", budget)
    plain, _ = wf.decode(wf.build_seed(wf.MODULE_6, question))
    with_block, tail = wf.decode(orch.build_route_seed(query=_graph(MAPPINGS), question=question))
    assert it.parse_value_mappings(tail)

    tier1 = {row["id"] for pair in wf.tier1_pairs(wf.MODULE_6) for row in pair}

    def kept(payload):
        return {r.get("id") for s in payload["systems"]
                for rules in (s.get("rules") or {}).values() for r in rules} & tier1
    assert [s["id"] for s in with_block["systems"]] == [s["id"] for s in plain["systems"]]
    assert kept(with_block) == kept(plain) == tier1


@pytest.mark.parametrize("question", [wf.IDENTIFIER_QUESTION, MODULE_6_QUESTION])
def test_without_a_stored_mapping_the_workshop_seed_is_unchanged(question):
    reads = []
    assert (orch.build_route_seed(query=_graph([], reads), question=question)
            == wf.build_seed(wf.MODULE_6, question))
    assert len(reads) == 1  # the question names an identifier, so the read ran


def _own_text(seed: str) -> dict:
    payload, _tail = wf.decode(seed)
    return {"systems": {s["id"]: {k: s.get(k) for k in ("name", "plants", "capabilities",
                                                         "query_guidance", "answer_requirements",
                                                         "legacy_guidance")}
                        for s in payload["systems"]},
            "shared": payload.get("shared_guidance"),
            "dropped": payload.get("guidance_delivery_truncation")}


@pytest.mark.parametrize("budget", [10_000, 14_000, 16_000])
@pytest.mark.parametrize("question", [wf.IDENTIFIER_QUESTION, MODULE_6_QUESTION])
def test_the_block_never_shortens_a_systems_own_text(monkeypatch, budget, question):
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", budget)
    plain = wf.build_seed(wf.MODULE_6, question)
    seed = orch.build_route_seed(query=_graph(MAPPINGS), question=question)
    assert _own_text(seed) == _own_text(plain)


@pytest.mark.parametrize("budget", [10_000, 14_000, 16_000])
@pytest.mark.parametrize("question", [wf.IDENTIFIER_QUESTION, MODULE_6_QUESTION])
def test_the_workshop_seed_with_the_block_carries_the_mapping_header(monkeypatch, budget, question):
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", budget)
    seed = orch.build_route_seed(query=_graph(MAPPINGS), question=question)
    assert seed.startswith(orch._ROUTE_SEED_VALUE_MAPPING_HEADERS[orch._ROUTE_SEED_HEADER])
    assert wf.build_seed(wf.MODULE_6, question).startswith(orch._ROUTE_SEED_HEADER)
