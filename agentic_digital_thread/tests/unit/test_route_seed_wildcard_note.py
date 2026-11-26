# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The ONE ``{*}`` wildcard note in the Mode-A route seed (ADR-0086).

A seed whose IdentityRules carry no ``{*}`` is byte-identical to the
pre-wildcard seed; a seed with any wildcard rule gains
``identifier_translation.WILDCARD_SEED_NOTE`` exactly once, on its own line,
after the JSON payload and after the optional IDENTIFIER TRANSLATIONS block.
"""
from agents import identifier_translation as it
from agents import orchestrator as orch
from agents import route_seed_trace as trace


def _rule(**over):
    base = {"id": "ir-1", "concept": "part", "source_system": "sys-a", "source_field": "code",
            "source_pattern": "X-{id}", "target_system": "sys-b", "target_field": "item_id",
            "target_pattern": "PLANT-2026-0{id}", "confidence": 0.85, "method": "value_core_match",
            "provenance": "3 of 3 sampled identifier cores"}
    base.update(over)
    return base


def _wild(**over):
    return _rule(**{"id": "ir-w", "source_pattern": "{id}", "target_pattern": "PRT-{id}-R-{*}",
                    **over})


def _query(rules):
    inventory = [{"system_id": sid, "system_name": sid.upper(), "plants": [], "capabilities": None,
                  "connection_type": None, "query_guidance": None} for sid in ("sys-a", "sys-b")]
    rows = [{**inventory[0], "mapped_concepts": [], "shared_concepts": [], "join_paths": [],
             "value_mappings": [], "identity_rules": list(rules)}]

    def fake_query(cypher, params=None):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        if cypher == orch._ROUTE_SEED_CYPHER:
            return rows
        return []
    return fake_query


def _raw_seed(monkeypatch, rules):
    """The seed with the optional trailing block disabled (header + JSON only)."""
    with monkeypatch.context() as m:
        m.setattr(orch, "_identifier_translation_block", lambda *_a: "")
        return orch.build_route_seed(query=_query(rules))


def test_wildcard_rule_appends_the_note_once_on_its_own_line(monkeypatch):
    raw = _raw_seed(monkeypatch, [_wild()])
    seed = orch.build_route_seed(query=_query([_wild()]), question=None)
    assert seed == raw + "\n" + it.WILDCARD_SEED_NOTE
    assert seed.count(it.WILDCARD_SEED_NOTE) == 1
    assert seed.endswith(it.WILDCARD_SEED_NOTE)


def test_wildcard_in_the_source_pattern_also_appends_the_note(monkeypatch):
    rule = _wild(source_pattern="PRT-{id}-R-{*}", target_pattern="{id}")
    raw = _raw_seed(monkeypatch, [rule])
    assert orch.build_route_seed(query=_query([rule])) == raw + "\n" + it.WILDCARD_SEED_NOTE


def test_two_wildcard_rules_still_append_one_note():
    rules = [_wild(), _wild(id="ir-w2", target_field="alt_id", target_pattern="DOC-{id}-{*}")]
    seed = orch.build_route_seed(query=_query(rules))
    assert seed.count(it.WILDCARD_SEED_NOTE) == 1
    assert seed.count("IDENTITY-RULE WILDCARDS:") == 1


def test_note_leaves_the_payload_decodable_and_the_translation_parse_empty():
    seed = orch.build_route_seed(query=_query([_wild()]))
    payload = trace._payload(seed)
    assert isinstance(payload, dict)
    assert payload["systems"]
    assert it.parse_rendered(seed) == []


def test_note_follows_the_translation_block_without_a_blank_line():
    rules = [_rule(), _wild()]
    seed = orch.build_route_seed(query=_query(rules), question="Which runs used batch X-2847?")
    block = it.render(it.find_translations("Which runs used batch X-2847?", rules))
    assert block and seed.endswith(block + it.WILDCARD_SEED_NOTE)
    assert [t["token"] for t in it.parse_rendered(seed)] == ["X-2847"]


def test_seed_without_a_wildcard_rule_is_byte_identical(monkeypatch):
    rules = [_rule()]
    raw = _raw_seed(monkeypatch, rules)
    assert orch.build_route_seed(query=_query(rules)) == raw
    assert orch.build_route_seed(query=_query(rules), question="What is line L3 doing?") == raw
    question = "Which runs used batch X-2847?"
    expected = raw + it.render(it.find_translations(question, rules))
    seeded = orch.build_route_seed(query=_query(rules), question=question)
    assert seeded == expected
    assert it.WILDCARD_SEED_NOTE not in seeded


def test_note_failure_drops_the_optional_block_and_keeps_the_seed(monkeypatch):
    raw = _raw_seed(monkeypatch, [_wild()])

    def boom(_rules):
        raise RuntimeError("note failed")
    monkeypatch.setattr(it, "wildcard_note", boom)
    assert orch._identifier_translation_block(None, [{"identity_rules": [_wild()]}]) == ""
    assert orch.build_route_seed(query=_query([_wild()])) == raw


def test_emergency_bounded_seed_carries_no_note(monkeypatch):
    monkeypatch.setattr(orch, "_serialize_route_seed",
                        lambda _p, _b=None: "x" * (orch._ROUTE_SEED_SERIALIZED_BUDGET + 1))
    seed = orch.build_route_seed(query=_query([_wild()]))
    assert seed is not None
    assert seed.startswith(orch._ROUTE_SEED_PARTIAL_HEADER)
    assert it.WILDCARD_SEED_NOTE not in seed


def _guided_query(rules, guidance="Use the item table for part lookups. " * 12):
    """Two systems with trimmable query guidance, so a fit can make room."""
    inventory = [{"system_id": sid, "system_name": sid.upper(), "plants": [], "capabilities": None,
                  "connection_type": None, "query_guidance": guidance} for sid in ("sys-a", "sys-b")]
    rows = [{**inventory[0], "mapped_concepts": [], "shared_concepts": [], "join_paths": [],
             "value_mappings": [], "identity_rules": list(rules)}]

    def fake_query(cypher, params=None):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        if cypher == orch._ROUTE_SEED_CYPHER:
            return rows
        return []
    return fake_query


def test_note_only_reserve_includes_the_leading_separator(monkeypatch):
    """With no translation or owner before it, the note is
    emitted as "\\n" + note, so its reserve is len(note) + 1. At a budget of
    exactly json + 1 + len(note), and at the old reserve boundary json + len(note),
    the note is delivered and the seed stays within budget."""
    query, question = _guided_query([_wild()]), "what changed this week?"
    raw = _raw_seed(monkeypatch, [_wild()])
    json_len = len(raw)
    assert orch._tail_room_reserves(question, query(orch._ROUTE_SEED_CYPHER)) == (
        len(it.WILDCARD_SEED_NOTE) + 1,)
    for budget in (json_len + 1 + len(it.WILDCARD_SEED_NOTE),):
        monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", budget)
        seed = orch.build_route_seed(query=_query([_wild()]), question=question)
        assert len(seed) <= budget and seed.endswith("\n" + it.WILDCARD_SEED_NOTE)
    # The old reserve boundary: the fit must make room for the separator too.
    guided_raw = None
    with monkeypatch.context() as m:
        m.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", 50_000)
        m.setattr(orch, "_identifier_translation_block", lambda *_a: "")
        guided_raw = orch.build_route_seed(query=query)
    budget = len(guided_raw) + len(it.WILDCARD_SEED_NOTE)
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", budget)
    seed = orch.build_route_seed(query=query, question=question)
    assert len(seed) <= budget and seed.endswith("\n" + it.WILDCARD_SEED_NOTE)
