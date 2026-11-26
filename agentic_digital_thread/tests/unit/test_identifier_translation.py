# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deterministic identifier translation in the route seed.

Event 16: the rule ``B-{id}`` ↔ ``LOT-2026-0{id}`` was in the seed and the
orchestrator still asked the wrong system with the untranslated value. The
rules engine now applies the pattern to the identifiers in the question and
the seed says so; the model decides what to ask, not how identifiers translate.
"""
from agents import identifier_translation as it
from agents import orchestrator as orch


def _rule(**over):
    base = {"id": "ir-1", "concept": "batch", "source_system": "mes-x", "source_field": "batch_number",
            "source_pattern": "X-{id}", "target_system": "erp-x", "target_field": "material_lot",
            "target_pattern": "PLANT-2026-0{id}", "confidence": 0.28, "method": "value_core_match",
            "provenance": "50 of 50 sampled value pairs"}
    base.update(over)
    return base


def test_pattern_regex_needs_literal_text_and_matches_whole_tokens():
    assert it.pattern_regex("{id}") is None
    assert it.pattern_regex("*") is None
    rx = it.pattern_regex("X-{id}")
    assert rx.search("Which orders shipped from batch X-2847?").group(0) == "X-2847"
    assert rx.search("PLANT-2026-02847") is None
    assert rx.search("MAX-2847") is None  # not a whole token


def test_apply_transforms_and_refuses_partial_fills():
    assert it.apply("X-{id}", "PLANT-2026-0{id}", "X-2847") == "PLANT-2026-02847"
    assert it.apply("PLANT-2026-0{id}", "X-{id}", "PLANT-2026-02847") == "X-2847"
    assert it.apply("X-{id}", "PLANT-2026-0{id}", "Y-2847") is None
    assert it.apply("X-{id}", "PLANT-{site}-{id}", "X-2847") is None  # {site} unfilled


def test_find_translations_both_directions_ranked_and_deduped():
    rules = [_rule(), _rule(id="ir-2", target_field="batch_number", target_pattern="PLANT-2026-0{id}"),
             _rule(id="ir-3", method="onboarding-llm", provenance=None, confidence=0.9,
                   target_field="material_lot")]  # same pair as ir-1, LLM tier: loses to evidence
    out = it.find_translations("Which orders shipped from batch X-2847 and lot PLANT-2026-02900?", rules)
    got = sorted((t["token"], t["to_system"], t["to_field"], t["translated"], t["method"]) for t in out)
    assert got == sorted([
        ("X-2847", "erp-x", "batch_number", "PLANT-2026-02847", "value_core_match"),
        ("X-2847", "erp-x", "material_lot", "PLANT-2026-02847", "value_core_match"),
        ("PLANT-2026-02900", "mes-x", "batch_number", "X-2900", "value_core_match"),
    ])
    assert all(t["_tier"] == 1 for t in out)  # the LLM duplicate for the same pair lost
    assert it.find_translations("no identifiers here", rules) == []
    assert it.find_translations("", rules) == []


def test_render_is_empty_without_translations_and_names_method_and_confidence():
    assert it.render([]) == ""
    text = it.render(it.find_translations("batch X-2847", [_rule()]))
    assert text.startswith(it.HEADER)
    assert "- X-2847 (mes-x.batch_number) → PLANT-2026-02847 (erp-x.material_lot) [value_core_match, confidence 0.28]" in text


def _fake_query_factory():
    inventory = [{"system_id": "mes-x", "system_name": "MES X", "plants": [], "capabilities": None,
                  "connection_type": None, "query_guidance": None},
                 {"system_id": "erp-x", "system_name": "ERP X", "plants": [], "capabilities": None,
                  "connection_type": None, "query_guidance": None}]
    rows = [{"system_id": "mes-x", "system_name": "MES X", "plants": [], "capabilities": None,
             "connection_type": None, "query_guidance": None, "mapped_concepts": [], "shared_concepts": [],
             "join_paths": [], "value_mappings": [], "identity_rules": [_rule()]}]

    def fake_query(cypher, params=None):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        if cypher == orch._ROUTE_SEED_CYPHER:
            return rows
        return []
    return fake_query


def test_seed_is_byte_identical_without_a_question_and_gains_the_block_with_one():
    q = _fake_query_factory()
    plain = orch.build_route_seed(query=q)
    assert plain and it.HEADER not in plain
    assert orch.build_route_seed(query=q, question=None) == plain
    assert orch.build_route_seed(query=q, question="What is line L3 doing?") == plain  # no match → identical

    seeded = orch.build_route_seed(query=q, question="Which orders shipped from batch X-2847?")
    assert seeded.startswith(plain)
    assert "X-2847 (mes-x.batch_number) → PLANT-2026-02847 (erp-x.material_lot) [value_core_match" in seeded


# --- ADR-0086 characterization: non-wildcard outputs are byte-identical ------

_LEGACY_LINE = ("- X-2847 (mes-x.batch_number) → PLANT-2026-02847 (erp-x.material_lot) "
                "[value_core_match, confidence 0.28]\n")


def test_characterization_legacy_render_find_and_parse_are_byte_identical():
    found = it.find_translations("batch X-2847", [_rule()])
    assert found == [{"token": "X-2847", "from_system": "mes-x", "from_field": "batch_number",
                      "to_system": "erp-x", "to_field": "material_lot",
                      "translated": "PLANT-2026-02847", "method": "value_core_match",
                      "confidence": 0.28, "_tier": 1}]
    assert it.render(found) == it.HEADER + _LEGACY_LINE
    assert it.parse_rendered(it.render(found)) == [
        {"token": "X-2847", "translated": "PLANT-2026-02847", "from_system": "mes-x",
         "from_field": "batch_number", "to_system": "erp-x", "to_field": "material_lot",
         "method": "value_core_match"}]
    rx = it.pattern_regex("PLANT-2026-0{id}")
    assert rx.pattern == r"(?<![\w-])PLANT\-2026\-0(?P<id>\w+)(?![\w-])"


# --- ADR-0086: wildcard candidate sets ---------------------------------------

def _wild(**over):
    base = {"id": "ir-w", "concept": "part", "source_system": "sys-a", "source_field": "code",
            "source_pattern": "ITM-{id}", "target_system": "sys-b", "target_field": "item_id",
            "target_pattern": "PRT-{id}-R-{*}", "confidence": 0.85, "method": "value_core_match",
            "provenance": "3 of 3 sampled identifier cores"}
    base.update(over)
    return base


_CANDIDATE_LINE = ("- ITM-5101 (sys-a.code) → PRT-5101-R-{*} (sys-b.item_id) "
                   "[value_core_match, confidence 0.85] (candidate set: several records share "
                   "this identifier core; ask for every identifier matching the pattern and "
                   "choose among them)")


def test_pattern_regex_wildcard_matches_a_varying_whole_token_part():
    rx = it.pattern_regex("PRT-{id}-R-{*}")
    assert rx.fullmatch("PRT-5101-R-C").group("id") == "5101"
    assert rx.search("PRT-5101-R-") is None
    assert rx.search("XPRT-5101-R-C") is None
    assert it.pattern_regex("{*}") is None
    assert it.pattern_regex("{id}{*}") is None


def test_apply_keeps_the_wildcard_forward_and_is_exact_in_reverse():
    assert it.apply("ITM-{id}", "PRT-{id}-R-{*}", "ITM-5101") == "PRT-5101-R-{*}"
    assert it.apply("PRT-{id}-R-{*}", "ITM-{id}", "PRT-5101-R-C") == "ITM-5101"
    assert it.apply("PRT-{id}-R-{*}", "{id}", "PRT-5101-R-B") == "5101"
    assert it.apply("X-{id}", "PLANT-{site}-{id}", "X-2847") is None
    assert it.apply("X-{id}", "PLANT-{site}-{id}-{*}", "X-2847") is None


def test_find_translations_flags_only_candidate_sets():
    fwd = it.find_translations("status of ITM-5101", [_wild()])
    assert len(fwd) == 1 and fwd[0]["candidate_set"] is True
    assert fwd[0]["translated"] == "PRT-5101-R-{*}"
    rev = it.find_translations("what is PRT-5101-R-C", [_wild()])
    assert len(rev) == 1 and rev[0]["translated"] == "ITM-5101"
    assert "candidate_set" not in rev[0]


def test_render_and_parse_round_trip_the_candidate_set():
    text = it.render(it.find_translations("status of ITM-5101", [_wild()]))
    assert text == it.HEADER + _CANDIDATE_LINE + "\n"
    assert "(" not in it.CANDIDATE_SUFFIX[1:-1] and ")" not in it.CANDIDATE_SUFFIX[1:-1]
    back = it.parse_rendered(text + it.WILDCARD_SEED_NOTE)
    assert back == [{"token": "ITM-5101", "translated": "PRT-5101-R-{*}", "from_system": "sys-a",
                     "from_field": "code", "to_system": "sys-b", "to_field": "item_id",
                     "method": "value_core_match", "candidate_set": True}]


def test_mixed_block_parses_exact_and_candidate_lines():
    found = it.find_translations("batch X-2847 and ITM-5101", [_rule(), _wild()])
    back = it.parse_rendered(it.render(found))
    assert sorted((b["token"], b.get("candidate_set", False)) for b in back) == [
        ("ITM-5101", True), ("X-2847", False)]


def test_candidate_phrase_trailing_and_mid_wildcard():
    assert it.candidate_phrase("PRT-5101-R-{*}") == "every identifier beginning PRT-5101-R-"
    assert it.candidate_phrase("PRT-{*}-5101") == "every identifier matching PRT-*-5101"


import pytest  # noqa: E402


@pytest.mark.parametrize("token", ["PRT-5101-R-C-D-E", "PRT-5101-R-C_x", "PRT-5101-R--"])
def test_the_wildcard_matches_one_alphanumeric_token_only(token):
    # The emitter only turns a single alphanumeric token into {*}; the matcher
    # must not stretch it over separators or underscores.
    rx = it.pattern_regex("PRT-{id}-R-{*}")
    assert rx.search(token) is None
    assert it.apply("PRT-{id}-R-{*}", "ITM-{id}", token) is None
    assert it.find_translations(f"what is {token}?", [_wild(source_pattern="PRT-{id}-R-{*}",
                                                            target_pattern="ITM-{id}")]) == []
