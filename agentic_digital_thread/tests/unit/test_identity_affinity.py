# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Identifier-core affinity: pattern IdentityRules discovered from sampled values.

The case that motivated it (test event 15): a plant MES keys runs by
``batch_number`` (concept ``batch``, values ``B-2847``) and another system keys
orders by ``lot_ref`` (concept ``lot-number``, values ``LOT-2026-02847``).
Different concept names, different formats — the LLM resolver never paired
them, so the rule had to be pre-taught. The values alone are enough evidence.
"""
import pytest

from agents import identity_affinity as aff
from agents.identity_affinity import SampledField


def _mes(values):
    return SampledField("mes-ind", "production_runs", "batch_number", "batch", tuple(values))


def _erp(values):
    return SampledField("sys-b", "orders", "lot_ref", "lot-number", tuple(values))


def test_digit_cores_strip_leading_zeros_and_short_runs():
    assert aff.digit_cores("LOT-2026-02847") == {"2026", "2847"}
    assert aff.digit_cores("B-2847") == {"2847"}
    assert aff.digit_cores("L3-M1") == frozenset()  # no run of three digits
    assert aff.digit_cores("000060009921") == {"60009921"}


def test_batch_and_lot_values_yield_the_pattern_rule_without_a_shared_concept():
    mes = _mes([f"B-{n}" for n in range(2800, 2951)])          # 151 batches
    erp = _erp([f"LOT-2026-0{n}" for n in range(2847, 2897)])  # 50 lots

    rule = aff.affinity_rule(mes, erp)

    assert rule is not None
    assert rule["concept"] == "batch"
    assert rule["source_field"] == "production_runs.batch_number"
    assert rule["source_pattern"] == "B-{id}"
    assert rule["target_system"] == "sys-b"
    assert rule["target_field"] == "orders.lot_ref"
    assert rule["target_pattern"] == "LOT-2026-0{id}"
    assert rule["method"] == aff.AFFINITY_METHOD
    assert rule["confidence"] == aff.AFFINITY_CONFIDENCE
    assert "50 of 50 sampled value pairs" in rule["reasoning"]
    assert "B-2847 ↔ LOT-2026-02847" in rule["reasoning"]


def test_the_rule_is_directional_and_works_from_the_erp_side_too():
    """The patterns follow the direction; the concept does not (ADR-0087
    amendment 2026-10-18): ``batch`` and ``lot-number`` share nothing and are
    not catalog-related, so the lexically first names the rule from either side."""
    mes = _mes([f"B-{n}" for n in range(2840, 2900)])
    erp = _erp([f"LOT-2026-0{n}" for n in range(2847, 2897)])

    rule = aff.affinity_rule(erp, mes)

    assert rule["source_pattern"] == "LOT-2026-0{id}"
    assert rule["target_pattern"] == "B-{id}"
    assert rule["concept"] == "batch"
    assert aff.affinity_rule(mes, erp)["concept"] == "batch"


def _pattern_meaning(source_system, rule):
    """A pattern rule's direction-free meaning: each endpoint with its pattern,
    the concept and the method."""
    return (frozenset({((source_system, rule["source_field"]), rule["source_pattern"]),
                       ((rule["target_system"], rule["target_field"]), rule["target_pattern"])}),
            rule["concept"], rule["method"])


@pytest.mark.parametrize("left_concept,right_concept,named", [
    ("production-run", "batch", "batch"),     # catalog-related: the declaring concept
    ("batch", "production-run", "batch"),
    ("alpha", "beta", "alpha"),               # unrelated: lexically first
    ("beta", "alpha", "alpha")])
def test_a_pattern_rule_names_the_same_concept_from_either_side(left_concept, right_concept, named):
    runs = SampledField("sys-a", "production_runs", "run_number", left_concept,
                        tuple(f"RUN-2026-{n:05d}" for n in range(12001, 12041)))
    batches = SampledField("sys-b", "shipment_items", "batch_id", right_concept,
                           tuple(f"BATCH-{n:05d}" for n in range(12001, 12041)))
    forward, reverse = aff.affinity_rule(runs, batches), aff.affinity_rule(batches, runs)
    assert forward["method"] == reverse["method"] == aff.AFFINITY_METHOD
    assert _pattern_meaning("sys-a", forward) == _pattern_meaning("sys-b", reverse)
    assert forward["concept"] == named


def test_a_shared_concept_names_the_pattern_rule():
    """Both fields also map ``lot-number``: the shared concept wins over each
    side's own primary concept, from either side."""
    import dataclasses
    mes = dataclasses.replace(_mes([f"B-{n}" for n in range(2840, 2900)]),
                              concepts=frozenset({"batch", "lot-number"}))
    erp = _erp([f"LOT-2026-0{n}" for n in range(2847, 2897)])
    assert aff.affinity_rule(mes, erp)["concept"] == aff.affinity_rule(erp, mes)["concept"] == "lot-number"


