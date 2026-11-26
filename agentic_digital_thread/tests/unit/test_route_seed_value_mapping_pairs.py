# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A ValueMapping reaches the route seed as a field-pair entry carrying a count.

A ValueMapping node maps ONE value of a field to one value of another field.
The seed used to project only the two field names, so every mapping on a
field pair collapsed into one entry that read as a field-to-field
relationship (observed live: two site-code mappings, each mapping a site
code to itself, showed up as one confirmed site-code -> line-name mapping,
and the planner sent line names to the site-code column).

The value-mapping decision (ADR-0035 amendment 2026-10-18) keeps that protection WITHOUT serving the
values in the systems payload: the mapped values reach the orchestrator only
through the question-scoped IDENTIFIER VALUE MAPPINGS block (its own test is
``test_route_seed_value_mappings.py``), because a served two-pair list made the
orchestrator claim those were the only two cells with data
(``s36/p3-cells-claim-report.md``). Pinned here is the field-pair ENTRY:

* the traversal groups the mappings of one field pair into ONE entry and emits
  ``value_pair_count`` (how many values the field pair maps); an entry with no
  value pair is never returned;
* the SERVED entry carries ``value_pair_count`` but NEVER the values (the
  values are held on the internal ``_value_pairs`` key and stripped from the
  serialized seed) -- its header says a value mapping maps only some values of
  its fields one by one, is not a whole-field rule, and the count is not the
  field's value count;
* under budget pressure a mapping the question names no value of is dropped as
  a whole entry, before any rule (authority rules included), and only as many
  as the budget needs;
* a grouped entry's confidence is the lowest of its pairs;
* the stored records are not touched (read-only).

Superseded by the decision and REMOVED from this file (each listed with its
reason, nothing that still applies was weakened):

* the old ``test_seed_carries_the_value_pairs`` asserting ``vm["value_pairs"]``
  on the SERVED entry -- values are no longer in the systems payload; the entry
  now carries only ``value_pair_count`` (covered by
  ``test_the_entry_carries_the_count_never_the_values``).
* the old ``test_header_says_a_value_mapping_maps_only_its_listed_values``
  asserting ``"valuemapping maps only its value_pairs" in header`` and that the
  new "row-level values" wording was ABSENT -- that was exactly the P3-bug
  wording the decision removes; the header now says the opposite (covered by
  ``test_header_describes_the_value_level_entry_without_serving_values``).
* every ``assert vm["value_pairs"] == [...]`` / ``all(vm.get("value_pairs"))``
  in the merge, bound, confidence, fits and budget tests -- the served entry
  has no ``value_pairs``; those assertions are replaced by the same checks on
  ``value_pair_count`` and on the values' ABSENCE from the serialized seed. The
  behavioural claims each test made (merge split rows, bound the count, drop an
  unnamed entry whole before any authority rule, only as many as the budget
  needs, lowest confidence, a fitting seed keeps the entry) are all KEPT.

The Memgraph-backed twins live in
``tests/integration/test_deactivated_rule_joinpath_memgraph.py`` and
``tests/integration/test_route_seed_live_export_memgraph.py``.

