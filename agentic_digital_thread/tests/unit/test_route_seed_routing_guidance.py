# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The route seed delivers every system's guidance at workshop scale.

The defect this pins (measured live 2026-10-04 on a nine-system deployment):
the 8,000-char seed delivered NONE of the systems' guidance. Six systems
carried the SAME connection-type default base (~580 fenced chars each), the
shrink pass emptied every guidance field, each emptied field then paid ~130
chars for a per-system drop report riding the prompt, and empty keys took
~600 more, so the short registration guidance that says which system owns
what never reached the orchestrator.

Three delivery-side levers, none of which drops content (ADR-0063 D7):

* identical connection-type default bases are DEDUPLICATED into one shared
  section (``shared_guidance``) that every such system references — every word
  still reaches the orchestrator, and a cut to the shared section is reported
  on every referencing system;
* the per-system drop reports ride the serialized seed as ONE bounded
  aggregate (``guidance_delivery_truncation``), still per system, still logged;
* empty keys are omitted (absent == empty for every reader).

The numbers are pinned against a version-controlled generic fixture
(``_route_seed_scale_fixture``) for 7, 8 and 9 systems, before (the three
levers disabled, i.e. the previous serializer) and after.

Since 2026-10-16 query-hint text does not ride the seed at all
(``guidance_scope.orchestrator_document``): the system agent gets it with every
question and the seed carries a count. The drop-accounting tests below carry
their notes as answer requirements, which still ride the seed under the same
cap and packer.
"""
from __future__ import annotations

import json
import logging

import pytest

from agents import orchestrator as orch
from agents import query_intelligence
from agents.connection_types import POSTGRESQL, REST_API, default_query_guidance
from agents.guidance_scope import (
    AGGREGATE_SYSTEM_CAP,
    GuidanceScope,
    SHARED_GUIDANCE_FIELD,
    aggregate_delivery_reports,
    append_note,
    composition_metadata,
    content_digest,
    decode_delivery_aggregate,
    dropped_note_digests,
    empty_delivery_report,
    fit_delivery_sections,
    parse_document,
    shared_reference,
    shared_reference_key,
)
from tests.unit import _route_seed_scale_fixture as fx

_PG_DEFAULT = default_query_guidance(POSTGRESQL)
_NOTE = "Filter production runs by run_number, never by the row id."
_ANSWER_NOTE = "Name the production run in every answer."


def _row(system_id: str, guidance, connection_type=POSTGRESQL) -> dict:
    return {"system_id": system_id, "system_name": system_id.title(),
            "connection_type": connection_type, "query_guidance": guidance}


def _payload(rows: list[dict]) -> dict:
    systems = orch._route_seed_inventory(rows)
    return {"systems": list(systems.values()), "rule_grain_cap_per_kind": 50}


def _decode(seed_or_serialized: str) -> dict:
    text = seed_or_serialized.split("RESULT:\n", 1)[-1]
    return json.JSONDecoder().raw_decode(text)[0]


def _seed(rows: list[dict]) -> str:
    def fake_query(cypher, *_args, **_kwargs):
        return rows if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER else []
    return orch.build_route_seed(query=fake_query)


# --------------------------------------------------------------------------- #
# Lever 1: identical connection-type defaults are shared, never dropped
# --------------------------------------------------------------------------- #
def test_sharing_delivers_the_default_once_and_references_it_from_each_system():
    payload = _payload([_row("system-a", _PG_DEFAULT), _row("system-b", _PG_DEFAULT)])

    assert orch._share_default_guidance(payload) is True
    serialized = _decode(orch._encode_route_seed(payload))

    assert _PG_DEFAULT in serialized["shared_guidance"][POSTGRESQL]
    assert orch._encode_route_seed(payload).count(_PG_DEFAULT) == 1
    for system in serialized["systems"]:
        assert system["query_guidance"] == shared_reference(POSTGRESQL)
        assert shared_reference_key(system["query_guidance"]) == POSTGRESQL


def test_the_shared_section_keeps_the_query_hints_fence_and_policy():
    payload = _payload([_row("system-a", _PG_DEFAULT), _row("system-b", _PG_DEFAULT)])
    orch._share_default_guidance(payload)
    plain = orch._route_seed_inventory([_row("x", _PG_DEFAULT)])["x"]["query_guidance"]

    assert payload["shared_guidance"][POSTGRESQL] == plain


def test_a_single_default_system_is_not_shared():
    payload = _payload([_row("system-a", _PG_DEFAULT),
                        _row("system-b", "Answers which line made a run.")])

    assert orch._share_default_guidance(payload) is False
    assert "shared_guidance" not in payload


def test_only_the_exact_default_of_the_systems_own_type_is_shared():
    payload = _payload([_row("system-a", _PG_DEFAULT, REST_API),
                        _row("system-b", _PG_DEFAULT, REST_API),
                        _row("system-c", _PG_DEFAULT + " Extra.")])

    assert orch._share_default_guidance(payload) is False
    for system in payload["systems"]:
        assert _PG_DEFAULT in system["query_guidance"]


def test_learned_notes_stay_in_the_systems_own_section_after_sharing():
    guidance = append_note(append_note(_PG_DEFAULT, _NOTE, GuidanceScope.QUERY_HINT),
                           _ANSWER_NOTE, GuidanceScope.ANSWER_REQUIREMENT)
    payload = _payload([_row("system-a", guidance), _row("system-b", _PG_DEFAULT)])
    orch._share_default_guidance(payload)
    system_a = payload["systems"][0]

    # The base moved to the shared section; the hint is referenced, not restated.
    assert system_a["query_guidance"] == shared_reference(POSTGRESQL)
    assert _NOTE not in orch._encode_route_seed(payload)
    assert system_a["query_hints"].startswith("1 query hint is delivered to system-a")
    assert _ANSWER_NOTE in system_a["answer_requirements"]
    assert _ANSWER_NOTE not in payload["shared_guidance"][POSTGRESQL]


def test_sharing_never_mutates_the_stored_document():
    from agents.guidance_scope import parse_document
    document = parse_document(append_note(_PG_DEFAULT, _NOTE, GuidanceScope.QUERY_HINT))
    payload = _payload([_row("system-a", document), _row("system-b", _PG_DEFAULT)])
    orch._share_default_guidance(payload)

    assert document["base"] == _PG_DEFAULT and document["query_hints"] == [_NOTE]


def test_a_seed_that_fits_is_byte_identical_and_shares_nothing():
    rows = [_row("system-a", _PG_DEFAULT), _row("system-b", _PG_DEFAULT)]
    seed = _seed(rows)

    assert "shared_guidance" not in seed
    assert seed.count(_PG_DEFAULT) == 2


def test_a_cut_to_the_shared_section_is_reported_on_every_referencing_system(caplog):
    payload = _payload([_row("system-a", _PG_DEFAULT), _row("system-b", _PG_DEFAULT)])
    orch._share_default_guidance(payload)

    with caplog.at_level(logging.WARNING, logger="agents.orchestrator"):
        orch._bound_shared_guidance(payload, 1024, only=POSTGRESQL, override=300)

    assert len(payload["shared_guidance"][POSTGRESQL]) <= 300
    for system in payload["systems"]:
        report = system["guidance_composition"]["delivery_truncation"]
        assert report["fields"] == [SHARED_GUIDANCE_FIELD]
        assert report["base_chars_dropped"] > 0
    lines = [r.getMessage() for r in caplog.records
             if "GUIDANCE_DELIVERY_TRUNCATION" in r.getMessage()]
    assert len(lines) == 2 and all("fields=shared_guidance" in ln for ln in lines)


def test_a_dropped_reference_is_reported_as_the_base_it_carried():
    payload = _payload([_row("system-a", _PG_DEFAULT), _row("system-b", _PG_DEFAULT)])
    orch._share_default_guidance(payload)
    system = payload["systems"][0]

    orch._bound_route_seed_guidance(system, 1024, overrides={"query_guidance": 13})

    report = system["guidance_composition"]["delivery_truncation"]
    assert system["query_guidance"] == ""
    assert report["base_chars_dropped"] == len(_PG_DEFAULT)


def test_shared_delivery_is_reported_in_composed_context():
    from agents.runtime_app import _composed_context_from_text, _planned_with_from_seed
    seed = fx.build_seed(9)

    composed = _composed_context_from_text(seed)
    assert sorted(composed["shared_guidance"][POSTGRESQL]) == fx.generic_ids(9)
    assert "delivery_truncation" not in composed
    assert _planned_with_from_seed(seed)["systems"] == 9


def test_term_resolution_strips_the_shared_prose_like_any_guidance():
    from agents import metric_term_resolution as mtr
    view = mtr.resolution_seed_view(fx.build_seed(9))

    assert _PG_DEFAULT not in view
    assert "shared_guidance" not in view


# --------------------------------------------------------------------------- #
# Lever 2: one bounded drop-report aggregate in the prompt
# --------------------------------------------------------------------------- #
def _report(**counts) -> dict:
    report = empty_delivery_report()
    report.update(truncated=True, fields=["query_guidance"], **counts)
    return report


def test_the_serialized_seed_carries_one_aggregate_not_per_system_reports():
    system = orch._route_seed_inventory([_row("system-a", "B" * 3000)])["system-a"]
    orch._bound_route_seed_prose([system])
    encoded = _decode(orch._encode_route_seed({"systems": [system]}))

    assert "guidance_composition" not in encoded["systems"][0]
    aggregate = encoded["guidance_delivery_truncation"]
    assert aggregate["systems"]["system-a"][0] > 0
    assert aggregate["systems"]["system-a"][3] == "q"
    # The in-memory payload keeps the full per-system report.
    assert system["guidance_composition"]["delivery_truncation"]["truncated"]


def test_the_aggregate_is_bounded_and_counts_what_it_omits():
    entries = [(f"s{i}", _report(chars_dropped=10, notes_dropped=2,
                                 scope_notes_dropped={"query_hint": 2}))
               for i in range(AGGREGATE_SYSTEM_CAP + 5)]
    aggregate = aggregate_delivery_reports(entries)

    assert len(aggregate["systems"]) == AGGREGATE_SYSTEM_CAP
    assert aggregate["systems_omitted"] == 5
    assert aggregate["chars_dropped"] == 10 * (AGGREGATE_SYSTEM_CAP + 5)
    assert aggregate["notes_dropped"] == 2 * (AGGREGATE_SYSTEM_CAP + 5)
    assert aggregate["systems"]["s0"] == [10, 2, 0, "q", {"q": 2}]
    assert "dropped_digests" not in aggregate  # counts identify the notes exactly


def test_malformed_aggregate_rows_are_unverified_not_ignored():
    decoded = decode_delivery_aggregate({"systems": {"a": "bad", "b": [1, 2],
                                                     "c": [1, 1, 0, "q", {"q": 1}]}})

    assert decoded["unverified"] == {"a", "b"}
    assert [entry["system"] for entry in decoded["entries"]] == ["c"]
    assert decode_delivery_aggregate(None) == {"entries": [], "unverified": set(),
                                               "systems_omitted": 0}


@pytest.mark.usefixtures("route_seed_pressure_budget")
def test_a_dropped_note_is_still_excluded_from_composed_context():
    from agents.runtime_app import _composed_context_from_text
    old, new = "S" * 400, "L" * 700
    document = append_note(append_note("", old, GuidanceScope.ANSWER_REQUIREMENT),
                           new, GuidanceScope.ANSWER_REQUIREMENT)
    seed = _seed([_row(f"system-{i}", document) for i in range(4)]
                 + [_row(f"padding-{i}", "F" * 900) for i in range(4)])

    composed = _composed_context_from_text(seed)
    dropped = composed["delivery_truncation"]["notes_dropped"]
    assert dropped >= 4
    assert content_digest(old) not in {a["digest"] for a in composed["artifacts"]}
    assert composed["artifact_count"] == 8 - dropped
    assert "delivery_truncation" not in seed.split("guidance_delivery_truncation")[0]


# --------------------------------------------------------------------------- #
# Exact dropped-note accounting: no dropped note is ever reported as delivered
# --------------------------------------------------------------------------- #
def _notes(prefix: str, count: int, size: int = 330) -> list[str]:
    return [f"{prefix} note {n}: " + "x" * size for n in range(count)]


def _document_with(notes: list[str], base: str = "Base guidance.",
                   scope: GuidanceScope = GuidanceScope.QUERY_HINT) -> dict:
    document = base
    for note in notes:
        document = append_note(document, note, scope)
    return parse_document(document)


def _answers_with(notes: list[str]) -> dict:
    """Seed-level vehicle: answer requirements still ride the route seed."""
    return _document_with(notes, scope=GuidanceScope.ANSWER_REQUIREMENT)


def _delivered_digests(payload: dict) -> set:
    """Digests of every note value in a serialized seed's answer fields."""
    return {content_digest(value) for system in payload["systems"]
            for line in (system.get("answer_requirements") or "").splitlines()
            if line.startswith("[") for value in json.loads(line)}


