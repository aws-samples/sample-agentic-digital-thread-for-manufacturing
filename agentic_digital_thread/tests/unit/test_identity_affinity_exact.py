# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Exact-agreement pass (ADR-0087): a same-format identifier shared by two
systems becomes a JoinPath-type rule ONLY on measured evidence — the same
concept on both fields, the same dominant literal value shape (not all
digits, at least two segments), at least ``JOIN_MIN_OVERLAP_COUNT`` exact
values confirmed by a membership probe, a real key space on both sides and
no 1..N enumeration. Without a probe every rule stays byte-identical.
"""
import dataclasses

import pytest

from agents import identity_affinity as aff
from agents.identity_affinity import SampledField
from agents.join_discovery import JOIN_MIN_OVERLAP_COUNT

RUNS = tuple(f"RUN-2026092{d}-9{d}{k:03d}" for d in (1, 2, 3) for k in range(6))
WOS = tuple(r.replace("RUN-", "WO-") for r in RUNS)


def _field(system, table, field, concept, values):
    return SampledField(system, table, field, concept, tuple(values))


def _probe_over(*columns):
    """A membership probe over full columns: which of ``values`` exist there."""
    full = {(c.system_id, c.table, c.field): set(c.values) for c in columns}

    def probe(field, values):
        return sorted(set(values) & full[(field.system_id, field.table, field.field)])
    return probe


def _pair(shared: int, *, mes_values=RUNS, concept_b="batch"):
    mes = _field("sys-a", "runs", "run_number", "batch", mes_values)
    erp_values = tuple(RUNS[:shared]) + tuple(f"RUN-2026090{i}-8{i}000" for i in range(1, 6))
    erp = _field("sys-b", "shipment_lines", "batch_id", concept_b, erp_values)
    return mes, erp


def test_literal_shape_keeps_delimiters_and_lengths():
    assert aff.literal_shape("RUN-20260921-91005") == "AAA-99999999-99999"
    assert aff.literal_shape("B-2847") == "A-9999"
    assert aff.literal_shape("12345") == "99999"


def test_join_shape_needs_two_segments_and_not_all_digits():
    assert aff.is_join_shape("AAA-99999999-99999")
    assert not aff.is_join_shape("99999")            # all digits
    assert not aff.is_join_shape("9999-99-99")       # all digits, even with delimiters
    assert not aff.is_join_shape("AAAAAA")           # one segment


def test_dominant_shape_needs_a_majority_and_no_tie():
    assert aff.dominant_shape(RUNS) == "AAA-99999999-99999"
    assert aff.dominant_shape(RUNS[:2] + WOS[:2]) is None     # tie
    assert aff.dominant_shape(("a1", "b-2", "c_3")) is None   # no majority


def test_qualifying_pair_yields_one_identity_rule_with_computed_evidence():
    mes, erp = _pair(len(RUNS))
    rule = aff.exact_rule(mes, erp, _probe_over(mes, erp))
    assert rule is not None
    assert (rule["source_pattern"], rule["target_pattern"]) == ("", "")
    assert (rule["method"], rule["transform"]) == (aff.EXACT_METHOD, "identity")
    assert rule["method"] == "value_exact_match"
    assert (rule["source_field"], rule["target_system"], rule["target_field"]) == (
        "runs.run_number", "sys-b", "shipment_lines.batch_id")
    assert rule["concept"] == "batch" and rule["shared_count"] == len(RUNS)
    assert f"{len(RUNS)} exact values" in rule["reasoning"]
    assert "AAA-99999999-99999" in rule["reasoning"] and "concept batch" in rule["reasoning"]
    assert len(rule["reasoning"]) <= aff.REASONING_CAP


def test_the_probe_confirms_values_the_samples_did_not_share():
    """Independent samples miss a sparse key; the probe asks the other column."""
    mes, erp = _pair(len(RUNS))
    sampled_mes = _field(mes.system_id, mes.table, mes.field, mes.concept, RUNS[:9])
    sampled_erp = _field(erp.system_id, erp.table, erp.field, erp.concept, RUNS[9:] + erp.values[-5:])
    rule = aff.exact_rule(sampled_mes, sampled_erp, _probe_over(mes, erp))
    assert rule is not None and rule["shared_count"] == len(RUNS)


def test_same_concept_but_different_shape_yields_nothing():
    mes, erp = _pair(len(RUNS), mes_values=WOS)
    assert aff.exact_rule(mes, erp, _probe_over(mes, erp)) is None


def test_four_shared_values_are_below_the_floor():
    assert JOIN_MIN_OVERLAP_COUNT == 5
    mes, erp = _pair(JOIN_MIN_OVERLAP_COUNT - 1)
    assert aff.exact_rule(mes, erp, _probe_over(mes, erp)) is None
    mes, erp = _pair(JOIN_MIN_OVERLAP_COUNT)
    assert aff.exact_rule(mes, erp, _probe_over(mes, erp))["shared_count"] == 5


def test_sequential_integers_yield_nothing():
    ints = tuple(str(i) for i in range(1, 30))
    a = _field("sys-a", "runs", "seq", "batch", ints)
    b = _field("sys-b", "lines", "seq", "batch", ints)
    assert aff.exact_rule(a, b, _probe_over(a, b)) is None


def test_sequential_integer_guard_holds_even_for_a_join_shape(monkeypatch):
    mes, erp = _pair(len(RUNS))
    monkeypatch.setattr(aff, "is_sequential_integer_set", lambda values: values == mes.values)
    assert aff.exact_rule(mes, erp, _probe_over(mes, erp)) is None


def test_identical_values_but_different_concepts_yield_nothing():
    mes, erp = _pair(len(RUNS), concept_b="equipment")
    assert aff.exact_rule(mes, erp, _probe_over(mes, erp)) is None
    for missing in ("", "unknown", "needs_review"):
        a = _field("sys-a", "runs", "run_number", missing, RUNS)
        b = _field("sys-b", "lines", "batch_id", missing, RUNS)
        assert aff.exact_rule(a, b, _probe_over(a, b)) is None


def test_a_failed_probe_yields_nothing():
    mes, erp = _pair(len(RUNS))
    assert aff.exact_rule(mes, erp, lambda field, values: None) is None


def test_same_system_pair_yields_nothing():
    mes, _erp = _pair(len(RUNS))
    twin = _field("sys-a", "other", "batch_id", "batch", RUNS)
    assert aff.exact_rule(mes, twin, _probe_over(mes, twin)) is None


def test_small_key_space_fails_key_cardinality():
    a = _field("sys-a", "runs", "run_number", "batch", RUNS[:4])
    b = _field("sys-b", "lines", "batch_id", "batch", RUNS)
    assert aff.exact_rule(a, b, _probe_over(a, b)) is None


def test_without_a_probe_the_pass_never_runs():
    mes, erp = _pair(len(RUNS))
    assert aff.affinity_rule(mes, erp) is None
    assert aff.find_affinity_rules([mes], [erp]) == []
    assert aff.find_affinity_rules([mes], [erp], probe=_probe_over(mes, erp))[0]["method"] == aff.EXACT_METHOD


def test_every_qualifying_column_of_a_peer_keeps_its_rule():
    """Two columns of one peer that both hold the source's values are two
    joins (a table and a table that references it); each passed its own
    probe, so each keeps its rule, in the order found."""
    mes, erp = _pair(len(RUNS))
    fewer = _field("sys-b", "lot_lines", "batch_id", "batch", RUNS[:6] + erp.values[-5:])
    other_sys = _field("sys-c", "lines", "batch_ref", "batch", RUNS)
    probe = _probe_over(mes, erp, fewer, other_sys)
    rules = aff.find_affinity_rules([mes], [fewer, erp, other_sys], probe=probe)
    assert [(r["target_system"], r["target_field"], r["shared_count"]) for r in rules] == [
        ("sys-b", "lot_lines.batch_id", 6), ("sys-b", "shipment_lines.batch_id", len(RUNS)),
        ("sys-c", "lines.batch_ref", len(RUNS))]


def test_one_rule_per_pair_of_physical_columns():
    """The same column sampled twice (a field with two identifier-role edges)
    is one column: one rule. A column with the same values in another table is
    a different column and keeps its own."""
    mes, erp = _pair(len(RUNS))
    again = _field("sys-b", erp.table, erp.field, "batch", erp.values)
    twin = _field("sys-b", "a_lines", "batch_id", "batch", erp.values)
    rules = aff.find_affinity_rules([mes], [erp, again, twin], probe=_probe_over(mes, erp, twin))
    assert [r["target_field"] for r in rules] == ["shipment_lines.batch_id", "a_lines.batch_id"]


def test_exact_rule_replaces_a_same_format_llm_rule_but_not_a_pattern_rule():
    mes, erp = _pair(len(RUNS))
    (exact,) = aff.find_affinity_rules([mes], [erp], probe=_probe_over(mes, erp))
    llm = {"source_field": "runs.run_number", "target_system": "sys-b",
           "target_field": "shipment_lines.batch_id", "source_pattern": "", "target_pattern": "",
           "method": "", "reasoning": "llm"}
    other = {"source_field": "runs.x", "target_system": "sys-b", "target_field": "t.y"}
    assert aff.merge_affinity_rules([other, llm], [exact]) == [other, exact]
    patterned = {**llm, "source_pattern": "RUN-{id}", "target_pattern": "{id}"}
    assert aff.merge_affinity_rules([patterned], [exact]) == [patterned]


# ── 2026-09-30: an exact rule replaces an LLM rule whose patterns add nothing ──
# Live evidence (frozen-12201093 step 4): the LLM proposed ``PN-*``/``PN-*``
# for a same-format pair, so the exact rule was dropped and the join was
# written as foreign_key_match — nondeterministic across re-onboards.

def _llm_with(src_pat, tgt_pat):
    return {"source_field": "runs.run_number", "target_system": "sys-b",
            "target_field": "shipment_lines.batch_id", "source_pattern": src_pat,
            "target_pattern": tgt_pat, "method": "", "reasoning": "llm"}


@pytest.mark.parametrize("src_pat, tgt_pat", [
    ("", ""), (None, None), ("AB-*", "AB-*"), ("{id}", "{id}"), ("*", "*"), ("*", ""),
    ("{*}", "{id}"), (" AB-{id} ", "AB-{id}")])
def test_patterns_that_add_nothing(src_pat, tgt_pat):
    assert aff.patterns_add_nothing(src_pat, tgt_pat) is True


@pytest.mark.parametrize("src_pat, tgt_pat", [
    ("AB-{id}-REV-{*}", "{id}"), ("RUN-{id}", "{id}"), ("AB-*", ""), ("AB-*", "CD-*")])
def test_transforming_patterns_add_something(src_pat, tgt_pat):
    assert aff.patterns_add_nothing(src_pat, tgt_pat) is False


@pytest.mark.parametrize("src_pat, tgt_pat, why", [
    ("AB-*", "AB-*", "LLM patterns 'AB-*'->'AB-*' identity-shaped"),
    ("", "", "LLM patterns empty"),
    ("*", "*", "LLM patterns '*'->'*' identity-shaped")])
def test_exact_rule_replaces_an_identity_shaped_llm_rule(src_pat, tgt_pat, why):
    mes, erp = _pair(len(RUNS))
    (exact,) = aff.find_affinity_rules([mes], [erp], probe=_probe_over(mes, erp))
    llm = _llm_with(src_pat, tgt_pat)
    merged = aff.merge_affinity_rules([llm], [exact])
    assert merged == [exact]
    (note,) = aff.exact_replacement_notes([llm], merged)
    assert note == f"runs.run_number -> sys-b.shipment_lines.batch_id ({why})"


def test_exact_rule_keeps_a_transforming_llm_rule():
    mes, erp = _pair(len(RUNS))
    (exact,) = aff.find_affinity_rules([mes], [erp], probe=_probe_over(mes, erp))
    llm = _llm_with("AB-{id}-REV-{*}", "{id}")
    merged = aff.merge_affinity_rules([llm], [exact])
    assert merged == [llm]
    assert aff.exact_replacement_notes([llm], merged) == []


# ── Concept agreement is a SHARED concept (any role), not the best one ──────
# Live evidence (ADR-0087): the MES run number MAPS_TO ``production-run``
# (identifier) AND ``batch`` (correlate); the ERP shipment line's batch_id
# MAPS_TO ``batch``. They key the same thing, so the pass must see it.

def _with(field, concepts, confidence=()):
    import dataclasses
    return dataclasses.replace(field, concepts=frozenset(concepts),
                               concept_confidence=tuple(confidence))


def test_a_shared_correlate_concept_qualifies():
    mes, erp = _pair(len(RUNS))
    run = _with(_field("sys-a", "runs", "run_number", "production-run", mes.values),
                {"production-run", "batch"}, (("production-run", 0.95), ("batch", 0.85)))
    rule = aff.exact_rule(run, _with(erp, {"batch"}), _probe_over(run, erp))
    assert rule is not None and rule["concept"] == "batch"
    assert "concept batch" in rule["reasoning"]


def test_the_shared_concept_is_seen_from_either_side():
    mes, erp = _pair(len(RUNS))
    run = _with(_field("sys-a", "runs", "run_number", "production-run", mes.values),
                {"production-run", "batch"})
    assert aff.exact_rule(erp, run, _probe_over(run, erp))["concept"] == "batch"


def test_disjoint_unrelated_concept_sets_yield_nothing():
    """Disjoint AND not a catalog-related identifier pair (``production-run``
    vs ``batch`` IS related since the one-clock live walk; see below)."""
    mes, erp = _pair(len(RUNS))
    run = _with(_field("sys-a", "runs", "run_number", "equipment", mes.values),
                {"equipment", "operation"})
    assert aff.exact_rule(run, _with(erp, {"batch", "lot"}), _probe_over(run, erp)) is None


def test_unknown_and_needs_review_never_count_as_shared():
    mes, erp = _pair(len(RUNS))
    run = _with(_field("sys-a", "runs", "run_number", "equipment", mes.values),
                {"equipment", "unknown", "needs_review", ""})
    ship = _with(_field("sys-b", erp.table, erp.field, "batch", erp.values),
                 {"batch", "unknown", "needs_review", ""})
    assert aff.exact_rule(run, ship, _probe_over(run, ship)) is None


def test_the_concept_set_is_excluded_from_equality():
    mes, _erp = _pair(len(RUNS))
    assert _with(mes, {"batch", "lot"}) == mes and hash(_with(mes, {"lot"})) == hash(mes)


def test_several_shared_concepts_rank_by_both_sides_confidence():
    """Direction-neutral (ADR-0087 amendment 2026-10-18): the combined
    confidence of both endpoints, tie lexically first; a side without a
    confidence for every shared concept ranks lexically. Either direction names
    the same concept."""
    mes, erp = _pair(len(RUNS))
    run = _with(mes, {"batch", "lot"}, (("batch", 0.8), ("lot", 0.9)))
    ship = _with(erp, {"batch", "lot"}, (("batch", 0.9), ("lot", 0.6)))
    probe = _probe_over(run, ship)
    assert aff.exact_rule(run, ship, probe)["concept"] == "batch"    # 1.7 vs 1.5
    assert aff.exact_rule(ship, run, probe)["concept"] == "batch"
    tied = _with(erp, {"batch", "lot"}, (("batch", 0.9), ("lot", 0.8)))   # 1.7 each
    assert aff.exact_rule(run, tied, _probe_over(run, tied))["concept"] == "batch"
    unscored = _with(erp, {"batch", "lot"})
    assert aff.exact_rule(run, unscored, _probe_over(run, unscored))["concept"] == "batch"
    assert aff.exact_rule(unscored, run, _probe_over(run, unscored))["concept"] == "batch"
    partial = _with(erp, {"batch", "lot"}, (("lot", 0.99),))
    assert aff.exact_rule(run, partial, _probe_over(run, partial))["concept"] == "batch"


def _normalized(source_system, rules):
    """A rule's direction-free meaning: its two endpoints, concept and kind."""
    return {(frozenset({(source_system, r["source_field"]), (r["target_system"], r["target_field"])}),
             r["concept"], r["method"], r.get("transform"), r.get("shared_count")) for r in rules}