def test_a_year_shared_with_dates_is_rejected_by_pattern_inconsistency():
    dates = SampledField("mes-ind", "production_runs", "start_time", "timestamp",
                         tuple(f"2026-02-{d:02d} 06:00" for d in range(1, 29)))
    erp = _erp([f"LOT-2026-0{n}" for n in range(2847, 2897)])

    # Every date shares the core 2026 with every lot, but the inferred
    # transform differs per pair — no single pattern explains the matches.
    assert aff.affinity_rule(dates, erp) is None


def test_identical_formats_are_not_a_pattern_rule():
    left = SampledField("erp", "orders", "order_id", "order",
                        tuple(f"0000600099{n:02d}" for n in range(10, 40)))
    right = SampledField("sys-b", "orders", "order_number", "order",
                         tuple(f"0000600099{n:02d}" for n in range(10, 40)))

    assert aff.affinity_rule(left, right) is None  # a JoinPath, not a transform


def test_too_few_matches_or_same_system_yield_nothing():
    mes = _mes(["B-2847", "B-2848", "B-9000", "B-9001", "B-9002", "B-9003",
                "B-9004", "B-9005", "B-9006", "B-9007", "B-9008", "B-9009"])
    erp = _erp(["LOT-2026-02847", "LOT-2026-02848", "LOT-2026-05000", "LOT-2026-05001",
                "LOT-2026-05002", "LOT-2026-05003", "LOT-2026-05004", "LOT-2026-05005",
                "LOT-2026-05006", "LOT-2026-05007", "LOT-2026-05008", "LOT-2026-05009"])
    assert aff.affinity_rule(mes, erp) is None  # 2 pairs < MIN_MATCHED_PAIRS

    same_system = SampledField("mes-ind", "quality_checks", "batch_number", "batch",
                               tuple(f"B-{n}" for n in range(2800, 2900)))
    assert aff.affinity_rule(_mes([f"B-{n}" for n in range(2800, 2900)]), same_system) is None


def test_find_and_merge_keep_llm_rules_first_and_dedupe_by_field_pair():
    mes = _mes([f"B-{n}" for n in range(2800, 2951)])
    erp = _erp([f"LOT-2026-0{n}" for n in range(2847, 2897)])
    other = SampledField("erp", "orders", "order_id", "order", ("A", "B", "C"))
    found = aff.find_affinity_rules([mes], [erp, other])
    assert [r["target_system"] for r in found] == ["sys-b"]

    llm_rule = {"concept": "batch", "source_field": "production_runs.batch_number",
                "target_system": "sys-b", "target_field": "orders.lot_ref",
                "source_pattern": "B-{id}", "target_pattern": "LOT-2026-0{id}",
                "confidence": 0.7}
    merged = aff.merge_affinity_rules([llm_rule], found)
    assert merged == [llm_rule]  # the LLM's own proposal for the pair wins

    merged = aff.merge_affinity_rules([], found)
    assert merged == found