def test_the_fit_drops_an_oldest_prefix_for_every_note_count_and_cap():
    """The invariant the per-scope counts rely on: whatever the cap, the
    dropped notes are exactly the oldest ``scope_notes_dropped`` of the scope,
    so the counts identify them without carrying a digest."""
    for count in range(1, 8):
        notes = _notes("n", count, size=120)
        document = _document_with(notes)
        digests = composition_metadata(document)["scope_digests"]
        for cap in range(0, 1400, 37):
            sections, report = fit_delivery_sections(document, {"query_guidance": cap})
            dropped = dropped_note_digests(digests, report)
            n = report["scope_notes_dropped"]["query_hint"]
            assert dropped == {content_digest(note) for note in notes[:n]}, (count, cap)
            delivered = sections["query_guidance"]
            assert not any(note in delivered for note in notes[:n])


def test_unverifiable_reports_are_refused():
    digests = {"query_hint": ["d1", "d2"]}

    assert dropped_note_digests(digests, {"notes_dropped": 0}) == set()
    assert dropped_note_digests(digests, {"notes_dropped": 0,
                                          "scope_notes_dropped": {"query_hint": 1}}) is None
    assert dropped_note_digests(digests, {"notes_dropped": -1,
                                          "scope_notes_dropped": {"query_hint": 1}}) is None
    assert dropped_note_digests(digests, {"notes_dropped": 0,
                                          "dropped_digests": ["d1"]}) is None
    assert dropped_note_digests(digests, {"notes_dropped": 3,
                                          "scope_notes_dropped": {"query_hint": 3}}) is None
    assert dropped_note_digests(digests, {"notes_dropped": 2,
                                          "scope_notes_dropped": {"query_hint": 1}}) is None
    assert dropped_note_digests(digests, {"notes_dropped": 1, "dropped_digests": ["d2"],
                                          "scope_notes_dropped": {"query_hint": 1}}) is None
    assert dropped_note_digests(digests, {"notes_dropped": 2}) is None
    assert dropped_note_digests(digests, {"notes_dropped": 1,
                                          "dropped_digests": ["d1"]}) == {"d1"}


