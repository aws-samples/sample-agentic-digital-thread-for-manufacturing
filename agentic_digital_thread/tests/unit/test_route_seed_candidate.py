# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""B2 try-it candidate injection into the Mode-A route seed (ADR-0052 (d)).

Offline: graph reads use an injected fake query (the
``test_orchestrator_route_seed.py`` idiom); no AWS, Neptune, or LLM. Pins:

- the candidate is appended to its owning system's rule list marked
  ``method="tryit-candidate"`` and classified tier 2 (R3) — it participates
  in the existing cap/budget machinery and can NEVER displace a steward
  (tier-0) or accepted (tier-1) artifact;
- ``candidate=None`` produces a byte-identical seed (every ordinary turn);
- the candidate is NEVER written to the graph — the query seam sees only the
  two known read traversals;
- row-level values and unshapeable candidates never reach the seed.
"""
from __future__ import annotations

import json

import agents.orchestrator as orch
from agents.catalog import REMOVING_SYSTEMS_READ


def _payload(seed: str) -> dict:
    for header in (orch._ROUTE_SEED_HEADER, orch._ROUTE_SEED_PARTIAL_HEADER):
        if seed.startswith(header):
            return json.loads(seed[len(header):])
    raise AssertionError("route seed has no recognized header")


def _fake_query(rule_rows=None):
    rows = rule_rows if rule_rows is not None else [{
        "system_id": "sys-a",
        "join_paths": [{
            "id": "jp-live-1", "from_system": "sys-a", "from_field": "t.a",
            "to_system": "sys-b", "to_field": "t.b", "confidence": 0.9,
            "provenance": "accepted",
        }],
    }]

    def query(cypher: str):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return [{"system_id": "sys-a", "system_name": "System A"},
                    {"system_id": "sys-b", "system_name": "System B"}]
        return rows

    return query


_CANDIDATE = {
    "proposed_type": "join_path",
    "from_system": "sys-a", "from_field": "orders.op_ref",
    "to_system": "sys-b", "to_field": "events.op_id",
    "concept": "operation.identity", "confidence": "0.7",
    "mapping_id": "cand-mapping-1",
}


def test_candidate_appended_tier2_and_budget_respected():
    seed = orch.build_route_seed(query=_fake_query(), candidate=dict(_CANDIDATE))

    assert seed is not None
    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    joins = {j["id"]: j for system in _payload(seed)["systems"]
             for j in system.get("rules", {}).get("join_paths", [])}
    assert "cand-mapping-1" in joins
    entry = joins["cand-mapping-1"]
    assert entry["method"] == orch._TRYIT_CANDIDATE_METHOD
    assert entry["from_field"] == "orders.op_ref"
    # Tier 2 by construction — both with and without the internal _tier key.
    assert orch._provenance_tier({"method": orch._TRYIT_CANDIDATE_METHOD}) == 2
    # The internal trim key never reaches the serialized seed.
    assert "_tier" not in json.dumps(_payload(seed))
    # The live accepted rule is untouched alongside it.
    assert "jp-live-1" in joins


def test_candidate_ranks_below_steward_and_accepted_rows():
    rows = [{
        "system_id": "sys-a",
        "join_paths": [
            {"id": "jp-steward", "from_system": "sys-a", "to_system": "sys-b",
             "confidence": 0.1, "method": "user_confirmed"},
            {"id": "jp-accepted", "from_system": "sys-a", "to_system": "sys-b",
             "confidence": 0.2, "provenance": "accepted"},
        ],
    }]
    seed = orch.build_route_seed(query=_fake_query(rows), candidate=dict(_CANDIDATE))

    joins = next(s for s in _payload(seed)["systems"] if s["id"] == "sys-a")[
        "rules"]["join_paths"]
    ids = [j["id"] for j in joins]
    # Candidate confidence (0.7) beats both rows, but tier ordering wins:
    # steward first, accepted second, candidate LAST.
    assert ids == ["jp-steward", "jp-accepted", "cand-mapping-1"]


def test_candidate_never_displaces_steward_rows_at_cap():
    steward_rows = [{"id": f"jp-user-{i}", "from_system": "sys-a",
                     "to_system": "sys-b", "confidence": 0.5,
                     "method": "user_confirmed"}
                    for i in range(orch._ROUTE_SEED_RULE_CAP)]
    rows = [{"system_id": "sys-a", "join_paths": steward_rows}]

    seed = orch.build_route_seed(query=_fake_query(rows), candidate=dict(_CANDIDATE))

    joins = next(s for s in _payload(seed)["systems"] if s["id"] == "sys-a")[
        "rules"]["join_paths"]
    ids = {j["id"] for j in joins}
    assert "cand-mapping-1" not in ids               # dropped, not displaced
    assert len(ids) == orch._ROUTE_SEED_RULE_CAP     # every steward row survives
    assert all(rule_id.startswith("jp-user-") for rule_id in ids)


def test_no_candidate_key_byte_identical_seed():
    baseline = orch.build_route_seed(query=_fake_query())
    with_none = orch.build_route_seed(query=_fake_query(), candidate=None)

    assert baseline == with_none


def test_candidate_never_written_to_graph():
    executed: list[str] = []

    def recording_query(cypher: str):
        executed.append(cypher)
        return _fake_query()(cypher)

    orch.build_route_seed(query=recording_query, candidate=dict(_CANDIDATE))

    # T1.6 added the read-only authority query, and the removal-marker
    # re-check follows the seed's reads; the write-verb sweep below is
    # the actual never-persisted pin — every executed query stays MATCH-only.
    assert executed == [orch._ROUTE_SEED_INVENTORY_CYPHER, orch._ROUTE_SEED_CYPHER,
                        orch._ROUTE_SEED_AUTHORITY_CYPHER, REMOVING_SYSTEMS_READ]
    for cypher in executed:
        assert "cand-mapping-1" not in cypher
        for write_verb in ("CREATE ", "MERGE ", "SET ", "DELETE ", "DETACH "):
            assert write_verb not in cypher


def test_identity_rule_candidate_maps_from_to_onto_source_target():
    candidate = {
        "proposed_type": "identity_rule",
        "from_system": "sys-a", "from_field": "orders.op_ref",
        "to_system": "sys-b", "to_field": "events.op_id",
        "concept": "operation.identity", "confidence": "0.8",
        "mapping_id": "cand-ir-1",
    }
    seed = orch.build_route_seed(query=_fake_query(), candidate=candidate)

    rules = next(s for s in _payload(seed)["systems"] if s["id"] == "sys-a")[
        "rules"]["identity_rules"]
    entry = next(r for r in rules if r.get("id") == "cand-ir-1")
    assert entry["source_system"] == "sys-a"
    assert entry["source_field"] == "orders.op_ref"
    assert entry["target_system"] == "sys-b"
    assert entry["target_field"] == "events.op_id"
    assert entry["method"] == orch._TRYIT_CANDIDATE_METHOD


def test_unshapeable_candidates_leave_seed_unchanged():
    baseline = orch.build_route_seed(query=_fake_query())

    for candidate in (
        {"proposed_type": "correlation_insight", "from_system": "sys-a"},
        {**_CANDIDATE, "from_system": "sys-zz", "to_system": "sys-zz"},
        {"proposed_type": "join_path"},
        "not-a-dict",
    ):
        assert orch.build_route_seed(query=_fake_query(), candidate=candidate) == baseline


def test_row_level_values_never_reach_the_seed():
    candidate = {**_CANDIDATE, "from_value": "must-not-leak",
                 "to_value": "must-not-leak"}

    seed = orch.build_route_seed(query=_fake_query(), candidate=candidate)

    assert "must-not-leak" not in seed


_VALUE_CANDIDATE = {
    "proposed_type": "value_mapping",
    "from_system": "sys-a", "from_field": "records.cell",
    "to_system": "sys-b", "to_field": "hierarchy.line_name",
    "concept": "production-line", "confidence": "1.0",
    "from_value": "CELL-7", "to_value": "CELL-7",
    "mapping_id": "cand-mapping-2",
}


def _value_mappings(seed: str) -> list:
    return [entry for system in _payload(seed)["systems"]
            for entry in (system.get("rules") or {}).get("value_mappings") or []]


def test_a_value_mapping_candidate_is_served_with_its_one_value_pair():
    seed = orch.build_route_seed(query=_fake_query(), candidate=dict(_VALUE_CANDIDATE))

    [entry] = [e for e in _value_mappings(seed) if e.get("method") == "tryit-candidate"]
    assert entry["value_pairs"] == [["CELL-7", "CELL-7"]]
    assert entry["from_field"] == "records.cell" and entry["to_field"] == "hierarchy.line_name"
    # The decision (ADR-0035 amendment 2026-10-18): the Try-it candidate keeps its
    # one value pair (it is the thing under test), while a served traversal
    # value-mapping entry carries only its value_pair_count. So every NON-
    # candidate entry carries no pairs, and the candidate carries exactly one.
    for e in _value_mappings(seed):
        if e.get("method") == "tryit-candidate":
            assert e.get("value_pairs")
        else:
            assert "value_pairs" not in e


def test_a_value_mapping_candidate_without_both_values_is_not_injected():
    baseline = orch.build_route_seed(query=_fake_query())
    for missing in ("from_value", "to_value"):
        candidate = {k: v for k, v in _VALUE_CANDIDATE.items() if k != missing}
        assert orch.build_route_seed(query=_fake_query(), candidate=candidate) == baseline
    blank = {**_VALUE_CANDIDATE, "to_value": None}
    assert orch.build_route_seed(query=_fake_query(), candidate=blank) == baseline


def test_a_value_mapping_candidates_values_are_bounded_like_served_pairs():
    long_value = "V" * (orch._ROUTE_SEED_VALUE_FIELD_CAP + 50)
    candidate = {**_VALUE_CANDIDATE, "from_value": long_value}

    seed = orch.build_route_seed(query=_fake_query(), candidate=candidate)

    [entry] = [e for e in _value_mappings(seed) if e.get("method") == "tryit-candidate"]
    assert long_value not in seed
    assert len(entry["value_pairs"][0][0]) <= orch._ROUTE_SEED_VALUE_FIELD_CAP + len(
        orch._ROUTE_SEED_ID_TRUNCATION_MARKER)


def _query_with_value_mappings(value_mappings):
    def query(cypher: str, *_a, **_k):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return [{"system_id": "sys-a", "system_name": "System A"},
                    {"system_id": "sys-b", "system_name": "System B"}]
        if cypher == orch._ROUTE_SEED_CYPHER:
            return [{"system_id": "sys-a", "shared_concepts": [], "join_paths": [],
                     "identity_rules": [], "value_mappings": value_mappings}]
        return []
    return query


def _persisted_mappings(width: int) -> list:
    return [{"from_system": "sys-a", "from_field": f"records.code_{i}", "to_system": "sys-b",
             "to_field": f"hierarchy.name_{i}", "concept": "line", "method": "user_confirmed",
             "confidence": 1.0 - i / 10,
             "value_pairs": [{"from": f"V{i}{j}" + "x" * width, "to": f"v{i}{j}" + "y" * width}
                             for j in range(3)],
             "value_pair_count": 3} for i in range(4)]


def _without_candidate(seed: str):
    """The seed's (header, payload, trailing text), the Try-it candidate left out."""
    for header in (orch._ROUTE_SEED_HEADER, orch._ROUTE_SEED_PARTIAL_HEADER):
        if seed.startswith(header):
            payload, end = json.JSONDecoder().raw_decode(seed, len(header))
            for system in payload.get("systems") or []:
                for kind, items in (system.get("rules") or {}).items():
                    if isinstance(items, list):
                        items[:] = [i for i in items if not (
                            isinstance(i, dict) and i.get("method") == "tryit-candidate")]
            return header, payload, seed[end:]
    raise AssertionError("route seed has no recognized header")