def test_a_serial_key_matched_by_a_handful_of_codes_is_chance_not_identity():
    """Event 16: six operator badges OP-0101..OP-0106 matched six of 133 spec
    serial ids under one transform (OP-0{id} ↔ {id}) and came out at 0.85 —
    the one wrong rule with the highest confidence. A bare-integer side must
    itself be substantially covered."""
    badges = SampledField("mes-ind", "production_runs", "operator_badge", "operator",
                          tuple(f"OP-0{n}" for n in range(101, 107)))
    specs = SampledField("plm", "specs", "spec_id", "specification",
                         tuple(str(n) for n in range(1, 134)))
    assert aff.affinity_rule(badges, specs) is None
    assert aff.affinity_rule(specs, badges) is None

    # A bare-integer side that IS covered (product codes ↔ part numbers) still passes.
    codes = SampledField("mes-ind", "production_runs", "product_code", "product",
                         ("7742", "7743", "7744"))
    parts = SampledField("plm", "items", "item_id", "part",
                         tuple(f"PN-{n}-REV-A" for n in range(7742, 7771)))
    rule = aff.affinity_rule(codes, parts)
    assert rule is not None and rule["source_pattern"] == "{id}"


def test_padded_small_sequences_never_pair():
    """Event 16: ten PM schedules PM-1001-0001..0010 paired with supplier codes
    SUPP0001..SUPP0010 (and customers CUST0001..) on the padded core '0007'.
    Leading zeros are padding; 7 is not an identifier."""
    assert aff.digit_cores("PM-1001-0007") == {"1001"}
    assert aff.digit_cores("SUPP0007") == frozenset()
    pms = SampledField("cmms", "pm_schedules", "pm_num", "maintenance-schedule",
                       tuple(f"PM-1001-{n:04d}" for n in range(1, 11)))
    suppliers = SampledField("erp", "suppliers", "supplier_code", "supplier",
                             tuple(f"SUPP{n:04d}" for n in range(1, 11)))
    assert aff.affinity_rule(pms, suppliers) is None
    assert aff.affinity_rule(suppliers, pms) is None


# --- Characterization (pinned at HEAD before the one-to-many change) -------
# The one-to-one path must stay byte-identical: dict content, key order and
# reasoning text.

_FORWARD_AT_HEAD = [
    ("concept", "batch"),
    ("source_field", "production_runs.batch_number"), ("source_table", "production_runs"),
    ("source_pattern", "B-{id}"),
    ("target_system", "sys-b"),
    ("target_field", "orders.lot_ref"), ("target_table", "orders"),
    ("target_pattern", "LOT-2026-0{id}"),
    ("confidence", 0.85), ("method", "value_core_match"),
    ("reasoning", "50 of 50 sampled value pairs share an identifier core under one transform "
                  "(B-2847 ↔ LOT-2026-02847); 151 and 50 distinct values sampled"),
]

#: The reverse rule names the same concept as the forward one (``batch`` and
#: ``lot-number`` are unrelated, so the lexically first): the concept no longer
#: follows the resolving side. Everything else is byte-identical.
_REVERSE_AT_HEAD = [
    ("concept", "batch"),
    ("source_field", "orders.lot_ref"), ("source_table", "orders"),
    ("source_pattern", "LOT-2026-0{id}"),
    ("target_system", "mes-ind"),
    ("target_field", "production_runs.batch_number"), ("target_table", "production_runs"),
    ("target_pattern", "B-{id}"),
    ("confidence", 0.85), ("method", "value_core_match"),
    ("reasoning", "50 of 50 sampled value pairs share an identifier core under one transform "
                  "(LOT-2026-02847 ↔ B-2847); 50 and 60 distinct values sampled"),
]


def test_characterization_one_to_one_rule_is_byte_identical():
    mes = _mes([f"B-{n}" for n in range(2800, 2951)])
    erp = _erp([f"LOT-2026-0{n}" for n in range(2847, 2897)])
    assert list(aff.affinity_rule(mes, erp).items()) == _FORWARD_AT_HEAD


