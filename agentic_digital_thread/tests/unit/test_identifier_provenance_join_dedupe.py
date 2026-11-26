# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""One link recorded from both of its sides is one join at the target.

Onboarding each system of a same-value link can write a JoinPath from its
own side, so the graph holds the link twice, once in each direction. At the
target both read as the same join. The identifier-provenance refusal names
it once, and no refusal decision depends on how many times it was recorded.

The schema here is made up: a test bench records a unit code on each test,
a catalog keys models by model code, a vault keys parts by vault reference,
and an archive keys sheets by sheet number. Nothing here names any real
deployment.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from agents import identifier_provenance as ip
from agents import identity_affinity as ia
from agents import orchestrator as orch

SYSTEMS = ("bench", "catalog", "vault", "archive")
TOOL = {s: f"{s}___query_{s}" for s in SYSTEMS}
QUESTION = "Which sheets cover the unit tested on rig 3 this morning?"
UNIT, MODEL, VAULT_REF = "UX-77-4410", "MD-5520-R2", "VR-0091-KQ"
SHEET_FIELD = "docstore.sheets.sheet_no"
CATALOG_FIELD = "catalog_db.models.model_code"
VAULT_FIELD = "vault_db.parts.vault_ref"


def _join(src: tuple, tgt: tuple, concept=None) -> dict:
    """A same-value JoinPath from ``src`` to ``tgt`` (``(system, field)``), as
    the exact-agreement pass writes it; its reasoning names ``concept``, and
    a join with no concept carries no reasoning at all."""
    (s_sys, s_field), (t_sys, t_field) = src, tgt
    join = {"id": f"jp-{s_sys}-{t_sys}-{s_field.rsplit('.', 1)[-1]}", "from_system": s_sys,
            "from_field": s_field, "to_system": t_sys, "to_field": t_field,
            "method": ia.EXACT_METHOD, "transform": "identity"}
    if concept:
        side = ia.SampledField(s_sys, "t", "c", concept, ("X-1",) * 20)
        join["provenance"] = ia._exact_reasoning(side, side, "AA-9", set(range(5)), concept)
    return join


#: One catalog-archive link, recorded from the catalog's side and from the
#: archive's side, each with and without concept evidence.
OUT = _join(("catalog", CATALOG_FIELD), ("archive", SHEET_FIELD), "assembly")
BACK = _join(("archive", SHEET_FIELD), ("catalog", CATALOG_FIELD), "assembly")
OUT_BARE = _join(("catalog", CATALOG_FIELD), ("archive", SHEET_FIELD))
BACK_BARE = _join(("archive", SHEET_FIELD), ("catalog", CATALOG_FIELD))
#: A different link into the archive, from the vault.
VAULT = _join(("vault", VAULT_FIELD), ("archive", SHEET_FIELD), "assembly")
VAULT_BACK = _join(("archive", SHEET_FIELD), ("vault", VAULT_FIELD), "assembly")

#: Every way the one catalog-archive link can be recorded with concept
#: evidence on at least one record.
LINK_RECORDS = {
    "catalog-side-only": [OUT],
    "archive-side-only": [BACK],
    "both-sides": [OUT, BACK],
    "both-sides-archive-record-bare": [OUT, BACK_BARE],
    "both-sides-catalog-record-bare": [OUT_BARE, BACK],
}

CATALOG_LINE = f"catalog {CATALOG_FIELD} -> archive {SHEET_FIELD}"
VAULT_LINE = f"vault {VAULT_FIELD} -> archive {SHEET_FIELD}"
OPENING = (f"[engine] Not sent to archive: {UNIT} was returned this turn by bench "
           "tests.unit_code, and the knowledge graph records no join between bench and "
           "archive, so archive is not known to hold that value. ")
CLOSING = "Sending this same request again goes through unchanged."


def _facts(joins, question=QUESTION, capped=()):
    rows = [{"system_id": s,
             "join_paths": [j for j in joins if s in (j["from_system"], j["to_system"])],
             "identity_rules": [{"id": f"r{i}"} for i in range(50)] if s in capped else [],
             "value_mappings": []} for s in SYSTEMS]
    return ip.facts_from(question, rows, cap=50)


def _typed_line(text: str, records) -> str:
    """The identifier-sources line for ``records`` ``[(table.column, value,
    concepts)]``, each position bound to its token."""
    body = []
    for field_name, value, concepts in records:
        start = text.index(value)
        body.append({"f": field_name, "c": concepts,
                     "at": [[start, start + len(value), ip.token_digest(value)]]})
    return "\n\n" + ip.LINE_PREFIX + json.dumps(body, separators=(",", ":"))


class _Systems:
    """System agents that answer with the line when the call asks for it."""

    def __init__(self, answers):
        self.answers = answers

    async def call_tool_async(self, tool_use_id, name, arguments=None,
                              read_timeout_seconds=None, meta=None):
        text, records = self.answers.get(name, ("No rows.", []))
        if (arguments or {}).get(ip.RESERVED_ARG) == ip.REQUESTED and records:
            text += _typed_line(text, records)
        return {"status": "success", "toolUseId": tool_use_id, "content": [{"text": text}]}


