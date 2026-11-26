# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Route-seed DELIVERY truncation must PRESERVE steward/learned guidance.

The defect this pins, found by the ``composed_context`` telemetry on its first
live run (2026-08-04) and re-confirmed 2026-09-08: ``query_guidance`` is capped
per field on the way into the prompt, and plain truncation is base-first. On the
live deployment base guidance occupied 2838 of 3738 chars, so the cap discarded
EVERY accumulated note before the model saw one. The deployment's own hard-won
context was the first thing dropped while the static connection-type prose —
which the agent also receives from its defaults — survived. Answers therefore
looked like the context was "present but ignored" when it had never been
composed at all.

Two further facts this file pins, both found 2026-09-08:

* the learned-preserving helper that was supposed to fix this was DEAD CODE —
  defined, unit-tested, and never called by either delivery pass, so every live
  turn used the naive head truncation. Hence the reachability ratchet below.
* the delivery fields are the RENDERED fenced sections, so a head truncation
  also SEVERED the closing fence of an untrusted-content block.

The store is not involved: an over-budget document is retained VERBATIM and
reported (ADR-0063 D7 — eviction is steward-confirmed CAS only), so the loss is
purely a delivery-composition loss and the fix stays on the delivery side.

Since 2026-10-16 the route seed shows the orchestrator no query-hint text
(``guidance_scope.orchestrator_document``: the system agent gets the hints with
every question, the seed carries a count). The seed-level tests below therefore
carry their learned notes as answer requirements, the scope that still rides
the seed with the same cap and packer; the pure ``fit_delivery_sections`` tests
keep query hints, which that function still fits for any caller.