def test_one_holder_against_two_keeps_both_rules_in_either_direction():
    """One holder in A, two in B with the same values: both joins qualified,
    so resolving A and resolving B both write the same two rules."""
    holder = _field("sys-a", "orders", "order_ref", "batch", RUNS)
    lines = _field("sys-b", "lines", "order_ref", "batch", RUNS)
    archive = _field("sys-b", "archive", "order_ref", "batch", RUNS)
    probe = _probe_over(holder, lines, archive)
    from_a = aff.find_affinity_rules([holder], [lines, archive], probe=probe)
    from_b = aff.find_affinity_rules([lines, archive], [holder], probe=probe)
    assert _normalized("sys-a", from_a) == _normalized("sys-b", from_b)
    assert sorted(r["target_field"] for r in from_a) == ["archive.order_ref", "lines.order_ref"]


def test_a_base_table_key_and_its_one_to_one_extension_both_join_the_peer():
    """A customer table's key and the key of its one-to-one extension table hold
    the same codes, and both join the peer's account code. Neither is an alias
    of the other, so both rules are kept, from either side."""
    codes = tuple(f"CUS-{n:04d}" for n in range(1001, 1019))
    base = _field("sys-a", "customers", "customer_code", "customer", codes)
    extension = _field("sys-a", "customer_credit", "customer_code", "customer", codes)
    account = _field("sys-b", "accounts", "customer_code", "customer", codes)
    probe = _probe_over(base, extension, account)
    from_a = aff.find_affinity_rules([base, extension], [account], probe=probe)
    from_b = aff.find_affinity_rules([account], [base, extension], probe=probe)
    assert _normalized("sys-a", from_a) == _normalized("sys-b", from_b)
    assert {pair for pair, *_rest in _normalized("sys-a", from_a)} == {
        frozenset({("sys-a", "customers.customer_code"), ("sys-b", "accounts.customer_code")}),
        frozenset({("sys-a", "customer_credit.customer_code"), ("sys-b", "accounts.customer_code")})}