def _overflow_seed_payload(systems: int = 9, notes: int = 4, dropped: int = 3) -> dict:
    """``systems`` systems, each with ``dropped`` of its ``notes`` notes dropped
    by the delivery cap: an overflow reproduction (27 drops is past
    the 20 digests the previous aggregate could carry)."""
    rows = [_row(f"system-{i}", _answers_with(_notes(f"s{i}", notes)))
            for i in range(systems)]
    payload = _payload(rows)
    for system in payload["systems"]:
        # 640, not the 600 the query field used: the answer fence's own title
        # and policy are longer, and the newest note must still fit whole.
        orch._bound_route_seed_guidance(system, 1024,
                                        overrides={"answer_requirements": 640})
        report = system["guidance_composition"]["delivery_truncation"]
        assert report["notes_dropped"] == dropped  # premise of the scenario
    return payload


def test_aggregate_overflow_never_reports_a_dropped_note_as_delivered():
    from agents.runtime_app import _composed_context_from_text
    payload = _overflow_seed_payload()
    seed = orch._ROUTE_SEED_HEADER + orch._encode_route_seed(payload)

    aggregate = _decode(seed)["guidance_delivery_truncation"]
    assert aggregate["notes_dropped"] == 27
    composed = _composed_context_from_text(seed)

    delivered = _delivered_digests(_decode(seed))
    claimed = {artifact["digest"] for artifact in composed["artifacts"]}
    assert composed["artifact_count"] == 9          # one kept note per system
    assert claimed <= delivered                     # nothing claimed that is absent
    assert composed["delivery_truncation"]["notes_dropped"] == 27
    assert "accounting" not in composed["delivery_truncation"]


