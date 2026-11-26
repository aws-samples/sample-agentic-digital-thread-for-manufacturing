# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The route seed tells the orchestrator a system's query hints exist; it never restates them.

Measured in an answer eval: a steward query hint said which timestamp column one
kind of count is windowed on. The hint reached the SQL agent with the question,
as it should, and also reached the orchestrator inside the route seed. Two of
five runs then copied the column name from that hint into the delegation,
against the orchestrator's own rule never to send column names, and the agent
windowed another state's rows on it. More than half the rows dropped out.

In Mode A the system agent composes the query and gets its query hints appended
to every question (``system_agent._augment_question_with_guidance``), so the
orchestrator needs only to know they exist. The seed now carries, per system
with hints, one reference line (how many, delivered to whom, do not restate
them or any column they name) instead of their text. Everything else the seed
routes on stays: the guidance base, answer requirements and legacy notes.
"""
from __future__ import annotations

import json

import agents.orchestrator as orch
import agents.system_agent as sa
import agents.system_guidance as system_guidance
from agents.guidance_scope import (
    GuidanceScope,
    append_note,
    delivery_document,
    parse_document,
)

_BASE = "System A answers which line ran a batch; ask it per batch."
_HINT_1 = "Window finished rows on col_done; split long counts into two-day pieces."
_HINT_2 = "Equipment ids sit in col_equipment as a JSON list."
_REQUIREMENT = "Name the batch in every answer."
_LEGACY = "Prefer the newest revision."


def _document(*, hints=(_HINT_1, _HINT_2)) -> str:
    text = _BASE
    for hint in hints:
        text = append_note(text, hint, GuidanceScope.QUERY_HINT)
    text = append_note(text, _REQUIREMENT, GuidanceScope.ANSWER_REQUIREMENT)
    return append_note(text, _LEGACY, GuidanceScope.LEGACY_UNSCOPED)


def _row(system_id: str, guidance: str) -> dict:
    return {"system_id": system_id, "system_name": system_id.title(),
            "connection_type": "Athena", "query_guidance": guidance}


def _seed(rows: list[dict]) -> str:
    def fake_query(cypher, *_args, **_kwargs):
        return rows if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER else []
    return orch.build_route_seed(query=fake_query)


def _systems(seed: str) -> dict:
    payload = json.JSONDecoder().raw_decode(seed.split("RESULT:\n", 1)[1])[0]
    return {system["id"]: system for system in payload["systems"]}


class _Backend:
    def __init__(self, texts: dict):
        self._texts = texts

    def version(self, system_id):
        return 1 if system_id in self._texts else 0

    def load(self, system_id):
        return self._texts.get(system_id, ""), self.version(system_id)

    def save(self, system_id, text, version):  # pragma: no cover - never written here
        raise AssertionError("delivery must not write the guidance store")


def test_hint_bodies_are_absent_from_the_orchestrator_seed():
    seed = _seed([_row("system-a", _document())])

    assert _HINT_1 not in seed and _HINT_2 not in seed
    assert "col_done" not in seed and "col_equipment" not in seed


def test_the_seed_carries_one_reference_line_instead():
    system = _systems(_seed([_row("system-a", _document())]))["system-a"]

    assert system["query_hints"] == (
        "2 query hints are delivered to system-a with each question; "
        "do not restate them or any column they name")


def test_what_the_seed_routes_on_is_still_delivered():
    seed = _seed([_row("system-a", _document())])
    system = _systems(seed)["system-a"]

    assert _BASE in system["query_guidance"]
    assert "QUERY HINTS" in system["query_guidance"]
    assert _REQUIREMENT in system["answer_requirements"]
    assert _LEGACY in system["legacy_guidance"]
    # Composition metadata still counts every note: the hints are delivered,
    # to the agent that writes the query.
    assert system["guidance_composition"]["scope_counts"]["query_hint"] == 2


def test_a_system_without_hints_gets_no_reference_and_the_same_guidance_field():
    document = _document(hints=())
    system = _systems(_seed([_row("system-a", document)]))["system-a"]

    assert "query_hints" not in system
    assert system["query_guidance"] == orch._route_seed_inventory(
        [_row("system-a", document)])["system-a"]["query_guidance"]


def test_the_system_agent_question_still_carries_every_hint():
    document = _document()
    prior = system_guidance._store
    system_guidance.set_store(system_guidance.SystemGuidanceStore(
        backend=_Backend({"system-a": delivery_document(parse_document(document))})))
    try:
        question = sa._augment_question_with_guidance("system-a", "Which formats appear?")
    finally:
        system_guidance.set_store(prior)

    assert question.startswith("Which formats appear?")
    assert _HINT_1 in question and _HINT_2 in question
    assert _REQUIREMENT not in question


def test_the_steward_health_projection_fits_the_same_orchestrator_document():
    """The steward surface projects the SAME delivery the seed makes, so hint
    text that never rides the seed is never reported as cut from it."""
    from agents import learning_health
    long_hints = [f"hint {n}: " + "x" * 400 for n in range(4)]
    document = parse_document(_document(hints=long_hints))
    row = {"system_id": "system-a", "base": document["base"],
           "query_hints": document["query_hints"],
           "answer_requirements": document["answer_requirements"],
           "legacy_unscoped": document["legacy_unscoped"]}

    stats = learning_health.guidance_delivery_stats([row])
    seed = _seed([_row("system-a", _document(hints=long_hints))])

    assert stats["truncated"] is False and stats["notes_dropped"] == 0
    assert "guidance_delivery_truncation" not in seed
