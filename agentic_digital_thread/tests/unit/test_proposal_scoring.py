# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the shared proposal scorer + degeneracy predicate.

``agents.proposal_scoring`` is import-pure (stdlib only) — no AWS, no Neptune, no
network. These prove the single source of truth for "high confidence" and for
"degenerate field_annotation" that the route, auto-accept, and the bulk endpoints
all reuse.
"""
from __future__ import annotations

from agents.proposal_scoring import (
    score_proposal,
    is_degenerate_field_annotation,
    is_knowledge_graph_system,
    is_degenerate_join_proposal,
)

_UUID = "12345678-1234-1234-1234-123456789abc"


# --------------------------------------------------------------------------- #
# score_proposal
# --------------------------------------------------------------------------- #
def test_value_mapping_uuid_high_confidence():
    s = score_proposal({"proposed_type": "value_mapping", "to_value": _UUID,
                        "from_value": "x", "confidence": "0.9",
                        "verified": "true", "verified_scope": "system"})
    assert s["ai_confidence"] == "high"
    assert s["ai_recommendation"].startswith("✅")


def test_value_mapping_uuid_at_threshold_085_is_high():
    s = score_proposal({"proposed_type": "value_mapping", "to_value": _UUID,
                        "from_value": "x", "confidence": 0.85,
                        "verified": "true", "verified_scope": "system"})
    assert s["ai_confidence"] == "high"


def test_value_mapping_uuid_below_threshold_is_medium():
    s = score_proposal({"proposed_type": "value_mapping", "to_value": _UUID,
                        "from_value": "x", "confidence": 0.5})
    assert s["ai_confidence"] == "medium"


def test_value_mapping_semantic_match_is_medium():
    s = score_proposal({"proposed_type": "value_mapping", "to_value": "not-a-uuid",
                        "from_value": "x", "confidence": 0.82})
    assert s["ai_confidence"] == "medium"


def test_value_mapping_low_confidence_is_low():
    s = score_proposal({"proposed_type": "value_mapping", "to_value": "",
                        "from_value": "", "confidence": 0.1})
    assert s["ai_confidence"] == "low"


def test_field_annotation_single_dot_is_medium():
    s = score_proposal({"proposed_type": "field_annotation", "from_field": "orders.status"})
    assert s["ai_confidence"] == "medium"


def test_field_annotation_bad_reference_is_low():
    s = score_proposal({"proposed_type": "field_annotation", "from_field": "orders"})
    assert s["ai_confidence"] == "low"


def test_join_path_with_dotted_field_is_high():
    s = score_proposal({"proposed_type": "join_path", "from_field": "orders.id",
                        "to_field": "work_orders.order_id"})
    assert s["ai_confidence"] == "high"


def test_join_path_incomplete_is_medium():
    s = score_proposal({"proposed_type": "join_path", "from_field": "", "to_field": ""})
    assert s["ai_confidence"] == "medium"


def test_identity_rule_is_medium():
    s = score_proposal({"proposed_type": "identity_rule"})
    assert s["ai_confidence"] == "medium"


def test_unknown_type_is_low():
    s = score_proposal({"proposed_type": "something_else"})
    assert s["ai_confidence"] == "low"


def test_confidence_non_numeric_string_defaults_low():
    # A garbage confidence must not raise — it degrades to 0.0.
    s = score_proposal({"proposed_type": "value_mapping", "to_value": _UUID,
                        "from_value": "x", "confidence": "n/a"})
    assert s["ai_confidence"] == "medium"  # UUID but confidence < 0.85


# --------------------------------------------------------------------------- #
# is_degenerate_field_annotation
# --------------------------------------------------------------------------- #
def test_degenerate_artifact_comma_list():
    assert is_degenerate_field_annotation(
        "erp", "orders.id, name", "orders.id, name", "", "", "some annotation"
    ) is True


def test_degenerate_available_columns_phrase():
    assert is_degenerate_field_annotation(
        "erp", "orders.available columns", "orders.available columns", "", "",
        "these are the columns"
    ) is True


def test_degenerate_empty_annotation():
    assert is_degenerate_field_annotation(
        "erp", "orders.status", "orders.status", "", "", "   "
    ) is True


def test_degenerate_annotation_restates_column():
    assert is_degenerate_field_annotation(
        "erp", "orders.status", "orders.status", "", "", "status"
    ) is True


def test_degenerate_annotation_restates_column_with_underscores():
    assert is_degenerate_field_annotation(
        "erp", "orders.order_state", "orders.order_state", "", "", "order state"
    ) is True


def test_degenerate_self_referential_value():
    # from==to field and from==to value, annotation echoes the value.
    assert is_degenerate_field_annotation(
        "erp", "orders.state", "orders.state", "OPEN", "OPEN", "OPEN"
    ) is True


def test_real_single_column_with_real_annotation_is_not_degenerate():
    assert is_degenerate_field_annotation(
        "cmms", "work_orders.status", "work_orders.status", "", "",
        "Enum values: OPEN, IN_PROGRESS, CLOSED — not free text."
    ) is False


# --------------------------------------------------------------------------- #
# is_knowledge_graph_system
# --------------------------------------------------------------------------- #
import pytest


@pytest.mark.parametrize("system", [
    "digital-thread",                 # runtime target id
    "Digital Thread",                 # display name (as seen in the live bug)
    "Digital Thread (knowledge graph)",
    "knowledge-graph",
    "KnowledgeGraph",
])
def test_knowledge_graph_system_detected(system):
    assert is_knowledge_graph_system(system) is True


@pytest.mark.parametrize("system", ["erp", "cmms", "mes-mil", "sitewise", "", None])
def test_non_graph_system_not_detected(system):
    assert is_knowledge_graph_system(system) is False


# --------------------------------------------------------------------------- #
# is_degenerate_join_proposal
# --------------------------------------------------------------------------- #
def test_join_self_join_same_system_same_field_is_degenerate():
    # The live bug: a JoinPath between a field and ITSELF within one system.
    assert is_degenerate_join_proposal(
        "digital-thread", "Asset.path", "digital-thread", "Asset.path",
        proposed_type="join_path",
    ) is True


def test_join_naming_knowledge_graph_endpoint_is_degenerate():
    # A "cross-system join" that names the graph itself on either side is degenerate.
    assert is_degenerate_join_proposal(
        "Digital Thread", "Asset.path", "erp", "orders.id",
        proposed_type="join_path",
    ) is True
    assert is_degenerate_join_proposal(
        "erp", "orders.id", "Digital Thread", "Asset.path",
        proposed_type="join_path",
    ) is True


def test_genuine_cross_system_join_is_not_degenerate():
    # erp → cmms on the same join-key column name is a REAL cross-system join.
    assert is_degenerate_join_proposal(
        "erp", "work_orders.asset_id", "cmms", "work_orders.asset_id",
        "u-1", "u-1", "join_path",
    ) is False


def test_value_mapping_value_to_itself_same_field_is_degenerate():
    assert is_degenerate_join_proposal(
        "erp", "orders.state", "erp", "orders.state", "OPEN", "OPEN", "value_mapping",
    ) is True


def test_value_mapping_same_field_different_value_is_preserved():
    # A reason-code → failure-mode proposal: same system, same field, but the
    # target VALUE differs (or is an empty pending label) — must NOT be dropped.
    assert is_degenerate_join_proposal(
        "sys-a", "reason_code", "sys-a", "reason_code", "131607000", "", "value_mapping",
    ) is False
    assert is_degenerate_join_proposal(
        "sys-a", "reason_code", "sys-a", "reason_code", "131607000", "Feeder jam",
        "value_mapping",
    ) is False


def test_identity_rule_across_distinct_systems_is_not_degenerate():
    assert is_degenerate_join_proposal(
        "cmms", "downtime", "mes-mil", "quality", proposed_type="identity_rule",
    ) is False


def test_non_join_like_type_never_flagged():
    # field_annotation / mint are single-system types — never flagged here.
    assert is_degenerate_join_proposal(
        "digital-thread", "Asset.path", "digital-thread", "Asset.path",
        proposed_type="field_annotation",
    ) is False


# --------------------------------------------------------------------------- #
# is_degenerate_join_proposal — TABLE-AWARE self-join refinement
# --------------------------------------------------------------------------- #
def test_same_system_different_tables_same_column_is_preserved():
    # A legitimate INTRA-SYSTEM join: the same column name across two DIFFERENT
    # tables of one system is a table-to-table join, NOT a self-join.
    assert is_degenerate_join_proposal(
        "mes-det", "work_orders.line_id", "mes-det", "production_lines.line_id",
        proposed_type="join_path",
    ) is False


def test_same_system_same_table_same_column_is_degenerate():
    # A field joined to ITSELF (same system, same table, same column) stays a
    # self-join — dropped.
    assert is_degenerate_join_proposal(
        "mes-det", "work_orders.line_id", "mes-det", "work_orders.line_id",
        proposed_type="join_path",
    ) is True


def test_unqualified_bare_column_equality_still_drops():
    # No table qualifier on either side → conservative legacy behavior kept:
    # bare-column equality within one system drops.
    assert is_degenerate_join_proposal(
        "s", "line_id", "s", "line_id", proposed_type="join_path",
    ) is True


def test_mixed_qualified_and_bare_column_is_conservative_drop():
    # One side qualified, the other bare → the unqualified side keeps today's
    # conservative drop (we cannot prove the tables differ).
    assert is_degenerate_join_proposal(
        "s", "work_orders.line_id", "s", "line_id", proposed_type="join_path",
    ) is True
    assert is_degenerate_join_proposal(
        "s", "line_id", "s", "work_orders.line_id", proposed_type="join_path",
    ) is True


def test_table_qualifier_comparison_is_case_insensitive():
    # Same table in different case is still the SAME table → self-join drop.
    assert is_degenerate_join_proposal(
        "s", "Work_Orders.line_id", "s", "work_orders.line_id",
        proposed_type="join_path",
    ) is True


def test_value_mapping_across_different_tables_is_preserved():
    # Table-awareness applies to value_mapping too: the same column across two
    # different tables (even with the same value) asserts joinability between
    # tables — not a value mapped to itself.
    assert is_degenerate_join_proposal(
        "mes-det", "work_orders.line_id", "mes-det", "production_lines.line_id",
        "DET-L1", "DET-L1", "value_mapping",
    ) is False


def test_identity_rule_same_table_still_drops():
    assert is_degenerate_join_proposal(
        "s", "work_orders.line_id", "s", "work_orders.line_id",
        proposed_type="identity_rule",
    ) is True


# --------------------------------------------------------------------------- #
# ADR-0051 P4 follow-up — the recommendation consults the `verified` stamp
# before any "confirmed" wording (a claim requires an actual passing check).
# --------------------------------------------------------------------------- #
def test_uuid_recommendation_never_says_confirmed_without_verified_stamp():
    s = score_proposal({"proposed_type": "value_mapping", "to_value": _UUID,
                        "from_value": "x", "confidence": "0.9"})
    assert s["ai_confidence"] == "medium"
    assert "confirmed" not in s["ai_recommendation"].lower()
    assert "unchecked" in s["ai_recommendation"].lower()


def test_uuid_recommendation_says_confirmed_only_with_verified_stamp():
    s = score_proposal({"proposed_type": "value_mapping", "to_value": _UUID,
                        "from_value": "x", "confidence": "0.9",
                        "verified": "true", "verified_scope": "system"})
    assert s["ai_confidence"] == "high"
    assert "confirmed" in s["ai_recommendation"].lower()


def test_verified_stamp_must_be_the_exact_true_string():
    s = score_proposal({"proposed_type": "value_mapping", "to_value": _UUID,
                        "from_value": "x", "confidence": "0.9",
                        "verified": "maybe"})
    assert "confirmed" not in s["ai_recommendation"].lower()


# --------------------------------------------------------------------------- #
# The UUID shape check is a PUBLIC seam.
# --------------------------------------------------------------------------- #
def test_is_uuid_like_public_helper():
    from agents.proposal_scoring import is_uuid_like

    assert is_uuid_like(_UUID) is True
    assert is_uuid_like(_UUID.upper()) is True
    assert is_uuid_like(f"  {_UUID}  ") is True  # strips like the old call site
    assert is_uuid_like("not-a-uuid") is False
    assert is_uuid_like("") is False
    assert is_uuid_like(None) is False


def test_no_cross_module_private_uuid_pattern_import():
    # Grep-style pin: agents/ modules use the public helper, never the
    # private ``_UUID_PATTERN``.
    from pathlib import Path

    import agents
    agents_dir = Path(agents.__file__).parent
    offenders = [
        p.name for p in sorted(agents_dir.rglob("*.py"))
        if p.name != "proposal_scoring.py"
        and "_UUID_PATTERN" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