@pytest.mark.usefixtures("route_seed_pressure_budget")
def test_end_to_end_overflow_claims_only_notes_present_in_the_prompt():
    from agents.runtime_app import _composed_context_from_text
    notes = {f"system-{i}": _notes(f"s{i}", 4) for i in range(9)}
    seed = _seed([_row(sid, _answers_with(values)) for sid, values in notes.items()])
    payload = _decode(seed)
    composed = _composed_context_from_text(seed)

    # A note the budget pass cut to a head is still delivered (counted as
    # truncated, not dropped), so presence is checked by the note's head.
    answers = {system["id"]: system.get("answer_requirements") or ""
               for system in payload["systems"]}
    by_digest = {content_digest(note): (sid, note)
                 for sid, values in notes.items() for note in values}
    assert payload["guidance_delivery_truncation"]["notes_dropped"] > 20
    for artifact in composed["artifacts"]:
        sid, note = by_digest[artifact["digest"]]
        assert note[:40] in answers[sid], (sid, note[:40])
    assert composed["artifact_count"] == 36 - composed["delivery_truncation"]["notes_dropped"]


def test_systems_past_the_row_cap_are_withheld_and_named_incomplete():
    from agents.runtime_app import _composed_context_from_text
    payload = _overflow_seed_payload()
    serialized = _decode(orch._encode_route_seed(payload))
    aggregate = serialized["guidance_delivery_truncation"]
    first = sorted(aggregate["systems"])[0]
    aggregate["systems"] = {first: aggregate["systems"][first]}
    aggregate["systems_omitted"] = 8
    seed = orch._ROUTE_SEED_HEADER + json.dumps(serialized)

    composed = _composed_context_from_text(seed)

    assert {a["system"] for a in composed["artifacts"]} == {first}
    truncation = composed["delivery_truncation"]
    assert truncation["accounting"] == "incomplete"
    assert len(truncation["unverified_systems"]) == 8
    assert truncation["unverified_notes"] == 32