Pure/offline: string-level helpers, an injected graph seam, and pure stat
builders. No dataset literals.
"""
from __future__ import annotations

import ast
import logging
from collections import Counter
from pathlib import Path

import pytest

from agents import orchestrator as orch
from agents.guidance_scope import (
    DELIVERY_FIELD_CAP,
    DELIVERY_FIELDS,
    GuidanceScope,
    TRUNCATION_MARKER,
    append_note,
    fit_delivery_sections,
    orchestrator_document,
    parse_document,
    render_orchestrator_sections,
    scoped_entries,
)

# The MEASURED live shape: base occupied 2838 chars; the two notes measured 285
# and 597 chars. Generic prose only — no dataset literals.
_LONG_BASE = "B" * 2838
_NOTE_OLD = "S" * 285
_NOTE_NEW = "L" * 597
_ANSWER_NOTE_OLD = "A" * 400
_ANSWER_NOTE_NEW = "Z" * 400
# A note pair that DOES fit the cap alongside a base head, for the
# preserve-everything assertions.
_SMALL_OLD = "s" * 120
_SMALL_NEW = "l" * 150


def _document(*, base: str = _LONG_BASE,
              hints: tuple[str, ...] = (_NOTE_OLD, _NOTE_NEW),
              answers: tuple[str, ...] = ()) -> dict:
    document = parse_document(base)
    for note in hints:
        document = parse_document(append_note(document, note,
                                              GuidanceScope.QUERY_HINT))
    for note in answers:
        document = parse_document(append_note(document, note,
                                              GuidanceScope.ANSWER_REQUIREMENT))
    return document


def _answer_pair_document() -> dict:
    """The measured long base, with the measured note pair as answer
    requirements: the seed carries those, and the pair does not both fit."""
    return _document(hints=(), answers=(_NOTE_OLD, _NOTE_NEW))


def _seed_system(document: dict) -> dict:
    """One route-seed system entry, built by the production inventory path."""
    return orch._route_seed_inventory([{
        "system_id": "system-a", "system_name": "System A",
        "query_guidance": document,
    }])["system-a"]


# --------------------------------------------------------------------------- #
# The regression itself — kill-the-mutant guards
# --------------------------------------------------------------------------- #
def test_plain_truncation_drops_every_note_and_severs_the_fence():
    rendered = render_orchestrator_sections(_document())["query_guidance"]
    plain = orch._truncate_seed_text(rendered, DELIVERY_FIELD_CAP)

    assert _NOTE_OLD not in plain and _NOTE_NEW not in plain
    assert "----- END QUERY HINTS -----" not in plain  # fence severed


def test_the_measured_live_guidance_does_not_fit_the_delivery_cap():
    """Without this, every preservation assertion below would be vacuous."""
    rendered = render_orchestrator_sections(_document())["query_guidance"]
    assert len(rendered) > DELIVERY_FIELD_CAP


# --------------------------------------------------------------------------- #
# The delivery path — wired, preserving, and well-formed
# --------------------------------------------------------------------------- #
def test_delivery_preserves_learned_notes_and_shortens_generic_base():
    system = _seed_system(_document(hints=(), answers=(_SMALL_OLD, _SMALL_NEW)))

    orch._bound_route_seed_prose([system])
    delivered = system["query_guidance"]

    assert _SMALL_OLD in system["answer_requirements"]
    assert _SMALL_NEW in system["answer_requirements"]
    assert len(delivered) <= DELIVERY_FIELD_CAP
    assert TRUNCATION_MARKER in delivered        # the generic base yielded room
    assert _LONG_BASE not in delivered
    assert delivered.count("B") < len(_LONG_BASE)


def test_delivered_section_is_still_a_well_formed_fence():
    system = _seed_system(_document())

    orch._bound_route_seed_prose([system])
    delivered = system["query_guidance"]

    assert delivered.startswith("----- BEGIN QUERY HINTS -----")
    assert delivered.rstrip().endswith("----- END QUERY HINTS -----")
    assert delivered.count("----- END QUERY HINTS -----") == 1


def test_under_cap_delivery_is_byte_identical_to_the_plain_render():
    """Deployments whose guidance fits see ZERO change from this fix."""
    document = _document(base="Short base.", hints=("one hint",))
    system = _seed_system(document)
    before = {field: system[field] for field in DELIVERY_FIELDS}

    orch._bound_route_seed_prose([system])

    assert {field: system[field] for field in DELIVERY_FIELDS} == before
    assert before == render_orchestrator_sections(orchestrator_document(document))
    assert "delivery_truncation" not in system["guidance_composition"]


def test_the_measured_note_pair_keeps_the_newest_and_reports_the_older():
    """HONEST measured fact: the fenced sections carry ~260 chars of fence and
    policy overhead, so the live 285+597 note pair does NOT both fit the 1024
    delivery cap. The newest note survives whole, the older one is dropped —
    and the drop is REPORTED. Before this fix the naive head truncation dropped
    BOTH silently (and severed the closing fence). Raising the cap is a
    separate, measurable prompt-budget decision: the first truncation pass runs
    before rule-detail trimming, so a bigger prose allowance moves the pressure
    onto the routing rules.
    """
    sections, report = fit_delivery_sections(_document())

    delivered = sections["query_guidance"]
    assert _NOTE_NEW in delivered and _NOTE_OLD not in delivered
    assert report["notes_dropped"] == 1
    assert report["scope_notes_dropped"]["query_hint"] == 1


def test_notes_keep_append_order_and_stay_parseable():
    sections, _report = fit_delivery_sections(
        _document(hints=(_SMALL_OLD, _SMALL_NEW)))
    delivered = sections["query_guidance"]

    assert delivered.index(_SMALL_OLD) < delivered.index(_SMALL_NEW)
    body = delivered.split("\n")[-2]
    assert body.startswith("[") and body.endswith("]")


def test_each_scoped_field_is_fitted_against_its_own_cap():
    document = _document(hints=(_SMALL_OLD, _SMALL_NEW),
                         answers=(_ANSWER_NOTE_OLD, _ANSWER_NOTE_NEW))
    sections, report = fit_delivery_sections(document)

    assert _SMALL_OLD in sections["query_guidance"]
    assert _ANSWER_NOTE_NEW in sections["answer_requirements"]
    assert _ANSWER_NOTE_OLD not in sections["answer_requirements"]
    assert _LONG_BASE not in sections["answer_requirements"]  # base is query-only
    assert report["scope_notes_dropped"]["answer_requirement"] == 1
    assert set(report["fields"]) == {"query_guidance", "answer_requirements"}


def test_a_single_oversized_note_keeps_its_head_inside_the_fence():
    document = _document(base="", hints=("Z" * 4000,))
    sections, report = fit_delivery_sections(document, {"query_guidance": 400})

    delivered = sections["query_guidance"]
    assert len(delivered) <= 400
    assert delivered.rstrip().endswith("----- END QUERY HINTS -----")
    assert TRUNCATION_MARKER in delivered
    assert report["notes_truncated"] == 1 and report["notes_dropped"] == 0


def test_a_cap_too_small_for_any_fence_empties_the_field_and_reports_it():
    sections, report = fit_delivery_sections(_document(), {"query_guidance": 20})

    assert sections["query_guidance"] == ""
    assert report["notes_dropped"] == 2
    assert report["chars_dropped"] >= len(_LONG_BASE)


def test_the_second_shrink_pass_also_preserves_the_notes():
    """The budget-pressure pass used the naive truncation too."""
    system = _seed_system(_document())
    payload = {"systems": [system], "rule_grain_cap_per_kind": 50}
    orch._bound_route_seed_prose([system])
    tight = len(orch._ROUTE_SEED_HEADER) + 64

    original = orch._ROUTE_SEED_SERIALIZED_BUDGET
    try:
        orch._ROUTE_SEED_SERIALIZED_BUDGET = tight
        orch._shrink_route_seed_prose(payload, [system],
                                      orch._encode_route_seed(payload))
    finally:
        orch._ROUTE_SEED_SERIALIZED_BUDGET = original

    delivered = system["query_guidance"]
    assert _LONG_BASE[:100] not in delivered  # generic prose went first
    if delivered:
        assert delivered.rstrip().endswith("----- END QUERY HINTS -----")


def test_hand_built_payloads_without_the_internal_document_still_bound():
    """Fail-soft: the pre-existing plain cap remains for documentless entries."""
    system = {"id": "sys-a", "name": "System A", "plants": [],
              "capabilities": None, "connection_type": None,
              "query_guidance": "X" * 4000, "answer_requirements": None,
              "legacy_guidance": None}

    orch._bound_route_seed_prose([system])

    assert len(system["query_guidance"]) == DELIVERY_FIELD_CAP
    assert system["query_guidance"].endswith(TRUNCATION_MARKER)


# --------------------------------------------------------------------------- #
# Observability — bounded telemetry, delivery/composition ONLY
# --------------------------------------------------------------------------- #
def test_the_drop_is_reported_with_correct_counts():
    system = _seed_system(_document(hints=(), answers=(_SMALL_OLD, _SMALL_NEW)))

    orch._bound_route_seed_prose([system])
    report = system["guidance_composition"]["delivery_truncation"]

    assert report["truncated"] is True
    assert report["fields"] == ["query_guidance"]
    assert report["field_cap"] == DELIVERY_FIELD_CAP
    assert report["notes_total"] == 2
    assert report["notes_dropped"] == 0        # both notes survived
    assert report["base_chars_dropped"] > 0
    assert report["chars_dropped"] == report["base_chars_dropped"]
    assert report["dropped_digests"] == []


def test_dropped_note_digests_are_reported_and_bounded():
    system = _seed_system(_answer_pair_document())

    orch._bound_route_seed_prose([system])
    report = system["guidance_composition"]["delivery_truncation"]

    assert report["notes_dropped"] == 1
    assert len(report["dropped_digests"]) == 1
    # Digests, never note text — the report is a payload-safe composition fact.
    assert _NOTE_OLD not in str(report)
    assert report["chars_dropped"] >= len(_NOTE_OLD)


def test_a_dropped_note_is_not_counted_as_composed_context():
    from agents import query_intelligence

    system = _seed_system(_answer_pair_document())
    orch._bound_route_seed_prose([system])

    composed = query_intelligence.build_composed_context_from_metadata([system])

    assert composed["artifact_count"] == 1          # only the delivered note
    assert composed["scope_counts"]["answer_requirement"] == 1
    truncation = composed["delivery_truncation"]
    assert truncation["notes_dropped"] == 1
    assert truncation["systems"][0]["system"] == "system-a"
    assert set(truncation["systems"][0]["fields"]) == {"query_guidance",
                                                       "answer_requirements"}


def test_truncation_is_visible_even_when_every_note_was_dropped():
    system = _seed_system(_answer_pair_document())
    orch._bound_route_seed_guidance(system, 20)

    from agents import query_intelligence
    composed = query_intelligence.build_composed_context_from_metadata([system])

    assert composed["artifact_count"] == 0
    assert composed["delivery_truncation"]["notes_dropped"] == 2


def test_untruncated_composition_is_unchanged():
    from agents import query_intelligence

    system = _seed_system(_document(base="Short base.", hints=("one hint",)))
    orch._bound_route_seed_prose([system])

    composed = query_intelligence.build_composed_context_from_metadata([system])

    assert composed["artifact_count"] == 1
    assert "delivery_truncation" not in composed


@pytest.mark.usefixtures("route_seed_pressure_budget")
def test_end_to_end_seed_reports_the_drop_and_keeps_every_fence():
    """The whole live path: build_route_seed → the turn's composed_context.

    Before the fix this exact shape delivered SIX head-truncated, fence-severed
    guidance fields while ``composed_context`` claimed six composed artifacts.
    """
    from agents.runtime_app import _composed_context_from_text

    rows = [{"system_id": f"system-{index}",
             "query_guidance": _answer_pair_document()} for index in range(3)]

    def fake_query(cypher: str, *_args, **_kwargs):
        return rows if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER else []

    seed = orch.build_route_seed(query=fake_query)

    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    assert seed.count("----- END QUERY HINTS -----") == 3   # no severed fence
    assert seed.count("----- END FINAL-ANSWER REQUIREMENTS -----") == 3
    composed = _composed_context_from_text(seed)
    assert composed["artifact_count"] == 3                  # NOT 6
    assert composed["delivery_truncation"]["notes_dropped"] == 3
    assert len(composed["delivery_truncation"]["systems"]) == 3


def test_the_drop_is_logged_once_naming_the_system_and_the_counts(caplog):
    system = _seed_system(_answer_pair_document())

    with caplog.at_level(logging.WARNING, logger="agents.orchestrator"):
        orch._bound_route_seed_prose([system])

    lines = [r.getMessage() for r in caplog.records
             if "GUIDANCE_DELIVERY_TRUNCATION" in r.getMessage()]
    assert len(lines) == 1
    assert "system_id=system-a" in lines[0]
    assert "notes_total=2" in lines[0] and "notes_dropped=1" in lines[0]
    assert "action=report-only" in lines[0]
    # Delivery/composition only — never a claim about what the model did.
    assert _NOTE_NEW not in lines[0] and "applied" not in lines[0]


def test_the_internal_document_never_reaches_the_prompt():
    system = _seed_system(_document())
    payload = {"systems": [system], "rule_grain_cap_per_kind": 50}

    serialized = orch._encode_route_seed(payload)

    assert "_guidance_document" in system
    assert "_guidance_document" not in serialized


# --------------------------------------------------------------------------- #
# The store is never touched
# --------------------------------------------------------------------------- #
def test_delivery_truncation_writes_nothing_to_the_guidance_store():
    seen: list[str] = []

    def fake_query(cypher: str, *_args, **_kwargs):
        seen.append(cypher)
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return [{"system_id": "system-a",
                     "query_guidance": _document()}]
        return []

    seed = orch.build_route_seed(query=fake_query)

    assert seed and len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    assert seen  # the seam really was exercised
    for cypher in seen:
        upper = cypher.upper()
        for verb in ("MERGE", "CREATE", "SET ", "DELETE", "REMOVE"):
            assert verb not in upper


def test_the_fit_never_mutates_the_document_it_was_given():
    document = _document()
    before = scoped_entries(document)

    fit_delivery_sections(document, {"query_guidance": 300})

    assert scoped_entries(document) == before
    assert document["base"] == _LONG_BASE


# --------------------------------------------------------------------------- #
# Reachability — the ratchet that would have caught the dead helper
# --------------------------------------------------------------------------- #
def _call_counts(source: str) -> Counter:
    tree = ast.parse(source)
    return Counter(
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name))


def test_no_private_guidance_helper_in_the_orchestrator_is_dead_code():
    """The exact shape of this defect: a learned-preserving truncation helper
    existed, had its own passing unit tests, and was never CALLED — so every
    live turn ran the naive head truncation the helper was written to replace.
    """
    source = Path(orch.__file__).read_text(encoding="utf-8")
    calls = _call_counts(source)
    defined = [node.name for node in ast.parse(source).body
               if isinstance(node, ast.FunctionDef)
               and node.name.startswith("_") and "guidance" in node.name]

    assert defined, "expected private guidance helpers in the orchestrator"
    assert [name for name in defined if not calls.get(name)] == []


def test_both_delivery_passes_use_the_preserving_fit():
    import inspect

    first = inspect.getsource(orch._bound_route_seed_prose)
    second = inspect.getsource(orch._shrink_route_seed_prose)
    fit = inspect.getsource(orch._bound_route_seed_guidance)

    assert "_bound_route_seed_guidance(" in first
    assert "_bound_route_seed_guidance(" in second
    assert "fit_delivery_sections" in fit
    # The guidance fields must not be head-truncated in either pass.
    for source in (first, second):
        assert '_truncate_seed_text(system["query_guidance"]' not in source


def test_only_query_guidance_gets_the_larger_cap():
    """capabilities is static prose with no learned notes — it keeps 512."""
    import inspect

    src = inspect.getsource(orch._bound_route_seed_prose)
    assert "_ROUTE_SEED_GUIDANCE_FIELD_CAP" in src
    assert 'system["capabilities"]' in src
    caps_line = next(ln for ln in src.splitlines()
                     if "capabilities" in ln and "get(" in ln)
    assert "_ROUTE_SEED_GUIDANCE_FIELD_CAP" not in caps_line


def test_the_delivery_cap_has_exactly_one_home():
    """The steward projection reports THIS number; a second literal would let
    the surface report a truncation the prompt never suffered."""
    assert orch._ROUTE_SEED_GUIDANCE_FIELD_CAP is DELIVERY_FIELD_CAP
    assert DELIVERY_FIELD_CAP > orch._ROUTE_SEED_TEXT_FIELD_CAP
    assert DELIVERY_FIELD_CAP <= orch._ROUTE_SEED_SERIALIZED_BUDGET // 4
    source = Path(orch.__file__).read_text(encoding="utf-8")
    assert "_ROUTE_SEED_GUIDANCE_FIELD_CAP = 1_024" not in source


# --------------------------------------------------------------------------- #
# The steward surface — annotate-only, next to the budget overage
# --------------------------------------------------------------------------- #
def _guidance_row(document: dict, system_id: str = "system-a") -> dict:
    return {"system_id": system_id, "base": document["base"],
            "query_hints": document["query_hints"],
            "answer_requirements": document["answer_requirements"],
            "legacy_unscoped": document["legacy_unscoped"]}


def test_health_surface_reports_the_delivery_truncation():
    from agents import learning_health as lh

    stats = lh.guidance_delivery_stats([_guidance_row(_document())])

    assert stats["available"] is True
    assert stats["systems"] == 1
    assert stats["field_cap"] == DELIVERY_FIELD_CAP
    assert stats["truncated"] is True
    assert stats["chars_dropped"] > 0
    assert stats["truncated_systems"][0]["system_id"] == "system-a"
    assert stats["truncated_systems"][0]["fields"] == ["query_guidance"]


def test_health_surface_stays_quiet_when_guidance_fits():
    from agents import learning_health as lh

    stats = lh.guidance_delivery_stats(
        [_guidance_row(_document(base="Short base.", hints=("one hint",)))])

    assert stats["truncated"] is False
    assert stats["truncated_systems"] == []
    assert stats["notes_dropped"] == 0 and stats["chars_dropped"] == 0


def test_health_surface_is_honestly_unavailable_and_bounded():
    from agents import learning_health as lh

    unavailable = lh.guidance_delivery_stats([], available=False)
    assert unavailable == {
        "available": False, "systems": None, "field_cap": DELIVERY_FIELD_CAP,
        "truncated": False, "notes_dropped": 0, "chars_dropped": 0,
        "truncated_systems": []}

    many = lh.guidance_delivery_stats(
        [_guidance_row(_document(), f"system-{index:03d}") for index in range(40)])
    assert many["truncated_systems"] and len(many["truncated_systems"]) == 20
    assert many["systems"] == 40  # the count is honest even when the list caps


def test_the_delivery_annotation_never_gates(monkeypatch):
    """Annotate-only: it rides the health read and cannot fail it."""
    from agents import learning_health as lh

    monkeypatch.setattr(lh, "_read_guidance_rows",
                        lambda: [_guidance_row(_document())])
    monkeypatch.setattr(lh, "read_graph_integrity", lambda: {})
    monkeypatch.setattr(lh, "usage_ledger_stats", lambda days: {})
    monkeypatch.setattr(lh, "staging_census", lambda: {})
    from agents import self_learning
    monkeypatch.setattr(self_learning, "get_all_mappings", lambda: [])

    health = lh.build_health()

    assert health["guidance_delivery"]["truncated"] is True
    assert health["guidance_budget"]["available"] is True
    assert "guidance_delivery" in lh.zero_health()


def test_rows_without_document_arrays_are_safe():
    from agents import learning_health as lh

    stats = lh.guidance_delivery_stats(
        [{"system_id": "sys-a"}, {"system_id": "sys-b", "query_hints": None},
         "not-a-row"])

    assert stats["systems"] == 2 and stats["truncated"] is False


@pytest.mark.parametrize("field", DELIVERY_FIELDS)
def test_every_delivery_field_is_capped(field):
    document = _document(hints=(_NOTE_OLD, _NOTE_NEW),
                         answers=(_ANSWER_NOTE_OLD, _ANSWER_NOTE_NEW))
    document["legacy_unscoped"] = ["Y" * 900, "W" * 900]

    sections, _report = fit_delivery_sections(document)

    assert len(sections[field]) <= DELIVERY_FIELD_CAP