def test_two_holders_on_each_side_keep_every_qualifying_pair():
    """Every pair that passed its probe is kept, whichever side resolves."""
    first = _field("sys-a", "orders", "order_ref", "batch", RUNS)
    second = _field("sys-a", "returns", "order_ref", "batch", RUNS[:12])
    lines = _field("sys-b", "lines", "order_ref", "batch", RUNS)
    archive = _field("sys-b", "archive", "order_ref", "batch", RUNS[:12])
    probe = _probe_over(first, second, lines, archive)
    from_a = aff.find_affinity_rules([first, second], [lines, archive], probe=probe)
    from_b = aff.find_affinity_rules([lines, archive], [first, second], probe=probe)
    assert _normalized("sys-a", from_a) == _normalized("sys-b", from_b)
    assert {(r["source_field"], r["target_field"], r["shared_count"]) for r in from_a} == {
        ("orders.order_ref", "lines.order_ref", 18), ("orders.order_ref", "archive.order_ref", 12),
        ("returns.order_ref", "lines.order_ref", 12), ("returns.order_ref", "archive.order_ref", 12)}


def test_exact_pass_stats_count_every_outcome_and_the_cap():
    mes, erp = _pair(len(RUNS))
    wos = _field("sys-b", "orders", "wo", "batch", tuple(f"B-{n}" for n in range(2800, 2830)))  # shape
    other = _field("sys-b", "orders", "po", "purchase-order", RUNS)           # concept
    few = _field("sys-b", "lot_lines", "batch_id", "batch", RUNS[:4])         # cardinality
    twin = _field("sys-b", "a_lines", "batch_id", "batch", erp.values)        # kept
    again = _field("sys-b", erp.table, erp.field, "batch", erp.values)        # capped (same column)
    stats = aff.ExactPassStats()
    rules = aff.find_affinity_rules([mes], [erp, wos, other, few, twin, again],
                                    probe=_probe_over(mes, erp, wos, other, few, twin), stats=stats)
    assert [r["target_field"] for r in rules] == ["shipment_lines.batch_id", "a_lines.batch_id"]
    assert (stats.considered, stats.refused_concept, stats.refused_shape, stats.refused_probe,
            stats.qualified, stats.emitted) == (6, 1, 1, 1, 3, 2)
    assert stats.summary() == ("exact pass: 6 pair(s) considered; refused concept=1 shape=1 "
                               "probe/cardinality=1; qualified=3 emitted=2")


