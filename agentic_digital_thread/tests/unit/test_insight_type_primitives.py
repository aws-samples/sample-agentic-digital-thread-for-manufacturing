# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the ``insight`` proposal-type primitives.

An insight is a NARRATIVE finding — informational context only, never a graph
rule. These prove its three pure primitives (Wave 1 / Task 1.1):

  - ``proposal_key.canonical_keys`` supports ``insight`` (ADR-0051 P1 grain:
    subject = ``from_system|concept|digest(annotation)`` — per-finding, so
    distinct findings coexist; target repeats the digest);
  - ``proposal_scoring.is_table_column_shaped`` — the ingestion shape-gate
    predicate that separates real ``table.column`` references from sentences;
  - ``proposal_scoring.score_proposal`` pins insight confidence to ``low`` so
    it can never enter a high-confidence accept path.

Both modules are import-pure (stdlib only) — no AWS, no network.
"""
from __future__ import annotations

import pytest

from agents.proposal_key import canonical_keys
from agents.proposal_scoring import (
    is_degenerate_join_proposal,
    is_table_column_shaped,
    score_proposal,
)


# --------------------------------------------------------------------------- #
# canonical_keys("insight", ...)
# --------------------------------------------------------------------------- #
def test_insight_canonical_keys_stable_and_type_prefixed():
    k1 = canonical_keys("insight", from_system="erp", concept="correlation",
                        annotation="Operator IDs correlate across MES and ERP")
    k2 = canonical_keys("insight", from_system="erp", concept="correlation",
                        annotation="Operator IDs correlate across MES and ERP")
    assert k1 == k2  # deterministic
    # ADR-0051 P1: the finding digest is folded into the SUBJECT (per-finding
    # grain) — subject = system|topic|digest(annotation).
    assert k1.subject_key.startswith("insight|erp|correlation|")
    digest = k1.subject_key.rsplit("|", 1)[-1]
    assert len(digest) == 16 and all(c in "0123456789abcdef" for c in digest)
    assert k1.target_key.startswith(k1.subject_key + ">>")


def test_insight_same_annotation_same_target_key():
    k1 = canonical_keys("insight", from_system="erp", concept="correlation", annotation="X")
    k2 = canonical_keys("insight", from_system="erp", concept="correlation", annotation="X")
    assert k1.target_key == k2.target_key


def test_insight_different_annotation_is_a_distinct_subject():
    k1 = canonical_keys("insight", from_system="erp", concept="correlation", annotation="X")
    k2 = canonical_keys("insight", from_system="erp", concept="correlation", annotation="Y")
    # ADR-0051 P1 (supersedes the pre-0051 pin): a DIFFERENT narrative on the
    # same system+topic is its own SUBJECT — distinct findings coexist instead
    # of superseding each other into ADR-0042 quarantine.
    assert k1.subject_key != k2.subject_key
    assert k1.target_key != k2.target_key
    common = "insight|erp|correlation|"
    assert k1.subject_key.startswith(common) and k2.subject_key.startswith(common)


def test_insight_annotation_digest_normalizes_case_and_whitespace():
    k1 = canonical_keys("insight", from_system="erp", concept="correlation", annotation="  X  ")
    k2 = canonical_keys("insight", from_system="erp", concept="correlation", annotation="x")
    assert k1.target_key == k2.target_key


def test_insight_subject_varies_with_system_and_concept():
    base = canonical_keys("insight", from_system="erp", concept="correlation", annotation="X")
    other_sys = canonical_keys("insight", from_system="mes-mil", concept="correlation", annotation="X")
    other_concept = canonical_keys("insight", from_system="erp", concept="quality", annotation="X")
    assert base.subject_key != other_sys.subject_key
    assert base.subject_key != other_concept.subject_key


def test_insight_has_no_table_column_lhs():
    # from_field plays no part in the insight key — narrative findings have no
    # table.column LHS.
    k1 = canonical_keys("insight", from_system="erp", concept="correlation",
                        annotation="X", from_field="a.b")
    k2 = canonical_keys("insight", from_system="erp", concept="correlation",
                        annotation="X", from_field="")
    assert k1 == k2


def test_unknown_type_still_raises():
    with pytest.raises(ValueError):
        canonical_keys("not_a_type", from_system="erp")


# --------------------------------------------------------------------------- #
# is_table_column_shaped
# --------------------------------------------------------------------------- #
def test_table_column_shaped_accepts_single_reference():
    assert is_table_column_shaped("work_orders.operator_id") is True


def test_table_column_shaped_accepts_deeper_dotted_struct_path():
    assert is_table_column_shaped("a.b.c") is True


def test_table_column_shaped_rejects_sentence():
    assert is_table_column_shaped("Operator IDs in MES correlate with ERP") is False


def test_table_column_shaped_rejects_no_dot():
    assert is_table_column_shaped("orders") is False


def test_table_column_shaped_rejects_empty_and_none():
    assert is_table_column_shaped("") is False
    assert is_table_column_shaped(None) is False


def test_table_column_shaped_rejects_commas_and_whitespace():
    assert is_table_column_shaped("orders.id,orders.name") is False
    assert is_table_column_shaped("orders. id") is False
    assert is_table_column_shaped("orders.id\t") is False
    assert is_table_column_shaped("orders.id\n") is False


def test_table_column_shaped_rejects_empty_segments():
    assert is_table_column_shaped("a..b") is False
    assert is_table_column_shaped("a.") is False
    assert is_table_column_shaped(".a") is False


def test_table_column_shaped_rejects_bad_segment_characters():
    assert is_table_column_shaped("orders.id;drop") is False
    assert is_table_column_shaped('orders."id"') is False


def test_table_column_shaped_allows_identifier_charset():
    assert is_table_column_shaped("mes-mil_v2.op$id-1") is True


def test_table_column_shaped_rejects_over_length():
    long_ref = "t" * 120 + "." + "c" * 20  # 141 chars > 128
    assert is_table_column_shaped(long_ref) is False
    ok_ref = "t" * 100 + "." + "c" * 27  # exactly 128
    assert is_table_column_shaped(ok_ref) is True


# --------------------------------------------------------------------------- #
# score_proposal insight branch
# --------------------------------------------------------------------------- #
def test_insight_scores_low_confidence():
    s = score_proposal({"proposed_type": "insight"})
    assert s["ai_confidence"] == "low"
    assert s["ai_recommendation"] == "💡 Finding — informational; never becomes a graph rule"


def test_insight_low_even_with_high_reported_confidence():
    # Confidence is pinned low regardless of the LLM's self-reported score —
    # insights must never enter a high-confidence accept path.
    s = score_proposal({"proposed_type": "insight", "confidence": 0.99,
                        "from_value": "x", "to_value": "y"})
    assert s["ai_confidence"] == "low"


# --------------------------------------------------------------------------- #
# insight is NOT join-like
# --------------------------------------------------------------------------- #
def test_insight_never_flagged_as_degenerate_join():
    # Even a knowledge-graph-named insight is not a join-like type — the
    # degeneracy predicate must ignore it (ingestion handles insight drops).
    assert is_degenerate_join_proposal(
        from_system="Knowledge Graph", from_field="",
        to_system="Knowledge Graph", to_field="",
        proposed_type="insight",
    ) is False
