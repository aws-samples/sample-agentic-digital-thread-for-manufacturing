# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Type-aware, two-grained canonical proposal key (pure, offline).

Implements ADR 0003 section A (the canonical key is type-aware and two-grained):
each proposal yields a ``subject_key`` (left-hand side — governs conflict/
supersede) and a ``target_key`` (subject + right-hand side — governs
idempotency).

Per ADR 0001 (determinism, no LLM): this module is import-pure — stdlib only
(``hashlib``, ``dataclasses``), no boto3, no network, no ``agents.*`` imports,
no LLM. It is therefore unit-testable with zero AWS.

Subject (LHS) / target (RHS) tuples mirror ``self_learning.log_proposed_mapping``
exactly for the proposed types:

| proposed_type     | subject (LHS)                          | target adds (RHS)               |
|-------------------|----------------------------------------|---------------------------------|
| value_mapping     | (from_system, from_field, from_value)  | (to_system, to_field, to_value) |
| join_path         | (from_system, from_field)              | (to_system, to_field)           |
| identity_rule     | (from_system, from_field, concept)     | (to_system, to_field)           |
| field_annotation  | (from_system, from_field)              | digest(annotation)              |
| concept_mapping   | (to_system, concept)                   | (to_field)                      |
| field_concept_mint| (to_system, to_field)                  | (concept)                       |
| concept_mapping_retire | (to_system, concept, to_field)    | (to_field)                      |
| insight           | (from_system, concept, digest(ann.))   | digest(annotation)              |
| correlation       | (from_system, to_system, concept)      | digest(annotation)              |
| schema_drift      | (from_system, from_field)              | digest(reason + description)    |
| coverage_gap      | (to_system, concept, to_field)         | (reason)                        |
| steward_question  | (from_system, concept, reason)         | digest(question text)           |

``field_concept_mint`` is the inverse grain of ``concept_mapping``: it is keyed
on the COLUMN (a column has exactly one authoritative field-concept meaning, so
re-mint on the same column supersedes), whereas ``concept_mapping`` is keyed on
the concept. That difference is intentional.