def _answers(unit_concept="assembly"):
    return {TOOL["bench"]: (f"Rig 3 tested unit {UNIT} at 07:40.",
                            [("tests.unit_code", UNIT, [unit_concept])]),
            TOOL["catalog"]: (f"Unit {UNIT} is model {MODEL}.",
                              [("models.model_code", MODEL, ["assembly"])])}


@pytest.fixture(autouse=True)
def _clean():
    ip.reset_turn_facts()
    yield
    ip.reset_turn_facts()


def _turn(facts, calls, answers=None) -> list:
    """``[(status, text)]`` of each ``(system, question)`` delegation in order."""
    ip._TURN_FACTS.set(facts)
    client = orch._wrap_gateway_client_with_decline_guard(_Systems(answers or _answers()))
    out = []
    for n, (system, question) in enumerate(calls):
        result = asyncio.run(client.call_tool_async(f"t{n}", TOOL[system],
                                                    {"question": question}))
        out.append((result["status"], result["content"][0]["text"]))
    return out


def _refusal(joins) -> str:
    """The archive's refusal text after the bench returns the unit code."""
    (status, text), = _turn(_facts(joins), [("bench", "Which unit did rig 3 test?"),
                                            ("archive", f"Sheets for unit {UNIT}?")])[1:]
    assert status == "error"
    return text


# --------------------------------------------------------------------------- #
# Reproduction: the link recorded from both sides is named twice
# --------------------------------------------------------------------------- #
def test_a_link_recorded_on_both_sides_is_named_once_in_the_refusal():
    """Reproduction. The catalog-archive link is recorded from each side; the
    refusal lists it once, word for word as for a link recorded on one side."""
    text = _refusal([OUT, BACK])
    assert text.count(CATALOG_LINE) == 1
    assert text == _refusal([OUT])


@pytest.mark.parametrize("records", list(LINK_RECORDS.values()), ids=list(LINK_RECORDS))
def test_every_recording_of_one_link_gives_the_same_refusal_text(records):
    assert _refusal(records) == _refusal([OUT])


def test_joins_at_keeps_one_join_per_link_with_the_concepts_of_all_its_records():
    """Each record's own concept evidence is kept on the one join."""
    back_other = _join(("archive", SHEET_FIELD), ("catalog", CATALOG_FIELD), "fitting")
    for records in ([OUT, back_other], [back_other, OUT]):
        assert ip.joins_at(_facts(records), "archive") == [
            ip.JoinEnd("catalog", CATALOG_FIELD, SHEET_FIELD, frozenset({"assembly", "fitting"}))]
    assert ip.joins_at(_facts([OUT, BACK_BARE]), "archive") == [
        ip.JoinEnd("catalog", CATALOG_FIELD, SHEET_FIELD, frozenset({"assembly"}))]


def test_joins_at_keeps_two_different_links_apart_in_the_order_the_facts_list_them():
    facts = _facts([VAULT_BACK, OUT, BACK, VAULT])
    assert [jp["id"] for jp in facts.join_paths][0] == OUT["id"]
    ends = ip.joins_at(facts, "archive")
    assert [(j.system, j.field, j.target_field) for j in ends] == [
        ("catalog", CATALOG_FIELD, SHEET_FIELD), ("vault", VAULT_FIELD, SHEET_FIELD)]


# --------------------------------------------------------------------------- #
# The refusal text: each sentence states its facts once
# --------------------------------------------------------------------------- #
def test_the_refusal_for_a_link_recorded_on_one_side_only():
    assert _refusal([OUT]) == (
        OPENING + f"The recorded join for that kind of record is {CATALOG_LINE}. "
        f"Read {CATALOG_FIELD} from catalog for that record and pass archive the value that "
        "join gives. " + CLOSING)


def test_the_refusal_for_a_link_recorded_on_both_sides():
    assert _refusal([OUT, BACK]) == _refusal([OUT])


@pytest.mark.parametrize("records", [[OUT, VAULT], [OUT, BACK, VAULT, VAULT_BACK]],
                         ids=["each-recorded-once", "each-recorded-on-both-sides"])
def test_the_refusal_for_two_different_joins(records):
    """Both joins are listed once each, and the reading step points at either
    of them rather than presenting the first as the only one."""
    assert _refusal(records) == (
        OPENING + f"The recorded joins for that kind of record are {CATALOG_LINE}; "
        f"{VAULT_LINE}. Use one of those joins: read its field from its system for that "
        "record and pass archive the value that join gives. " + CLOSING)


