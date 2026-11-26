# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Wildcard grammar contract for IdentityRule patterns (ADR-0086, Wave 0).

``{*}`` marks the part of an identifier that varies across several records
sharing one identifier core. These tests pin the grammar constants and the
conditional route-seed note that every other wave codes against.
"""
from __future__ import annotations

import re

import pytest

from agents.identifier_translation import (
    WILDCARD,
    WILDCARD_SEED_NOTE,
    has_wildcard,
    wildcard_note,
)


def _rule(source_pattern="{id}", target_pattern="PRT-{id}-R-A"):
    return {"source_system": "sys-a", "source_field": "code",
            "target_system": "sys-b", "target_field": "item_id",
            "source_pattern": source_pattern, "target_pattern": target_pattern}


def test_wildcard_token_is_braced_star():
    assert WILDCARD == "{*}"


@pytest.mark.parametrize("pattern,expected", [
    ("PRT-{id}-R-{*}", True),
    ("{*}", True),
    ("PRT-{id}-R-A", False),
    ("*", False),          # bare-star direct-match convention is unrelated
    ("", False),
    (None, False),
    (42, False),
])
def test_has_wildcard(pattern, expected):
    assert has_wildcard(pattern) is expected


def test_note_empty_for_no_rules():
    assert wildcard_note([]) == ""
    assert wildcard_note(None) == ""


def test_note_empty_for_non_wildcard_rules():
    rules = [_rule(), _rule("ITM-{id}", "PRT-{id}"), _rule("", "")]
    assert wildcard_note(rules) == ""


def test_note_tolerates_non_dict_entries():
    assert wildcard_note([None, "PRT-{id}-R-{*}", 3, ["{*}"]]) == ""
    assert wildcard_note([None, _rule(target_pattern="PRT-{id}-R-{*}")]) \
        == WILDCARD_SEED_NOTE


@pytest.mark.parametrize("rule", [
    _rule(target_pattern="PRT-{id}-R-{*}"),
    _rule(source_pattern="PRT-{id}-R-{*}", target_pattern="{id}"),
])
def test_note_once_for_wildcard_either_direction(rule):
    note = wildcard_note([_rule(), rule])
    assert note == WILDCARD_SEED_NOTE
    assert note.count("IDENTITY-RULE WILDCARDS:") == 1


def test_note_once_for_several_wildcard_rules():
    rules = [_rule(target_pattern="PRT-{id}-R-{*}"),
             _rule(source_pattern="DOC-{*}-{id}", target_pattern="{id}")]
    assert wildcard_note(rules).count("IDENTITY-RULE WILDCARDS:") == 1


def test_note_shape():
    assert WILDCARD_SEED_NOTE.startswith("IDENTITY-RULE WILDCARDS: a {*} ")
    assert WILDCARD_SEED_NOTE.endswith("\n")
    assert WILDCARD_SEED_NOTE.count("\n") == 1
    assert "Never fill {*} with a value yourself." in WILDCARD_SEED_NOTE
    assert "translated_identifiers" in WILDCARD_SEED_NOTE
    assert '"every identifier beginning (or matching) the fixed part"' in WILDCARD_SEED_NOTE


def test_note_chooses_by_the_event_date_before_the_current_record():
    assert ("when the question concerns a past event, use the record whose effective or "
            "release window covers that event's date; otherwise use the one the target "
            "marks current, released or active; always state which record you used and "
            "why.\n") in WILDCARD_SEED_NOTE


def test_note_carries_no_query_fragment():
    assert "{id}" not in WILDCARD_SEED_NOTE
    assert "%" not in WILDCARD_SEED_NOTE
    for word in ("LIKE", "SELECT"):
        assert not re.search(rf"\b{word}\b", WILDCARD_SEED_NOTE, re.IGNORECASE)