def test_a_malformed_row_withholds_that_systems_notes():
    from agents.runtime_app import _composed_context_from_text
    serialized = _decode(orch._encode_route_seed(_overflow_seed_payload(systems=2)))
    serialized["guidance_delivery_truncation"]["systems"]["system-0"] = "garbled"
    seed = orch._ROUTE_SEED_HEADER + json.dumps(serialized)

    composed = _composed_context_from_text(seed)

    assert {a["system"] for a in composed["artifacts"]} == {"system-1"}
    assert composed["delivery_truncation"]["unverified_systems"] == ["system-0"]


#: Syntactically valid rows whose counts contradict each other or are not
#: non-negative integers. The first two reproduce the case where
#: a dropped query note was reported as delivered with no incomplete marker.
_CONTRADICTORY_ROWS = [
    [0, 0, 0, "q", {"q": 1}],      # total 0, scope count 1
    [0, -1, 0, "q", {"q": 1}],     # negative total
    [0, 3, 0, "q", {"q": 2}],      # counts do not add up
    [0, 3, 0, "q"],                # drops with no per-scope counts
    [0, 3, 0, "q", {"q": -3}],     # negative scope count
    [0, 3, 0, "q", {"z": 3}],      # unknown scope code
    [0, 3, 0, "q", {"q": 3.0}],    # non-integer count
    [0, 3, 0, "q", {"q": "3"}],    # string count
    [0, True, 0, "q", {"q": 1}],   # bool total
    [-5, 3, 0, "q", {"q": 3}],     # negative chars
    [0, 3, 0, ["q"], {"q": 3}],    # field codes not a string
]