def test_exact_pass_stats_stay_zero_without_a_probe():
    mes, erp = _pair(len(RUNS))
    stats = aff.ExactPassStats()
    assert aff.find_affinity_rules([mes], [erp], stats=stats) == []
    assert stats.considered == 0 and stats.emitted == 0


# ── Concept agreement through a catalog-RELATED identifier pair ─────────────
# Live evidence (one-clock walk, widened run view): the MES run number MAPS_TO
# ``production-run`` ONLY (identifier 0.95, no ``batch`` correlate); the ERP
# shipment line's batch_id MAPS_TO ``batch``. The catalog declares the two
# identifier concepts interchangeable across systems
# (``catalog.RELATED_IDENTIFIER_CONCEPTS``), so they agree; every measured
# gate still applies, and the rule names the declaring concept.

def _run_only(values=RUNS):
    return _with(_field("sys-a", "runs", "run_number", "production-run", values),
                 {"production-run"}, (("production-run", 0.95),))


def test_the_catalog_declares_the_run_batch_identifier_pair():
    from agents import catalog
    assert ("production-run", "batch") in catalog.RELATED_IDENTIFIER_CONCEPTS
    for pair in catalog.RELATED_IDENTIFIER_CONCEPTS:
        concept, declaring = pair
        assert concept != declaring
        assert {concept, declaring} <= set(catalog.CORE_CONCEPT_DEFINITIONS)


