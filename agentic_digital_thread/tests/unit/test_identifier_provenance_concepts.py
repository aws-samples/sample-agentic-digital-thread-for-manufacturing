# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Identifier provenance refuses only through a join of the value's own concept.

A refusal points the model at a recorded join into the target. That join
has to be about what the value identifies: a machine key sent to a system
must never be refused with guidance to read a batch join. The schema here is
made up: an upkeep system keys assets by tag, a shop floor keys jobs, a
ledger keys items, and a drawing register keys drawings.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from agents import identifier_provenance as ip
from agents import identity_affinity as ia
from agents import orchestrator as orch

SYSTEMS = ("upkeep", "shop-floor", "ledger", "drawings")
TOOL = {s: f"{s}___query_{s.replace('-', '_')}" for s in SYSTEMS}
QUESTION = "What is the status of the work on line 4 this week?"
ASSET, PART = "AT-4401-XK", "AB-CD-1234"


def _exact_join(src: tuple, tgt: tuple, concept, *, provenance=True, **extra) -> dict:
    """A JoinPath as the exact-agreement pass writes it (its reasoning names
    the concept), or with no concept evidence when ``provenance`` is False."""
    (s_sys, s_tab, s_col), (t_sys, t_tab, t_col) = src, tgt
    a = ia.SampledField(s_sys, s_tab, s_col, concept or "", ("X-1",) * 20)
    b = ia.SampledField(t_sys, t_tab, t_col, concept or "", ("X-1",) * 20)
    join = {"id": f"jp-{s_sys}-{t_sys}-{s_col}", "from_system": s_sys,
            "from_field": f"{s_sys}_db.{s_tab}.{s_col}", "to_system": t_sys,
            "to_field": f"{t_sys}_db.{t_tab}.{t_col}", "method": ia.EXACT_METHOD,
            "transform": "identity", **extra}
    if provenance:
        join["provenance"] = ia._exact_reasoning(a, b, "AA-9", set(range(5)), concept)
    return join


JOB_JOIN = _exact_join(("shop-floor", "jobs", "job_no"), ("ledger", "items", "job_ref"), "job")
PART_JOIN = _exact_join(("drawings", "records", "drawing_no"),
                        ("ledger", "items", "design_code"), "part")


def _facts(joins, rules=()):
    rows = [{"system_id": s,
             "join_paths": [j for j in joins if s in (j["from_system"], j["to_system"])],
             "identity_rules": [r for r in rules if s in (r["source_system"],
                                                          r["target_system"])],
             "value_mappings": []} for s in SYSTEMS]
    return ip.facts_from(QUESTION, rows, cap=50)


def _typed_line(text: str, records) -> str:
    """The line for ``records`` ``[(table.column, value, concepts or None)]``,
    each position bound to its token."""
    body = []
    for field, value, concepts in records:
        start = text.index(value)
        entry = {"f": field, "at": [[start, start + len(value), ip.token_digest(value)]]}
        if concepts is not None:
            entry["c"] = concepts
        body.append(entry)
    return "\n\n" + ip.LINE_PREFIX + json.dumps(body, separators=(",", ":"))


class _Systems:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    async def call_tool_async(self, tool_use_id, name, arguments=None,
                              read_timeout_seconds=None, meta=None):
        self.calls.append(name)
        text, records = self.answers.get(name, ("No rows.", []))
        if (arguments or {}).get(ip.RESERVED_ARG) == ip.REQUESTED and records:
            text += _typed_line(text, records)
        return {"status": "success", "toolUseId": tool_use_id, "content": [{"text": text}]}


@pytest.fixture(autouse=True)
def _clean():
    ip.reset_turn_facts()
    yield
    ip.reset_turn_facts()


def _ask(facts, source_answer, target, value):
    """The target's result when ``value``, returned by upkeep, is sent to it."""
    ip._TURN_FACTS.set(facts)
    systems = _Systems({TOOL["upkeep"]: source_answer})
    client = orch._wrap_gateway_client_with_decline_guard(systems)
    asyncio.run(client.call_tool_async("t1", TOOL["upkeep"], {"question": "Open work?"}))
    return asyncio.run(client.call_tool_async("t2", TOOL[target],
                                              {"question": f"Records for {value}?"}))


ASSET_ANSWER = (f"Asset {ASSET} has one open order.",
                [("assets.asset_tag", ASSET, ["equipment"])])
PART_ANSWER = (f"The order replaces part {PART}.", [("orders.part_ref", PART, ["part"])])