Offline: the graph is a fake. GENERIC: synthetic systems only.
"""
from __future__ import annotations

import json

import agents.orchestrator as orch

SYS_A, SYS_B = "sys-a", "sys-b"


def _payload(seed: str) -> dict:
    headers = (orch._ROUTE_SEED_HEADER, orch._ROUTE_SEED_PARTIAL_HEADER,
               *orch._ROUTE_SEED_VALUE_MAPPING_HEADERS.values())
    for header in headers:
        if seed.startswith(header):
            body = seed[len(header):]
            # The IDENTIFIER VALUE MAPPINGS block (when present) follows the
            # JSON payload; decode only the leading JSON object.
            obj, _end = json.JSONDecoder().raw_decode(body)
            return obj
    raise AssertionError("route seed has no recognized header")


def _vm_clause() -> str:
    return orch._ROUTE_SEED_CYPHER.split("OPTIONAL MATCH (vm:ValueMapping)")[1].split(
        "AS value_mappings")[0]


def test_traversal_counts_value_pairs_per_field_pair():
    clause = _vm_clause()
    # The traversal still reads the pairs (they feed value_pair_count and the
    # never-empty rule); the count is served, the pairs are the input channel.
    assert "value_pair_count:" in clause
    assert "vm.from_value" in clause and "vm.to_value" in clause
    # An entry with no value pair collects as null, so it never takes a slot.
    assert "size(vpairs) = 0 THEN null" in clause
    # Still capped per kind, still provenance-ordered.
    assert f"[0..{orch._ROUTE_SEED_RULE_CAP}]" in clause
    assert "'user_confirmed', 'crosswalk_confirmed'" in clause
    assert "SET " not in clause and "MERGE" not in clause and "DELETE" not in clause


def _query(value_mappings):
    def query(cypher, *_a, **_k):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return [{"system_id": SYS_A, "system_name": "System A"},
                    {"system_id": SYS_B, "system_name": "System B"}]
        if cypher == orch._ROUTE_SEED_CYPHER:
            return [{"system_id": SYS_A, "shared_concepts": [], "join_paths": [],
                     "identity_rules": [], "value_mappings": value_mappings}]
        return []
    return query


_PAIRED = {"from_system": SYS_A, "from_field": "records.site_code", "to_system": SYS_B,
           "to_field": "hierarchy.line_name", "concept": "line", "method": "user_confirmed",
           "confidence": 1.0,
           "value_pairs": [{"from": "S1", "to": "S1"}, {"from": "S2", "to": "S2"}],
           "value_pair_count": 2}


def _vms(seed):
    return [vm for system in _payload(seed)["systems"]
            for vm in system.get("rules", {}).get("value_mappings", [])]


def test_the_entry_carries_the_count_never_the_values():
    seed = orch.build_route_seed(query=_query([_PAIRED]))
    [vm] = _vms(seed)
    assert vm["value_pair_count"] == 2
    assert "value_pairs" not in vm and "_value_pairs" not in vm
    assert "S1" not in seed and "S2" not in seed  # no mapped value anywhere
    assert vm["from_field"] == "records.site_code"  # the field pair still identifies it


def test_an_entry_without_value_pairs_is_never_served():
    bare = {k: v for k, v in _PAIRED.items() if k not in ("value_pairs", "value_pair_count")}
    empty = {**_PAIRED, "value_pairs": [], "value_pair_count": 0}
    junk = {**_PAIRED, "value_pairs": [{"from": None, "to": "S1"}, "S2", {"from": "S3"}, ["S4"]]}
    seed = orch.build_route_seed(query=_query([bare, empty, junk]))
    assert _vms(seed) == []
    assert "records.site_code" not in seed


def test_the_count_is_disclosed_and_the_values_are_not():
    long_value = "V" * 1000
    pairs = [{"from": f"{long_value}{i}", "to": str(i)} for i in range(orch._ROUTE_SEED_VALUE_PAIR_CAP + 4)]
    seed = orch.build_route_seed(query=_query([{**_PAIRED, "value_pairs": pairs,
                                                "value_pair_count": len(pairs)}]))
    [vm] = _vms(seed)
    assert vm["value_pair_count"] == len(pairs)  # the total the field pair maps
    assert "value_pairs" not in vm
    assert long_value not in seed  # not even a bounded prefix of a value is served


def _rows_query(inventory, rows):
    def query(cypher, *_a, **_k):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        if cypher == orch._ROUTE_SEED_CYPHER:
            return rows
        return []
    return query


_INVENTORY = [{"system_id": SYS_A, "system_name": "System A"},
              {"system_id": SYS_B, "system_name": "System B"}]


def test_split_rows_of_one_field_pair_are_merged_into_one_count():
    """An engine may return one system's grouped rows split, each with part
    of the pairs: the seed shows one entry whose count covers all of them."""
    one = {**_PAIRED, "value_pairs": [{"from": "S2", "to": "S2"}], "value_pair_count": 1}
    two = {**_PAIRED, "value_pairs": [{"from": "S1", "to": "S1"}], "value_pair_count": 1}
    rows = [{"system_id": SYS_A, "value_mappings": [one]},
            {"system_id": SYS_A, "value_mappings": [two]},
            {"system_id": SYS_B, "value_mappings": [two, one]}]
    seed = orch.build_route_seed(query=_rows_query(_INVENTORY, rows))
    [vm] = _vms(seed)  # merged per system, then kept under one of its systems
    assert vm["value_pair_count"] == 2  # S1 and S2, counted once each
    assert "value_pairs" not in vm
    assert "S1" not in seed and "S2" not in seed


def test_under_budget_pressure_an_unnamed_mapping_is_dropped_whole(monkeypatch):
    """The seed is over budget: a mapping holding a value the question names
    keeps its entry; a mapping the question names no value of is dropped as a
    whole entry. Named detection reads the values held internally, not served."""
    # The budget this over-budget seed was sized against.
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", 10_000)
    named = {**_PAIRED, "value_pairs": [{"from": "SITEX9", "to": "SITEX9"}], "value_pair_count": 1}
    other = {**_PAIRED, "from_field": "records.route", "to_field": "hierarchy.route_name",
             "value_pairs": [{"from": "R1", "to": "r1"}, {"from": "R2", "to": "r2"}],
             "value_pair_count": 2}
    # Over budget even after the prose pass: wide tier-2 detail on the rows.
    shared = [{"concept": f"concept-{i:02d}-" + "c" * 200, "peer_system": SYS_B} for i in range(50)]
    rows = [{"system_id": SYS_A, "shared_concepts": shared, "value_mappings": [named, other]}]
    seed = orch.build_route_seed(query=_rows_query(_INVENTORY, rows),
                                 question="What did SITEX9 make yesterday?")
    by_field = {vm["from_field"]: vm for vm in _vms(seed)}
    assert by_field["records.site_code"]["value_pair_count"] == 1  # the named entry stayed
    assert "records.route" not in by_field  # the unnamed entry was dropped whole
    assert "records.route" not in seed
    assert all("value_pairs" not in vm for vm in _vms(seed))  # never served with its pairs


def _authority_query(value_mappings, authority):
    def query(cypher, *_a, **_k):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return _INVENTORY
        if cypher == orch._ROUTE_SEED_CYPHER:
            return [{"system_id": SYS_A, "shared_concepts": [], "join_paths": [],
                     "identity_rules": [], "value_mappings": value_mappings}]
        if cypher == orch._ROUTE_SEED_AUTHORITY_CYPHER:
            return authority
        return []
    return query


def _authority_ids(seed) -> set:
    return {rule["id"] for system in _payload(seed)["systems"]
            for rule in system.get("rules", {}).get("authority_rules", [])}


def test_unnamed_mappings_are_dropped_before_any_authority_rule(monkeypatch):
    """Authority rules decide routing. Under budget pressure every mapping the
    question names no value of goes before any of them, whatever the
    mapping's provenance tier."""
    authority = [{"id": f"ar-{SYS_A}-concept_{i}", "concept": f"concept_{i}", "system_id": SYS_A,
                  "method": "onboarding-llm", "confidence": 0.8} for i in range(7)]
    mappings = [{**_PAIRED, "from_field": f"records.code_{i}", "to_field": f"hierarchy.name_{i}",
                 "value_pairs": [{"from": f"V{i}{j}" + "x" * 60, "to": f"v{i}{j}" + "y" * 60}
                                 for j in range(3)], "value_pair_count": 3}
                for i in range(4)]
    baseline = orch.build_route_seed(query=_authority_query([], authority),
                                     question="What happened yesterday?")
    assert _authority_ids(baseline) == {a["id"] for a in authority}
    # A budget the seed without mappings fits, and the seed with them does not.
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", len(baseline) + 40)
    seed = orch.build_route_seed(query=_authority_query(mappings, authority),
                                 question="What happened yesterday?")
    assert _authority_ids(seed) == {a["id"] for a in authority}
    assert all("value_pairs" not in vm for vm in _vms(seed))