def test_the_refusal_never_says_to_pass_a_source_value_unchanged():
    """A join that rewrites its values (a pattern join) sits beside a
    same-value one. The refusal says to pass the target the value the chosen
    join gives, never the source field's own value, so a join whose transform
    changes the value is not read as a pass-through. The pattern join's
    concept comes from an identity rule over the same two fields."""
    pattern = {"id": "jp-vault-archive-pattern", "from_system": "vault",
               "from_field": VAULT_FIELD, "to_system": "archive", "to_field": SHEET_FIELD,
               "method": "value_format_match", "transform": "pattern:VR-{n}->SH-{n}"}
    rule = {"id": "ir-vault-archive", "concept": "assembly", "source_system": "vault",
            "source_field": VAULT_FIELD, "target_system": "archive", "target_field": SHEET_FIELD}
    facts = ip.facts_from(QUESTION, [
        {"system_id": s,
         "join_paths": [j for j in (OUT, pattern) if s in (j["from_system"], j["to_system"])],
         "identity_rules": [rule] if s in ("vault", "archive") else [],
         "value_mappings": []} for s in SYSTEMS], cap=50)
    (status, text), = _turn(facts, [("bench", "Which unit did rig 3 test?"),
                                    ("archive", f"Sheets for unit {UNIT}?")])[1:]
    assert status == "error"
    assert f"{CATALOG_LINE}; {VAULT_LINE}" in text
    assert "pass that value" not in text
    assert "pass archive the value that join gives" in text


def test_a_refusal_listing_some_of_its_joins_says_it_lists_some():
    extra = [_join((f"site-{n}", f"site_db.items.code_{n}"), ("archive", SHEET_FIELD),
                   "assembly") for n in range(3)]
    facts = ip.facts_from(QUESTION, [
        {"system_id": s, "join_paths": [j for j in [OUT, *extra]
                                        if s in (j["from_system"], j["to_system"])],
         "identity_rules": [], "value_mappings": []}
        for s in (*SYSTEMS, "site-0", "site-1", "site-2")], cap=50)
    candidate = (UNIT, ("bench", "tests", "unit_code"))
    text = ip.refusal_text(candidate, "archive", ip.joins_at(facts, "archive"))
    assert "The recorded joins for that kind of record include " in text
    assert text.count(" -> archive ") == 3


# --------------------------------------------------------------------------- #
# How a link was recorded changes no refusal decision
# --------------------------------------------------------------------------- #
SCENARIOS = {
    "bare-value-is-refused": (
        {}, [("bench", "Which unit?"), ("archive", f"Sheets for unit {UNIT}?")]),
    "repeat-goes-through": (
        {}, [("bench", "Which unit?"), ("archive", f"Sheets for {UNIT}?"),
             ("archive", f"Sheets for {UNIT}?")]),
    "joined-value-carried": (
        {}, [("bench", "Which unit?"), ("catalog", "Which model was on rig 3?"),
             ("archive", f"Sheets for {MODEL} (unit {UNIT})?")]),
    "value-in-question": (
        {"question": f"Which sheets cover unit {UNIT}?"},
        [("bench", "Which unit?"), ("archive", f"Sheets for {UNIT}?")]),
    "family-search": (
        {}, [("bench", "Which unit?"), ("archive", f"Sheets beginning {UNIT}?")]),
    "source-system-joined-to-target": (
        {"extra": [_join(("bench", "bench_db.rigs.rig_no"), ("archive", "docstore.sheets.rig"),
                         "rig")]},
        [("bench", "Which unit?"), ("archive", f"Sheets for {UNIT}?")]),
    "target-capped": (
        {"capped": ("archive",)}, [("bench", "Which unit?"), ("archive", f"Sheets for {UNIT}?")]),
    "value-of-another-concept": (
        {"unit_concept": "fixture"},
        [("bench", "Which unit?"), ("archive", f"Sheets for {UNIT}?")]),
}


def _statuses(records, options, calls) -> list:
    facts = _facts(records + options.get("extra", []),
                   question=options.get("question", QUESTION),
                   capped=options.get("capped", ()))
    return [status for status, _text in
            _turn(facts, calls, _answers(options.get("unit_concept", "assembly")))]


@pytest.mark.parametrize("scenario", list(SCENARIOS), ids=list(SCENARIOS))
def test_how_a_link_was_recorded_changes_no_decision(scenario):
    """Guard: every recording of the one link gives the same status for
    every call of each scenario, refused and passed alike."""
    options, calls = SCENARIOS[scenario]
    seen = {name: _statuses(records, options, calls) for name, records in LINK_RECORDS.items()}
    assert len({tuple(s) for s in seen.values()}) == 1, seen
    assert ("error" in seen["both-sides"]) == (scenario in (
        "bare-value-is-refused", "repeat-goes-through"))


def test_a_link_without_concept_evidence_on_either_record_still_passes():
    calls = [("bench", "Which unit?"), ("archive", f"Sheets for {UNIT}?")]
    for records in ([OUT_BARE], [BACK_BARE], [OUT_BARE, BACK_BARE]):
        assert [s for s, _t in _turn(_facts(records), calls)] == ["success", "success"]
