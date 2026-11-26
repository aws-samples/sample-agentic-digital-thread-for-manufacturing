# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Identifier-core affinity: the wildcard slot is decided WITHIN each core.

Per ADR-0086, a ``{*}`` slot is only the token positions that vary
among ONE core's fitting values; the per-core merged patterns must then agree
exactly across cores (a ``{*}`` agrees with any single-token literal at that
position). A token that differs only ACROSS cores is never a wildcard, at
least two agreeing cores must be one-to-many, and when the wildcard path
yields no rule the one-to-one pass runs exactly as before. Each fixture below
reproduces a case where one coincidental sibling rewrote or killed a
well-supported one-to-one rule; the expected dicts are the pre-change output.
"""
import pytest

from agents import identity_affinity as aff
from agents.identity_affinity import SampledField

_BATCHES = tuple(f"B-{n}" for n in range(2840, 2900))
_LOTS = tuple(f"LOT-2026-0{n}" for n in range(2847, 2897))


def _mes(values):
    return SampledField("sys-a", "production_runs", "batch_number", "batch", tuple(values))


def _erp(values):
    return SampledField("sys-b", "orders", "lot_ref", "lot-number", tuple(values))


def _codes(values=("5101", "5202", "5303")):
    return SampledField("sys-a", "runs", "product_code", "product", tuple(values))


def _items(values):
    return SampledField("sys-b", "items", "item_id", "part", tuple(values))


def _lot_rule(target_count):
    return {
        "concept": "batch",
        "source_field": "production_runs.batch_number", "source_table": "production_runs",
        "source_pattern": "B-{id}",
        "target_system": "sys-b",
        "target_field": "orders.lot_ref", "target_table": "orders",
        "target_pattern": "LOT-2026-0{id}",
        "confidence": 0.85, "method": "value_core_match",
        "reasoning": ("50 of 50 sampled value pairs share an identifier core under one "
                      "transform (B-2847 ↔ LOT-2026-02847); "
                      f"60 and {target_count} distinct values sampled"),
    }


def test_one_coincidental_sibling_never_rewrites_a_one_to_one_rule():
    # (i) One lot of another year shares a core: only ONE core is one-to-many.
    rule = aff.affinity_rule(_mes(_BATCHES), _erp(_LOTS + ("LOT-2025-02850",)))
    assert list(rule.items()) == list(_lot_rule(51).items())


def test_one_coincidental_ambiguous_sibling_never_kills_a_one_to_one_rule():
    # (ii) Plus a sibling whose merge would glue {*} to the core.
    rule = aff.affinity_rule(_mes(_BATCHES), _erp(_LOTS + ("LOT-2025-02850", "LOT-2026-A2850")))
    assert list(rule.items()) == list(_lot_rule(52).items())


def test_a_token_that_varies_only_across_cores_is_never_a_wildcard():
    # (iii) PRT/DOC/QQQ differ between cores; only one core has siblings.
    items = _items(("PRT-5101-R-A", "PRT-5101-R-B", "DOC-5202-R-A", "QQQ-5303-R-A"))
    assert aff.affinity_rule(_codes(), items) is None  # the pre-change result


def test_a_single_core_with_two_statuses_keeps_the_pre_change_rule():
    # (iv) One unit tested twice is not evidence of a one-to-many identifier.
    orders = SampledField("sys-a", "runs", "work_order", "order",
                          tuple(f"WO-{n}" for n in (5101, 5202, 5303, 5404, 5505)))
    tests = SampledField("sys-b", "tests", "result", "test",
                         ("Unit 5101 passed", "Unit 5101 failed", "Unit 5202 passed",
                          "Unit 5303 passed", "Unit 5404 passed", "Unit 5505 passed"))
    assert aff.affinity_rule(orders, tests) == {
        "concept": "order",
        "source_field": "runs.work_order", "source_table": "runs",
        "source_pattern": "WO-{id}",
        "target_system": "sys-b",
        "target_field": "tests.result", "target_table": "tests",
        "target_pattern": "Unit {id} passed",
        "confidence": 0.85, "method": "value_core_match",
        "reasoning": ("5 of 5 sampled value pairs share an identifier core under one "
                      "transform (WO-5101 ↔ Unit 5101 passed); 5 and 6 distinct values sampled"),
    }


def test_the_frozen_shaped_fixture_still_yields_the_wildcard_rule():
    items = _items(("PRT-5101-R-A", "PRT-5101-R-B", "PRT-5101-R-C", "PRT-5202-R-A",
                    "PRT-5202-R-B", "PRT-5303-R-A", *[f"PRT-{n}-R-A" for n in range(6001, 6024)]))
    rule = aff.affinity_rule(_codes(), items)
    assert (rule["source_pattern"], rule["target_pattern"]) == ("{id}", "PRT-{id}-R-{*}")


def test_a_single_one_to_many_core_is_not_enough_for_a_wildcard():
    items = _items(("PRT-5101-R-A", "PRT-5101-R-B", "PRT-5202-R-A", "PRT-5303-R-A"))
    rule = aff.affinity_rule(_codes(), items)
    assert rule["target_pattern"] == "PRT-{id}-R-A"  # the one-to-one pass, as before
    assert rule["reasoning"].startswith("3 of 3 sampled value pairs")


def test_cores_whose_wildcards_sit_in_different_places_do_not_agree():
    # 5101 varies in the tail, 5202 varies in the prefix: neither pattern is a
    # consensus, and the one-to-one pass rejects the mixed representatives.
    items = _items(("PRT-5101-R-A", "PRT-5101-R-B", "DOC-5202-R-A", "PRT-5202-R-A",
                    "PRT-5303-R-A"))
    assert aff.affinity_rule(_codes(), items) is None


def test_two_shapes_tied_at_the_threshold_fail_closed():
    # Every core is reachable from BOTH source formats: two wildcard patterns tie.
    codes = _codes(("5101", "ITM-5101", "ITM-5202", "5202", "5303", "ITM-5303"))
    items = _items(("PRT-5101-R-A", "PRT-5101-R-B", "PRT-5202-R-A", "PRT-5202-R-B",
                    "PRT-5303-R-A"))
    assert aff.affinity_rule(codes, items) is None


def test_the_one_to_one_fallback_can_never_itself_be_a_tie():
    # The tie fallback relies on this: a pattern holding >= 0.8 of the pairs is unique.
    assert aff.MIN_PATTERN_AGREEMENT > 0.5


def test_non_fitting_siblings_that_sort_first_never_crowd_out_the_fitting_ones():
    junk = tuple(f"AAA-5101-X{n}" for n in range(1, aff.MAX_VALUES_PER_CORE + 2))
    fitting = ("PRT-5101-R-A", "PRT-5101-R-B", "PRT-5101-R-C", "PRT-5202-R-A",
               "PRT-5202-R-B", "PRT-5303-R-A", *[f"PRT-{n}-R-A" for n in range(6001, 6024)])
    assert sorted(junk + fitting)[:aff.MAX_VALUES_PER_CORE] == sorted(junk)[:aff.MAX_VALUES_PER_CORE]

    rule = aff.affinity_rule(_codes(), _items(junk + fitting))
    assert rule["target_pattern"] == "PRT-{id}-R-{*}"
    assert "5101 ↔ PRT-5101-R-A, PRT-5101-R-B, PRT-5101-R-C" in rule["reasoning"]


def test_the_per_core_bound_keeps_the_fields_commonest_shape_first():
    junk = tuple(f"AAA-5101-X{n}" for n in range(1, 11))
    groups = aff.matched_groups(_codes(("5101",)), _items(junk + ("PRT-5101-R-A", "PRT-5101-R-B",
                                                                  "PRT-6001-R-A", "PRT-6002-R-A",
                                                                  *[f"PRT-{n}-R-A" for n in range(7001, 7020)])))
    kept = groups[0][2]
    assert len(kept) == aff.MAX_VALUES_PER_CORE
    assert kept[:2] == ("PRT-5101-R-A", "PRT-5101-R-B")


def test_the_pass_is_bounded_on_a_full_sample(monkeypatch):
    codes = _codes(tuple(str(n) for n in range(5000, 5200)))
    items = _items(tuple(f"PRT-{n}-R-{rev}" for n in range(5000, 5100) for rev in "AB"))
    calls = []
    real = aff.infer_pattern

    def counting(source_value, target_value):
        calls.append(1)
        return real(source_value, target_value)

    monkeypatch.setattr(aff, "infer_pattern", counting)
    groups = aff.matched_groups(codes, items)
    product = sum(len(s) * len(t) for _, s, t in groups)
    calls.clear()

    rule = aff.affinity_rule(codes, items)
    assert rule["target_pattern"] == "PRT-{id}-R-{*}"
    assert len(calls) <= product + 2 * len(groups)
    assert len(calls) <= aff.MAX_VALUES_PER_CORE ** 2 * len(groups)


@pytest.mark.parametrize("seed", range(5))
def test_the_fallback_decision_is_independent_of_the_targets_row_order(seed):
    import random
    values = list(_LOTS + ("LOT-2025-02850",))
    random.Random(seed).shuffle(values)
    values.remove("LOT-2026-02850")
    values.insert(0, "LOT-2026-02850")  # keep the one-to-one representative stable
    rule = aff.affinity_rule(_mes(_BATCHES), _erp(values))
    assert rule["target_pattern"] == "LOT-2026-0{id}"


# --- The one-to-many share (MIN_WILDCARD_SHARE) -----------------------------
# A fixed count of two one-to-many cores does not scale: two coincidental
# siblings among 50 or 200 cores are chance, not a varying part. The expected
# dicts below are the pre-wildcard (HEAD) output for the same samples.

_TWO_SIBLINGS = ("LOT-2025-02850", "LOT-2025-02851")


def _lot_rule_at(pairs, source_count, target_count):
    return {**_lot_rule(target_count), "reasoning": (
        f"{pairs} of {pairs} sampled value pairs share an identifier core under one "
        "transform (B-2847 ↔ LOT-2026-02847); "
        f"{source_count} and {target_count} distinct values sampled")}


def test_the_share_constant_and_its_floor():
    assert aff.MIN_WILDCARD_SHARE == 0.25
    assert aff.MIN_WILDCARD_CORES == 2


def test_two_coincidental_siblings_among_fifty_cores_keep_the_one_to_one_rule():
    rule = aff.affinity_rule(_mes(_BATCHES), _erp(_LOTS + _TWO_SIBLINGS))
    assert list(rule.items()) == list(_lot_rule_at(50, 60, 52).items())


def test_two_coincidental_siblings_among_two_hundred_cores_keep_the_one_to_one_rule():
    batches = tuple(f"B-{n}" for n in range(2840, 3060))
    lots = tuple(f"LOT-2026-0{n}" for n in range(2847, 3047))
    rule = aff.affinity_rule(_mes(batches), _erp(lots + _TWO_SIBLINGS))
    assert list(rule.items()) == list(_lot_rule_at(200, 220, 202).items())


def test_two_retested_units_among_many_keep_the_one_to_one_rule():
    ids = range(5101, 5113)
    orders = SampledField("sys-a", "runs", "work_order", "order", tuple(f"WO-{n}" for n in ids))
    tests = SampledField("sys-b", "tests", "result", "test",
                         (*[f"Unit {n} passed" for n in ids], "Unit 5101 failed", "Unit 5102 failed"))
    assert list(aff.affinity_rule(orders, tests).items()) == list({
        "concept": "order",
        "source_field": "runs.work_order", "source_table": "runs",
        "source_pattern": "WO-{id}",
        "target_system": "sys-b",
        "target_field": "tests.result", "target_table": "tests",
        "target_pattern": "Unit {id} passed",
        "confidence": 0.85, "method": "value_core_match",
        "reasoning": ("12 of 12 sampled value pairs share an identifier core under one "
                      "transform (WO-5101 ↔ Unit 5101 passed); 12 and 14 distinct values sampled"),
    }.items())


def test_a_literal_shape_replica_with_two_of_three_one_to_many_cores_is_a_wildcard():
    items = _items(("ITM-5101-V-A", "ITM-5101-V-B", "ITM-5101-V-C",
                    "ITM-5202-V-A", "ITM-5202-V-B", "ITM-5303-V-A"))
    rule = aff.affinity_rule(_codes(), items)
    assert (rule["source_pattern"], rule["target_pattern"]) == ("{id}", "ITM-{id}-V-{*}")
    assert rule["reasoning"].startswith("3 of 3 sampled identifier cores share one transform; "
                                        "2 of 3 cores match several target values")


@pytest.mark.parametrize("agreeing, required", [(3, 2), (8, 2), (9, 3), (50, 13), (200, 50)])
def test_the_required_one_to_many_count_is_the_floor_or_the_share(agreeing, required):
    assert aff.required_one_to_many(agreeing) == required


def test_a_share_that_does_scale_still_yields_the_wildcard():
    # A quarter of 12 agreeing cores (3) are one-to-many: the part does vary.
    codes = _codes(tuple(str(n) for n in range(5101, 5113)))
    items = _items((*[f"PRT-{n}-R-A" for n in range(5101, 5113)],
                    "PRT-5101-R-B", "PRT-5102-R-B", "PRT-5103-R-B"))
    assert aff.affinity_rule(codes, items)["target_pattern"] == "PRT-{id}-R-{*}"
    two_only = _items((*[f"PRT-{n}-R-A" for n in range(5101, 5113)], "PRT-5101-R-B", "PRT-5102-R-B"))
    assert aff.affinity_rule(codes, two_only)["target_pattern"] == "PRT-{id}-R-A"