def test_only_as_many_unnamed_mappings_go_as_the_budget_needs(monkeypatch):
    mappings = [{**_PAIRED, "from_field": f"records.code_{i}", "to_field": f"hierarchy.name_{i}",
                 "confidence": 1.0 - i / 10,
                 "value_pairs": [{"from": f"V{i}" + "x" * 80, "to": f"v{i}" + "y" * 80}],
                 "value_pair_count": 1} for i in range(4)]
    full = orch.build_route_seed(query=_authority_query(mappings, []))
    one_entry = len(json.dumps(_vms(full)[-1], separators=(",", ":")))
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", len(full) - one_entry // 2)
    seed = orch.build_route_seed(query=_authority_query(mappings, []))
    kept = [vm["from_field"] for vm in _vms(seed)]
    # The lowest-ranked entry went; the others are served whole (count only).
    assert kept == ["records.code_0", "records.code_1", "records.code_2"]
    assert all("value_pairs" not in vm for vm in _vms(seed))


def test_a_grouped_mappings_confidence_is_its_weakest_pair():
    """One entry stands for every pair it groups, so it carries the lowest
    confidence among them, never the highest."""
    assert "min(vm.confidence) AS vconf" in _vm_clause()
    assert "max(vm.confidence)" not in _vm_clause()
    strong = {**_PAIRED, "confidence": 1.0, "value_pairs": [{"from": "S1", "to": "S1"}],
              "value_pair_count": 1}
    weak = {**_PAIRED, "confidence": 0.6, "value_pairs": [{"from": "S2", "to": "S2"}],
            "value_pair_count": 1}
    rows = [{"system_id": SYS_A, "value_mappings": [strong]},
            {"system_id": SYS_A, "value_mappings": [weak]}]
    seed = orch.build_route_seed(query=_rows_query(_INVENTORY, rows))
    [vm] = _vms(seed)
    assert vm["value_pair_count"] == 2
    assert vm["confidence"] == 0.6