def test_a_related_identifier_pair_qualifies_and_names_the_declaring_concept():
    _mes, erp = _pair(len(RUNS))
    run = _run_only()
    rule = aff.exact_rule(run, _with(erp, {"batch"}), _probe_over(run, erp))
    assert rule is not None and rule["concept"] == "batch"
    assert "concept batch" in rule["reasoning"] and "production-run" in rule["reasoning"]
    assert len(rule["reasoning"]) <= aff.REASONING_CAP


def test_a_related_identifier_pair_is_seen_from_either_side():
    _mes, erp = _pair(len(RUNS))
    run = _run_only()
    rule = aff.exact_rule(_with(erp, {"batch"}), run, _probe_over(run, erp))
    assert rule is not None and rule["concept"] == "batch"


def test_a_shared_concept_wins_over_a_related_pair():
    _mes, erp = _pair(len(RUNS))
    run = _with(_run_only(), {"production-run", "lot"}, (("production-run", 0.95), ("lot", 0.5)))
    rule = aff.exact_rule(run, _with(erp, {"batch", "lot"}), _probe_over(run, erp))
    assert rule["concept"] == "lot" and "production-run" not in rule["reasoning"]


def test_a_related_pair_bypasses_no_measured_gate():
    _mes, erp = _pair(len(RUNS))
    ship = _with(erp, {"batch"})
    wos = _run_only(WOS)
    assert aff.exact_verdict(wos, ship, _probe_over(wos, ship))[1] == aff.EXACT_REFUSED_SHAPE
    _mes, few = _pair(JOIN_MIN_OVERLAP_COUNT - 1)
    few = _with(few, {"batch"})
    run = _run_only()
    assert aff.exact_verdict(run, few, _probe_over(run, few))[1] == aff.EXACT_REFUSED_PROBE
    assert aff.exact_verdict(run, ship, lambda field, values: None)[1] == aff.EXACT_REFUSED_PROBE


def test_the_related_relation_is_not_transitive_or_wildcard():
    _mes, erp = _pair(len(RUNS))
    run = _run_only()
    for other in ("lot-number", "work-order", "sales-order"):
        target = _with(_field(erp.system_id, erp.table, erp.field, other, erp.values), {other})
        assert aff.exact_verdict(run, target, _probe_over(run, target))[1] == aff.EXACT_REFUSED_CONCEPT


# ── work-order ~ batch (ADR-0087 amendment 2026-10-05) ───────────────────────
# Live evidence (2026-10-05): the ERP lot-tracking row's batch_id holds
# the MES work-order number and MAPS_TO ``batch``; the MES work-order number
# MAPS_TO ``work-order``. With no related pair the concept gate refused the
# lot hop, and lot-to-work-order questions fell back to run numbers.

def _wo_number(values=WOS):
    return _with(_field("sys-a", "work_orders", "work_order_number", "work-order", values),
                 {"work-order"}, (("work-order", 0.95),))


def _lot_tracking(values):
    return _with(_field("sys-b", "lot_tracking", "batch_id", "batch", values), {"batch"})


def test_the_catalog_declares_the_work_order_batch_identifier_pair():
    from agents import catalog
    assert ("work-order", "batch") in catalog.RELATED_IDENTIFIER_CONCEPTS


def test_a_work_order_number_joins_a_batch_column_that_holds_it():
    wo = _wo_number()
    track = _lot_tracking(WOS + tuple(f"WO-2026090{i}-8{i}000" for i in range(1, 6)))
    rule = aff.exact_rule(wo, track, _probe_over(wo, track))
    assert rule is not None and rule["concept"] == "batch"
    assert "catalog-related work-order" in rule["reasoning"]
    assert rule["shared_count"] == len(WOS)
    # Symmetric: the ERP's own resolve sees the same agreement.
    assert aff.exact_rule(track, wo, _probe_over(wo, track))["concept"] == "batch"


def test_a_work_order_number_never_joins_a_batch_column_of_run_numbers():
    wo = _wo_number()
    ship = _lot_tracking(RUNS)
    assert aff.exact_verdict(wo, ship, _probe_over(wo, ship))[1] == aff.EXACT_REFUSED_SHAPE
    # Same shape, no shared value: the probe floor refuses.
    other = _lot_tracking(tuple(f"WO-2026080{d}-7{d}{k:03d}" for d in (1, 2, 3) for k in range(6)))
    assert aff.exact_verdict(wo, other, _probe_over(wo, other))[1] == aff.EXACT_REFUSED_PROBE


def test_work_order_and_production_run_stay_unrelated():
    """Both relate to ``batch``; the relation is not transitive."""
    wo = dataclasses.replace(_wo_number(RUNS), system_id="sys-c")
    run = _run_only()
    assert aff.exact_verdict(wo, run, _probe_over(wo, run))[1] == aff.EXACT_REFUSED_CONCEPT


# ── production-order ~ production-run (ADR-0087 amendment 2026-10-21) ────────
# Live evidence (Tulip connector, 2026-10-21): an MES column labelled "ERP
# Order ID" holds the ERP's production run numbers and MAPS_TO
# ``production-order``; a run-number column MAPS_TO ``production-run``. The
# concept gate refused the pair, so no identity rule was proposed and the
# chat matched orders to runs by timestamp.

ORDER_ONLY_EXTRA = tuple(f"RUN-2026090{i}-8{i}000" for i in range(1, 6))


def _order_id(values=RUNS, system_id="sys-t"):
    return _with(_field(system_id, "production_orders", "erp_order_id", "production-order", values),
                 {"production-order"}, (("production-order", 0.95),))