def test_characterization_reverse_one_to_one_rule_is_byte_identical():
    mes = _mes([f"B-{n}" for n in range(2840, 2900)])
    erp = _erp([f"LOT-2026-0{n}" for n in range(2847, 2897)])
    assert list(aff.affinity_rule(erp, mes).items()) == _REVERSE_AT_HEAD


# --- One-to-many identifier cores (ADR-0086) --------------------------------
# One source code maps to SEVERAL target records sharing its core (revisions
# A/B/C of one item). Keeping one value per core turned the varying tail into
# a constant that depended on the sample's row order.

import random  # noqa: E402


from agents.identifier_translation import WILDCARD  # noqa: E402

_ONE_TO_MANY_TARGETS = ("PRT-5101-R-A", "PRT-5101-R-B", "PRT-5101-R-C",
                        "PRT-5202-R-A", "PRT-5202-R-B", "PRT-5303-R-A",
                        *[f"PRT-{n}-R-A" for n in range(6001, 6024)])


def _codes(values=("5101", "5202", "5303")):
    return SampledField("sys-a", "runs", "product_code", "product", tuple(values))


def _items(values=_ONE_TO_MANY_TARGETS):
    return SampledField("sys-b", "items", "item_id", "part", tuple(values))


_WILDCARD_PREFIX = ("3 of 3 sampled identifier cores share one transform; "
                    "2 of 3 cores match several target values (up to 3)")


def test_one_to_many_target_values_yield_a_wildcard_slot():
    assert len(_ONE_TO_MANY_TARGETS) == 29
    rule = aff.affinity_rule(_codes(), _items())

    assert rule is not None
    assert rule["source_pattern"] == "{id}"
    assert rule["target_pattern"] == "PRT-{id}-R-" + WILDCARD
    assert rule["method"] == aff.AFFINITY_METHOD
    assert rule["confidence"] == aff.AFFINITY_CONFIDENCE
    assert rule["target_system"] == "sys-b" and rule["target_field"] == "items.item_id"
    assert rule["reasoning"].startswith(_WILDCARD_PREFIX)
    assert "5101 ↔ PRT-5101-R-A, PRT-5101-R-B, PRT-5101-R-C" in rule["reasoning"]
    assert rule["reasoning"].endswith("3 and 29 distinct values sampled")
    assert len(rule["reasoning"]) <= 300  # origin_context is truncated at 300


def test_one_to_many_rule_is_the_exact_frozen_reasoning():
    rule = aff.affinity_rule(_codes(), _items())
    assert rule["reasoning"] == (
        "3 of 3 sampled identifier cores share one transform; 2 of 3 cores match several "
        "target values (up to 3), so the part that varies is a {*} wildcard "
        "(e.g. 5101 ↔ PRT-5101-R-A, PRT-5101-R-B, PRT-5101-R-C); "
        "3 and 29 distinct values sampled")


@pytest.mark.parametrize("seed", range(10))
def test_one_to_many_rule_is_independent_of_sample_row_order(seed):
    baseline = aff.affinity_rule(_codes(), _items())
    shuffled = list(_ONE_TO_MANY_TARGETS)
    random.Random(seed).shuffle(shuffled)
    codes = ["5101", "5202", "5303"]
    random.Random(seed + 100).shuffle(codes)
    assert aff.affinity_rule(_codes(codes), _items(shuffled)) == baseline


def test_a_row_order_the_legacy_pass_rejected_still_yields_the_wildcard_rule():
    # First value per core disagrees (A, B, A): representatives alone agree 2 of 3.
    ordered = ("PRT-5101-R-A", "PRT-5202-R-B", "PRT-5303-R-A", "PRT-5101-R-B",
               "PRT-5101-R-C", "PRT-5202-R-A", *[f"PRT-{n}-R-A" for n in range(6001, 6024)])
    legacy = aff.dominant_pattern(aff.matched_pairs(_codes(), _items(ordered)))
    assert legacy[2] == 2  # the representative-only vote would have failed at 0.67

    rule = aff.affinity_rule(_codes(), _items(ordered))
    assert rule is not None and rule["target_pattern"] == "PRT-{id}-R-{*}"


