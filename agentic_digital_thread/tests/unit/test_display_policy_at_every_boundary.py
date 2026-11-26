# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every human-readable text shows a sampled value under one display policy.

Resolve's evidence keeps every value it sampled (``examples``, the
translation checks' ``examples``, a check's ``odd_value``), so the gates and
the measurement read them whole. What a person reads is limited by one
policy, the server's (``resolve_evidence._fit_for_a_prompt``: at most
``MAX_PROMPT_VALUE_CHARS`` characters and no control, format or line
separator character): the resolve prompts, the checkpoint and the rejection
reasons. The checkpoint renders the display-safe ``shown_examples`` the
server adds to each evidence row and translation check, as they are. A value
a reason would quote is replaced by a fixed marker when the policy leaves it
out; provision's reasons, which show the first 40 characters of a value,
apply the policy to what they show.

Fixture names are generic.
"""
from __future__ import annotations

import pytest

import agents.onboard as onboard
import agents.resolve_evidence as rev
from agents import resolve_gates
from tests.unit.test_resolve_rules_are_storable import (
    NEW, PEER, _mapping, _model_rule, _peer_row, wizard_resolve)

LONG_TOKEN = "J-" + "7" * 300
BIDI = "J-9\u202eevil"
NOT_SHOWN = [LONG_TOKEN, BIDI, "J-8\nforged line", "J-6\u200bzero-width"]
IDS = ["long", "bidi", "newline", "zero-width"]
#: What a reason says in place of a value the policy leaves out.
MARKER = "(not shown: over 256 characters, or holding a control or format character)"


def _resolve(monkeypatch, new_values, target_values, patterns):
    return wizard_resolve(
        monkeypatch, mappings=[_mapping("jobs", "job_ref", "work-order")],
        peer_rows=[_peer_row("orders", "order_ref", "work-order")],
        samples={(NEW, "jobs", "job_ref"): list(new_values),
                 (PEER, "orders", "order_ref"): list(target_values)},
        model_rules=[_model_rule("jobs.job_ref", "orders.order_ref", "work-order", patterns)])


def test_an_evidence_row_shows_only_display_safe_examples_and_keeps_every_one(monkeypatch):
    shared = ["J-1", "J-2", LONG_TOKEN, BIDI]
    result = _resolve(monkeypatch, shared, shared, ("", ""))
    (row,) = result["evidence"]
    assert LONG_TOKEN in row["examples"] and BIDI in row["examples"]
    assert row["shown_examples"] == ["J-1", "J-2"]
    assert [r["source_field"] for r in result["rules"]] == ["jobs.job_ref"]


def test_a_translation_check_shows_only_display_safe_examples(monkeypatch):
    digits = ["1", "2", "3", "1" * 300]
    result = _resolve(monkeypatch, [f"WO-{d}" for d in digits], [f"MFG-{d}" for d in digits],
                      ("WO-{n}", "MFG-{n}"))
    (check,) = result["transform_checks"]
    assert f"MFG-{'1' * 300}" in check["examples"]
    assert check["shown_examples"] == ["MFG-1", "MFG-2", "MFG-3"]
    assert [r["source_field"] for r in result["rules"]] == ["jobs.job_ref"]


def _disagreeing(odd):
    row = {"concept": "work-order", "source_field": "jobs.job_ref", "target_system": PEER,
           "target_field": "orders.order_ref", "overlap_count": 0, "unchanged_count": 0,
           "examples": [], "ratio": 0.0, "a_tokens": 3, "b_tokens": 3}
    check = {"concept": "work-order", "source_field": "jobs.job_ref", "target_system": PEER,
             "target_field": "orders.order_ref", "source_pattern": "WO-{n}",
             "target_pattern": "MFG-{n}", "a_tokens": 3, "fit": 3, "translated": 3,
             "overlap_count": 3, "examples": [], "ratio": 1.0,
             "readers": resolve_gates.READERS_DIFFER, "odd_value": odd}
    rule = _model_rule("jobs.job_ref", "orders.order_ref", "work-order", ("WO-{n}", "MFG-{n}"))
    rule["target_system"] = PEER
    return resolve_gates.rule_rejection(rule, [], [row], [check])


@pytest.mark.parametrize("odd", NOT_SHOWN, ids=IDS)
def test_a_reader_disagreement_reason_does_not_quote_a_value_unfit_to_show(odd):
    reason = _disagreeing(odd)
    assert reason == ("measured through its pattern: the two identifier readers would read value "
                      f"{MARKER} differently with 'WO-{{n}}' -> 'MFG-{{n}}'")


def test_a_reader_disagreement_reason_still_quotes_an_ordinary_value():
    assert "would read value 'WO-7a' differently" in _disagreeing("WO-7a")


@pytest.mark.parametrize("value", [BIDI, "J-8\nforged line", "\x1b[31mJ-5"],
                         ids=["bidi", "newline", "escape"])
def test_provision_reasons_do_not_quote_a_value_unfit_to_show(value):
    assert onboard._quoted([value, "A-1"]) == f"{MARKER}, A-1"
    assert MARKER in onboard._readers_disagree_reason("WO-{n}", "MFG-{n}", value, "t.c")
    assert value not in onboard._readers_disagree_reason("WO-{n}", "MFG-{n}", value, "t.c")


def test_provision_reasons_still_show_the_first_40_characters_of_a_long_ordinary_value():
    assert onboard._quoted([LONG_TOKEN]) == LONG_TOKEN[:40]
    assert f"value '{LONG_TOKEN[:40]}' differently" in onboard._readers_disagree_reason(
        "WO-{n}", "MFG-{n}", LONG_TOKEN, "t.c")


def test_the_display_policy_is_one_function_on_the_server():
    assert rev.VALUE_NOT_SHOWN == MARKER
    assert rev.shown_text(BIDI) == MARKER and rev.shown_text("J-1") == "J-1"
    assert rev.quoted_text(LONG_TOKEN) == MARKER
    assert rev.quoted_text(LONG_TOKEN, 40) == f"'{LONG_TOKEN[:40]}'"