def _run_column(values=RUNS + ORDER_ONLY_EXTRA, system_id="sys-b"):
    return _with(_field(system_id, "runs", "run_number", "production-run", values),
                 {"production-run"}, (("production-run", 0.95),))


def test_the_catalog_declares_the_production_order_run_identifier_pair():
    from agents import catalog
    assert ("production-run", "production-order") in catalog.RELATED_IDENTIFIER_CONCEPTS
    # The declaring concept's own definition says the two numbers can be equal.
    definition = catalog.CORE_CONCEPT_DEFINITIONS["production-order"]
    assert "production order number can equal another's production run number" in definition


def test_a_production_order_field_joins_a_run_number_column_that_holds_it():
    order, run = _order_id(), _run_column()
    rule = aff.exact_rule(order, run, _probe_over(order, run))
    assert rule is not None and rule["concept"] == "production-order"
    assert rule["shared_count"] == len(RUNS)
    assert "concept production-order (catalog-related production-run)" in rule["reasoning"]
    assert len(rule["reasoning"]) <= aff.REASONING_CAP
    # Symmetric: the run column's own resolve sees the same agreement.
    reverse = aff.exact_rule(run, order, _probe_over(order, run))
    assert reverse is not None and reverse["concept"] == "production-order"


def test_without_the_relation_the_order_run_pair_is_refused_on_concept(monkeypatch):
    """The refusal seen live: the same evidence, the relation as it stood before."""
    order, run = _order_id(), _run_column()
    before = frozenset({("production-run", "batch"), ("work-order", "batch")})
    monkeypatch.setattr(aff, "RELATED_IDENTIFIER_CONCEPTS", before)
    assert aff.exact_verdict(order, run, _probe_over(order, run))[1] == aff.EXACT_REFUSED_CONCEPT


def test_the_order_run_relation_is_not_transitive():
    """``production-order`` relates to ``production-run`` and ``batch`` only:
    not to the ``work-order`` that ``batch`` relates to, nor to the other order
    concepts or an unrelated identifier."""
    order = _order_id()
    for other in ("work-order", "sales-order", "purchase-order", "lot-number", "equipment-id"):
        target = _with(_field("sys-b", "lines", "ref", other, RUNS + ORDER_ONLY_EXTRA), {other})
        assert aff.exact_verdict(order, target, _probe_over(order, target))[1] == aff.EXACT_REFUSED_CONCEPT, other


def test_the_order_run_relation_bypasses_no_measured_gate():
    run = _run_column()
    # Wrong shape: the order column holds work-order numbers, the run column run numbers.
    wos = _order_id(WOS)
    assert aff.exact_verdict(wos, run, _probe_over(wos, run))[1] == aff.EXACT_REFUSED_SHAPE
    # Low probe hit rate: one value short of the floor.
    few = _order_id(RUNS[:JOIN_MIN_OVERLAP_COUNT - 1] + tuple(f"RUN-2026080{d}-7{d}{k:03d}" for d in (1, 2)
                                                             for k in range(6)))
    assert aff.exact_verdict(few, run, _probe_over(few, run))[1] == aff.EXACT_REFUSED_PROBE
    # A probe that cannot answer is not evidence.
    order = _order_id()
    assert aff.exact_verdict(order, run, lambda field, values: None)[1] == aff.EXACT_REFUSED_PROBE
    # A side whose whole key space is smaller than the floor (key cardinality).
    tiny = _order_id(RUNS[:4])
    assert aff.exact_verdict(tiny, run, _probe_over(tiny, run))[1] == aff.EXACT_REFUSED_PROBE


def test_a_shared_concept_still_wins_over_the_order_run_relation():
    order = _with(_order_id(), {"production-order", "batch"}, (("production-order", 0.95), ("batch", 0.5)))
    run = _with(_run_column(), {"production-run", "batch"}, (("production-run", 0.95), ("batch", 0.85)))
    rule = aff.exact_rule(order, run, _probe_over(order, run))
    assert rule["concept"] == "batch" and "catalog-related" not in rule["reasoning"]


def test_a_pattern_rule_between_an_order_and_a_run_names_the_same_concept_as_before(monkeypatch):
    """``production-order`` declares the relation, so a pattern rule between
    the two concepts names ``production-order`` with or without it (the
    lexical fallback picked it already): no LINKS_CONCEPT edge moves."""
    order, run = _order_id(), _run_column()
    assert aff.rule_concept(order, run) == aff.rule_concept(run, order) == "production-order"
    monkeypatch.setattr(aff, "RELATED_IDENTIFIER_CONCEPTS",
                        frozenset({("production-run", "batch"), ("work-order", "batch")}))
    assert aff.rule_concept(order, run) == aff.rule_concept(run, order) == "production-order"


# ── production-order ~ batch (ADR-0087 amendment 2026-10-21) ─────────────────
# The ERP holds its production run numbers only in the shipment line's
# ``batch_id`` (``batch``), so an MES order column that holds those numbers
# (``production-order``) reaches the ERP only through this pair.

#: The relation as the production-order ~ production-run pair left it.
_RELATION_WITHOUT_ORDER_BATCH = frozenset({("production-run", "batch"), ("work-order", "batch"),
                                           ("production-run", "production-order")})
#: Every relation the existing hops' names are checked under.
_RELATIONS = (frozenset({("production-run", "batch"), ("work-order", "batch")}),
              _RELATION_WITHOUT_ORDER_BATCH)