def test_the_wildcard_rule_is_directional_and_says_source_values_in_reverse():
    rule = aff.affinity_rule(_items(), _codes())

    assert rule["source_pattern"] == "PRT-{id}-R-{*}"
    assert rule["target_pattern"] == "{id}"
    assert rule["concept"] == "part" and rule["target_system"] == "sys-a"
    assert "several source values (up to 3)" in rule["reasoning"]
    assert "PRT-5101-R-A, PRT-5101-R-B, PRT-5101-R-C ↔ 5101" in rule["reasoning"]
    assert rule["reasoning"].endswith("29 and 3 distinct values sampled")


def test_a_wildcard_adjacent_to_the_core_is_ambiguous_and_never_emitted():
    # {*} glued to the core is ambiguous: no wildcard rule, so the one-to-one
    # pass runs exactly as before this change (its pre-change output).
    for values in (("PRT-5101A", "PRT-5101B", "PRT-5202A", "PRT-5303A"),
                   ("PRT-5101A", "PRT-5101B", "PRT-5202A", "PRT-5202B", "PRT-5303A")):
        rule = aff.affinity_rule(_codes(), _items(values))
        assert rule["target_pattern"] == "PRT-{id}A"
        assert rule["reasoning"].startswith("3 of 3 sampled value pairs share an identifier core")


def test_a_wildcard_family_below_the_agreement_threshold_yields_nothing():
    # The winning shape explains 3 of 5 matched cores (0.6 < MIN_PATTERN_AGREEMENT).
    codes = _codes(("5101", "5202", "5303", "5404", "5505"))
    items = _items(("PRT-5101-R-A", "PRT-5101-R-B", "PRT-5202-R-A", "PRT-5303-R-A",
                    "DOC-5404", "DOC-5505"))
    assert aff.affinity_rule(codes, items) is None


def test_a_sibling_that_does_not_fit_the_winning_transform_is_ignored():
    rule = aff.affinity_rule(_codes(), _items(_ONE_TO_MANY_TARGETS + ("DOC-5101",)))
    assert rule["target_pattern"] == "PRT-{id}-R-{*}"
    assert "DOC-5101" not in rule["reasoning"]
    assert rule["reasoning"].endswith("3 and 30 distinct values sampled")


def test_matched_groups_keep_every_value_per_core_sorted_and_deduped():
    groups = aff.matched_groups(_codes(), _items(tuple(reversed(_ONE_TO_MANY_TARGETS)) + ("PRT-5101-R-B",)))
    assert groups == [
        ("5101", ("5101",), ("PRT-5101-R-A", "PRT-5101-R-B", "PRT-5101-R-C")),
        ("5202", ("5202",), ("PRT-5202-R-A", "PRT-5202-R-B")),
        ("5303", ("5303",), ("PRT-5303-R-A",)),
    ]


def test_matched_groups_are_bounded_per_core():
    many = tuple(f"PRT-5101-R-{c}" for c in "ABCDEFGHIJKL")
    groups = aff.matched_groups(_codes(("5101",)), _items(many))
    assert len(groups[0][2]) == aff.MAX_VALUES_PER_CORE
    assert groups[0][2] == tuple(sorted(many))[:aff.MAX_VALUES_PER_CORE]


@pytest.mark.parametrize("patterns, expected", [
    (["PRT-{id}-R-A", "PRT-{id}-R-B"], "PRT-{id}-R-{*}"),
    (["PRT-{id}-R-A", "PRT-{id}-R-A"], "PRT-{id}-R-A"),
    (["{id}", "{id}"], "{id}"),
    (["A-{id}-X", "B-{id}-X"], "{*}-{id}-X"),
    (["PRT-{id}A", "PRT-{id}B"], None),        # {*} glued to the core: ambiguous
    (["A1-{id}", "B2-{id}"], None),            # no alphanumeric literal left
    (["A-{id}", "B-{id}-C"], None),            # different skeletons never merge
    (["A-{id}", "A-{id"], None),               # an unparseable pattern never merges
    (["A-{id"], None),
    ([], None),
])
def test_merge_patterns(patterns, expected):
    assert aff.merge_patterns(patterns) == expected


