# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Under the response-integrity gate, a peer's values keep their field label.

With ``RESPONSE_INTEGRITY_MODE=enforce`` a Mode A system agent answers with a
validated ``SystemAnswer`` envelope: each value sits in a claim input's
``value`` with its ``semantic_field`` beside it, and the input's evidence
entry names the physical field. Read as generic JSON, every such value was
labelled ``value``, so no identity rule could be matched by provenance and a
delegation that carried those values got no concept and no join.
``carried_values`` now reads the envelope itself.

The peer's answer is built the way the system agent builds it: its tool
result captured at the trusted connector boundary
(``capture_trusted_tool_result``), the model's draft validated against it
(``system_answer_from_result``), serialized with ``model_dump_json`` and, in
one case, carrying the engine's reserved note member. Both made-up schemas of
``_route_facts_world``, through the real gateway client and Strands'
``MCPAgentTool.stream``. Offline.
"""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

import agents.system_agent as sa
from agents.response_integrity_capture import capture_trusted_tool_result
from agents.response_integrity_contracts import (
    AnswerClaim, ClaimInput, ClaimKind, SystemAnswerDraft,
)
from agents.response_integrity_validation import system_answer_from_result
from tests.unit._route_facts_world import (  # noqa: F401 - isolated_turn is a fixture
    WORLDS, block, isolated_turn, label, peer_then_target, sent,
)

#: The peer table each world's answer is read from (any name works).
_TABLES = {"line-log": "consignment_lines", "press-floor": "dispatch_rows"}


@pytest.fixture(autouse=True)
def _enforced(isolated_turn, monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)
    yield


@pytest.fixture(params=WORLDS, ids=lambda w: w.name)
def world(request):
    return request.param


def _system_answer(world, column: str, alias: str, *, note: bool = False) -> str:
    """The peer's serialized SystemAnswer for its run ids, read from
    ``column`` of its table and returned under ``alias``."""
    table = _TABLES[world.name]
    payload = {"columns": ["shipment", alias],
               "rows": [[f"S-{i}", run] for i, run in enumerate(world.run_ids)],
               "row_count": len(world.run_ids)}
    state: dict = {}
    capture_trusted_tool_result(
        {"content": [{"json": payload}]}, state, system_id=world.peer, boundary="sql",
        tool_name=f"query_{world.peer.replace('-', '_')}",
        executed_query=f"SELECT shipment AS shipment, {column} AS {alias} FROM {table}",  # nosec B608 - test fixture
        physical_schema={table: {"relation_kind": "base_table",
                                 "columns": {"shipment": "varchar", column: "varchar"}}})
    draft = SystemAnswerDraft(claims=tuple(AnswerClaim(
        claim_id=f"c{i}", kind=ClaimKind.VALUE, label="Shipped run", value=run,
        inputs=(ClaimInput(label="run", value=run, semantic_field=alias,
                           locator=f"row:{i}"),),
    ) for i, run in enumerate(world.run_ids)))
    answer = system_answer_from_result(SimpleNamespace(structured_output=draft), state)
    assert answer is not None and len(answer.claims) == len(world.run_ids)
    assert answer.evidence[0].physical_field == f"{table}.{column}"
    text = answer.model_dump_json()
    return sa._with_result_cut_fact(text, "[engine] Result cut: 2 of 2 rows.") if note else text


def _target_kg(monkeypatch, world, answer: str) -> str:
    world = dataclasses.replace(world, peer_answer=answer)
    fake = peer_then_target(monkeypatch, world, {"question": world.target_question})
    return sent(fake, 1).get("kg_context", "")


@pytest.mark.parametrize("note", [False, True], ids=["plain", "engine-note"])
def test_a_validated_answer_labels_each_value_by_its_semantic_field(monkeypatch, world, note):
    answer = _system_answer(world, world.peer_field, world.peer_field, note=note)
    kg = _target_kg(monkeypatch, world, answer)
    assert kg.startswith(f"```ROUTE:\ntarget_system: {world.target}\n")
    assert label(kg, "canonical_concept") == [f"canonical_concept: {world.concept}"]
    assert world.join_line in block(kg).splitlines()
    assert world.second_join["id"] not in kg and world.unconfirmed["id"] not in kg


def test_an_aliased_value_is_labelled_by_its_evidence_physical_field(monkeypatch, world):
    kg = _target_kg(monkeypatch, world, _system_answer(world, world.peer_field, "shipped_ref"))
    assert label(kg, "canonical_concept") == [f"canonical_concept: {world.concept}"]
    assert world.join_line in block(kg).splitlines()


def test_a_value_from_another_field_carries_nothing(monkeypatch, world):
    kg = _target_kg(monkeypatch, world, _system_answer(world, "dock_ref", "dock_ref"))
    assert kg == ""


def test_an_alias_naming_a_rule_field_does_not_override_the_physical_field(monkeypatch, world):
    # The model picks the output alias; the server records the physical column.
    kg = _target_kg(monkeypatch, world, _system_answer(world, "dock_ref", world.peer_field))
    assert kg == ""