def _ship_batch(values=RUNS + ORDER_ONLY_EXTRA, system_id="sys-e"):
    return _with(_field(system_id, "shipment_lines", "batch_id", "batch", values),
                 {"batch"}, (("batch", 0.9),))


def test_the_catalog_declares_the_production_order_batch_identifier_pair():
    from agents import catalog
    assert ("production-order", "batch") in catalog.RELATED_IDENTIFIER_CONCEPTS
    # The declaring concept's own definition says the two numbers can be equal.
    definition = catalog.CORE_CONCEPT_DEFINITIONS["batch"]
    assert "production order number can equal another's batch number" in definition


def test_a_production_order_field_joins_a_batch_column_that_holds_it():
    order, ship = _order_id(), _ship_batch()
    rule = aff.exact_rule(order, ship, _probe_over(order, ship))
    assert rule is not None and rule["concept"] == "batch"
    assert rule["shared_count"] == len(RUNS)
    assert "concept batch (catalog-related production-order)" in rule["reasoning"]
    assert len(rule["reasoning"]) <= aff.REASONING_CAP
    # Symmetric: the batch column's own resolve sees the same agreement.
    reverse = aff.exact_rule(ship, order, _probe_over(order, ship))
    assert reverse is not None and reverse["concept"] == "batch"


def test_without_the_order_batch_pair_the_order_column_is_refused_on_concept(monkeypatch):
    """The same evidence under the relation the production-order ~
    production-run pair left: the order column cannot reach a batch column."""
    order, ship = _order_id(), _ship_batch()
    monkeypatch.setattr(aff, "RELATED_IDENTIFIER_CONCEPTS", _RELATION_WITHOUT_ORDER_BATCH)
    assert aff.exact_verdict(order, ship, _probe_over(order, ship))[1] == aff.EXACT_REFUSED_CONCEPT


def test_the_order_batch_pair_bypasses_no_measured_gate():
    ship = _ship_batch()
    # Wrong shape: the order column holds work-order numbers, the batch column run numbers.
    wos = _order_id(WOS)
    assert aff.exact_verdict(wos, ship, _probe_over(wos, ship))[1] == aff.EXACT_REFUSED_SHAPE
    # Low probe hit rate: one value short of the floor.
    few = _order_id(RUNS[:JOIN_MIN_OVERLAP_COUNT - 1] + tuple(f"RUN-2026080{d}-7{d}{k:03d}" for d in (1, 2)
                                                             for k in range(6)))
    assert aff.exact_verdict(few, ship, _probe_over(few, ship))[1] == aff.EXACT_REFUSED_PROBE
    # A probe that cannot answer is not evidence.
    order = _order_id()
    assert aff.exact_verdict(order, ship, lambda field, values: None)[1] == aff.EXACT_REFUSED_PROBE
    # A side whose whole key space is smaller than the floor (key cardinality).
    tiny = _order_id(RUNS[:4])
    assert aff.exact_verdict(tiny, ship, _probe_over(tiny, ship))[1] == aff.EXACT_REFUSED_PROBE


def test_the_order_batch_pair_renames_no_existing_hop(monkeypatch):
    """The run-number hop and the work-order lot hop are named ``batch`` under
    every relation, the one on disk included, with the same reasoning label;
    so is a run column the mapper also gave ``production-order``."""
    ship = _ship_batch()
    run, wo = _run_column(), _wo_number()
    track = _lot_tracking(WOS + tuple(f"WO-2026090{i}-8{i}000" for i in range(1, 6)))
    run_and_order = _with(run, {"production-run", "production-order"},
                          (("production-run", 0.95), ("production-order", 0.6)))
    relations = (*_RELATIONS, aff.RELATED_IDENTIFIER_CONCEPTS)
    assert ("production-order", "batch") in relations[-1]
    for relation in relations:
        monkeypatch.setattr(aff, "RELATED_IDENTIFIER_CONCEPTS", relation)
        for source, target in ((run, ship), (ship, run)):
            assert aff.agreed_concept(source, target) == ("batch", "batch (catalog-related production-run)")
            assert aff.exact_rule(source, target, _probe_over(run, ship))["concept"] == "batch"
        for source, target in ((wo, track), (track, wo)):
            assert aff.agreed_concept(source, target) == ("batch", "batch (catalog-related work-order)")
            assert aff.exact_rule(source, target, _probe_over(wo, track))["concept"] == "batch"
        assert aff.exact_rule(run_and_order, ship, _probe_over(run, ship))["concept"] == "batch"


def test_work_order_stays_unrelated_to_production_order():
    """Both relate to ``batch``; the relation is not transitive."""
    wo = dataclasses.replace(_wo_number(RUNS), system_id="sys-c")
    order = _order_id()
    assert aff.exact_verdict(wo, order, _probe_over(wo, order))[1] == aff.EXACT_REFUSED_CONCEPT
    assert aff.exact_verdict(order, wo, _probe_over(wo, order))[1] == aff.EXACT_REFUSED_CONCEPT


def test_a_pattern_rule_between_an_order_and_a_batch_names_the_same_concept_as_before(monkeypatch):
    """``batch`` declares the pair, so a pattern rule between the two concepts
    names ``batch`` with or without it (the lexical fallback picked it
    already): no LINKS_CONCEPT edge moves."""
    order, ship = _order_id(), _ship_batch()
    assert aff.rule_concept(order, ship) == aff.rule_concept(ship, order) == "batch"
    monkeypatch.setattr(aff, "RELATED_IDENTIFIER_CONCEPTS", _RELATION_WITHOUT_ORDER_BATCH)
    assert aff.rule_concept(order, ship) == aff.rule_concept(ship, order) == "batch"