def test_a_seed_that_fits_keeps_the_entry_with_its_count():
    named = {**_PAIRED, "value_pairs": [{"from": "R1", "to": "r1"}, {"from": "R2", "to": "r2"}],
             "value_pair_count": 2}
    seed = orch.build_route_seed(query=_query([named]), question="What happened yesterday?")
    [vm] = _vms(seed)
    assert vm["value_pair_count"] == 2
    assert "value_pairs" not in vm
    assert "R1" not in seed and "R2" not in seed


def test_header_describes_the_value_level_entry_without_serving_values():
    for header in (orch._ROUTE_SEED_HEADER, orch._ROUTE_SEED_PARTIAL_HEADER):
        # A value mapping maps only SOME values of its fields, one by one;
        # it is not a whole-field rule; the count is not the field's value count.
        assert "maps some values of its fields one by one, not the whole field" in header
        assert "value_pair_count is how many it maps, not the field's value count" in header
        # The plain header says no mapped value is listed; the P3-bug wording is gone.
        assert "No stored mapped value is listed" in header
        assert "maps only its value_pairs" not in header.lower()
    # The question-scoped variant points at the IDENTIFIER VALUE MAPPINGS block.
    for mapped in orch._ROUTE_SEED_VALUE_MAPPING_HEADERS.values():
        assert "mapped values for identifiers this question names are listed" in mapped
        assert "IDENTIFIER VALUE MAPPINGS block" in mapped


# --- New tests required by the decision -----------------------------------
# Both exercise a two-pair field-pair ENTRY in the systems payload together
# with main's question-scoped IDENTIFIER VALUE MAPPINGS block, and both assert
# int's concept notes are present. The entry never carries pairs; values reach
# the orchestrator only through the block, and only for an identifier the
# question names.
import agents.concept_notes as _cn
import agents.identifier_translation as _it
from agents.catalog import REMOVING_SYSTEMS_READ