def test_a_candidate_that_would_evict_a_baseline_mapping_is_not_fitted(monkeypatch):
    # Room for the candidate exists only by dropping a steward-confirmed
    # mapping the baseline run keeps: the candidate run then uses the
    # baseline seed itself, so both runs share the same routing context and
    # the run honestly reports the candidate as not in it.
    import agents.runtime_app as runtime_app

    query = _query_with_value_mappings(_persisted_mappings(20))
    baseline_full = orch.build_route_seed(query=query)
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", len(baseline_full) + 10)
    baseline = orch.build_route_seed(query=query)
    assert len(_value_mappings(baseline)) == 4

    seed = orch.build_route_seed(query=query, candidate=dict(_VALUE_CANDIDATE))

    assert seed == baseline
    assert "tryit_candidate" not in runtime_app._planned_with_from_seed(seed)


# NOTE (ADR-0035 amendment 2026-10-18): the former
# test_a_candidate_fitted_beside_an_already_trimmed_baseline_changes_nothing_else
# was removed. It exercised the one narrow budget where trimming a single
# traversal value-mapping entry freed exactly enough room for the Try-it
# candidate, so the candidate run held the candidate AND still equalled the
# trimmed baseline. Under the decision a served traversal entry carries only
# value_pair_count (no values), while the Try-it candidate KEEPS its one value
# pair (decision point 3), so the candidate entry (212 chars here) is larger
# than a traversal entry (184). Any budget that forces a baseline trim makes
# the candidate run trim differently, so _seed_with_candidate returns the
# baseline (candidate dropped) -- the scenario is unreachable. The invariant it
# shared ("a held candidate never changes the rest of the seed") is fully
# covered by test_a_held_candidate_never_changes_the_rest_of_the_seed_at_any_budget.