def _exact_between(source, target, table_s, table_t, concept, values):
    left = _field(source, table_s, "external_id", concept, values)
    right = _field(target, table_t, "external_id", concept, values)
    return left, right, aff.exact_rule(left, right, _probe_over(left, right))


def test_two_affinity_rules_on_one_leaf_pair_in_different_tables_are_both_merged():
    """Two exact joins between one pair of systems over the same column names
    in different tables are two rules; the merge keeps both, in either order."""
    _l1, _r1, customer = _exact_between("sys-a", "sys-b", "customers", "accounts", "customer",
                                         tuple(f"CUS-{n:04d}" for n in range(1001, 1019)))
    _l2, _r2, supplier = _exact_between("sys-a", "sys-b", "vendors", "suppliers", "supplier",
                                         tuple(f"SUP-{n:04d}" for n in range(5001, 5019)))
    assert aff.merge_affinity_rules([], [customer, supplier]) == [customer, supplier]
    assert aff.merge_affinity_rules([], [supplier, customer]) == [supplier, customer]
    assert aff.merge_affinity_rules([], [customer, dict(customer)]) == [customer]


def test_an_llm_rule_still_pairs_on_its_leaf_names():
    """An LLM rule naming the leaf columns pairs with every affinity rule on
    those leaves, whichever comes first: an exact rule replaces it when its
    patterns add nothing, and yields to it when they transform."""
    _l1, _r1, customer = _exact_between("sys-a", "sys-b", "customers", "accounts", "customer",
                                         tuple(f"CUS-{n:04d}" for n in range(1001, 1019)))
    _l2, _r2, supplier = _exact_between("sys-a", "sys-b", "vendors", "suppliers", "supplier",
                                         tuple(f"SUP-{n:04d}" for n in range(5001, 5019)))
    plain = {"source_field": "external_id", "target_system": "sys-b", "target_field": "external_id",
             "source_pattern": "", "target_pattern": "", "method": "", "reasoning": "llm"}
    for order in ([customer, supplier], [supplier, customer]):
        assert aff.merge_affinity_rules([plain], order) == order
    patterned = {**plain, "source_pattern": "X-{id}", "target_pattern": "{id}"}
    for order in ([customer, supplier], [supplier, customer]):
        assert aff.merge_affinity_rules([patterned], order) == [patterned]


def _customer_and_supplier():
    _l1, _r1, customer = _exact_between("sys-a", "sys-b", "customers", "accounts", "customer",
                                         tuple(f"CUS-{n:04d}" for n in range(1001, 1019)))
    _l2, _r2, supplier = _exact_between("sys-a", "sys-b", "vendors", "suppliers", "supplier",
                                         tuple(f"SUP-{n:04d}" for n in range(5001, 5019)))
    return customer, supplier


def _scoped_llm(source_table, target_table, *, with_table_keys, **over):
    """An LLM rule for ``external_id`` scoped to two tables, in either shape
    the resolver returns: table-qualified fields (the prompt's format), or
    also ``source_table``/``target_table``."""
    rule = {"concept": "customer", "source_field": f"{source_table}.external_id",
            "target_system": "sys-b", "target_field": f"{target_table}.external_id",
            "source_pattern": "X-{id}", "target_pattern": "{id}", "method": "", "reasoning": "llm"}
    if with_table_keys:
        rule.update(source_table=source_table, target_table=target_table)
    rule.update(over)
    return rule


@pytest.mark.parametrize("with_table_keys", [False, True])
def test_a_table_scoped_llm_rule_pairs_only_with_its_own_tables(with_table_keys):
    """A transforming LLM rule on customers/accounts wins over the exact rule
    on those tables, and leaves the exact rule on vendors/suppliers alone, in
    either order: it names its tables, so it is not a claim about theirs."""
    customer, supplier = _customer_and_supplier()
    llm = _scoped_llm("customers", "accounts", with_table_keys=with_table_keys)
    for order in ([customer, supplier], [supplier, customer]):
        assert aff.merge_affinity_rules([llm], order) == [llm, supplier]


@pytest.mark.parametrize("with_table_keys", [False, True])
def test_an_identity_shaped_scoped_llm_rule_is_replaced_by_its_own_tables_rule(with_table_keys):
    customer, supplier = _customer_and_supplier()
    llm = _scoped_llm("customers", "accounts", with_table_keys=with_table_keys,
                      source_pattern="", target_pattern="")
    for order in ([customer, supplier], [supplier, customer]):
        assert aff.merge_affinity_rules([llm], order) == [customer, supplier]
    (note,) = aff.exact_replacement_notes([llm], aff.merge_affinity_rules([llm], [supplier, customer]))
    assert note.startswith("customers.external_id -> sys-b.accounts.external_id")


def test_an_llm_rule_on_other_tables_suppresses_no_exact_rule():
    """Scoped to tables neither exact rule is on: all three are kept."""
    customer, supplier = _customer_and_supplier()
    llm = _scoped_llm("clients", "accounts", with_table_keys=False)
    for order in ([customer, supplier], [supplier, customer]):
        assert aff.merge_affinity_rules([llm], order) == [llm, *order]


def test_a_schema_qualified_or_differently_cased_table_still_names_the_table():
    customer, supplier = _customer_and_supplier()
    llm = _scoped_llm("ERP_DATA.Customers", "hub.Accounts", with_table_keys=True,
                      source_field="ERP_DATA.Customers.external_id",
                      target_field="hub.Accounts.external_id")
    assert aff.merge_affinity_rules([llm], [supplier, customer]) == [llm, supplier]