def test_a_one_to_one_sample_still_takes_the_unchanged_path():
    codes = SampledField("sys-a", "runs", "product_code", "product", ("7742", "7743", "7744"))
    parts = SampledField("sys-b", "items", "item_id", "part",
                         tuple(f"PRT-{n}-R-A" for n in range(7742, 7771)))
    rule = aff.affinity_rule(codes, parts)
    assert rule["target_pattern"] == "PRT-{id}-R-A"
    assert rule["reasoning"].startswith("3 of 3 sampled value pairs share an identifier core")


def _llm(target_pattern):
    return {"concept": "product", "source_field": "runs.product_code",
            "target_system": "sys-b", "target_field": "items.item_id",
            "source_pattern": "{id}", "target_pattern": target_pattern, "confidence": 0.7}


def test_a_wildcard_affinity_rule_replaces_the_llm_constant_in_place():
    wildcard = aff.affinity_rule(_codes(), _items())
    other = {"concept": "x", "source_field": "runs.other", "target_system": "sys-c",
             "target_field": "t.f", "source_pattern": "", "target_pattern": ""}
    merged = aff.merge_affinity_rules([other, _llm("PRT-{id}-R-A")], [wildcard])
    assert merged[0] == other
    replaced = merged[1]
    assert {k: v for k, v in replaced.items() if k != "reasoning"} == {
        k: v for k, v in wildcard.items() if k != "reasoning"}
    assert replaced["reasoning"].startswith(_WILDCARD_PREFIX)
    assert replaced["reasoning"].endswith("; replaced proposed constant PRT-{id}-R-A")
    assert len(replaced["reasoning"]) <= aff.REASONING_CAP
    assert "replaced" not in wildcard["reasoning"]  # the input rule is not mutated


def test_the_replacement_note_stays_inside_the_reasoning_cap():
    rule = {**_llm("PRT-{id}-R-{*}"), "reasoning": "x" * 400}
    merged = aff.merge_affinity_rules([_llm("PRT-{id}-R-A")], [rule])
    assert len(merged[0]["reasoning"]) == aff.REASONING_CAP
    assert merged[0]["reasoning"].startswith("x" * 200)
    assert merged[0]["reasoning"].endswith("; replaced proposed constant PRT-{id}-R-A")


def test_a_non_wildcard_affinity_rule_never_replaces_the_llm_rule():
    codes = SampledField("sys-a", "runs", "product_code", "product", ("7742", "7743", "7744"))
    parts = SampledField("sys-b", "items", "item_id", "part",
                         tuple(f"PRT-{n}-R-A" for n in range(7742, 7771)))
    constant = aff.affinity_rule(codes, parts)
    llm = _llm("PRT-{id}-R-B")
    assert aff.merge_affinity_rules([llm], [constant]) == [llm]


def test_a_wildcard_rule_replaces_every_duplicate_llm_rule_for_its_pair():
    wildcard = aff.affinity_rule(_codes(), _items())
    other = {"concept": "x", "source_field": "runs.other", "target_system": "sys-c",
             "target_field": "t.f", "source_pattern": "", "target_pattern": ""}
    llm = [_llm("PRT-{id}-R-A"), other, _llm("PRT-{id}-R-B"), _llm("PRT-{id}")]
    merged = aff.merge_affinity_rules(llm, [wildcard])
    assert len(merged) == 2
    assert merged[0]["target_pattern"] == "PRT-{id}-R-{*}"
    assert merged[0]["reasoning"].endswith("; replaced proposed constant PRT-{id}-R-A")
    assert merged[1] == other


def test_duplicate_llm_rules_survive_when_no_wildcard_replaces_them():
    llm = [_llm("PRT-{id}-R-A"), _llm("PRT-{id}-R-B")]
    assert aff.merge_affinity_rules(llm, []) == llm