def test_an_equipment_key_does_not_trigger_a_join_of_another_concept():
    """Reproduction: the ledger's only joins are a job join and a part join;
    an asset tag from the upkeep system is sent to it unchanged."""
    result = _ask(_facts([JOB_JOIN, PART_JOIN]), ASSET_ANSWER, "ledger", ASSET)
    assert result["status"] == "success"


def test_a_key_of_the_joins_concept_is_refused_naming_that_join_only():
    result = _ask(_facts([JOB_JOIN, PART_JOIN]), PART_ANSWER, "ledger", PART)
    text = result["content"][0]["text"]
    assert result["status"] == "error"
    assert "drawings drawings_db.records.drawing_no -> ledger ledger_db.items.design_code" in text
    assert "jobs.job_no" not in text


def test_concept_evidence_can_come_from_the_identity_rule_of_the_join():
    bare = _exact_join(("drawings", "records", "drawing_no"),
                       ("ledger", "items", "design_code"), None, provenance=False)
    rule = {"concept": "part", "source_system": "drawings", "source_field": "drawing_no",
            "target_system": "ledger", "target_field": "items.design_code"}
    assert _ask(_facts([bare], [rule]), PART_ANSWER, "ledger", PART)["status"] == "error"


def test_a_join_without_concept_evidence_passes():
    bare = _exact_join(("drawings", "records", "drawing_no"),
                       ("ledger", "items", "design_code"), None, provenance=False)
    assert _ask(_facts([bare]), PART_ANSWER, "ledger", PART)["status"] == "success"


def test_a_source_column_without_a_concept_passes():
    answer = (PART_ANSWER[0], [("orders.part_ref", PART, None)])
    assert _ask(_facts([PART_JOIN]), answer, "ledger", PART)["status"] == "success"


def test_an_exact_join_recorded_out_of_the_target_counts():
    """A value join holds the same values on both sides, so the direction it
    happened to be recorded in does not matter."""
    result = _ask(_facts([PART_JOIN]), PART_ANSWER, "drawings", PART)
    assert result["status"] == "error"
    assert "ledger ledger_db.items.design_code -> drawings" in result["content"][0]["text"]


def test_a_transforming_join_recorded_out_of_the_target_does_not_count():
    shaped = {**PART_JOIN, "method": "value_pattern_match", "transform": "prefix_strip"}
    assert _ask(_facts([shaped]), PART_ANSWER, "drawings", PART)["status"] == "success"


def test_the_join_concept_is_read_back_from_the_exact_passs_own_reasoning():
    side = ia.SampledField("a", "t", "c", "x", ("X-1",) * 20)
    for label, concept in (("part", "part"), ("batch (catalog-related production-run)", "batch")):
        text = ia._exact_reasoning(side, side, "AA-9", set(range(5)), label)
        assert ia.exact_concept_from_reasoning(text) == concept
    assert ia.exact_concept_from_reasoning("joined by name, concept part") is None
    assert ia.exact_concept_from_reasoning(None) is None


@pytest.mark.parametrize("metadata", [
    {"method": "value_pattern_match", "transform": "prefix_strip"},
    {"method": "value_pattern_match", "transform": "identity"},
    {"method": ia.EXACT_METHOD, "transform": "prefix_strip"},
    {"method": "foreign_key_match", "transform": "direct"},
], ids=["pattern-prefix", "pattern-identity", "exact-prefix", "foreign-key"])
def test_exact_looking_reasoning_on_a_non_exact_join_gives_no_concept(metadata):
    """Reasoning text in the exact pass's format counts only on a join whose
    method and transform say it was written by that pass; on any other join
    it is no evidence, so an equipment key is not refused through it."""
    mismatched = {**_exact_join(("shop-floor", "jobs", "job_no"),
                                ("ledger", "items", "job_ref"), "equipment"), **metadata}
    assert ip.join_concepts(_facts([mismatched]), mismatched) == frozenset()
    assert _ask(_facts([mismatched]), ASSET_ANSWER, "ledger", ASSET)["status"] == "success"


def test_exact_reasoning_on_an_exact_identity_join_still_counts():
    assert ip.join_concepts(_facts([PART_JOIN]), PART_JOIN) == frozenset({"part"})
    legacy = {k: v for k, v in PART_JOIN.items() if k != "transform"}
    assert ip.join_concepts(_facts([legacy]), legacy) == frozenset({"part"})
