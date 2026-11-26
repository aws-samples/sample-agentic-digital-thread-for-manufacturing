# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Identity-rule evidence gates (``agents/resolve_gates.py``).

Live finding (2026-09-18 workshop test): the Resolve checkpoint proposed junk
identity rules from value overlap on MEASURES — six ``cost`` rules pairing a
maintenance system's ``unit_cost`` with an ERP's ``standard_cost`` /
``list_price`` / ``unit_price`` at 7–27% overlap sharing ``120.00``,
``45.00``, ``420.00``; an ``inspection`` rule pairing a ``log_id`` with a
``sample_number`` on the shared small integers ``1 2 3 4 5``; and a
``schedule`` rule pairing two date columns on ONE shared date at 14%.

The gate is pure and generic: it reads the resolver's own measured evidence
(``overlap_count`` / ``ratio`` / ``examples``) plus the rule's concept and
field names — never a schema, domain or naming convention.
"""
from __future__ import annotations

import pytest

from agents import resolve_gates
from agents.resolve_gates import gate_overlap_rules, rule_rejection

NEW_SYS, ERP, MES = "cmms-a", "erp-a", "mes-a"


def _rule(concept, source_field, target_system, target_field, confidence=0.7):
    return {"concept": concept, "source_field": source_field, "source_pattern": "*",
            "target_system": target_system, "target_field": target_field,
            "target_pattern": "*", "confidence": confidence, "reasoning": "measured overlap"}


def _evidence(concept, source_field, target_system, target_field, examples, *,
              a_tokens, b_tokens):
    shared = len(examples)
    return {"concept": concept, "source_field": source_field, "target_system": target_system,
            "target_field": target_field, "overlap_count": shared, "examples": list(examples),
            "unchanged_count": shared, "a_tokens": a_tokens, "b_tokens": b_tokens,
            "ratio": round(shared / (min(a_tokens, b_tokens) or 1), 3)}


# ── the three live junk shapes ───────────────────────────────────────────────

COST_TARGETS = ("products.standard_cost", "products.list_price",
                "order_line_items.unit_price", "inventory_items.unit_cost")


@pytest.mark.parametrize("target_field", COST_TARGETS)
def test_decimal_measure_overlap_is_rejected(target_field):
    rules = [_rule("cost", "spare_parts.unit_cost", ERP, target_field)]
    evidence = [_evidence("cost", "spare_parts.unit_cost", ERP, target_field,
                          ["120.00", "45.00", "420.00"], a_tokens=11, b_tokens=40)]
    out = gate_overlap_rules({"rules": rules, "evidence": evidence})
    assert out["rules"] == []
    assert len(out["rejected_rules"]) == 1
    reason = out["rejected_rules"][0]["rejected_reason"]
    assert "cost" in reason or "measure" in reason


def test_small_dense_integer_range_is_rejected():
    rules = [_rule("inspection", "work_order_log.log_id", MES, "quality_checks.sample_number")]
    evidence = [_evidence("inspection", "work_order_log.log_id", MES, "quality_checks.sample_number",
                          ["1", "2", "3", "4", "5"], a_tokens=5, b_tokens=5)]
    out = gate_overlap_rules({"rules": rules, "evidence": evidence})
    assert out["rules"] == []
    assert "integer" in out["rejected_rules"][0]["rejected_reason"]


def test_single_shared_date_is_rejected():
    rules = [_rule("schedule", "pm_schedules.next_due_date", ERP, "orders.scheduled_start")]
    evidence = [_evidence("schedule", "pm_schedules.next_due_date", ERP, "orders.scheduled_start",
                          ["2026-03-14"], a_tokens=7, b_tokens=30)]
    out = gate_overlap_rules({"rules": rules, "evidence": evidence})
    assert out["rules"] == []


# ── generic gate semantics ───────────────────────────────────────────────────

def test_low_ratio_or_too_few_shared_values_is_rejected_even_for_string_identifiers():
    rules = [_rule("work-order", "work_orders.wo_number", ERP, "orders.order_ref")]
    weak = [_evidence("work-order", "work_orders.wo_number", ERP, "orders.order_ref",
                      ["WO-1001", "WO-1002"], a_tokens=20, b_tokens=20)]      # 2 shared, 10%
    assert gate_overlap_rules({"rules": rules, "evidence": weak})["rules"] == []
    sparse = [_evidence("work-order", "work_orders.wo_number", ERP, "orders.order_ref",
                        ["WO-1001", "WO-1002", "WO-1003"], a_tokens=20, b_tokens=20)]  # 3 shared, 15%
    assert gate_overlap_rules({"rules": rules, "evidence": sparse})["rules"] == []


@pytest.mark.parametrize("concept", ["cost", "unit-price", "quantity", "order_date",
                                     "start-time", "schedule", "timestamp"])
def test_measure_and_temporal_concepts_are_never_proposed(concept):
    rules = [_rule(concept, "t.some_code", ERP, "u.some_code")]
    strong = [_evidence(concept, "t.some_code", ERP, "u.some_code",
                        ["A-1", "A-2", "A-3", "A-4"], a_tokens=5, b_tokens=6)]
    assert gate_overlap_rules({"rules": rules, "evidence": strong})["rules"] == []


def test_rule_without_measured_evidence_needs_identifier_like_fields_on_both_sides():
    guess_ok = _rule("part", "parts.part_number", ERP, "items.sku_code")
    guess_bad = _rule("part", "parts.description", ERP, "items.label")
    out = gate_overlap_rules({"rules": [guess_ok, guess_bad], "evidence": []})
    assert out["rules"] == [guess_ok]
    assert [r["source_field"] for r in out["rejected_rules"]] == ["parts.description"]


def test_evidence_matches_rule_when_llm_drops_the_table_prefix():
    rules = [_rule("work-order", "wo_number", ERP, "order_ref")]
    strong = [_evidence("work-order", "work_orders.wo_number", ERP, "orders.order_ref",
                        ["WO-1001", "WO-1002", "WO-1003", "WO-1004"], a_tokens=6, b_tokens=8)]
    assert len(gate_overlap_rules({"rules": rules, "evidence": strong})["rules"]) == 1


# ── the positive case: string identifiers with strong overlap still pass ────

def test_string_identifier_with_strong_overlap_is_proposed():
    rule = _rule("work-order", "work_orders.wo_number", ERP, "orders.order_ref")
    strong = [_evidence("work-order", "work_orders.wo_number", ERP, "orders.order_ref",
                        ["WO-1001", "WO-1002", "WO-1003", "WO-1004", "WO-1005"],
                        a_tokens=8, b_tokens=10)]                              # 5 shared, 62%
    out = gate_overlap_rules({"rules": [rule], "evidence": strong})
    assert out["rules"] == [rule]
    assert out["rejected_rules"] == []
    assert out["evidence"] == strong                       # evidence itself is untouched


def test_gate_preserves_other_result_keys_and_tolerates_missing_lists():
    out = gate_overlap_rules({"recommendations": [{"x": 1}]})
    assert out["recommendations"] == [{"x": 1}]
    assert out["rules"] == [] and out["rejected_rules"] == []


@pytest.mark.parametrize("values, expected", [
    (["120.00", "45.00", "420.00"], "decimal measure"),
    (["1", "2", "3", "4", "5"], "small dense integer range"),
    (["2026-03-14", "2026-03-15", "2026-03-16"], "date/timestamp"),
    (["2026-03-14T10:00:00Z", "2026-03-15 11:30:00", "2026-03-16T09:00:00"], "date/timestamp"),
    (["true", "false"], "boolean"),
    (["WO-1001", "WO-1002", "WO-1003"], ""),
    (["10045", "20077", "31090"], ""),          # sparse large integers can be identifiers
])
def test_value_shape_classifier(values, expected):
    assert resolve_gates.non_identifier_shape(values) == expected


# ── wiring: the resolve job applies the gate to the LLM rules only ──────────

def test_run_resolve_job_gates_llm_rules_before_affinity_merge(monkeypatch):
    import agents.onboard as onboard
    from agents.models import OnboardResolveRequest

    junk = _rule("cost", "spare_parts.unit_cost", ERP, "products.standard_cost")
    evidence = [_evidence("cost", "spare_parts.unit_cost", ERP, "products.standard_cost",
                          ["120.00", "45.00", "420.00"], a_tokens=11, b_tokens=40)]
    monkeypatch.setattr(onboard, "_do_resolve", lambda body: {
        "rules": [junk], "evidence": evidence, "recommendations": [], "value_mappings": []})
    seen: dict = {}

    def _fake_affinity(body, result):
        seen["rules_in"] = list(result["rules"])
        return result

    monkeypatch.setattr(onboard, "_augment_with_affinity_rules", _fake_affinity)
    monkeypatch.setattr(onboard, "_join_candidate_summaries", lambda disc: [])
    import agents.join_discovery as jd
    monkeypatch.setattr(jd, "discover_joins_for_system", lambda sid: {"joins": [], "crosswalks": []})

    onboard._run_resolve_job("job-x", OnboardResolveRequest(system_id=NEW_SYS, mappings=[]))
    job = onboard._resolve_jobs.pop("job-x")
    assert job["status"] == "done"
    assert seen["rules_in"] == []                       # gated BEFORE affinity rules join
    assert job["result"]["rules"] == []
    assert job["result"]["rejected_rules"][0]["concept"] == "cost"


def test_rule_rejection_reason_names_the_gate():
    junk = _rule("inspection", "work_order_log.log_id", MES, "quality_checks.sample_number")
    ev = _evidence("inspection", "work_order_log.log_id", MES, "quality_checks.sample_number",
                   ["1", "2", "3", "4", "5"], a_tokens=5, b_tokens=5)
    assert rule_rejection(junk, [ev])
    assert rule_rejection(_rule("work-order", "a.wo_number", ERP, "b.order_ref"), [
        _evidence("work-order", "a.wo_number", ERP, "b.order_ref",
                  ["WO-1", "WO-2", "WO-3", "WO-4"], a_tokens=5, b_tokens=5)]) == ""