@pytest.mark.parametrize("row", _CONTRADICTORY_ROWS, ids=repr)
def test_a_contradictory_row_withholds_that_systems_notes(row):
    from agents.runtime_app import _composed_context_from_text
    serialized = _decode(orch._encode_route_seed(_overflow_seed_payload(systems=2)))
    rows = serialized["guidance_delivery_truncation"]["systems"]
    assert rows["system-0"] == [rows["system-0"][0], 3, 0, "a", {"a": 3}]  # producer row
    rows["system-0"] = row
    seed = orch._ROUTE_SEED_HEADER + json.dumps(serialized)

    composed = _composed_context_from_text(seed)

    assert {a["system"] for a in composed["artifacts"]} == {"system-1"}
    truncation = composed["delivery_truncation"]
    assert truncation["accounting"] == "incomplete"
    assert truncation["unverified_systems"] == ["system-0"]
    assert truncation["unverified_notes"] == 4


def test_a_consistent_zero_drop_row_still_delivers_every_note():
    """The validation must not refuse a producer-shaped clean row."""
    from agents.runtime_app import _composed_context_from_text
    serialized = _decode(orch._encode_route_seed(_overflow_seed_payload(systems=2)))
    serialized["guidance_delivery_truncation"]["systems"]["system-0"] = [12, 0, 0, "a"]
    seed = orch._ROUTE_SEED_HEADER + json.dumps(serialized)

    composed = _composed_context_from_text(seed)

    assert sum(a["system"] == "system-0" for a in composed["artifacts"]) == 4
    assert "accounting" not in composed["delivery_truncation"]


def test_the_in_memory_report_path_counts_past_the_per_report_digest_cap():
    """A report lists at most DROPPED_DIGEST_CAP digests; the per-scope count
    still excludes every dropped note (the previous reader re-added the rest)."""
    from agents.guidance_scope import DROPPED_DIGEST_CAP
    notes = _notes("n", DROPPED_DIGEST_CAP + 4, size=60)
    system = _payload([_row("system-a", _answers_with(notes))])["systems"][0]
    orch._bound_route_seed_guidance(system, 1024, overrides={"answer_requirements": 400})
    report = system["guidance_composition"]["delivery_truncation"]
    assert report["notes_dropped"] > DROPPED_DIGEST_CAP

    composed = query_intelligence.build_composed_context_from_metadata([system])

    kept = len(notes) - report["notes_dropped"]
    assert composed["artifact_count"] == kept
    assert {a["digest"] for a in composed["artifacts"]} == {
        content_digest(note) for note in notes[-kept:]}


# --------------------------------------------------------------------------- #
# Lever 3: empty keys are omitted, the authority marker is not
# --------------------------------------------------------------------------- #
def test_empty_keys_are_omitted_but_the_authority_marker_stays():
    seed = _seed([_row("system-a", ""), _row("system-b", "")])
    system = _decode(seed)["systems"][0]

    for key in ("query_guidance", "answer_requirements", "legacy_guidance",
                "guidance_composition", "shared_concepts"):
        assert key not in system
    assert system["rules"] == {"authority_rules": []}


def test_every_seed_reader_tolerates_the_omitted_keys():
    from agents.route_seed_trace import seed_step_text
    from agents.runtime_app import (
        _composed_context_from_text,
        _planned_with_from_seed,
        _seed_rule_index_from_text,
    )
    seed = _seed([_row("system-a", ""), _row("system-b", "")])

    planned = _planned_with_from_seed(seed)
    assert planned["systems"] == 2 and planned["authority_rules"] == 0
    assert planned["join_paths"] == 0 and planned["shared_concepts"] == []
    assert _seed_rule_index_from_text(seed) == []
    assert _composed_context_from_text(seed) is None
    assert "System-A" in seed_step_text(seed)


