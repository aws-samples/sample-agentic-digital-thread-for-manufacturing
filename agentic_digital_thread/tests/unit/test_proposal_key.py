# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure unit tests for the canonical proposal key (ADR 0003 A, Task 1.2).

No AWS, no graph, no DB, no LLM, no fixtures, no markers — runs offline in the
default (unmarked) pytest lane. The expected strings below are derived directly
from agents/proposal_key.py: _SEP="|", _RHS=">>", subject_key =
``proposed_type + "|" + "|".join(norm(LHS))``, target_key =
``subject_key + ">>" + RHS`` (RHS = joined normalized fields, or the sha256[:16]
digest for field_annotation).
"""
from __future__ import annotations

import hashlib

import pytest

from agents.proposal_key import CanonicalKeys, canonical_keys, unordered_value_pair


def _digest(text: str) -> str:
    """Mirror proposal_key._digest for digest assertions."""
    return hashlib.sha256(("" if text is None else str(text)).strip().lower().encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Per-type subject/target tuple correctness — exact expected strings.
# --------------------------------------------------------------------------

def test_value_mapping_exact_strings():
    keys = canonical_keys(
        "value_mapping",
        from_system="erp",
        from_field="orders.customer_id",
        from_value="c-100",
        to_system="crm",
        to_field="accounts.id",
        to_value="acct-7",
    )
    assert keys.subject_key == "value_mapping|erp|orders.customer_id|c-100"
    assert keys.target_key == "value_mapping|erp|orders.customer_id|c-100>>crm|accounts.id|acct-7"


def test_join_path_exact_strings():
    keys = canonical_keys(
        "join_path",
        from_system="erp",
        from_field="orders.customer_id",
        to_system="crm",
        to_field="accounts.id",
    )
    assert keys.subject_key == "join_path|erp|orders.customer_id"
    assert keys.target_key == "join_path|erp|orders.customer_id>>crm|accounts.id"


def test_identity_rule_exact_strings():
    keys = canonical_keys(
        "identity_rule",
        from_system="erp",
        from_field="orders.customer_id",
        to_system="crm",
        to_field="accounts.id",
        concept="customer",
    )
    assert keys.subject_key == "identity_rule|erp|orders.customer_id|customer"
    assert keys.target_key == "identity_rule|erp|orders.customer_id|customer>>crm|accounts.id"


def test_field_annotation_exact_strings():
    annotation = "represents the shipping address"
    keys = canonical_keys(
        "field_annotation",
        from_system="erp",
        from_field="orders.ship_to",
        annotation=annotation,
    )
    expected_subject = "field_annotation|erp|orders.ship_to"
    assert keys.subject_key == expected_subject
    assert keys.target_key == expected_subject + ">>" + _digest(annotation)


# --------------------------------------------------------------------------
# Normalization: case folding + whitespace stripping yield the same key.
# --------------------------------------------------------------------------

def test_system_normalization_folds_case_and_whitespace():
    a = canonical_keys("join_path", from_system="ERP ", from_field="f", to_system="crm", to_field="g")
    b = canonical_keys("join_path", from_system="erp", from_field="f", to_system="crm", to_field="g")
    c = canonical_keys("join_path", from_system=" Erp", from_field="f", to_system="crm", to_field="g")
    assert a == b == c
    assert a.subject_key == "join_path|erp|f"


def test_mixed_case_values_fold():
    a = canonical_keys(
        "value_mapping",
        from_system="erp",
        from_field="F",
        from_value="C-100",
        to_system="crm",
        to_field="g",
        to_value="ACCT-7",
    )
    b = canonical_keys(
        "value_mapping",
        from_system="erp",
        from_field="f",
        from_value="c-100",
        to_system="crm",
        to_field="g",
        to_value="acct-7",
    )
    assert a == b
    assert a.target_key == "value_mapping|erp|f|c-100>>crm|g|acct-7"


# --------------------------------------------------------------------------
# field_annotation digest behaviour.
# --------------------------------------------------------------------------

def test_field_annotation_same_annotation_same_target():
    base = dict(proposed_type="field_annotation", from_system="erp", from_field="f")
    a = canonical_keys(**base, annotation="this is the order date")
    b = canonical_keys(**base, annotation="this is the order date")
    assert a.target_key == b.target_key


def test_field_annotation_different_annotation_different_target():
    base = dict(proposed_type="field_annotation", from_system="erp", from_field="f")
    a = canonical_keys(**base, annotation="this is the order date")
    b = canonical_keys(**base, annotation="this is the ship date")
    assert a.subject_key == b.subject_key  # same subject
    assert a.target_key != b.target_key    # digest discriminates


def test_field_annotation_digest_normalizes():
    base = dict(proposed_type="field_annotation", from_system="erp", from_field="f")
    a = canonical_keys(**base, annotation="Order Date ")
    b = canonical_keys(**base, annotation="order date")
    assert a.target_key == b.target_key


# --------------------------------------------------------------------------
# None / missing fields normalize to '' without crashing.
# --------------------------------------------------------------------------

def test_none_value_normalizes_to_empty():
    keys = canonical_keys(
        "value_mapping",
        from_system="erp",
        from_field="f",
        from_value="c-100",
        to_system="crm",
        to_field="g",
        to_value=None,
    )
    assert keys.target_key == "value_mapping|erp|f|c-100>>crm|g|"


def test_missing_optional_fields_normalize_to_empty():
    # join_path leaves from_value/to_value/concept/annotation at defaults.
    keys = canonical_keys("join_path", from_system="erp", from_field="f", to_system="crm", to_field="g")
    assert keys.subject_key == "join_path|erp|f"
    assert keys.target_key == "join_path|erp|f>>crm|g"


def test_none_annotation_does_not_crash():
    keys = canonical_keys("field_annotation", from_system="erp", from_field="f", annotation=None)
    assert keys.target_key == "field_annotation|erp|f>>" + _digest(None)


# --------------------------------------------------------------------------
# Idempotency — same inputs → identical CanonicalKeys (frozen → equal).
# --------------------------------------------------------------------------

def test_idempotency_same_inputs_equal():
    kwargs = dict(
        from_system="erp",
        from_field="orders.customer_id",
        from_value="c-100",
        to_system="crm",
        to_field="accounts.id",
        to_value="acct-7",
    )
    a = canonical_keys("value_mapping", **kwargs)
    b = canonical_keys("value_mapping", **kwargs)
    assert a == b
    assert a.subject_key == b.subject_key
    assert a.target_key == b.target_key


def test_frozen_is_hashable_and_immutable():
    keys = canonical_keys("join_path", from_system="erp", from_field="f", to_system="crm", to_field="g")
    assert isinstance(keys, CanonicalKeys)
    assert hash(keys) == hash(canonical_keys("join_path", from_system="erp", from_field="f", to_system="crm", to_field="g"))
    with pytest.raises(Exception):
        keys.subject_key = "mutated"  # frozen dataclass forbids assignment


# --------------------------------------------------------------------------
# Discrimination — same subject + different target.
# --------------------------------------------------------------------------

def test_same_subject_different_target():
    a = canonical_keys(
        "value_mapping",
        from_system="erp",
        from_field="f",
        from_value="c-100",
        to_system="crm",
        to_field="g",
        to_value="acct-7",
    )
    b = canonical_keys(
        "value_mapping",
        from_system="erp",
        from_field="f",
        from_value="c-100",
        to_system="crm",
        to_field="g",
        to_value="acct-9",  # different RHS only
    )
    assert a.subject_key == b.subject_key
    assert a.target_key != b.target_key


# --------------------------------------------------------------------------
# Unknown proposed_type → ValueError (fail-loud, no silent default).
# --------------------------------------------------------------------------

def test_unknown_type_raises_value_error():
    with pytest.raises(ValueError):
        canonical_keys("bogus_type", from_system="erp", from_field="f")


# --------------------------------------------------------------------------
# concept_mapping (S3.2 / W1.1) — subject=(to_system, concept), target adds
# (to_field). The subject governs supersede; a different corrected column on
# the same (system, concept) is a NEW target on the SAME subject.
# --------------------------------------------------------------------------

def test_concept_mapping_exact_strings():
    keys = canonical_keys(
        "concept_mapping",
        to_system="cmms",
        concept="mro-inventory",
        to_field="parts.qty_on_hand",
    )
    assert keys.subject_key == "concept_mapping|cmms|mro-inventory"
    assert keys.target_key == "concept_mapping|cmms|mro-inventory>>parts.qty_on_hand"


def test_concept_mapping_same_subject_different_target_on_recorrection():
    # A second correction picking a DIFFERENT column for the same (system,
    # concept) keeps the SAME subject (→ supersede) but a different target.
    a = canonical_keys(
        "concept_mapping", to_system="cmms", concept="mro-inventory",
        to_field="parts.qty_on_hand",
    )
    b = canonical_keys(
        "concept_mapping", to_system="cmms", concept="mro-inventory",
        to_field="inventory.on_hand",  # different corrected column
    )
    assert a.subject_key == b.subject_key            # same subject → supersede
    assert a.target_key != b.target_key              # different chosen column


def test_concept_mapping_normalizes_case_and_whitespace():
    a = canonical_keys(
        "concept_mapping", to_system="CMMS ", concept=" MRO-Inventory",
        to_field="Parts.Qty_On_Hand",
    )
    b = canonical_keys(
        "concept_mapping", to_system="cmms", concept="mro-inventory",
        to_field="parts.qty_on_hand",
    )
    assert a == b
    assert a.subject_key == "concept_mapping|cmms|mro-inventory"
    assert a.target_key == "concept_mapping|cmms|mro-inventory>>parts.qty_on_hand"


def test_concept_mapping_different_system_or_concept_different_subject():
    base = canonical_keys(
        "concept_mapping", to_system="cmms", concept="mro-inventory",
        to_field="parts.qty_on_hand",
    )
    diff_system = canonical_keys(
        "concept_mapping", to_system="erp", concept="mro-inventory",
        to_field="parts.qty_on_hand",
    )
    diff_concept = canonical_keys(
        "concept_mapping", to_system="cmms", concept="availability",
        to_field="parts.qty_on_hand",
    )
    assert base.subject_key != diff_system.subject_key
    assert base.subject_key != diff_concept.subject_key


# --------------------------------------------------------------------------
# field_concept_mint (W1.2) — subject=(to_system, to_field), target adds
# (concept). This is the INVERSE grain of concept_mapping: keyed on the COLUMN
# (a column has exactly one authoritative field-concept meaning), so a re-mint
# on the same column with a different concept supersedes (same subject, new
# target). subject_key is independent of `concept`; target_key includes it.
# --------------------------------------------------------------------------

def test_field_concept_mint_exact_strings():
    keys = canonical_keys(
        "field_concept_mint",
        to_system="cmms",
        to_field="parts.qty_on_hand",
        concept="mro-inventory",
    )
    assert keys.subject_key == "field_concept_mint|cmms|parts.qty_on_hand"
    assert keys.target_key == "field_concept_mint|cmms|parts.qty_on_hand>>mro-inventory"


def test_field_concept_mint_subject_independent_of_concept():
    # subject_key is governed by (to_system, to_field) only — varying the
    # concept must NOT change the subject_key.
    a = canonical_keys(
        "field_concept_mint", to_system="cmms", to_field="parts.qty_on_hand",
        concept="mro-inventory",
    )
    b = canonical_keys(
        "field_concept_mint", to_system="cmms", to_field="parts.qty_on_hand",
        concept="availability",  # different minted concept
    )
    assert a.subject_key == b.subject_key            # same column → supersede
    assert a.target_key != b.target_key              # different minted concept


def test_field_concept_mint_target_includes_concept():
    base = canonical_keys(
        "field_concept_mint", to_system="cmms", to_field="parts.qty_on_hand",
        concept="mro-inventory",
    )
    assert base.target_key.endswith(">>mro-inventory")


def test_field_concept_mint_different_column_different_subject():
    base = canonical_keys(
        "field_concept_mint", to_system="cmms", to_field="parts.qty_on_hand",
        concept="mro-inventory",
    )
    diff_field = canonical_keys(
        "field_concept_mint", to_system="cmms", to_field="inventory.on_hand",
        concept="mro-inventory",
    )
    diff_system = canonical_keys(
        "field_concept_mint", to_system="erp", to_field="parts.qty_on_hand",
        concept="mro-inventory",
    )
    assert base.subject_key != diff_field.subject_key   # different column
    assert base.subject_key != diff_system.subject_key  # different system


def test_field_concept_mint_normalizes_case_and_whitespace():
    a = canonical_keys(
        "field_concept_mint", to_system="CMMS ", to_field=" Parts.Qty_On_Hand",
        concept="MRO-Inventory ",
    )
    b = canonical_keys(
        "field_concept_mint", to_system="cmms", to_field="parts.qty_on_hand",
        concept="mro-inventory",
    )
    assert a == b
    assert a.subject_key == "field_concept_mint|cmms|parts.qty_on_hand"
    assert a.target_key == "field_concept_mint|cmms|parts.qty_on_hand>>mro-inventory"


def test_field_concept_mint_is_inverse_grain_of_concept_mapping():
    # Same (system, field, concept) tuple, opposite keying: concept_mapping is
    # keyed on the concept; field_concept_mint is keyed on the column.
    cm = canonical_keys(
        "concept_mapping", to_system="cmms", concept="mro-inventory",
        to_field="parts.qty_on_hand",
    )
    fcm = canonical_keys(
        "field_concept_mint", to_system="cmms", to_field="parts.qty_on_hand",
        concept="mro-inventory",
    )
    assert cm.subject_key == "concept_mapping|cmms|mro-inventory"
    assert fcm.subject_key == "field_concept_mint|cmms|parts.qty_on_hand"
    assert cm.subject_key != fcm.subject_key


# --------------------------------------------------------------------------
# ADR-0051 P1 — insight subject key folds the finding digest (per-finding grain).
# --------------------------------------------------------------------------

def test_insight_subject_key_folds_digest():
    a = canonical_keys("insight", from_system="erp", concept="correlation",
                       annotation="finding narrative one")
    b = canonical_keys("insight", from_system="erp", concept="correlation",
                       annotation="finding narrative two")
    # Distinct findings on the same (system, topic) are DISTINCT subjects —
    # they coexist instead of superseding into ADR-0042 quarantine.
    assert a.subject_key != b.subject_key
    assert a.subject_key == "insight|erp|correlation|" + _digest("finding narrative one")
    # Identical narrative → identical subject AND target (replace-in-place dup).
    again = canonical_keys("insight", from_system="erp", concept="correlation",
                           annotation="finding narrative one")
    assert again == a


# --------------------------------------------------------------------------
# ADR-0051 P2 — correlation grain (graduated findings).
# --------------------------------------------------------------------------

def test_correlation_exact_strings():
    keys = canonical_keys(
        "correlation", from_system="erp", to_system="mes",
        concept="throughput", annotation="evidence summary",
    )
    assert keys.subject_key == "correlation|erp|mes|throughput"
    assert keys.target_key == keys.subject_key + ">>" + _digest("evidence summary")


def test_correlation_new_evidence_same_subject_new_target():
    a = canonical_keys("correlation", from_system="erp", to_system="mes",
                       concept="throughput", annotation="3 corroborations")
    b = canonical_keys("correlation", from_system="erp", to_system="mes",
                       concept="throughput", annotation="4 corroborations")
    assert a.subject_key == b.subject_key   # same group → supersede grain
    assert a.target_key != b.target_key     # fresh evidence is a real change


# --------------------------------------------------------------------------
# ADR-0051 P3 — unordered endpoint-pair canonicalization (exported lookup).
# --------------------------------------------------------------------------

def test_unordered_pair_canonicalization():
    forward = unordered_value_pair("erp", "orders.customer_id", "c-100",
                                   "crm", "accounts.id", "acct-7")
    reverse = unordered_value_pair("crm", "accounts.id", "acct-7",
                                   "erp", "orders.customer_id", "c-100")
    assert forward == reverse  # (A,B) and (B,A) canonicalize identically
    different_value = unordered_value_pair("crm", "accounts.id", "acct-7",
                                           "erp", "orders.customer_id", "c-999")
    assert different_value != forward  # a different counterpart is a different pair


def test_unordered_pair_normalizes_case_and_field_paths():
    a = unordered_value_pair("ERP ", "Orders.Customer_ID", "C-100",
                             "crm", "accounts.id", "acct-7")
    b = unordered_value_pair("crm", "accounts.id", "acct-7",
                             "erp", "orders.customer_id", "c-100")
    assert a == b


# --------------------------------------------------------------------------
# ADR-0051 P9 — field-path normalization in key construction (*_field slots only).
# --------------------------------------------------------------------------

def test_qualified_and_bare_field_paths_collide():
    # The audited defect (generic names): the same column keyed bare in one
    # proposal and behind a raw/composite payload qualifier in another.
    qualified = canonical_keys(
        "value_mapping", from_system="mes", from_field="record_details_raw.cell",
        from_value="c-9", to_system="erp", to_field="lines.id", to_value="l-1")
    bare = canonical_keys(
        "value_mapping", from_system="mes", from_field="cell",
        from_value="c-9", to_system="erp", to_field="lines.id", to_value="l-1")
    assert qualified.subject_key == bare.subject_key
    assert qualified.target_key == bare.target_key


def test_distinct_columns_do_not_collide():
    a = canonical_keys(
        "value_mapping", from_system="mes", from_field="record_details_raw.cell",
        from_value="c-9", to_system="erp", to_field="lines.id", to_value="l-1")
    b = canonical_keys(
        "value_mapping", from_system="mes", from_field="record_details_raw.slot",
        from_value="c-9", to_system="erp", to_field="lines.id", to_value="l-1")
    assert a.subject_key != b.subject_key
    # Ordinary table.column references are untouched by the normalization.
    c = canonical_keys(
        "value_mapping", from_system="mes", from_field="work_orders.cell",
        from_value="c-9", to_system="erp", to_field="lines.id", to_value="l-1")
    assert c.subject_key == "value_mapping|mes|work_orders.cell|c-9"
    assert c.subject_key != a.subject_key


def test_deep_paths_collapse_to_table_column_tail():
    deep = canonical_keys(
        "join_path", from_system="erp", from_field="db.schema.orders.customer_id",
        to_system="crm", to_field="accounts.id")
    tail = canonical_keys(
        "join_path", from_system="erp", from_field="orders.customer_id",
        to_system="crm", to_field="accounts.id")
    assert deep == tail


def test_field_path_normalization_leaves_value_slots_alone():
    # A VALUE that happens to look path-ish must keep plain _norm semantics —
    # only the *_field slots get _norm_field_path (ADR-0050 alias safety).
    keys = canonical_keys(
        "value_mapping", from_system="erp", from_field="orders.customer_id",
        from_value="details_raw.cell", to_system="crm", to_field="accounts.id",
        to_value="x")
    assert keys.subject_key == "value_mapping|erp|orders.customer_id|details_raw.cell"


# --------------------------------------------------------------------------
# ADR-0059 D3 — steward_question grain (diagnostic; acknowledge-only accept).
# Subject = (from_system, concept, reason) — the failure-shape key, so there
# is ONE pending question per shape-subject; RHS digests the question text.
# --------------------------------------------------------------------------

def test_steward_question_exact_strings():
    question = "Is there a translation between these identifiers?"
    keys = canonical_keys(
        "steward_question",
        from_system="erp",
        concept="work-order",
        reason="terminology_miss",
        annotation=question,
    )
    assert keys.subject_key == "steward_question|erp|work-order|terminology_miss"
    assert keys.target_key == keys.subject_key + ">>" + _digest(question)


def test_steward_question_same_shape_reworded_question_supersedes():
    # A reworded question on the SAME shape-subject keeps the subject (→
    # supersede) but changes the target — never two coexisting pendings.
    base = dict(proposed_type="steward_question", from_system="erp",
                concept="work-order", reason="terminology_miss")
    a = canonical_keys(**base, annotation="question wording one")
    b = canonical_keys(**base, annotation="question wording two")
    assert a.subject_key == b.subject_key
    assert a.target_key != b.target_key


def test_steward_question_identical_refile_is_idempotent():
    kwargs = dict(proposed_type="steward_question", from_system="erp",
                  concept="work-order", reason="empty_join",
                  annotation="same question text")
    assert canonical_keys(**kwargs) == canonical_keys(**kwargs)


def test_steward_question_different_shape_or_system_different_subject():
    base = canonical_keys(
        "steward_question", from_system="erp", concept="work-order",
        reason="terminology_miss", annotation="q")
    diff_reason = canonical_keys(
        "steward_question", from_system="erp", concept="work-order",
        reason="empty_join", annotation="q")
    diff_system = canonical_keys(
        "steward_question", from_system="mes", concept="work-order",
        reason="terminology_miss", annotation="q")
    diff_concept = canonical_keys(
        "steward_question", from_system="erp", concept="cell",
        reason="terminology_miss", annotation="q")
    assert base.subject_key != diff_reason.subject_key
    assert base.subject_key != diff_system.subject_key
    assert base.subject_key != diff_concept.subject_key


def test_steward_question_normalizes_case_and_whitespace():
    a = canonical_keys(
        "steward_question", from_system="ERP ", concept=" Work-Order",
        reason="Terminology_Miss", annotation="The Question ")
    b = canonical_keys(
        "steward_question", from_system="erp", concept="work-order",
        reason="terminology_miss", annotation="the question")
    assert a == b
    assert a.subject_key == "steward_question|erp|work-order|terminology_miss"