# A concept whose (multi-word) name a question can mention, so a concept note
# is injected via ``concepts=`` (no graph read needed for it).
_NOTE_CONCEPT = _cn.Concept(
    id="production_line", name="production line",
    description="A production line is a line of equipment; ask for it by its line name.",
    systems=(SYS_A,))

# A trusted stored ValueMapping (deterministic id prefix + method) the
# question-scoped read returns when the question names one of its values.
_STORED_VM = {"id": "vm-equip-1", "from_system": SYS_A, "from_field": "records.site_code",
              "from_value": "S1", "to_system": SYS_B, "to_field": "hierarchy.line_name",
              "to_value": "S1", "concept": "line", "method": "value_format_match",
              "confidence": 1.0, "provenance": None}


def _combined_query(question_value_rows):
    """Answers every read build_route_seed makes: inventory, the rule cypher
    (one two-pair value-mapping entry on SYS_A), the question-scoped value
    mapping read (``question_value_rows``), and the removing read (none)."""
    def query(cypher, params=None):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return _INVENTORY
        if cypher == orch._ROUTE_SEED_CYPHER:
            return [{"system_id": SYS_A, "shared_concepts": [], "join_paths": [],
                     "identity_rules": [], "value_mappings": [_PAIRED]}]
        if cypher == orch._ROUTE_SEED_AUTHORITY_CYPHER:
            return []
        if cypher == REMOVING_SYSTEMS_READ:
            return []
        if "\nMATCH (vm:ValueMapping)\nWHERE " in cypher:
            return question_value_rows
        return []
    return query


def test_a_two_pair_mapping_a_question_names_no_value_of_has_count_and_no_values():
    """Question names NEITHER value (names the concept 'line', not S1/S2): the
    entry carries count 2, no values are anywhere in the seed, there is no
    IDENTIFIER VALUE MAPPINGS block, and the concept note is present."""
    seed = orch.build_route_seed(query=_combined_query([]),
                                 question="Which production line ran yesterday?",
                                 concepts=(_NOTE_CONCEPT,))
    [vm] = _vms(seed)
    assert vm["value_pair_count"] == 2
    assert "value_pairs" not in vm
    assert "S1" not in seed and "S2" not in seed  # no mapped value anywhere
    assert not _it.carries_value_mappings(seed)  # no question-scoped block
    payload = _payload(seed)
    assert payload.get("concept_notes")  # int's concept note present
    assert "production line" in json.dumps(payload["concept_notes"])


def test_a_two_pair_mapping_a_question_names_one_value_of_lists_that_row_only():
    """Question names ONE value (S1): that one row appears in the IDENTIFIER
    VALUE MAPPINGS block, the systems-payload entry still carries no pairs, and
    the concept note is present."""
    seed = orch.build_route_seed(query=_combined_query([_STORED_VM]),
                                 question="Where does production line S1 run?",
                                 concepts=(_NOTE_CONCEPT,))
    [vm] = _vms(seed)
    assert vm["value_pair_count"] == 2
    assert "value_pairs" not in vm  # the entry still carries no pairs
    # The one named row is in the question-scoped block, not the systems payload.
    assert _it.carries_value_mappings(seed)
    block = seed[seed.index(_it.VALUE_MAPPINGS_HEADER):]
    assert "S1" in block
    assert "S2" not in seed  # the value the question did not name is never served
    payload = _payload(seed)
    assert payload.get("concept_notes")
    assert "production line" in json.dumps(payload["concept_notes"])


# --- Namedness from the display sample OR the question-scoped read ----------
# Namedness of a value-mapping entry is decided from its three-value display
# sample OR from the question-scoped value-mapping read (same field pair). The
# question-scoped read compares the question's identifiers against EVERY stored
# value, so a value beyond the display sample is still caught -- with no cap on
# the entry side. When that read cannot be trusted (it raised or hit its cap),
# the unnamed-drop pressure pass fails safe and drops no value-mapping entry.
#
# (The former _ROUTE_SEED_VALUE_NAMEDNESS_CAP / named_values test was removed:
# the namedness-value set it pinned is gone -- it failed open past its cap and
# cost up to thousands of strings per row; namedness now rides the existing
# question-scoped read, which has no entry-side cap.)
_READ_CAP = orch._ROUTE_SEED_VALUE_MAPPING_READ_CAP