def test_a_held_candidate_never_changes_the_rest_of_the_seed_at_any_budget(monkeypatch):
    # Paired-seed invariant across budgets: whenever the candidate run's seed
    # holds the candidate, the rest of it is exactly the baseline seed;
    # otherwise it is the baseline seed.
    query = _query_with_value_mappings(_persisted_mappings(40))
    full = orch.build_route_seed(query=query, candidate=dict(_VALUE_CANDIDATE))
    held = 0
    for budget in range(len(full) - 1600, len(full) + 50, 37):
        monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", budget)
        baseline = orch.build_route_seed(query=query)
        seed = orch.build_route_seed(query=query, candidate=dict(_VALUE_CANDIDATE))
        if seed is None or baseline is None:
            # Budget below the header length: no seed can be built at all. Both
            # runs return None together; the paired invariant does not apply.
            assert seed is None and baseline is None, budget
            continue
        if "tryit-candidate" in seed:
            held += 1
            assert _without_candidate(seed) == _without_candidate(baseline), budget
        else:
            assert seed == baseline, budget
    assert held > 0


def test_the_routing_summary_says_whether_the_candidate_is_in_the_seed():
    import agents.runtime_app as runtime_app

    with_candidate = orch.build_route_seed(query=_fake_query(), candidate=dict(_CANDIDATE))
    assert runtime_app._planned_with_from_seed(with_candidate)["tryit_candidate"] is True
    # Not in the seed (ordinary turn, or a candidate that could not be shaped):
    # the key is absent, so every ordinary summary is unchanged.
    for seed in (orch.build_route_seed(query=_fake_query()),
                 orch.build_route_seed(query=_fake_query(),
                                       candidate={k: v for k, v in _VALUE_CANDIDATE.items()
                                                  if k != "to_value"})):
        assert "tryit_candidate" not in runtime_app._planned_with_from_seed(seed)