Field-path normalization (ADR-0051 P9): the ``from_field``/``to_field`` slots
are normalized by :func:`_norm_field_path` before key construction — qualifier
segments naming raw/composite payload re-qualifications are stripped and paths
collapse to a canonical ``table.column`` tail, so aliased references to the
SAME column collide at key time. Rows keyed under the previous normalization
are NOT migrated; they resolve/TTL out (the build's backward-compat policy).
The other slots (system/value/concept) keep :func:`_norm` byte-unchanged, so
annotation digests and the ADR-0050 ``key_norm``/``key_digest`` aliases keep
their meaning.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

_SEP = "|"   # field separator (stable, unlikely in identifiers)
_RHS = ">>"  # subject→target boundary marker


@dataclass(frozen=True)
class CanonicalKeys:
    subject_key: str  # governs conflict / supersede
    target_key: str   # governs idempotency (subject + RHS)


def _norm(v: str | None) -> str:
    """Lowercase + strip; None/empty → '' (stable normalization)."""
    return ("" if v is None else str(v)).strip().lower()


def _digest(text: str | None) -> str:
    """Stable annotation digest: sha256 hex of the normalized text (first 16 chars)."""
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()[:16]


# Generic raw/composite-payload qualifier markers (shape tokens, NOT dataset
# names): a qualifier segment ending in one of these re-qualifies the same
# physical column (a nested/raw record projection), so it is stripped before
# key construction (ADR-0051 P9).
_RAW_PATH_MARKER_SUFFIXES = ("_raw", "_json", "_payload")

# The canonical-key slots that carry field paths (and ONLY those slots get
# _norm_field_path — system/value/concept slots keep _norm untouched).
_FIELD_PATH_SLOTS = frozenset({"from_field", "to_field"})


def _norm_field_path(v: str | None) -> str:
    """Normalize a ``*_field`` slot for key construction (ADR-0051 P9). Pure.

    After :func:`_norm`: qualifier segments ending in a raw/composite-payload
    marker (``_raw``/``_json``/``_payload`` — generic shapes, no dataset
    literals) are stripped, and the path collapses to its trailing
    ``table.column`` pair. So ``<record>_raw.cell`` and bare ``cell`` collide
    (the audited aliased-column defect), while distinct columns and ordinary
    ``table.column`` references are unchanged. A bare (dot-less) field passes
    through :func:`_norm` only.
    """
    f = _norm(v)
    if "." not in f:
        return f
    segments = f.split(".")
    head = [s for s in segments[:-1]
            if s and not s.endswith(_RAW_PATH_MARKER_SUFFIXES)]
    return ".".join((head + [segments[-1]])[-2:])


def _slot_value(name: str, values: dict) -> str:
    """Normalized key material for one dispatch slot.

    The literal ``"annotation"`` slot digests the narrative text (so it can sit
    in the LHS as well as the RHS — the ADR-0051 P1 insight grain); field-path
    slots get :func:`_norm_field_path`; everything else keeps :func:`_norm`.
    """
    if name == "annotation":
        return _digest(values["annotation"])
    if name in _FIELD_PATH_SLOTS:
        return _norm_field_path(values[name])
    return _norm(values[name])


def unordered_value_pair(
    from_system: str, from_field: str, from_value: str,
    to_system: str, to_field: str, to_value: str,
) -> tuple[tuple[str, str, str], tuple[str, str, str]]:
    """Canonical UNORDERED endpoint pair — ``(A,B)`` and ``(B,A)`` collide.

    ADR-0051 P3 (an ADR-0003 collision-policy refinement): normalizes the two
    ``(system, field, value)`` endpoint triples (field slots via
    :func:`_norm_field_path`) and returns them in one canonical ordering, so a
    reverse-direction restatement of the same mapping canonicalizes to the
    same pair. This is a LOOKUP primitive only — no subject/target grain uses
    it. Pure/deterministic.
    """
    a = (_norm(from_system), _norm_field_path(from_field), _norm(from_value))
    b = (_norm(to_system), _norm_field_path(to_field), _norm(to_value))
    return (a, b) if a <= b else (b, a)


# Dispatch: proposed_type → (LHS field names, RHS field names). A literal
# "annotation" sentinel in the RHS slot means "digest the annotation" rather
# than join the named fields.
_DISPATCH: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "value_mapping": (
        ("from_system", "from_field", "from_value"),
        ("to_system", "to_field", "to_value"),
    ),
    # A crosswalk is a human-confirmed value↔value bridge for disjoint-vocabulary
    # entities (confirmed → ValueMapping), so it shares the value_mapping grain:
    # keyed on the full from/to value pair (each candidate pair is independently
    # reviewable + idempotent).
    "crosswalk": (
        ("from_system", "from_field", "from_value"),
        ("to_system", "to_field", "to_value"),
    ),
    "join_path": (
        ("from_system", "from_field"),
        ("to_system", "to_field"),
    ),
    "identity_rule": (
        ("from_system", "from_field", "concept"),
        ("to_system", "to_field"),
    ),
    "field_annotation": (
        ("from_system", "from_field"),
        ("annotation",),
    ),
    "concept_mapping": (
        ("to_system", "concept"),   # subject (LHS) — governs supersede
        ("to_field",),              # target RHS adds the chosen column ("table.column")
    ),
    "field_concept_mint": (
        ("to_system", "to_field"),  # subject (LHS) — the COLUMN gaining a field concept
        ("concept",),               # target RHS adds the minted/assigned field-concept id
    ),
    # A steward's retirement of ONE concept -> column link. The column is in
    # the SUBJECT (unlike concept_mapping's (to_system, concept)): two
    # retirements of one concept's links are distinct subjects that coexist,
    # and a retirement never supersedes the concept's correction rows.
    "concept_mapping_retire": (
        ("to_system", "concept", "to_field"),
        ("to_field",),
    ),
    # An insight is a NARRATIVE finding — informational only, never a graph rule.
    # ADR-0051 P1: the finding digest is folded into the SUBJECT, so each
    # DISTINCT finding on the same (from_system, concept) is its own subject —
    # distinct findings COEXIST as separately adjudicable pendings (the old
    # "a DIFFERENT narrative on the same subject supersedes" semantics are
    # retired; supersede-churn fed the ADR-0042 quarantine pathology). An
    # identical re-observed finding still dedupes (subject==target grain ⇒
    # dup_pending) — the replace-in-place pending cap of 1 per finding.
    "insight": (
        ("from_system", "concept", "annotation"),  # subject — system + topic + finding digest
        ("annotation",),                           # target RHS repeats the digest (idempotency)
    ),
    # ADR-0051 P2: ONE graduated proposal per corroborated cross-system
    # correlation group. Subject = the (from_system, to_system, concept)
    # group; the RHS digests the evidence summary, so a re-run of the
    # graduation sweep dedupes (dup_pending) and NEW corroborating evidence
    # supersedes the prior pending graduation. In the auto-accept deny-tuple —
    # a correlation is reviewed by a human, never applied automatically.
    "correlation": (
        ("from_system", "to_system", "concept"),
        ("annotation",),
    ),
    # ADR-0053 E5: a schema-drift finding is a DIAGNOSTIC proposal (acknowledge-
    # only accept, never a graph rule). Subject = the drifted ``table.column``
    # (``table.`` for table-level events, ``from_system == to_system``); the RHS
    # digests the drift descriptor (``reason`` + ``description``), so the SAME
    # unresolved drift dedupes as dup_pending while a type-flap (retyped A→B
    # then later B→A) files a distinct, separately-reviewable row.
    "schema_drift": (
        ("from_system", "from_field"),
        ("reason", "description"),
    ),
    # Coverage-gap finding — a DIAGNOSTIC proposal like schema_drift
    # (acknowledge-only accept in self_learning._accept_coverage_gap; applying
    # the mapping stays the steward's explicit remap action). The candidate
    # COLUMN sits in the SUBJECT — deliberately a different grain from
    # concept_mapping's (to_system, concept): two candidate fields for the
    # same unmapped concept are DISTINCT subjects, so they coexist as
    # separately-reviewable pending rows instead of the second silently
    # superseding the first via _classify_active_diff's same-subject path.
    # The RHS digests ``reason`` so the SAME re-detected finding dedupes as
    # dup_pending.
    "coverage_gap": (
        ("to_system", "concept", "to_field"),
        ("reason",),
    ),
    # ADR-0059 D3: a steward question is a DIAGNOSTIC proposal (acknowledge-
    # only accept in self_learning._accept_steward_question — accepting means
    # "seen", NEVER a graph rule; answering happens through the guided-turn
    # machinery, which supersedes the row). Subject = the failure-shape key
    # (``reason`` carries the shape, e.g. terminology_miss/empty_join), so
    # there is ONE pending question per shape-subject by construction; the RHS
    # digests the question text (rides ``query_context`` → annotation), so an
    # identical re-file dedupes as dup_pending while a reworded question on
    # the same subject supersedes.
    "steward_question": (
        ("from_system", "concept", "reason"),
        ("annotation",),
    ),
}