def _cell_mapping(n):
    """A SYS_A->SYS_B field-pair rule-cypher row with ``n`` value pairs
    CELL1..CELLn: the display sample is bounded to the display cap and the full
    count is n. No namedness set -- namedness comes from the question-scoped
    read, not the inventory row."""
    values = [f"CELL{i}" for i in range(1, n + 1)]
    return {**_PAIRED, "from_field": "records.cell", "to_field": "hierarchy.line_name",
            "value_pairs": [{"from": v, "to": v} for v in values[:orch._ROUTE_SEED_VALUE_PAIR_CAP]],
            "value_pair_count": n}


_UNNAMED_OTHER = {**_PAIRED, "from_field": "records.route", "to_field": "hierarchy.route_name",
                  "value_pairs": [{"from": "R1", "to": "r1"}, {"from": "R2", "to": "r2"}],
                  "value_pair_count": 2}

#: Over-budget pressure: wide tier-2 shared-concept prose on the SYS_A row.
_WIDE_SHARED = [{"concept": f"concept-{i:02d}-" + "c" * 200, "peer_system": SYS_B}
                for i in range(50)]


def _vm_row(from_value, to_value):
    """A question-scoped value-mapping read row for the SYS_A->SYS_B field pair
    (the endpoints the entry carries), shaped as _ROUTE_SEED_VALUE_MAPPING_RETURN
    returns them. Direction preserved: from_* is the mapping's from side."""
    return {"id": "vm-equip-x", "from_system": SYS_A, "from_field": "records.cell",
            "from_value": from_value, "to_system": SYS_B, "to_field": "hierarchy.line_name",
            "to_value": to_value, "concept": "line", "method": "value_format_match",
            "confidence": 1.0, "provenance": None}


def _pressured_query(value_mappings, vm_read):
    """Inventory + rule cypher (SYS_A rows, wide shared prose for pressure) +
    the question-scoped value-mapping read answered by ``vm_read`` (a list, or a
    callable that may raise or return a capped list), + removing (none)."""
    def query(cypher, params=None):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return _INVENTORY
        if cypher == orch._ROUTE_SEED_CYPHER:
            return [{"system_id": SYS_A, "shared_concepts": _WIDE_SHARED, "join_paths": [],
                     "identity_rules": [], "value_mappings": value_mappings}]
        if cypher == orch._ROUTE_SEED_AUTHORITY_CYPHER:
            return []
        if cypher == REMOVING_SYSTEMS_READ:
            return []
        if "\nMATCH (vm:ValueMapping)\nWHERE " in cypher:
            return vm_read() if callable(vm_read) else vm_read
        return []
    return query


def test_a_value_named_only_by_the_question_scoped_read_keeps_its_entry(monkeypatch):
    """A field pair far larger than any sample (201 values); the question names
    CELL201, which is not in the display sample. The question-scoped read
    returns a row for the same field pair, so the entry is named and survives
    the unnamed-mapping pressure pass, while an unnamed entry is dropped."""
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", 10_000)
    named = _cell_mapping(201)
    assert ["CELL201", "CELL201"] not in [[p["from"], p["to"]] for p in named["value_pairs"]]
    query = _pressured_query([named, _UNNAMED_OTHER], [_vm_row("CELL201", "CELL201")])
    seed = orch.build_route_seed(query=query, question="Where is CELL201?")
    by_field = {vm["from_field"]: vm for vm in _vms(seed)}
    assert by_field["records.cell"]["value_pair_count"] == 201  # the named entry stayed
    assert "records.route" not in by_field  # the unnamed entry was dropped whole
    assert all("value_pairs" not in vm for vm in _vms(seed))