def test_the_capped_summary_keeps_only_a_true_candidate_flag():
    from agents import query_intelligence as qi

    base = {"systems": 2, "shared_concepts": [], "join_paths": 1}
    assert qi._capped_planned_with({**base, "tryit_candidate": True})["tryit_candidate"] is True
    for value in (False, 1, "true", None):
        assert "tryit_candidate" not in qi._capped_planned_with({**base, "tryit_candidate": value})
    assert "tryit_candidate" not in qi._capped_planned_with(base)


# --- Regression tests: candidate survival with a question-scoped block ------
# A question-scoped IDENTIFIER VALUE MAPPINGS block swaps the header for a
# mapping-aware one; _seed_parts must still parse it so a fitting Try-it
# candidate is retained (not silently dropped). And with such a candidate
# present, the header's value-absence claim must be true (it is about STORED
# values; the candidate, never persisted, may show its one pair).
import agents.concept_notes as _cn
import agents.identifier_translation as _it

_R1_NOTE = _cn.Concept(
    id="production_line", name="production line",
    description="A production line is a line of equipment; ask for it by its line name.",
    systems=("sys-a",))

#: A trusted stored ValueMapping (deterministic id prefix + method) the
#: question-scoped read returns when the question names its value CELL-7.
_R1_STORED_VM = {"id": "vm-equip-7", "from_system": "sys-a", "from_field": "records.cell",
                 "from_value": "CELL-7", "to_system": "sys-b", "to_field": "hierarchy.line_name",
                 "to_value": "CELL-7", "concept": "production-line", "method": "value_format_match",
                 "confidence": 1.0, "provenance": None}


