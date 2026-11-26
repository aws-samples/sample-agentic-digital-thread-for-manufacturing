# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The resolve step's affinity merge keeps two dotted columns apart.

A flattened struct column (``billing.customer.id`` in an Athena table) or a
tag path is sampled by its whole name, so the affinity pass can propose a
rule for each of two columns that end the same way. The merge pairs an
affinity rule with an LLM rule for the same field pair; for a dotted column
the pair is the whole column, with its table, on either side, so the rule of
one column never replaces or hides the rule of another. A rule on single-dot
columns pairs exactly as before.
"""
from __future__ import annotations

import pytest

from agents import identity_affinity as aff
from agents.identity_affinity import SampledField

RUNS = tuple(f"RUN-2026092{d}-9{d}{k:03d}" for d in (1, 2, 3) for k in range(6))
BATCHES = tuple(f"B-{n}" for n in range(2800, 2951))
LOTS = tuple(f"LOT-2026-0{n}" for n in range(2847, 2897))


def _field(system, table, field, concept, values):
    return SampledField(system, table, field, concept, tuple(values))


def _probe_over(*columns):
    full = {(c.system_id, c.table, c.field): set(c.values) for c in columns}

    def probe(field, values):
        return sorted(set(values) & full[(field.system_id, field.table, field.field)])
    return probe


def _llm(source_field, target_field="orders.id", **over):
    rule = {"concept": "customer", "source_field": source_field, "target_system": "erp",
            "target_field": target_field, "source_pattern": "", "target_pattern": "",
            "method": "", "reasoning": "llm"}
    rule.update(over)
    return rule


BILLING = _field("sys-a", "T", "billing.customer.id", "customer", RUNS)
SHIPPING = _field("sys-a", "T", "shipping.customer.id", "customer", RUNS)
ORDERS = _field("erp", "orders", "id", "customer", RUNS)


def _exact(source, target=ORDERS):
    rule = aff.exact_rule(source, target, _probe_over(source, target))
    assert rule is not None and rule["method"] == aff.EXACT_METHOD
    return rule


# ── two dotted source columns with one last segment ────────────────────────

def test_an_exact_rule_on_one_dotted_column_keeps_the_llm_rule_on_another():
    llm = _llm("T.billing.customer.id")
    shipping = _exact(SHIPPING)
    merged = aff.merge_affinity_rules([llm], [shipping])
    assert merged == [llm, shipping]
    assert aff.exact_replacement_notes([llm], merged) == []


def test_two_sampled_dotted_columns_with_one_target_both_keep_their_rule():
    billing = _field("sys-a", "T", "billing.customer.id", "batch", BATCHES)
    shipping = _field("sys-a", "T", "shipping.customer.id", "batch", BATCHES)
    lots = _field("erp", "orders", "id", "lot-number", LOTS)
    found = aff.find_affinity_rules([billing, shipping], [lots])
    assert [r["source_field"] for r in found] == ["T.billing.customer.id",
                                                  "T.shipping.customer.id"]
    assert aff.merge_affinity_rules([], found) == found


@pytest.mark.parametrize("source_field,source_table", [
    ("T.billing.customer.id", None),
    ("T.billing.customer.id", "T"),
    ("billing.customer.id", None),
    ("billing.customer.id", "T"),
    ("db.T.billing.customer.id", None),
])
def test_an_exact_rule_replaces_only_the_llm_rule_on_its_own_column(source_field, source_table):
    over = {"source_table": source_table} if source_table else {}
    billing_llm = _llm(source_field, **over)
    shipping_llm = _llm("T.shipping.customer.id")
    exact = _exact(BILLING)
    merged = aff.merge_affinity_rules([billing_llm, shipping_llm], [exact])
    assert merged == [exact, shipping_llm]
    (note,) = aff.exact_replacement_notes([billing_llm, shipping_llm], merged)
    assert note.startswith("T.billing.customer.id -> erp.orders.id (")


def test_an_llm_rule_naming_only_the_shared_segment_is_not_replaced():
    llm = _llm("customer.id")
    merged = aff.merge_affinity_rules([llm], [_exact(BILLING), _exact(SHIPPING)])
    assert merged == [llm, _exact(BILLING), _exact(SHIPPING)]


# ── two dotted target columns with one last segment ────────────────────────

def test_two_dotted_target_columns_with_one_source_both_keep_their_rule():
    runs = _field("sys-a", "runs", "run_number", "customer", RUNS)
    header = _field("erp", "orders", "header.id", "customer", RUNS)
    line = _field("erp", "orders", "line.id", "customer", RUNS)
    found = [aff.exact_rule(runs, t, _probe_over(runs, t)) for t in (header, line)]
    assert [r["target_field"] for r in found] == ["orders.header.id", "orders.line.id"]
    llm = _llm("runs.run_number", target_field="orders.header.id")
    merged = aff.merge_affinity_rules([llm], found)
    assert merged == found
    (note,) = aff.exact_replacement_notes([llm], merged)
    assert note.startswith("runs.run_number -> erp.orders.header.id (")


# ── single-dot rules pair exactly as before ────────────────────────────────

def test_a_single_dot_llm_rule_still_wins_its_pair_beside_a_dotted_column():
    mes = _field("mes-ind", "production_runs", "batch_number", "batch", BATCHES)
    flat = _field("mes-ind", "T", "billing.customer.id", "batch", BATCHES)
    lots = _field("sys-b", "orders", "lot_ref", "lot-number", LOTS)
    found = aff.find_affinity_rules([mes, flat], [lots])
    assert len(found) == 2
    llm = {"concept": "batch", "source_field": "batch_number", "target_system": "sys-b",
           "target_field": "lot_ref", "source_pattern": "B-{id}",
           "target_pattern": "LOT-2026-0{id}", "confidence": 0.7}
    assert aff.merge_affinity_rules([llm], found) == [llm, found[1]]


@pytest.mark.parametrize("rule", [
    {"source_field": "orders.order_id", "target_system": "erp", "target_field": "wo.wo_id"},
    {"source_field": "order_id", "target_system": "erp", "target_field": "wo_id"},
    {"source_field": "mes.orders.order_id", "target_system": "erp", "target_field": "x.wo.wo_id"},
    {"source_field": "", "target_system": "", "target_field": ""},
    {},
])
def test_a_merge_key_without_dotted_columns_is_the_last_segment(rule):
    assert aff._merge_key(rule) == (str(rule.get("source_field", "")).split(".")[-1],
                                    str(rule.get("target_system", "")),
                                    str(rule.get("target_field", "")).split(".")[-1])
