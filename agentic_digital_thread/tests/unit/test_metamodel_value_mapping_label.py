# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The graph metamodel tells a ValueMapping from an IdentityRule.

Rehearsal walk, 2026-10-22: an answer said its machine-to-line match "came
from the knowledge graph's stored identity rules". The match was a stored
ValueMapping, and the trace showed the orchestrator reading the ValueMapping
node. The route seed labels the two kinds apart (``rules.value_mappings`` and
``rules.identity_rules``, the IDENTIFIER VALUE MAPPINGS block), but the live
graph metamodel the orchestrator reads at session start
(``metamodel.fetch_metamodel_text``, written on every provision by
``write_metamodel``) described a ValueMapping as a "Cross-system identity
fact", next to IdentityRule's "Field-level identity rule".

Each description now says what its kind is in its own words and how it
differs from the other: a ValueMapping is one explicit equivalence or
translation between two field values, an IdentityRule is a field-level
pattern. A ValueMapping is not always about one record: one can map a status
or category code in one system to its counterpart in another (an ERP order
status to an MES state, ``tests/unit/test_local_intelligence_summary.py``),
and ``self_learning._apply_value_mapping`` writes it as a row-level value
translation. So the description names record identity as one use, never as
what every mapping means. Wording only: no answer text, no dataset names.
Offline.
"""
from __future__ import annotations

import re

from agents import metamodel


def _description(label: str) -> str:
    return next(m["description"] for m in metamodel.META_NODES if m["label"] == label)


def _sentence_with(text: str, word: str) -> str:
    """The sentence of ``text`` that holds ``word``."""
    return next(s for s in re.split(r"(?<=\.)\s+", text) if word in s)


def test_a_value_mapping_is_not_described_as_an_identity_fact_or_rule():
    text = _description("ValueMapping").lower()
    assert "identity fact" not in text
    assert "identity rule" not in text


def test_a_value_mapping_is_one_explicit_equivalence_between_two_field_values():
    text = _description("ValueMapping")
    assert text.startswith("Value mapping: one explicit equivalence or translation "
                           "between two field values")
    assert "not a rule" in text
    # It names the other kind, so the difference is stated where it is read.
    assert "IdentityRule" in text
    # And it still documents where an equipment mapping carries its target.
    assert "to_element_id" in text and "to_path" in text


def test_a_status_or_category_mapping_is_not_read_as_one_record():
    # An ERP order status mapped to an MES state is two equivalent codes, not
    # one record: the description must not claim every mapping names a record.
    text = _description("ValueMapping")
    lowered = text.lower()
    assert "same record" not in lowered
    assert "is the same" not in lowered
    assert re.search(r"\b(status|state)\b", lowered) and "category" in lowered
    # Record identity is named as one use only.
    identity = _sentence_with(text, "record")
    assert re.search(r"\b(one use|can|may)\b", identity), identity


def test_an_identity_rule_says_it_is_a_pattern_not_a_value_pair():
    text = _description("IdentityRule")
    assert "pattern" in text
    assert "ValueMapping" in text