def _r1_combined_query(question_value_rows, rule_value_mappings=()):
    """Answers every read build_route_seed makes so a question-scoped block can
    appear: inventory, the rule cypher, the question-scoped value-mapping read,
    and the removing read (none)."""
    def query(cypher, params=None):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return [{"system_id": "sys-a", "system_name": "System A"},
                    {"system_id": "sys-b", "system_name": "System B"}]
        if cypher == orch._ROUTE_SEED_CYPHER:
            return [{"system_id": "sys-a", "shared_concepts": [], "join_paths": [],
                     "identity_rules": [], "value_mappings": list(rule_value_mappings)}]
        if cypher == orch._ROUTE_SEED_AUTHORITY_CYPHER:
            return []
        if cypher == REMOVING_SYSTEMS_READ:
            return []
        if "\nMATCH (vm:ValueMapping)\nWHERE " in cypher:
            return question_value_rows
        return []
    return query


def _r1_header_of(seed: str) -> str:
    for header in (*orch._ROUTE_SEED_VALUE_MAPPING_HEADERS.values(),
                   orch._ROUTE_SEED_HEADER, orch._ROUTE_SEED_PARTIAL_HEADER):
        if seed.startswith(header):
            return header
    raise AssertionError("route seed has no recognized header")


def _r1_payload(seed: str) -> dict:
    body = seed[len(_r1_header_of(seed)):]
    obj, _end = json.JSONDecoder().raw_decode(body)
    return obj


def _r1_value_mappings(seed: str) -> list:
    return [e for s in _r1_payload(seed)["systems"]
            for e in (s.get("rules") or {}).get("value_mappings") or []]


def test_a_fitting_candidate_survives_a_question_scoped_mapping_block():
    """A stored CELL-7 mapping (so the header is mapping-aware) plus
    a fitting CELL-7 Try-it candidate. The candidate is retained, and apart
    from it the block and every baseline byte are identical."""
    q = _r1_combined_query([_R1_STORED_VM])
    question = "Where does production line CELL-7 run?"
    baseline = orch.build_route_seed(query=q, question=question, concepts=(_R1_NOTE,))
    seed = orch.build_route_seed(query=q, question=question, candidate=dict(_VALUE_CANDIDATE),
                                 concepts=(_R1_NOTE,))
    assert _it.carries_value_mappings(baseline)  # the block is present
    held = [e for e in _r1_value_mappings(seed) if e.get("method") == "tryit-candidate"]
    assert held, "the Try-it candidate was dropped despite a question-scoped block"
    assert held[0]["value_pairs"] == [["CELL-7", "CELL-7"]]
    # Apart from the candidate, the seed equals the baseline (same block, same bytes).
    seed_no_cand = [e for e in _r1_value_mappings(seed) if e.get("method") != "tryit-candidate"]
    assert seed_no_cand == _r1_value_mappings(baseline)
    assert seed[seed.index(_it.VALUE_MAPPINGS_HEADER):] == (
        baseline[baseline.index(_it.VALUE_MAPPINGS_HEADER):])


def test_the_header_value_absence_claim_is_true_with_a_candidate():
    """With a Try-it value-mapping candidate the systems payload
    carries the candidate's one pair, so the header's absence claim must be
    about STORED values only. Pin plain-plus-candidate and block-plus-candidate."""
    # Plain header + candidate (no question-scoped block).
    plain = orch.build_route_seed(query=_fake_query(), candidate=dict(_VALUE_CANDIDATE))
    header = _r1_header_of(plain)
    assert "CELL-7" in plain  # the candidate's pair is served
    assert "No mapped value is listed." not in header  # the bare absence claim is gone
    assert "stored" in header.lower()  # the claim is qualified to stored values

    # Mapping-aware header + candidate (question-scoped block present).
    q = _r1_combined_query([_R1_STORED_VM])
    seed = orch.build_route_seed(query=q, question="Where does production line CELL-7 run?",
                                 candidate=dict(_VALUE_CANDIDATE), concepts=(_R1_NOTE,))
    block_header = _r1_header_of(seed)
    assert block_header in orch._ROUTE_SEED_VALUE_MAPPING_HEADERS.values()
    assert "no other mapped value is" not in block_header  # the absolute claim is gone
    assert "stored" in block_header.lower()