# --------------------------------------------------------------------------- #
# The reproduction fixture: 7, 8 and 9 systems, before and after
# --------------------------------------------------------------------------- #
#: (systems, variant) -> the pinned delivery numbers. "before" disables the
#: three levers (the serializer as it was); "after" is this serializer. Both
#: run at ``fx.LEVER_BUDGET``, the budget the levers were measured under.
#: Registration bases: 2 plant systems at 7, 3 from 8 up; generic bases: 5 at
#: 7 and 8, 6 at 9. The figures quoted in the merge request come from here.
EXPECTED = {
    (7, "before"): {"registration_bases": 2, "generic_bases": 0, "capabilities": 7,
                    "rules": 10, "drop_report_chars": 531},
    (7, "after"): {"registration_bases": 2, "generic_bases": 5, "capabilities": 7,
                   "rules": 10, "drop_report_chars": 0},
    (8, "before"): {"registration_bases": 0, "generic_bases": 0, "capabilities": 8,
                    "rules": 10, "drop_report_chars": 849},
    (8, "after"): {"registration_bases": 3, "generic_bases": 5, "capabilities": 8,
                   "rules": 10, "drop_report_chars": 0},
    (9, "before"): {"registration_bases": 0, "generic_bases": 0, "capabilities": 4,
                    "rules": 10, "drop_report_chars": 954},
    (9, "after"): {"registration_bases": 3, "generic_bases": 6, "capabilities": 9,
                   "rules": 10, "drop_report_chars": 0},
}


def _measured(count: int, variant: str, learned_notes: int = 0,
              budget: "int | None" = fx.LEVER_BUDGET) -> tuple[str, dict]:
    if variant == "before":
        with fx.main_behaviour():
            seed = fx.build_seed(count, learned_notes=learned_notes, budget=budget)
    else:
        seed = fx.build_seed(count, learned_notes=learned_notes, budget=budget)
    return seed, fx.measure(seed, count)


@pytest.mark.parametrize("count,variant", sorted(EXPECTED))
def test_fixture_delivery_numbers_are_pinned(count, variant):
    seed, measured = _measured(count, variant)

    assert measured["seed_chars"] <= fx.LEVER_BUDGET
    assert {k: v for k, v in measured.items() if k != "seed_chars"} == EXPECTED[(count, variant)]


@pytest.mark.parametrize("count", [7, 8, 9])
def test_production_budget_delivers_every_base_and_references_both_notes(count):
    """At the production budget (raised 2026-10-08, ADR-0035) the fixture's
    9-system pressure that cost the generic default its words is gone: every
    base, every capability and every rule arrives, and nothing is reported
    dropped. The two learned notes are query hints, so since 2026-10-16 they
    reach the system agent with each question and the seed carries a count."""
    seed, measured = _measured(count, "after", learned_notes=2, budget=None)

    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    assert measured == {**EXPECTED[(count, "after")], "seed_chars": len(seed)}
    assert not any(note in seed for note in fx.LEARNED_NOTES[:2])
    systems = {s["id"]: s for s in _decode(seed)["systems"]}
    assert systems["plant-execution"]["query_hints"].startswith(
        "2 query hints are delivered to plant-execution")
    assert "guidance_delivery_truncation" not in _decode(seed)


@pytest.mark.parametrize("count", [7, 8, 9])
def test_after_delivers_every_registration_base_and_capability(count):
    _seed_text, measured = _measured(count, "after")
    registration = sum(sid in fx.REGISTRATION for sid in fx.system_ids(count))

    assert measured["registration_bases"] == registration
    assert measured["generic_bases"] == len(fx.generic_ids(count))
    assert measured["capabilities"] == count


def test_learned_query_hints_ride_as_a_reference_under_the_lever_budget():
    """Two learned query-hint notes on one plant system, at the budget the
    levers were measured under: the previous serializer dropped both under the
    pressure they caused. Since 2026-10-16 they never ride the seed; one count
    line does. At 7 and 8 systems the seed then delivers exactly what it
    delivers without them; at 9 the budget is genuinely short even for the
    count line, so the GENERIC shared default yields first and every loss is
    reported."""
    for count in (7, 8, 9):
        seed, measured = _measured(count, "after", learned_notes=2)
        assert not any(note in seed for note in fx.LEARNED_NOTES[:2])
        systems = {s["id"]: s for s in _decode(seed)["systems"]}
        assert systems["plant-execution"]["query_hints"].startswith("2 query hints")
        if count < 9:
            assert {k: v for k, v in measured.items() if k != "seed_chars"} == (
                EXPECTED[(count, "after")])
            continue
        assert measured["capabilities"] == 9 and measured["rules"] == 10
        aggregate = _decode(seed)["guidance_delivery_truncation"]
        assert aggregate["chars_dropped"] > 0
        assert set(fx.generic_ids(9)) <= set(aggregate["systems"])