def canonical_keys(
    proposed_type: str,
    *,
    from_system: str = "",
    from_field: str = "",
    from_value: str = "",
    to_system: str = "",
    to_field: str = "",
    to_value: str = "",
    concept: str = "",
    annotation: str = "",  # field_annotation RHS source (query_context, deprefixed)
    reason: str = "",       # schema_drift RHS source (the drift kind)
    description: str = "",  # schema_drift RHS source ("was <old>, now <new>")
) -> CanonicalKeys:
    """Compute the two-grained canonical key for a proposal (ADR 0003 A)."""
    spec = _DISPATCH.get(proposed_type)
    if spec is None:
        raise ValueError(f"Unknown proposed_type: {proposed_type!r}")
    lhs_fields, rhs_fields = spec
    values = locals()
    subject_key = proposed_type + _SEP + _SEP.join(
        _slot_value(f, values) for f in lhs_fields)
    if rhs_fields == ("annotation",):
        rhs = _digest(annotation)
    elif rhs_fields == ("reason", "description"):
        # ADR-0053 E5: digest the drift descriptor — deliberately NOT the
        # query_context (which carries the detection date and would defeat
        # the same-drift dedup).
        rhs = _digest(_norm(reason) + _SEP + _norm(description))
    else:
        rhs = _SEP.join(_slot_value(f, values) for f in rhs_fields)
    return CanonicalKeys(subject_key=subject_key, target_key=subject_key + _RHS + rhs)
