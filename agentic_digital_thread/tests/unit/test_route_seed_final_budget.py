# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The composed route seed never exceeds the budget.

The case: a rule-heavy but valid graph (two systems, many
evidence-backed exact rules, one wildcard rule) with an exact-shape identifier
question. The plain fit lands close to the budget, so no owners reserve is
affordable and the floor keeps the plain fit. The identifier tail (owners
block, translations, the ~954-char wildcard note) must be fitted into the room
left, never appended past ``_ROUTE_SEED_SERIALIZED_BUDGET``.
"""
from __future__ import annotations

import hashlib

import pytest

from agents import identifier_translation as it
from agents import identity_affinity as ia
from agents import orchestrator as orch
from agents.connection_types import POSTGRESQL, default_capabilities

#: An identifier whose literal shape matches the exact joins below, so the
#: question yields an IDENTIFIER OWNERS block.
RUN_NUMBER = "RUN-20261008-90001"
QUESTION = (f"Was machine L3-M1 maintained on schedule before production run "
            f"{RUN_NUMBER} ran?")

_A, _B = "system-a", "system-b"


def _exact_rule(idx: int) -> tuple[dict, dict]:
    """One evidence-backed exact IdentityRule + its JoinPath (tier 1)."""
    src = ia.SampledField(_A, f"production_runs_{idx}", "run_number", "batch",
                          (RUN_NUMBER,) * 40)
    tgt = ia.SampledField(_B, f"shipment_items_{idx}", "batch_id", "batch",
                          (RUN_NUMBER,) * 400)
    reasoning = ia._exact_reasoning(src, tgt, ia.literal_shape(RUN_NUMBER),
                                    set(range(19)), "batch")
    digest = hashlib.sha256(f"{idx}".encode()).hexdigest()[:16]
    rule = {"id": f"ir-{_A}-{digest}", "concept": "batch", "source_system": _A,
            "source_field": f"production_runs_{idx}.run_number", "target_system": _B,
            "target_field": f"shipment_items_{idx}.batch_id", "source_pattern": "",
            "target_pattern": "", "method": ia.EXACT_METHOD, "confidence": 0.85,
            "provenance": reasoning}
    join = {"id": f"jp-{_A}-{digest}", "from_system": _A,
            "from_field": f"a_schema.production_runs_{idx}.run_number", "to_system": _B,
            "to_field": f"b_schema.shipment_items_{idx}.batch_id", "transform": "identity",
            "method": ia.EXACT_METHOD, "confidence": 0.85, "provenance": reasoning}
    return rule, join


def _wildcard_rule(src_field: str = "products.item_id",
                   tgt_field: str = "products.part_number") -> tuple[dict, dict]:
    """A pattern rule carrying a ``{*}`` wildcard, so ``wildcard_note`` fires
    and the ~954-char note is appended to the tail (ADR-0086)."""
    rule = {"id": f"ir-{_A}-wild", "concept": "product", "source_system": _A,
            "source_field": src_field, "target_system": _B,
            "target_field": tgt_field, "source_pattern": "PN-{id}",
            "target_pattern": "PART-{id}-{*}", "method": "value_format_match",
            "confidence": 0.8, "provenance": "pattern probed"}
    join = {"id": f"jp-{_A}-wild", "from_system": _A, "from_field": "a_schema.products.item_id",
            "to_system": _B, "to_field": "b_schema.products.part_number",
            "transform": "pattern", "method": "value_format_match", "confidence": 0.8}
    return rule, join


def _graph(n_exact: int, wild=None):
    exact = [_exact_rule(i) for i in range(n_exact)]
    wild = wild or _wildcard_rule()
    identity_rules = [r for r, _j in exact] + [wild[0]]
    join_paths = [j for _r, j in exact] + [wild[1]]
    rows = {
        _A: {"system_id": _A, "system_name": "System A", "connection_type": POSTGRESQL,
             "identity_rules": list(identity_rules), "join_paths": list(join_paths),
             "shared_concepts": [], "value_mappings": []},
        _B: {"system_id": _B, "system_name": "System B", "connection_type": POSTGRESQL,
             "identity_rules": list(identity_rules), "join_paths": list(join_paths),
             "shared_concepts": [], "value_mappings": []},
    }
    inventory = [{"system_id": sid, "system_name": rows[sid]["system_name"],
                  "plants": ["plant-1"], "capabilities": default_capabilities(POSTGRESQL),
                  "connection_type": POSTGRESQL,
                  "query_guidance": default_capabilities(POSTGRESQL)}
                 for sid in (_A, _B)]

    def query(cypher, *_a, **_k):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        if cypher == orch._ROUTE_SEED_CYPHER:
            return [rows[_A], rows[_B]]
        return []
    return query


def test_composed_seed_with_unaffordable_reserve_stays_within_budget():
    """Exact owners + a wildcard note whose reserve the floor
    cannot afford. The composed return must not exceed the budget; the tail is
    dropped in priority order rather than appended past it."""
    query = _graph(n_exact=29)
    rows = query(orch._ROUTE_SEED_CYPHER)
    # The scenario genuinely exercises the tail: a wildcard note and owners are
    # computed from the graph (else the repro would prove nothing).
    _translations, owners, note = orch._identifier_block_parts(QUESTION, rows)
    assert note == it.WILDCARD_SEED_NOTE and owners

    seed = orch.build_route_seed(query=query, question=QUESTION)
    assert seed is not None
    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET, (
        f"composed seed {len(seed)} > budget {orch._ROUTE_SEED_SERIALIZED_BUDGET}")


def test_tail_components_are_dropped_in_priority_order_under_a_tight_room():
    """Priority (room too small for the whole tail), most valuable kept first:
    (1) translations — the exact deterministic value, degraded line by line, the
    correctness floor; (2) the wildcard note — the safety instruction, ranked
    ABOVE advisory owners when a wildcard applies; (3) owners
    — advisory routing, smallest form then omitted, last."""
    translations = "IDENTIFIER TRANSLATIONS:\n- A -> B\n"
    owners = it.find_owners(QUESTION, *_owner_inputs())
    note = it.WILDCARD_SEED_NOTE
    whole = orch._compose_identifier_tail(translations, owners, note, None)
    assert translations in whole and it.OWNERS_HEADER in whole and note in whole

    # Room for translations + note but not the owners block: owners go, the
    # wildcard safety note survives (it outranks advisory owners).
    room = len(translations) + len(note) + 5
    tight = orch._compose_identifier_tail(translations, owners, note, room)
    assert len(tight) <= room and translations in tight and note in tight
    assert it.OWNERS_HEADER not in tight

    # Room for only the translations: owners and note both dropped.
    only = orch._compose_identifier_tail(translations, owners, note, len(translations) + 1)
    assert only == translations


def _owner_inputs():
    rows = _graph(n_exact=1)(orch._ROUTE_SEED_CYPHER)
    rules = orch._seed_identity_rules(rows)
    return rules, orch._seed_rule_dicts(rows, "join_paths")


# A product identifier that matches the wildcard pattern rule PN-{id} -> PART-{id}-{*}
# but carries a shape NO exact owner rule shares, so the pressured seed has a
# deterministic translation and the wildcard note but ZERO owners.
PRODUCT_ID = "PN-123"
TRANSLATION_ONLY_QUESTION = f"Show the lifecycle record for {PRODUCT_ID}"


def _prose_pressured_graph(n_systems: int = 8, guidance_len: int = 420):
    """An ``n_systems``-system graph pressured to NEAR budget by long per-system
    query-guidance prose (TRIMMABLE by the shrink pass), carrying the wildcard
    pattern rule but NO exact owner for PN-123. The plain fit lands within one
    translation of the budget, so the old composer dropped the whole
    translation block into the accidental remainder; the reserve must trim the
    prose further to keep the deterministic translation. Nothing
    tier-0/1 is at risk — only advisory prose gives way."""
    wild = _wildcard_rule()
    guide = "G" * guidance_len
    sids = [f"sys-{i:02d}" for i in range(n_systems)]
    rows = {s: {"system_id": s, "system_name": s.upper(), "connection_type": POSTGRESQL,
                "identity_rules": [wild[0]], "join_paths": [wild[1]],
                "shared_concepts": [], "value_mappings": []} for s in sids}
    inventory = [{"system_id": s, "system_name": s.upper(), "plants": ["plant-1"],
                  "capabilities": default_capabilities(POSTGRESQL),
                  "connection_type": POSTGRESQL, "query_guidance": guide} for s in sids]

    def query(cypher, *_a, **_k):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        if cypher == orch._ROUTE_SEED_CYPHER:
            return [rows[s] for s in sids]
        return []
    return query


@pytest.mark.usefixtures("route_seed_pressure_budget")
def test_translation_and_wildcard_survive_under_pressure_without_owners():
    """A wildcard pattern translation with NO exact owner,
    pressured to near budget by trimmable guidance prose. The final length must
    not be made safe by dropping the deterministic translation (or the wildcard
    note) into the accidental remainder, which would send the original
    identifier to the target. The translated value and the wildcard semantics
    survive (the reserve trims prose for them), within the 10,000
    ceiling."""
    query = _prose_pressured_graph()
    rows = query(orch._ROUTE_SEED_CYPHER)
    translations, owners, note = orch._identifier_block_parts(TRANSLATION_ONLY_QUESTION, rows)
    # The scenario genuinely has a translation + wildcard note but no owners.
    assert translations and "PART-123-" in translations
    assert note == it.WILDCARD_SEED_NOTE and not owners
    # Pressured: the plain (no-question) seed leaves less than one translation of room.
    plain = orch.build_route_seed(query=query)
    assert 0 < orch._ROUTE_SEED_SERIALIZED_BUDGET - len(plain) < len(translations)

    seed = orch.build_route_seed(query=query, question=TRANSLATION_ONLY_QUESTION)
    assert seed is not None
    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET, (
        f"composed seed {len(seed)} > budget {orch._ROUTE_SEED_SERIALIZED_BUDGET}")
    assert it.HEADER in seed, "the deterministic translations block was dropped"
    assert "PART-123-" in seed, "the translated value did not survive"
    # The wildcard semantics survive: the candidate-set suffix on the translation
    # line and/or the wildcard note.
    assert it.CANDIDATE_SUFFIX in seed or it.WILDCARD_SEED_NOTE in seed


def test_composed_seed_stays_within_budget_when_the_reserve_is_unaffordable():
    """A pure
    tier-1 wall (29 exact rules, two systems, one wildcard rule) leaves ~281
    characters after the plain fit, less than the full translation line. The
    matched deterministic translation must still survive — its translated value
    AND its candidate-set (wildcard) semantics — and the seed stays <= 10,000."""
    query = _graph(n_exact=29)
    seed = orch.build_route_seed(query=query, question=TRANSLATION_ONLY_QUESTION)
    assert seed is not None
    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    assert "PART-123-{*}" in seed, "the translated value did not survive the tier-1 wall"
    assert "candidate set" in seed and "never fill {*}" in seed, "wildcard semantics lost"


def test_compact_translation_form_keeps_value_target_and_wildcard_semantics():
    """The last degradation step before a translation is dropped is a compact
    line: source value -> translated value, target system/field, candidate-set
    semantics; no evidence prose."""
    rows = _graph(1)(orch._ROUTE_SEED_CYPHER)
    items = it.find_translations(TRANSLATION_ONLY_QUESTION, orch._seed_identity_rules(rows))
    full = it.render(items)
    compact = it.render_compact(items)
    assert len(compact) < len(full)
    # "system: field" — exact even with a dotted, table-qualified field.
    assert "PN-123 (system-a: products.item_id) → PART-123-{*} (system-b: products.part_number)" in compact
    assert it.parse_rendered(compact)[0]["translated"] == "PART-123-{*}"
    assert it.parse_rendered(compact)[0]["candidate_set"] is True
    assert "candidate set" in compact and "never fill {*}" in compact
    assert "confidence" not in compact and "value_format_match" not in compact
    # The composer falls back to it when the full first line does not fit.
    room = len(compact) + 1
    tail = orch._compose_identifier_tail(full, [], "", room, items=items)
    assert tail == compact and len(tail) <= room


def test_note_outranks_owners_when_room_fits_translation_plus_note_only():
    """Room enough for translation + note but not
    translation + owners + note. The wildcard safety note survives and the
    advisory owners give way (one owner must never evict
    the 953-char note). Composed tail stays within its room (the 10,000 ceiling
    end-to-end is covered by the pressured-seed test above)."""
    translations = it.render(it.find_translations(
        TRANSLATION_ONLY_QUESTION, orch._seed_identity_rules(_graph(1)(orch._ROUTE_SEED_CYPHER))))
    assert translations
    owners = it.find_owners(QUESTION, *_owner_inputs())
    assert owners  # a real owner is offered but must yield to the note
    note = it.WILDCARD_SEED_NOTE

    room = len(translations) + len(note)
    tail = orch._compose_identifier_tail(translations, owners, note, room)
    assert len(tail) <= room
    assert translations in tail and note in tail
    assert it.OWNERS_HEADER not in tail


def test_translations_degrade_line_by_line_not_whole_block():
    """A matched deterministic translation is the correctness floor: when room
    is tight it loses its LAST lines, never the whole block, so the first
    (highest-priority) translated value survives."""
    header = it.HEADER
    line_a = "- AAA (s.f) → BBB (t.g) [confirmed]\n"
    line_b = "- CCC (s.f) → DDD (t.g) [confirmed]\n"
    translations = header + line_a + line_b
    full = orch._compose_identifier_tail(translations, [], "", None)
    assert full == translations
    # Room for the header + first line only: the second line is dropped, not both.
    room = len(header) + len(line_a) + 1
    trimmed = orch._compose_identifier_tail(translations, [], "", room)
    assert len(trimmed) <= room
    assert header in trimmed and line_a in trimmed and line_b not in trimmed


_C, _D = "system-c", "system-d"


def _pair_rule(src, tgt, idx, *, method=ia.EXACT_METHOD, provenance="measured"):
    """One rule + join between ``src`` and ``tgt`` (tier 1 by default; tier 0 with
    a steward-confirmed method)."""
    digest = hashlib.sha256(f"{src}-{tgt}-{idx}".encode()).hexdigest()[:16]
    rule = {"id": f"ir-{src}-{digest}", "concept": "batch", "source_system": src,
            "source_field": f"orders_{idx}.order_number", "target_system": tgt,
            "target_field": f"receipts_{idx}.order_ref", "source_pattern": "",
            "target_pattern": "", "method": method, "confidence": 0.85,
            "provenance": provenance}
    join = {"id": f"jp-{src}-{digest}", "from_system": src,
            "from_field": f"s.orders_{idx}.order_number", "to_system": tgt,
            "to_field": f"t.receipts_{idx}.order_ref", "transform": "identity",
            "method": method, "confidence": 0.85, "provenance": provenance}
    return rule, join


def _four_system_wall(n_ab: int, n_cd: int, tier0_cd: int = 1, wild=None):
    """The translation's own systems (a, b) carry ``n_ab`` tier-1 rules plus the
    wildcard rule; two UNRELATED systems (c, d) carry ``n_cd`` tier-1 rules and
    ``tier0_cd`` steward-confirmed ones. Nothing tier-2 or prose is trimmable."""
    ab = [_exact_rule(i) for i in range(n_ab)] + [wild or _wildcard_rule()]
    cd = ([_pair_rule(_C, _D, i) for i in range(n_cd)]
          + [_pair_rule(_C, _D, 1000 + i, method="user_confirmed") for i in range(tier0_cd)])
    rows = {}
    for sid, pairs in ((_A, ab), (_B, ab), (_C, cd), (_D, cd)):
        rows[sid] = {"system_id": sid, "system_name": sid.upper(), "connection_type": POSTGRESQL,
                     "identity_rules": [r for r, _j in pairs],
                     "join_paths": [j for _r, j in pairs],
                     "shared_concepts": [], "value_mappings": []}
    inventory = [{"system_id": sid, "system_name": sid.upper(), "plants": ["plant-1"],
                  "capabilities": None, "connection_type": POSTGRESQL, "query_guidance": None}
                 for sid in (_A, _B, _C, _D)]

    def query(cypher, *_a, **_k):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        if cypher == orch._ROUTE_SEED_CYPHER:
            return [rows[s] for s in (_A, _B, _C, _D)]
        return []
    return query


def _rule_ids(seed: str) -> set:
    import json
    payload, _end = json.JSONDecoder().raw_decode(seed[seed.index("RESULT:\n") + len("RESULT:\n"):])
    return {(system["id"], rule.get("id")) for system in payload["systems"]
            for rules in (system.get("rules") or {}).values() for rule in rules}


@pytest.mark.usefixtures("route_seed_pressure_budget")
def test_unrelated_tier1_rules_yield_when_even_the_compact_translation_cannot_fit(monkeypatch):
    """When even the compact translation cannot fit,
    tier-1 rules on systems the translation does not touch yield to it first,
    lowest priority first. Never a tier-0 rule; here no rule on the
    translation's own systems is needed; the seed stays within budget with no extra fit.

    Budget pinned to 9,700 (below the fixture's 10,000): the route-seed header
    grew for the value-mapping decision and again for the stored-values
    wording, shifting the pressure window, so the full wall at 10,000 leaves room
    for the full translation form. 9,700 reproduces the exact window this test
    exercises (compact form only, reached by yielding unrelated tier-1 rules),
    with the wall fixture unchanged."""
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", 9_700)
    query = _four_system_wall(29, 20)
    with monkeypatch.context() as m:  # the fit without step (b)
        m.setattr(orch, "_yield_rules_for_translation", lambda *_a, **_k: None)
        baseline = orch.build_route_seed(query=query, question=TRANSLATION_ONLY_QUESTION)
    assert "PART-123-{*}" not in baseline  # the compact form alone cannot fit here

    fits = []
    real = orch._held_fit
    monkeypatch.setattr(orch, "_held_fit", lambda *a, **k: (fits.append(1), real(*a, **k))[1])
    seed = orch.build_route_seed(query=query, question=TRANSLATION_ONLY_QUESTION)
    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET and len(fits) <= 3
    assert it.COMPACT_HEADER in seed and "PART-123-{*}" in seed and "never fill {*}" in seed
    before, after = _rule_ids(baseline), _rule_ids(seed)
    own = {(s, r) for s, r in before if s in (_A, _B)}
    assert own <= after, "a rule on the translation's own systems was yielded"
    tier0 = {(s, r) for s, r in before if r and r.startswith(f"ir-{_C}-")
             and r in {_pair_rule(_C, _D, 1000)[0]["id"]}}
    assert tier0 <= after, "a tier-0 rule was yielded"
    yielded = before - after
    assert yielded and all(s in (_C, _D) for s, _r in yielded)


# ── Exact translation identities; question-relevant rules kept ──

from agents import route_seed_trace as trace  # noqa: E402

#: Table-qualified ~100-char translation fields: the compact line no longer fits
#: the room a tier-1 wall leaves, and a dotted field must never be mistaken for
#: part of a system id.
_LONG_SRC = "engineering_domain." + "products_released_for_manufacturing_" + "x" * 30 + ".item_id"
_LONG_TGT = "erp_domain." + "products_released_for_procurement_" + "y" * 40 + ".part_number"


def test_compact_parse_rendered_preserves_exact_system_and_field_ids():
    items = [{"token": "PN-123", "from_system": _A, "from_field": "products.item_id",
              "to_system": _B, "to_field": "products.part_number",
              "translated": "PART-123-{*}", "candidate_set": True}]
    back = it.parse_rendered(it.render_compact(items))
    assert [(t["from_system"], t["from_field"], t["to_system"], t["to_field"]) for t in back] == [
        (_A, "products.item_id", _B, "products.part_number")]
    assert back[0]["translated"] == "PART-123-{*}" and back[0]["candidate_set"] is True


def test_route_seed_trace_names_the_exact_translation_systems():
    """The user-visible Knowledge Graph trace must name System A / System B with
    the table-qualified field, never a nonexistent ``system-a.products``, for both
    the full form and the compact form (the 29-rule wall)."""
    for query in (_graph(1), _graph(29)):
        seed = orch.build_route_seed(query=query, question=TRANSLATION_ONLY_QUESTION)
        text = trace.seed_step_text(seed)
        assert "System A (products.item_id)" in text, text
        assert "System B (products.part_number)" in text, text
        assert "system-a.products" not in text and "system-b.products" not in text


@pytest.mark.usefixtures("route_seed_pressure_budget")
def test_yield_never_removes_rules_on_systems_with_table_qualified_fields(monkeypatch):
    """Step (b) reads the translation's identities from the structured items, so a
    ~100-char table-qualified field cannot hide system-a/system-b from it: rules
    on systems unrelated to the translation yield FIRST, so here every A/B rule
    survives."""
    query = _four_system_wall(20, 1, wild=_wildcard_rule(_LONG_SRC, _LONG_TGT))
    with monkeypatch.context() as m:
        m.setattr(orch, "_yield_rules_for_translation", lambda *_a, **_k: None)
        baseline = orch.build_route_seed(query=query, question=TRANSLATION_ONLY_QUESTION)
    assert "PART-123-{*}" not in baseline  # the long compact line needs step (b)
    seed = orch.build_route_seed(query=query, question=TRANSLATION_ONLY_QUESTION)
    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET and "PART-123-{*}" in seed
    before, after = _rule_ids(baseline), _rule_ids(seed)
    assert {x for x in before if x[0] in (_A, _B)} <= after, "an A/B rule was yielded"
    assert before - after and all(s in (_C, _D) for s, _r in before - after)
    text = trace.seed_step_text(seed)
    assert f"{_A.upper()} ({_LONG_SRC})" in text and f"{_B.upper()} ({_LONG_TGT})" in text


def test_yield_keeps_a_join_the_question_names(monkeypatch):
    """A question naming the C->D join by its fields: that tier-1 join is
    question-relevant, so it never yields to the translation."""
    question = ("For system-c orders_0, join to system-d receipts_0 and show the "
                "lifecycle record for PN-123")
    _assert_named_cd_join_kept(monkeypatch, question)


def _assert_named_cd_join_kept(monkeypatch, question):
    query = _four_system_wall(20, 1, wild=_wildcard_rule(_LONG_SRC, _LONG_TGT))
    with monkeypatch.context() as m:
        m.setattr(orch, "_yield_rules_for_translation", lambda *_a, **_k: None)
        baseline = orch.build_route_seed(query=query, question=question)
    named = {x for x in _rule_ids(baseline) if x[0] in (_C, _D)
             and x[1] in {_pair_rule(_C, _D, 0)[0]["id"], _pair_rule(_C, _D, 0)[1]["id"]}}
    assert named, "the fixture must deliver the named C->D join before step (b)"
    seed = orch.build_route_seed(query=query, question=question)
    assert named <= _rule_ids(seed), "the question-named C->D join was yielded"
    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    return seed


# ── Step (b) protects the producing rule and question-relevant rules ───

WILD_ID = f"ir-{_A}-wild"
_LONG_IDS = {_A: "plm-engineering-domain01", _B: "erp-procurement-ledger02"}


def _renamed(query, mapping: dict):
    """``query`` with every system id in ``mapping`` replaced throughout."""
    import json

    def swap(obj):
        text = json.dumps(obj)
        for old, new in mapping.items():
            text = text.replace(old, new)
        return json.loads(text)

    def renamed(cypher, *a, **k):
        return swap(query(cypher, *a, **k))
    return renamed


def test_question_naming_only_the_systems_keeps_their_join(monkeypatch):
    """The question names system-c and system-d (not their
    fields). Their C->D join is question-relevant and must survive step (b)."""
    seed = _assert_named_cd_join_kept(
        monkeypatch, "Use system-c and system-d, then show lifecycle PN-123")
    assert "PART-123-{*}" in seed  # unrelated A/B summaries yielded instead


@pytest.mark.parametrize("pad,ids", [(4, None), (0, _LONG_IDS), (8, _LONG_IDS)])
def test_two_system_wall_keeps_the_translation_at_ordinary_endpoint_lengths(pad, ids, monkeypatch):
    """The 29-rule two-system wall with longer table-qualified
    fields and/or 24-character system ids. The compact line no longer fits, and a
    two-system graph has no unrelated system, so unrelated tier-1 summaries on
    the translation's own systems yield. The translated value and candidate-set
    instruction survive under 10,000; the producing rule is never removed."""
    wild = _wildcard_rule("products" + "x" * pad + ".item_id",
                          "products" + "y" * pad + ".part_number")
    query, producing = _graph(29, wild=wild), WILD_ID
    if ids:
        query, producing = _renamed(query, ids), WILD_ID.replace(_A, ids[_A])
    with monkeypatch.context() as m:  # the fit without step (b)
        m.setattr(orch, "_yield_rules_for_translation", lambda *_a, **_k: None)
        baseline = orch.build_route_seed(query=query, question=TRANSLATION_ONLY_QUESTION)
    assert "PART-123-{*}" not in baseline  # the compact line alone no longer fits: needs step (b)
    seed = orch.build_route_seed(query=query, question=TRANSLATION_ONLY_QUESTION)
    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    assert "PART-123-{*}" in seed and "candidate set" in seed
    kept = {r for _s, r in _rule_ids(seed)}
    if producing in {r for _s, r in _rule_ids(baseline)}:
        assert producing in kept, "the producing rule was yielded"


def test_the_producing_rule_never_yields(monkeypatch):
    """Step (b) never removes the rule that produced the translation, even when
    it is a tier-1 rule on the translation's own systems: with the pool reduced
    to only that rule, nothing is removed and the translation is dropped."""
    wild_rule, wild_join = _wildcard_rule("products" + "x" * 8 + ".item_id",
                                          "products" + "y" * 8 + ".part_number")
    rows = orch._seed_identity_rules(_graph(1, wild=(wild_rule, wild_join))(orch._ROUTE_SEED_CYPHER))
    items = orch._translation_items(TRANSLATION_ONLY_QUESTION, [{"identity_rules": rows}])
    assert items and items[0]["rule_ids"] == {WILD_ID}
    payload = {"systems": [{"id": _A, "rules": {"identity_rules": [dict(wild_rule)]}}]}
    pool = orch._yield_pool(payload, {_A, _B}, {WILD_ID}, TRANSLATION_ONLY_QUESTION)
    assert pool == []


def test_translation_dropped_when_every_tier1_rule_is_question_relevant(monkeypatch, caplog):
    """When the question names both translation systems every rule on them is
    relevant; with nothing eligible the translation is dropped (logged) and the
    seed is exactly the fit without step (b)."""
    wild = _wildcard_rule("products" + "x" * 8 + ".item_id", "products" + "y" * 8 + ".part_number")
    query = _graph(29, wild=wild)
    question = "In system-a and system-b, show the lifecycle record for PN-123"
    with monkeypatch.context() as m:
        m.setattr(orch, "_yield_rules_for_translation", lambda *_a, **_k: None)
        baseline = orch.build_route_seed(query=query, question=question)
    with caplog.at_level("WARNING", logger="agents.orchestrator"):
        seed = orch.build_route_seed(query=query, question=question)
    assert seed == baseline and "ROUTE_SEED_TRANSLATION_DROPPED" in caplog.text


def test_full_form_endpoint_of_an_unknown_system_stays_one_label():
    """With only ``mes`` known, ``mes-ind.table.col`` must not be
    split into an invented system ``mes-ind.table`` and field ``col``."""
    item = {"token": "X-1", "from_system": "mes-ind", "from_field": "table.col",
            "to_system": "mes", "to_field": "t.c", "translated": "Y-1", "method": "m",
            "confidence": None}
    back = it.parse_rendered(it.render([item]), systems=["mes"])
    assert (back[0]["from_system"], back[0]["from_field"]) == ("mes-ind.table.col", "")
    assert (back[0]["to_system"], back[0]["to_field"]) == ("mes", "t.c")


def test_trace_with_a_translation_system_missing_from_the_inventory():
    """Partial inventory: system-b is not delivered, so the trace keeps its full
    endpoint as one label rather than naming a system ``system-b.products``."""
    base = _graph(1)

    def query(cypher, *a, **k):
        rows = base(cypher, *a, **k)
        if cypher in (orch._ROUTE_SEED_INVENTORY_CYPHER, orch._ROUTE_SEED_CYPHER):
            return [r for r in rows if r["system_id"] == _A]
        return rows
    text = trace.seed_step_text(orch.build_route_seed(query=query, question=TRANSLATION_ONLY_QUESTION))
    assert "system-b.products.part_number" in text, text
    assert "system-b.products (part_number)" not in text