def test_the_four_value_cell4_regression_via_the_question_scoped_read(monkeypatch):
    """The four-value CELL4 case, kept because the question-scoped read
    matches the field pair (not because of a namedness value set)."""
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", 10_000)
    query = _pressured_query([_cell_mapping(4), _UNNAMED_OTHER], [_vm_row("CELL4", "CELL4")])
    seed = orch.build_route_seed(query=query, question="Where is CELL4?")
    by_field = {vm["from_field"]: vm for vm in _vms(seed)}
    assert by_field["records.cell"]["value_pair_count"] == 4
    assert "records.route" not in by_field


def test_the_pressure_pass_drops_no_value_mapping_when_the_read_raised(monkeypatch):
    """When the question has identifiers but the question-scoped read RAISED,
    namedness is unknown: the unnamed-mapping pressure pass drops no
    value-mapping entry (fail safe). The block is omitted as before."""
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", 10_000)
    def raise_read():
        raise RuntimeError("graph refused the value-mapping read")
    query = _pressured_query([_cell_mapping(4), _UNNAMED_OTHER], raise_read)
    seed = orch.build_route_seed(query=query, question="Where is CELL4?")
    by_field = {vm["from_field"]: vm for vm in _vms(seed)}
    assert "records.cell" in by_field and "records.route" in by_field  # neither dropped
    assert not _it.carries_value_mappings(seed)  # the block is omitted


def test_the_pressure_pass_drops_no_value_mapping_when_the_read_hit_its_cap(monkeypatch):
    """Same fail-safe when the read returned its cap of rows (a matching row
    may be past the cap): no value-mapping entry is dropped; block omitted."""
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", 10_000)
    capped = [_vm_row(f"X{i}", f"X{i}") for i in range(_READ_CAP)]  # exactly the cap
    query = _pressured_query([_cell_mapping(4), _UNNAMED_OTHER], capped)
    seed = orch.build_route_seed(query=query, question="Where is CELL4?")
    by_field = {vm["from_field"]: vm for vm in _vms(seed)}
    assert "records.cell" in by_field and "records.route" in by_field  # neither dropped
    assert not _it.carries_value_mappings(seed)


def test_a_question_with_no_identifier_still_drops_unnamed_entries_first(monkeypatch):
    """No identifier -> no question-scoped read is made (value_rows is []),
    so behaviour is unchanged: an unnamed value-mapping entry is dropped first
    under pressure."""
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", 10_000)
    query = _pressured_query([_UNNAMED_OTHER], [])
    seed = orch.build_route_seed(query=query, question="What happened yesterday?")
    assert "records.route" not in {vm["from_field"] for vm in _vms(seed)}


def test_split_rows_named_in_one_part_make_the_merged_entry_named(monkeypatch):
    """One system returns the SYS_A->SYS_B field pair split across rows; the
    question-scoped read names it. The merged entry is named (OR of parts) and
    survives, while an unnamed entry is dropped."""
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", 10_000)
    one = {**_cell_mapping(4), "value_pairs": [{"from": "CELL2", "to": "CELL2"}],
           "value_pair_count": 2}
    two = {**_cell_mapping(4), "value_pairs": [{"from": "CELL1", "to": "CELL1"}],
           "value_pair_count": 2}
    def query(cypher, params=None):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return _INVENTORY
        if cypher == orch._ROUTE_SEED_CYPHER:
            return [{"system_id": SYS_A, "shared_concepts": _WIDE_SHARED, "join_paths": [],
                     "identity_rules": [], "value_mappings": [one, two, _UNNAMED_OTHER]}]
        if cypher == orch._ROUTE_SEED_AUTHORITY_CYPHER:
            return []
        if cypher == REMOVING_SYSTEMS_READ:
            return []
        if "\nMATCH (vm:ValueMapping)\nWHERE " in cypher:
            return [_vm_row("CELL4", "CELL4")]
        return []
    seed = orch.build_route_seed(query=query, question="Where is CELL4?")
    by_field = {vm["from_field"]: vm for vm in _vms(seed)}
    assert "records.cell" in by_field  # merged entry named, kept
    assert "records.route" not in by_field  # the unnamed entry dropped
