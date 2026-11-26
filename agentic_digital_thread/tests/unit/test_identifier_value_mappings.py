# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The IDENTIFIER VALUE MAPPINGS block, pure part (ADR-0035 amendment 2026-10-18).

``identifier_translation.find_value_mappings`` decides which stored
ValueMapping rows a question reaches: a stored value equal to one of its
identifiers, a stored value whose trailing segments are one of them, and one
link further through a value of a matched row in the same system. The render
carries what is stored, one JSON object per line, and degrades line by line.
Rows here are written by the deterministic writer (``vm-equip-`` ids) unless a
test says otherwise; which rows the block may carry at all, and what it does
with hostile or ambiguous ones, is in ``test_value_mapping_block_safety.py``.
Synthetic values only.
"""
from __future__ import annotations

import json

import pytest

from agents import identifier_translation as it


def _vm(vid, fs, fv, ts, tv, *, ff="t.code", tf="u.code", method="value_format_match",
        confidence=0.95, provenance=None):
    confirmed = method in it.CONFIRMED_VALUE_MAPPING_METHODS
    row = {"id": ("subj-" if confirmed else "vm-equip-") + vid, "from_system": fs,
           "from_field": ff, "from_value": fv, "to_system": ts, "to_field": tf, "to_value": tv,
           "concept": "equipment", "method": method, "confidence": confidence,
           "provenance": provenance}
    # A steward-confirmed row carries the one-row accept's stamp.
    return {**row, **ONE_ROW_STAMP} if confirmed else row


#: What a one-row accept writes on the node (``value_mapping_trusted``).
ONE_ROW_STAMP = {"confirmation_mode": "single_row",
                 "accepted_by": "steward@example.test"}


def _pairs(items):
    return [(i["from_value"], i["to_value"], i["match"]) for i in items]


def _lines(text):
    """The JSON objects of a rendered block, in order."""
    return [json.loads(line[2:]) for line in text.splitlines() if line.startswith("- {")]


# ---------------------------------------------------------------- tokens and terms

def test_tokens_are_identifiers_in_order_without_case_repeats():
    question = "After the follow-up, is QX-101 or qx-101 linked to KB-7, or to re-run?"
    assert it.value_mapping_tokens(question) == ["QX-101", "KB-7"]


def test_tokens_are_capped():
    question = " ".join(f"A{i}-B{i}" for i in range(20))
    assert len(it.value_mapping_tokens(question)) == it.VALUE_MAPPING_TOKEN_CAP


def test_no_identifier_means_no_terms():
    assert it.value_mapping_terms("Which machines need maintenance?") == ([], [])
    assert it.value_mapping_terms(None) == ([], [])


def test_terms_are_the_lower_cased_tokens_and_every_separator_suffix():
    exact, suffixes = it.value_mapping_terms("Is QX-101 running?")
    assert exact == ["qx-101"]
    assert suffixes == [sep + "qx-101" for sep in it.SEGMENT_SEPARATORS]


# ---------------------------------------------------------------- matching

@pytest.mark.parametrize("value", ["QX-101", "qx-101", "Qx-101"])
def test_an_equal_value_is_an_exact_match(value):
    assert it.value_match(value, "QX-101") == it.MATCH_EXACT


@pytest.mark.parametrize("value", ["77-QX-101", "Mill_QX-101", "p/QX-101", "q.QX-101",
                                   "Mill Center QX-101", "9-Long-Name-qx-101"])
def test_a_value_ending_with_whole_segments_is_a_segment_match(value):
    assert it.value_match(value, "QX-101") == it.MATCH_SEGMENT


@pytest.mark.parametrize("value", ["AQX-101", "QX-1011", "QX-101-B", "QX-10", "", None, 101])
def test_a_value_matching_inside_a_segment_or_elsewhere_does_not_match(value):
    assert it.value_match(value, "QX-101") is None


def test_a_row_matches_on_either_side():
    rows = [_vm("v1", "sys-a", "QX-101", "sys-b", "K-55"),
            _vm("v2", "sys-c", "Z-1", "sys-d", "qx-101")]
    assert _pairs(it.find_value_mappings("Is QX-101 running?", rows)) == [
        ("QX-101", "K-55", "exact"), ("Z-1", "qx-101", "exact")]


def test_one_hop_through_a_value_in_the_same_system_and_no_further():
    rows = [_vm("v1", "sys-a", "QX-101", "sys-b", "K-55"),
            _vm("v2", "sys-b", "K-55", "sys-c", "M-9"),      # one hop through sys-b K-55
            _vm("v3", "sys-c", "M-9", "sys-d", "P-3"),       # two hops: never
            _vm("v4", "sys-q", "K-55", "sys-r", "N-1")]      # same value, other system: never
    items = it.find_value_mappings("Is QX-101 running?", rows)
    assert _pairs(items) == [("QX-101", "K-55", "exact"), ("K-55", "M-9", "hop")]
    hop = items[1]
    assert (hop["via"], hop["via_system"]) == ("K-55", "sys-b")


def test_the_hop_matches_the_shared_value_without_regard_to_case():
    rows = [_vm("v1", "sys-a", "QX-101", "sys-b", "K-55"),
            _vm("v2", "sys-c", "M-9", "sys-b", "k-55")]
    assert _pairs(it.find_value_mappings("QX-101?", rows))[1] == ("M-9", "k-55", "hop")


def test_hop_values_are_the_endpoints_of_the_direct_matches_only():
    rows = [_vm("v1", "sys-a", "QX-101", "sys-b", "K-55"),
            _vm("v2", "sys-b", "K-55", "sys-c", "M-9")]
    assert it.hop_values("Is QX-101 running?", rows) == ["k-55", "qx-101"]
    assert it.hop_values("Which machines?", rows) == []


def test_no_identifier_or_no_row_finds_nothing():
    rows = [_vm("v1", "sys-a", "QX-101", "sys-b", "K-55")]
    assert it.find_value_mappings("Which machines?", rows) == []
    assert it.find_value_mappings("QX-101?", []) == []
    assert it.find_value_mappings(None, rows) == []


def test_order_is_tier_then_exact_segment_hop_then_confidence():
    rows = [
        _vm("v-seg", "sys-j", "77-QX-101", "sys-b", "B-2", confidence=0.99),
        _vm("v-low", "sys-a", "QX-101", "sys-c", "C-3", confidence=0.5),
        _vm("v-high", "sys-a", "QX-101", "sys-d", "D-4", confidence=0.9),
        _vm("v-steward", "sys-e", "E-5", "sys-f", "Mill-QX-101", method="user_confirmed",
            confidence=0.8),
        _vm("v-evidence", "sys-g", "QX-101", "sys-h", "H-6", method="equipment_name_match",
            provenance="measured"),
        _vm("v-hop", "sys-d", "D-4", "sys-i", "I-7", confidence=1.0),
    ]
    assert [i["id"] for i in it.find_value_mappings("QX-101?", rows)] == [
        "subj-v-steward", "vm-equip-v-evidence", "vm-equip-v-high", "vm-equip-v-low",
        "vm-equip-v-seg", "vm-equip-v-hop"]


def test_duplicates_of_one_pair_collapse_to_the_best_row():
    rows = [_vm("gen", "sys-a", "QX-101", "sys-b", "K-55", confidence=0.95),
            _vm("learned", "sys-b", "k-55", "sys-a", "qx-101", method="user_confirmed",
                confidence=0.8)]
    items = it.find_value_mappings("QX-101?", rows)
    assert [i["id"] for i in items] == ["subj-learned"]


def test_a_value_that_cannot_be_rendered_unchanged_is_left_out():
    rows = [_vm("v1", "sys-a", "QX-101", "sys-b", "line one\nline two"),
            _vm("v2", "sys-a", "QX-101", "sys-c", "X" * (it.VALUE_MAPPING_VALUE_CAP + 1)),
            _vm("v3", "sys-a", "QX-101", "sys-d", "   "),
            _vm("v4", "sys-a", "QX-101", "", "D-1"),
            "not a row",
            _vm("v5", "sys-a", "QX-101", "sys-e", "E-1")]
    assert [i["id"] for i in it.find_value_mappings("QX-101?", rows)] == ["vm-equip-v5"]


def test_the_cap_keeps_the_best_and_counts_the_rest():
    rows = [_vm(f"v{i:02d}", "sys-a", "QX-101", f"sys-{i:02d}", f"T-{i}", confidence=i / 100)
            for i in range(it.VALUE_MAPPING_CAP + 3)]
    items = it.find_value_mappings("QX-101?", rows)
    assert len(items) == it.VALUE_MAPPING_CAP and items.omitted == 3
    assert items[0]["id"] == f"vm-equip-v{it.VALUE_MAPPING_CAP + 2:02d}"
    assert it.render_value_mappings(items).endswith(it.VALUE_MAPPINGS_OMITTED.format(n=3))


# ---------------------------------------------------------------- render

def test_full_line_is_one_json_object_of_the_stored_values_fields_method_and_match():
    rows = [_vm("v1", "sys-a", "QX-101", "sys-b", "K-55", ff="t.tag", tf="u.asset",
                provenance="same name in both")]
    text = it.render_value_mappings(it.find_value_mappings("QX-101?", rows))
    assert text == (it.VALUE_MAPPINGS_HEADER
                    + '- {"from_system":"sys-a","from_field":"t.tag","from_value":"QX-101",'
                      '"to_system":"sys-b","to_field":"u.asset","to_value":"K-55",'
                      '"method":"value_format_match","match":"exact","via":"QX-101"}\n')


def test_provenance_and_confidence_never_reach_the_model():
    rows = [_vm("v1", "sys-a", "QX-101", "sys-b", "K-55", confidence=0.87,
                provenance="the same name in both registries")]
    text = it.render_value_mappings(it.find_value_mappings("QX-101?", rows))
    assert "registries" not in text and "0.87" not in text
    assert set(_lines(text)[0]) == {"from_system", "from_field", "from_value", "to_system",
                                    "to_field", "to_value", "method", "match", "via"}


def test_segment_and_hop_lines_say_how_the_question_reached_them():
    rows = [_vm("v1", "sys-a", "77-QX-101", "sys-b", "K-55", confidence=None, ff=""),
            _vm("v2", "sys-b", "K-55", "sys-c", "M-9")]
    lines = _lines(it.render_value_mappings(it.find_value_mappings("QX-101?", rows)))
    assert lines[0] == {"from_system": "sys-a", "from_value": "77-QX-101", "to_system": "sys-b",
                        "to_field": "u.code", "to_value": "K-55", "method": "value_format_match",
                        "match": "segment", "via": "QX-101"}
    assert lines[1] == {"from_system": "sys-b", "from_field": "t.code", "from_value": "K-55",
                        "to_system": "sys-c", "to_field": "u.code", "to_value": "M-9",
                        "method": "value_format_match", "match": "hop", "via": "K-55",
                        "via_system": "sys-b"}


def _three():
    return it.find_value_mappings("QX-101?", [
        _vm(f"v{i}", "sys-a", "QX-101", f"sys-{i}", f"T-{i}", confidence=0.9 - i / 10)
        for i in range(3)])


def test_render_degrades_line_by_line_then_to_compact_lines_then_nothing():
    items = _three()
    whole = it.render_value_mappings(items)
    assert it.render_value_mappings(items, max_chars=len(whole)) == whole
    two = it.render_value_mappings(items, max_chars=len(whole) - 1)
    assert two.startswith(it.VALUE_MAPPINGS_HEADER)
    assert len(_lines(two)) == 2
    assert two.endswith(it.VALUE_MAPPINGS_OMITTED.format(n=1))
    whole_size, smallest = it.value_mapping_block_sizes(items)
    assert whole_size == len(whole)
    compact = it.render_value_mappings(items, max_chars=smallest)
    assert compact == (it.VALUE_MAPPINGS_COMPACT_HEADER
                       + '- {"from_system":"sys-a","from_value":"QX-101","to_system":"sys-0",'
                         '"to_value":"T-0","method":"value_format_match"}\n'
                       + it.VALUE_MAPPINGS_OMITTED.format(n=2))
    assert len(compact) == smallest
    assert it.render_value_mappings(items, max_chars=smallest - 1) == ""


def test_no_mapping_renders_nothing():
    assert it.render_value_mappings([]) == ""
    assert it.value_mapping_block_sizes([]) == (0, 0)


# ---------------------------------------------------------------- read back

def test_parse_reads_back_the_full_form():
    rows = [_vm("v1", "sys-a", "QX-101", "sys-b", "K-55", ff="s.t.tag", tf="",
                method="user_confirmed"),
            _vm("v2", "sys-b", "K-55", "sys-c", "Mill Center (east) 9")]
    text = "{} after the payload\n" + it.render_value_mappings(
        it.find_value_mappings("QX-101?", rows)) + "IDENTIFIER OWNERS (...):\n- other\n"
    assert it.parse_value_mappings(text) == [
        {"from_value": "QX-101", "from_system": "sys-a", "from_field": "s.t.tag",
         "to_value": "K-55", "to_system": "sys-b", "to_field": "",
         "method": "user_confirmed", "match": "exact", "via": "QX-101", "via_system": ""},
        {"from_value": "K-55", "from_system": "sys-b", "from_field": "t.code",
         "to_value": "Mill Center (east) 9", "to_system": "sys-c", "to_field": "u.code",
         "method": "value_format_match", "match": "hop", "via": "K-55", "via_system": "sys-b"}]


def test_parse_reads_back_the_compact_form():
    items = _three()
    compact = it.render_value_mappings(items, max_chars=it.value_mapping_block_sizes(items)[1])
    assert it.parse_value_mappings(compact) == [
        {"from_value": "QX-101", "from_system": "sys-a", "from_field": "", "to_value": "T-0",
         "to_system": "sys-0", "to_field": "", "method": "value_format_match"}]


def test_parse_of_a_text_without_the_block_is_empty():
    assert it.parse_value_mappings("IDENTIFIER TRANSLATIONS (x):\n- a\n") == []
    assert it.parse_value_mappings(None) == []
