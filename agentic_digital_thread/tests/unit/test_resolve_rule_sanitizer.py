# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Both resolve parsers strip the server-owned evidence fields off a model
rule, so a model cannot forge measured evidence by writing one itself.

The concept-pass prompt and the chat's resolve prompt ask the model for a
rule's concept, source/target system and field, patterns, confidence and
reasoning only. They never ask for ``value_check``, ``method`` / ``transform``
(an affinity or exact-agreement rule) or ``shared_count`` (its count): those
are written by the deterministic passes. ``resolve_gates.sanitize_model_rules``
drops them from every parsed model rule, and both parsers run it, so a
model-written evidence field never reaches a gate, the chat checkpoint, the
stored session or provision, and a model-written exact method never poses as a
deterministic pass's.
"""
from __future__ import annotations

import json

from agents import resolve_gates
from agents.onboard_agent import _parse_resolve_response


def _rule(**over) -> dict:
    base = {"concept": "lot-number", "source_system": "cell-hub",
            "source_field": "jobs.ext_lot_code", "source_table": "jobs", "source_pattern": "",
            "target_system": "ledger", "target_field": "lines.lot_ref", "target_table": "lines",
            "target_pattern": "", "confidence": 0.9, "reasoning": "both carry the lot code"}
    base.update(over)
    return base


# ── the pure sanitizer ──────────────────────────────────────────────────────

def test_the_server_owned_fields_are_the_six_the_prompts_never_ask_for():
    assert resolve_gates.SERVER_OWNED_RULE_FIELDS == (
        "value_check", "method", "transform", "shared_count", resolve_gates.MEASURED_PAIR_KEY,
        resolve_gates.MEASURED_AS_KEY)


def test_every_server_owned_field_is_dropped_and_the_rest_kept():
    forged = _rule(value_check={"result": "no_shared_value", "asked": 99, "note": "L-1, L-2"},
                   method="value_exact_match", transform="identity", shared_count=7)
    (out,) = resolve_gates.sanitize_model_rules([forged])
    assert out == _rule()  # exactly the model-writable keys, nothing added
    assert forged.get("value_check")  # the input is not mutated in place


def test_a_rule_with_none_of_the_fields_passes_through_unchanged():
    rule = _rule()
    (out,) = resolve_gates.sanitize_model_rules([rule])
    assert out is rule  # untouched, same object


def test_a_non_dict_rule_and_an_empty_list_are_left_alone():
    assert resolve_gates.sanitize_model_rules(["not-a-rule", 3]) == ["not-a-rule", 3]
    assert resolve_gates.sanitize_model_rules([]) == []
    assert resolve_gates.sanitize_model_rules(None) == []


# ── the chat parser ──────────────────────────────────────────────────────────

def test_the_chat_parser_strips_a_model_written_value_check():
    """A model that copies prompt samples into a ``value_check`` on its rule:
    the parser drops it, so it never reaches the chat checkpoint or the
    stored session. Every model-writable key is kept."""
    text = json.dumps({"rules": [_rule(
        value_check={"result": "no_shared_value", "asked": 70, "note": "copied L-1001, L-1002"})],
        "value_mappings": []})
    rules, _vm = _parse_resolve_response(text)
    assert rules == [_rule()]
    assert "value_check" not in rules[0]


def test_the_chat_parser_strips_a_model_written_exact_method():
    """A model-written ``value_exact_match`` with its count and transform is
    stripped, so a forged method cannot earn a measured-method rule at
    provision."""
    text = json.dumps({"rules": [_rule(method="value_exact_match", shared_count=40,
                                       transform="identity")], "value_mappings": []})
    rules, _vm = _parse_resolve_response(text)
    assert rules == [_rule()]
    assert not any(k in rules[0] for k in ("method", "shared_count", "transform"))


def test_the_chat_parser_keeps_a_clean_rule_as_it_is():
    text = json.dumps({"rules": [_rule()], "value_mappings": [{"method": "semantic_similarity"}]})
    rules, vm = _parse_resolve_response(text)
    assert rules == [_rule()]
    # value_mappings is a separate array the prompt does ask ``method`` for.
    assert vm == [{"method": "semantic_similarity"}]
