# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared proposal scoring + degeneracy detection (pure, offline).

Single source of truth for two decisions:

- ``score_proposal(mapping)`` → ``{"ai_confidence", "ai_recommendation"}`` — the
  ONE confidence/recommendation logic, so the ``/api/proposed-mappings`` route,
  the ingestion-time
  auto-accept, and the bulk ``accept-high-confidence`` endpoint all agree on what
  "high confidence" means.
- ``is_degenerate_field_annotation(...)`` → ``bool`` — the ONE predicate that
  flags a ``field_annotation`` proposal as low-value noise (self-referential /
  describe-output artifacts), reused by ``log_proposed_mapping`` (drop-at-source)
  and the bulk ``reject-degenerate`` endpoint.

Per ADR 0001 (determinism, no LLM): import-pure — stdlib only (``re``), no boto3,
no network, no module-level ``agents.*`` imports (``provenance_tier`` is the ONE
sanctioned LAZY in-function delegate to ``orchestrator._provenance_tier`` — see
its docstring). Unit-testable with zero AWS. GENERIC — carries
no dataset literals; the artifact phrases below are generic describe-output junk
tokens, not dataset knowledge.
"""
from __future__ import annotations

import re

# A canonical UUID (matches the row-level identifier ERP/MES emit for join keys).
_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


def is_uuid_like(value) -> bool:
    """True when ``value`` is a canonical UUID string (the row-level
    identifier shape ERP/MES emit for join keys). PUBLIC seam — cross-module
    callers use this, never the private ``_UUID_PATTERN``."""
    return bool(_UUID_PATTERN.match(str(value or "").strip()))

# The agent states a proposal's confidence as a word (low | medium | high);
# the proposal store wants a float. ONE table for every channel that turns the
# word into a number: the ``save_value_mapping`` / ``save_correlation`` tools
# (``digital_thread_server``) and the VALUE-MAPPING-PROPOSAL /
# CORRELATION-PROPOSAL response-text blocks (``runtime_app``). The text blocks
# used to store any non-high word as 0.7, so a proposal marked low looked the
# same as one marked medium once stored, and the same word was stored
# differently depending on the channel. An absent or unrecognised word is
# stored as medium, which is what both channels did before.
CONFIDENCE_WORD_FLOAT = {"low": 0.5, "medium": 0.7, "high": 0.9}


def confidence_word_to_float(word, *, high_if_contains: bool = False) -> float:
    """The stored float for a confidence word; medium when absent/unknown.

    ``high_if_contains`` keeps the response-text parsers' older rule that a
    word containing ``high`` (``highest``, ``very_high``) is high, so the fix
    for low never lowers a proposal those parsers used to store as high. The
    tools never had that rule and don't pass it."""
    key = str(word or "").strip().lower()
    if key in CONFIDENCE_WORD_FLOAT:
        return CONFIDENCE_WORD_FLOAT[key]
    if high_if_contains and "high" in key:
        return CONFIDENCE_WORD_FLOAT["high"]
    return CONFIDENCE_WORD_FLOAT["medium"]


# Generic describe-output / column-list artifacts that must never become a
# single-column field annotation. NOT dataset knowledge — these are the fragments
# an LLM emits when it echoes a "describe table" result instead of naming ONE column.
_ARTIFACT_PHRASES = ("available columns", "columns", "id, name")


def _score_value_mapping(mapping: dict, confidence: float) -> dict:
    """The ``value_mapping`` scoring branch (thresholds unchanged).

    ADR-0051 P4 follow-up: the recommendation text consults the row's
    ``verified`` stamp (set only by a PASSING target existence/name check)
    before using any "confirmed" wording — an unverified UUID-shaped target
    is a FORMAT match and says so. Confidence levels are byte-unchanged (the
    auto-accept path scores the row BEFORE verification runs, then requires
    its own passing check)."""
    from_val = mapping.get("from_value", "")
    to_val = mapping.get("to_value", "")
    verified = (
        str(mapping.get("verified") or "").strip().lower() == "true"
        and str(mapping.get("verified_scope") or "").strip().lower() == "system"
    )
    if _UUID_PATTERN.match(to_val or "") and confidence >= 0.85 and verified:
        return {"ai_recommendation":
                "✅ Accept — direct hierarchy traversal, UUID target confirmed by graph check",
                "ai_confidence": "high"}
    if _UUID_PATTERN.match(to_val or ""):
        return {"ai_recommendation":
                "⚠️ Review — UUID-shaped target; TO-system existence is unchecked",
                "ai_confidence": "medium"}
    if from_val and to_val and confidence >= 0.8:
        return {"ai_recommendation": "✅ Likely valid — semantic match",
                "ai_confidence": "medium"}
    return {"ai_recommendation": "⚠️ Review — low confidence or missing values",
            "ai_confidence": "low"}


def _score_crosswalk(mapping: dict) -> dict:
    """The ``crosswalk`` scoring branch (extracted verbatim).

    A crosswalk candidate is a name/token-similarity GUESS for disjoint-value
    entities — always human-confirmed, NEVER auto-accepted (it is also absent
    from self_learning._AUTO_ACCEPT_TYPES). Higher similarity → nudge toward
    review-accept, but never 'high'."""
    try:
        sim = float(mapping.get("confidence", 0))
    except (TypeError, ValueError):
        sim = 0.0
    if mapping.get("from_value", "") and mapping.get("to_value", "") and sim >= 0.8:
        return {"ai_recommendation": "⚠️ Review — strong name/token match; confirm the crosswalk",
                "ai_confidence": "medium"}
    return {"ai_recommendation": "⚠️ Review — candidate crosswalk (disjoint values); verify before confirming",
            "ai_confidence": "low"}


def score_proposal(mapping: dict) -> dict:
    """Score a proposal → ``{"ai_confidence": high|medium|low, "ai_recommendation": str}``.

    This is the extracted, single source of truth for the inline logic that used
    to live in ``server.get_proposed_mappings`` (same thresholds; per-type
    branches decomposed into ``_score_*`` helpers). Reused by the
    ingestion-time auto-accept and the bulk accept-high-confidence endpoint.
    """
    ptype = mapping.get("proposed_type", "")
    try:
        confidence = float(mapping.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    from_field = mapping.get("from_field", "")
    to_field = mapping.get("to_field", "")

    if ptype == "value_mapping":
        return _score_value_mapping(mapping, confidence)

    if ptype == "field_annotation":
        if "." in from_field and from_field.count(".") == 1:
            return {"ai_recommendation": "⚠️ Review — verify column exists in schema before accepting",
                    "ai_confidence": "medium"}
        return {"ai_recommendation": "❌ Suspect — field reference format invalid",
                "ai_confidence": "low"}

    if ptype == "join_path":
        if from_field and to_field and "." in from_field:
            return {"ai_recommendation": "✅ Accept — cross-system join path",
                    "ai_confidence": "high"}
        return {"ai_recommendation": "⚠️ Review — incomplete join specification",
                "ai_confidence": "medium"}

    if ptype == "identity_rule":
        return {"ai_recommendation": "⚠️ Review — identity rules affect all future queries",
                "ai_confidence": "medium"}

    if ptype == "insight":
        # An insight is a NARRATIVE finding — informational context only. It never
        # becomes a graph rule (its accept path writes nothing), so confidence is
        # pinned 'low' to keep it permanently outside every high-confidence accept
        # path (auto-accept, bulk accept-high-confidence).
        return {"ai_recommendation": "💡 Finding — informational; never becomes a graph rule",
                "ai_confidence": "low"}

    if ptype == "crosswalk":
        return _score_crosswalk(mapping)

    return {"ai_recommendation": "⚠️ Unknown type — review manually",
            "ai_confidence": "low"}


def _column(field: str | None) -> str:
    """The column token of a ``table.column`` reference, lowercased+stripped."""
    f = (field or "").strip().lower()
    return f.rsplit(".", 1)[-1] if f else ""


def _field_is_artifact(field: str | None) -> bool:
    """True when a field reference is a list/space/describe-output artifact rather
    than ONE real column name."""
    f = (field or "").strip().lower()
    if not f:
        return False
    col = _column(f)
    # A real column is a single identifier — a comma, ", ", or an embedded space
    # means the LLM handed us a list or a describe-output fragment, not a column.
    if ".," in col or ", " in col or " " in col:
        return True
    # Generic describe-output phrases anywhere in the reference.
    return any(phrase in f for phrase in _ARTIFACT_PHRASES)


# A field slot that LISTS fields (``a.x, b.y`` / ``a.x / b.y`` / ``a.x or b.y``)
# names more than one. Conservative by construction: split only on separators
# OUTSIDE quotes (``,`` ``;`` ``|``, or a space-delimited ``/``, ``and``,
# ``or``) and report a list only when at least TWO parts are each a WHOLE
# dotted field reference. A single name that merely contains a separator
# (``Model.Run/Stop``, ``asset.Power and Energy``, ``"Order, Type"``) or a
# field followed by prose (``t.c / some note``) stays one field.
_FIELD_QUOTES = {'"': '"', "'": "'", "`": "`"}
_FIELD_LIST_SEPARATOR = re.compile(r"[,;|]|\s+(?:/|and|or)\s+", re.IGNORECASE)
_FIELD_REF_SEGMENT = r"(?:[A-Za-z_][A-Za-z0-9_$]*|\"[^\"]+\"|`[^`]+`)"
_DOTTED_FIELD_REF = re.compile(
    rf"{_FIELD_REF_SEGMENT}(?:\.{_FIELD_REF_SEGMENT})+")
_FIELD_ENCLOSERS = (("(", ")"), ("[", "]"), ('"', '"'), ("'", "'"), ("`", "`"))


def _mask_quoted(text: str) -> str:
    """``text`` with the inside of every CLOSED quoted span blanked to NULs
    (same length), so a separator inside quotes never splits. An unclosed
    quote (an apostrophe in prose) is literal."""
    out, i = list(text), 0
    while i < len(text):
        close = _FIELD_QUOTES.get(text[i])
        end = text.find(close, i + 1) if close else -1
        if end == -1:
            i += 1
            continue
        out[i + 1:end] = "\0" * (end - i - 1)
        i = end + 1
    return "".join(out)


def _is_dotted_field_ref(part: str) -> bool:
    """True when ``part`` is one whole dotted field reference, peeling
    enclosing brackets or quotes one layer at a time (``(b.y)`` and
    ``"b.y"`` count; ``"t"."c"`` matches before any peeling)."""
    part = part.strip()
    while True:
        if _DOTTED_FIELD_REF.fullmatch(part):
            return True
        if len(part) < 2 or not any(
                part[0] == o and part[-1] == c for o, c in _FIELD_ENCLOSERS):
            return False
        part = part[1:-1].strip()


def names_multiple_fields(field: str | None) -> bool:
    """True when ONE field slot lists two or more dotted field references
    (see the rule above). Pure; GENERIC."""
    text = field or ""
    parts, start = [], 0
    for sep in _FIELD_LIST_SEPARATOR.finditer(_mask_quoted(text)):
        parts.append(text[start:sep.start()])
        start = sep.end()
    parts.append(text[start:])
    return sum(_is_dotted_field_ref(part) for part in parts) >= 2


# One segment of a dotted ``table.column`` reference: a single identifier token
# (letters, digits, underscore, ``$``, ``-``). A space or comma anywhere means
# the "field" is prose or a list, not a column reference.
_FIELD_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_$-]+$")

# Hard cap on a plausible field reference; anything longer is narrative text.
_FIELD_MAX_LEN = 128


def is_table_column_shaped(field: str | None) -> bool:
    """True iff ``field`` is exactly one dotted ``table.column`` reference.

    Shape contract (structural, dataset-agnostic):
      - non-empty, total length ≤ 128;
      - at least one dot (deeper dotted struct paths like ``a.b.c`` are allowed);
      - NO whitespace and NO commas anywhere (a sentence or a column list is not
        a field reference);
      - every dot-separated segment is a single identifier token
        (``[A-Za-z0-9_$-]+`` — so ``a..b``, ``a.``, ``.a`` all fail).

    This is the ingestion shape-gate predicate for ``identity_rule``/``join_path``
    fields: ``"work_orders.operator_id"`` → True;
    ``"Operator IDs in MES correlate with ERP"`` → False; ``"orders"`` → False.
    Pure/deterministic — no I/O, no LLM, no dataset literals.
    """
    if not field:
        return False
    if len(field) > _FIELD_MAX_LEN:
        return False
    if "," in field or any(ch.isspace() for ch in field):
        return False
    if "." not in field:
        return False
    return all(_FIELD_SEGMENT_PATTERN.match(seg) for seg in field.split("."))


def is_degenerate_field_annotation(
    from_system: str,
    from_field: str,
    to_field: str,
    from_value: str,
    to_value: str,
    annotation: str,
) -> bool:
    """True when a ``field_annotation`` proposal is low-value noise.

    A field annotation is degenerate (junk to be dropped/rejected) when ANY holds:
      - the field reference is a list/space/describe-output artifact (``', '``,
        an embedded space, or a phrase like ``available columns`` / ``id, name``);
      - the annotation is empty/whitespace;
      - the annotation merely restates the column name (adds no NEW context);
      - ``from_field == to_field`` AND ``from_value == to_value`` AND the annotation
        adds nothing (restates the field or the value — a self-referential no-op).

    Pure/deterministic — no I/O, no LLM. GENERIC — the artifact phrases are generic
    describe-output tokens, not dataset literals.
    """
    ann = (annotation or "").strip()
    if not ann:
        return True

    if _field_is_artifact(from_field) or _field_is_artifact(to_field):
        return True

    ann_l = ann.lower()
    for field in (from_field, to_field):
        col = _column(field)
        if col and (ann_l == col or ann_l == col.replace("_", " ")):
            return True  # restates the column name — no new domain context

    if from_field == to_field and (from_value or "") == (to_value or ""):
        col = _column(from_field)
        fval = (from_value or "").strip().lower()
        # Self-referential: annotation just echoes the field or the value.
        if ann_l == col or (fval and ann_l == fval):
            return True

    return False


# --------------------------------------------------------------------------- #
# Cross-system join / value-mapping degeneracy.
#
# A "cross-system" identity join is only meaningful BETWEEN TWO DISTINCT DATA
# systems. Two shapes are degenerate — they must never be saved, and must never be
# described to a human as a "cross-system join":
#   1. A SELF-JOIN — the SAME system AND the SAME field on both sides (a field
#      joined to itself adds no edge); for a value_mapping, additionally the SAME
#      value (a value mapped to itself — a no-op).
#   2. Either endpoint is the KNOWLEDGE-GRAPH target itself. The Digital Thread
#      graph is the rules engine, not a joinable data source — a "join" naming it
#      is a same-graph traversal, not a cross-system identity join.
#
# GENERIC/pure — the graph-identity tokens are INFRASTRUCTURE identifiers for the
# graph target (its runtime id / its "knowledge graph" identity), NOT dataset
# literals. Kept inline (not imported from agents.*) to preserve this module's
# import-purity contract (stdlib only).
# --------------------------------------------------------------------------- #

# Normalized identity tokens for the knowledge-graph target itself. Matched as a
# SUBSTRING of the normalized system name so both the runtime id ("digital-thread")
# and any display form ("Digital Thread (knowledge graph)") are recognized.
_KNOWLEDGE_GRAPH_TOKENS = ("digitalthread", "knowledgegraph")

# Proposal types that assert a relationship BETWEEN systems (and so can be a
# degenerate self-join / graph-self-reference). Other types (field_annotation,
# field_concept_mint, concept_mapping) are single-system and never flagged here.
# ``insight`` is deliberately NOT join-like: it is a single-subject narrative
# finding (keyed on from_system + concept) with no table.column endpoints — it
# never asserts a joinable relationship and never becomes a graph rule.
_JOIN_LIKE_TYPES = ("join_path", "value_mapping", "identity_rule")


def _normalize_system(system: str | None) -> str:
    """Lowercase a system id/name and strip separators for token matching."""
    return re.sub(r"[^a-z0-9]", "", (system or "").strip().lower())


def _table_qualifier(field: str | None) -> str:
    """The table/path qualifier of a ``table.column`` reference, lowercased.

    ``"work_orders.line_id"`` → ``"work_orders"``; a deeper dotted path keeps
    everything before the final column segment; a bare column (no dot) → ``""``.
    Pure — the counterpart of :func:`_column`."""
    f = (field or "").strip().lower()
    return f.rsplit(".", 1)[0] if "." in f else ""


def is_knowledge_graph_system(system: str | None) -> bool:
    """True when a proposal 'system' actually names the knowledge graph itself.

    The Digital Thread graph is the rules engine, NOT a joinable data source, so a
    "cross-system join" that names it on either side is degenerate. Detected by the
    graph's infrastructure identity tokens (its runtime id / "knowledge graph"),
    NOT any dataset literal. Pure/deterministic.
    """
    norm = _normalize_system(system)
    return bool(norm) and any(tok in norm for tok in _KNOWLEDGE_GRAPH_TOKENS)


def is_degenerate_join_proposal(
    from_system: str,
    from_field: str,
    to_system: str,
    to_field: str,
    from_value: str = "",
    to_value: str = "",
    proposed_type: str = "join_path",
) -> bool:
    """True when a join_path / value_mapping / identity_rule proposal is degenerate.

    Degenerate (junk to drop — never a real "cross-system join") when ANY holds:
      - either endpoint IS the knowledge-graph target (not a joinable data source);
      - a SELF-JOIN: the SAME system AND the SAME TABLE AND the SAME column on
        both sides — for a value_mapping additionally the SAME value (a value
        mapped to itself), so a legitimate same-system code→label mapping
        (different field OR different value, e.g. a reason-code → failure-mode
        proposal) is PRESERVED.

    TABLE-AWARE self-join check: the same COLUMN name across two DIFFERENT
    tables of one system (both sides table-qualified, e.g.
    ``work_orders.line_id → production_lines.line_id``) is a legitimate
    INTRA-SYSTEM join — NOT a self-join — and is preserved. A side WITHOUT a
    table qualifier keeps the conservative bare-column-equality drop (legacy
    emitters that never qualified their fields).

    Pure/deterministic — no I/O, no LLM. Only the join-like types are ever flagged;
    every other type returns False. GENERIC — no dataset literals.
    """
    if proposed_type not in _JOIN_LIKE_TYPES:
        return False

    # (1) The graph target is the rules engine, not a joinable data system.
    if is_knowledge_graph_system(from_system) or is_knowledge_graph_system(to_system):
        return True

    # (2) Self-join: same system AND same table AND same column on both sides.
    same_system = _normalize_system(from_system) == _normalize_system(to_system)
    from_col = _column(from_field)
    to_col = _column(to_field)
    same_field = bool(from_col) and from_col == to_col
    if same_system and same_field:
        # TABLE-AWARE refinement: both sides table-qualified with DIFFERENT
        # tables → an intra-system join between two tables, not a self-join.
        # Either side unqualified → conservative drop (today's behavior).
        from_tbl = _table_qualifier(from_field)
        to_tbl = _table_qualifier(to_field)
        if from_tbl and to_tbl and from_tbl != to_tbl:
            return False
        if proposed_type == "value_mapping":
            # Only a value mapped to ITSELF is a no-op; a code→label proposal
            # (empty or different target value) is legitimate and preserved.
            return (from_value or "").strip() == (to_value or "").strip()
        return True

    return False


def is_equal_literal_echo(
    from_system: str,
    from_field: str,
    to_system: str,
    to_field: str,
    from_value: str,
    to_value: str,
) -> bool:
    """True for the SAME-SYSTEM equal-literal cross-field echo value pair.

    The shape: ONE system, two DIFFERENT columns, the SAME literal on both
    sides — it restates one observed literal as a "mapping", encoding no
    transform and resolving no lookup. PROPHYLACTIC: this same-system shape
    is not currently present in the live pilot-env sample — the live degenerates
    are all CROSS-system equal-literal pairs, surfaced by the advisory
    :func:`is_cross_system_equal_literal` badge flag, not by this predicate.

    Fires only when ALL hold:
      - both systems normalize to the SAME non-empty token;
      - both fields carry a non-empty COLUMN token and those tokens DIFFER
        (the same column is ``is_degenerate_join_proposal``'s turf, and the
        same column across two tables is intra-system join evidence);
      - both values are non-empty after ``.strip()`` and EXACTLY equal — NO
        ``casefold`` (a case-changing pair ``FOO1 → foo1`` encodes a REAL
        transform and must survive).

    TYPE SCOPE IS THE CALLER'S JOB — this predicate is value-level and pure.
    Apply it to ``value_mapping`` proposals ONLY:
    ``join_path``/``identity_rule`` are field-grain (values incidental), and
    the crosswalk story lives in :func:`is_cross_system_equal_literal`.

    Pure/deterministic — no I/O, no LLM. GENERIC — no dataset literals.
    """
    norm_from = _normalize_system(from_system)
    if not norm_from or norm_from != _normalize_system(to_system):
        return False
    from_col = _column(from_field)
    to_col = _column(to_field)
    if not from_col or not to_col or from_col == to_col:
        return False
    from_val = (from_value or "").strip()
    to_val = (to_value or "").strip()
    return bool(from_val) and from_val == to_val


def is_cross_system_equal_literal(
    from_system: str,
    from_field: str,
    to_system: str,
    to_field: str,
    from_value: str,
    to_value: str,
) -> bool:
    """True for the CROSS-system equal-literal pair — the live degenerate shape.

    Fires when the normalized systems DIFFER (both non-empty), the COLUMN
    tokens differ (last-segment compare via :func:`_column` — live fields can
    be table-qualified, e.g. ``segment_record_details_raw.cell``), and both
    trimmed values are non-empty and EXACTLY equal (``.strip()``, no casefold
    — the :func:`is_equal_literal_echo` normalization). Applied by
    :func:`degeneracy_flags` to ``value_mapping`` AND ``crosswalk`` rows.

    Steward precedent: the 2026-07-27 Phase-A curation explicitly rejected
    this shape — equal-literal cell→line pairs — see
    ``curation_manifest.json`` (21 recorded degenerate rejects). Equal values
    on a crosswalk are evidence ONLY for name→UUID-style bridges whose
    literals DIFFER — an equal-literal crosswalk resolves no lookup. A
    cross-system SAME-column-token equal-value pair is NOT this shape
    (legitimate join corroboration) and is never flagged by it.

    ADVISORY ONLY — surfaces as the ``cross_system_equal_literal`` badge
    flag; NEVER a generation gate (an ingestion drop would starve the
    3rd-distinct-pending-value → rule promotion trigger and the ADR-0051 P7
    corroboration path; the generation-side answer is onboarding-time
    promotion to field-grain identity rules). Pure/deterministic — no I/O,
    no LLM. GENERIC — no dataset literals.
    """
    norm_from = _normalize_system(from_system)
    norm_to = _normalize_system(to_system)
    if not norm_from or not norm_to or norm_from == norm_to:
        return False
    from_col = _column(from_field)
    to_col = _column(to_field)
    if not from_col or not to_col or from_col == to_col:
        return False
    from_val = (from_value or "").strip()
    return bool(from_val) and from_val == (to_value or "").strip()


# Proposal types that NEVER receive degeneracy flags: narrative findings,
# column annotations, and drift/coverage-gap notices carry no joinable
# value-pair semantics for the advisory badge to judge.
_FLAG_EXEMPT_TYPES = ("insight", "field_annotation", "schema_drift", "coverage_gap")


def _same_field_flags(from_system, from_field, to_system, to_field,
                      from_value: str, to_value: str) -> list[str]:
    """``same_system_same_field`` / ``identity_self_mapping`` sub-flags.

    TABLE-AWARE like :func:`is_degenerate_join_proposal`: the same column
    across two DIFFERENT tables of one system is a legitimate intra-system
    join — NOT flagged. Pure helper for :func:`degeneracy_flags`."""
    norm_from = _normalize_system(from_system)
    if not norm_from or norm_from != _normalize_system(to_system):
        return []
    from_col, to_col = _column(from_field), _column(to_field)
    if not from_col or from_col != to_col:
        return []
    from_tbl, to_tbl = _table_qualifier(from_field), _table_qualifier(to_field)
    if from_tbl and to_tbl and from_tbl != to_tbl:
        return []
    flags = ["same_system_same_field"]
    if from_value.strip() and from_value.strip() == to_value.strip():
        flags.append("identity_self_mapping")
    return flags


def degeneracy_flags(mapping: dict) -> list[str]:
    """Advisory degeneracy classifier for a proposal row → list of flag tokens.

    Server-computed for the review API (which attaches this list plus
    ``likely_low_value`` when non-empty) — never re-derived client-side.
    Flags (stable order): ``knowledge_graph_endpoint`` — either endpoint
    names the knowledge graph itself (the rules engine, not a joinable data
    source); ``same_system_same_field`` / ``identity_self_mapping`` — see
    :func:`_same_field_flags`; ``equal_literal_echo`` —
    :func:`is_equal_literal_echo`, ``value_mapping`` rows ONLY (prophylactic
    — a same-system shape not currently present live);
    ``cross_system_equal_literal`` — :func:`is_cross_system_equal_literal`
    (the live degenerate shape; steward precedent in ITS docstring),
    ``value_mapping`` AND ``crosswalk`` rows.

    Returns ``[]`` for ``insight``/``field_annotation``/``schema_drift`` rows
    (nothing joinable to judge). ADVISORY ONLY — no route may auto-reject on
    these flags. Pure — no I/O, no dataset literals.
    """
    proposed_type = str(mapping.get("proposed_type") or "").strip()
    if proposed_type in _FLAG_EXEMPT_TYPES:
        return []
    from_system, to_system = mapping.get("from_system"), mapping.get("to_system")
    from_field, to_field = mapping.get("from_field"), mapping.get("to_field")
    from_value = str(mapping.get("from_value") or "")
    to_value = str(mapping.get("to_value") or "")

    flags: list[str] = []
    if is_knowledge_graph_system(from_system) or is_knowledge_graph_system(to_system):
        flags.append("knowledge_graph_endpoint")
    flags.extend(_same_field_flags(from_system, from_field, to_system, to_field,
                                   from_value, to_value))
    if proposed_type == "value_mapping" and is_equal_literal_echo(
            from_system, from_field, to_system, to_field,
            from_value, to_value):
        flags.append("equal_literal_echo")
    if proposed_type in ("value_mapping", "crosswalk") and \
            is_cross_system_equal_literal(
                from_system, from_field, to_system, to_field,
                from_value, to_value):
        flags.append("cross_system_equal_literal")
    return flags


# --------------------------------------------------------------------------- #
# Steward-review consequence scoring (ADR-0052 program).
#
# The review list orders pending proposals by CONSEQUENCE = evidence strength ×
# blast radius, so the steward sees the highest-impact claims first. All helpers
# below are pure over an already-enriched mapping dict (the route computes
# ``ai_confidence``/``schema_verdict`` first); the ONE exception is
# ``provenance_tier``, which delegates to ``orchestrator._provenance_tier`` via
# a LAZY in-function import — the single source of the R3 tier semantics (the
# route badge and the route-seed Cypher CASE must never drift; parity is pinned
# by ``test_proposal_consequence.py::test_tier_parity_with_orchestrator``).
# Module import stays stdlib-pure.
# --------------------------------------------------------------------------- #

# Evidence-strength base per shared-scorer confidence level (pinned contract).
_EVIDENCE_BASE = {"high": 1.0, "medium": 0.6, "low": 0.2}
_EVIDENCE_FLOOR_SCHEMA_VERIFIED = 0.8   # schema_verdict == "verified"
_EVIDENCE_FLOOR_TARGET_VERIFIED = 0.9   # verified == "true" (graph existence check)
_EVIDENCE_CAP_QUARANTINED = 0.1         # quarantined == "true"


def _flag(mapping: dict, key: str, expected: str) -> bool:
    """True when a string attr equals ``expected`` (case/space tolerant)."""
    return str(mapping.get(key) or "").strip().lower() == expected


def evidence_strength(mapping: dict) -> float:
    """Evidence strength ∈ (0, 1] for a proposal row (pinned 2.2 formula).

    Base from ``ai_confidence`` (high 1.0 / medium 0.6 / low 0.2 — unknown
    folds to low); ``schema_verdict == "verified"`` floors it at 0.8;
    ``verified == "true"`` (a PASSING target existence check) floors it at
    0.9; ``quarantined == "true"`` caps the result at 0.1 (a quarantined row
    is never high-consequence-by-evidence). Pure — no I/O."""
    conf = str(mapping.get("ai_confidence") or "").strip().lower()
    strength = _EVIDENCE_BASE.get(conf, _EVIDENCE_BASE["low"])
    if _flag(mapping, "schema_verdict", "verified"):
        strength = max(strength, _EVIDENCE_FLOOR_SCHEMA_VERIFIED)
    if _flag(mapping, "verified", "true"):
        strength = max(strength, _EVIDENCE_FLOOR_TARGET_VERIFIED)
    if _flag(mapping, "quarantined", "true"):
        strength = min(strength, _EVIDENCE_CAP_QUARANTINED)
    return strength


def touched_systems(mapping: dict) -> list[str]:
    """Distinct non-empty endpoint systems of a row, sorted (``touches.systems``)."""
    return sorted({
        s for s in (mapping.get("from_system"), mapping.get("to_system"))
        if s and str(s).strip()
    })


def consequence(mapping: dict, concept_system_count: int = 0) -> float:
    """Consequence = evidence_strength × blast_radius (pinned 2.2 formula).

    ``blast_radius ≥ 1``: the row's distinct endpoint systems plus
    ``concept_system_count`` — how many catalog systems share the row's
    concept (the caller batches that graph read; 0 = unknown/none). The float
    is the pending list's sort key. Pure — no I/O."""
    try:
        shared = max(0, int(concept_system_count or 0))
    except (TypeError, ValueError):
        shared = 0
    blast_radius = max(1, len(touched_systems(mapping)) + shared)
    return round(evidence_strength(mapping) * blast_radius, 2)


def supersede_counts(mappings: list) -> dict[str, int]:
    """Per-``subject_key`` count of SUPERSEDED rows in an already-fetched list.

    One pass over the route's full scan — the ``touches.supersede_count``
    input ("replaces N prior") with no extra query. Rows without a
    subject_key are skipped. Pure — no I/O."""
    counts: dict[str, int] = {}
    for m in mappings:
        if str(m.get("status") or "") != "superseded":
            continue
        subject = str(m.get("subject_key") or "")
        if subject:
            counts[subject] = counts.get(subject, 0) + 1
    return counts


def _family_grain_is_discriminating(mapping: dict) -> bool:
    """True when the family grain actually DISCRIMINATES for this row.

    Live-observed on a matured queue (2026-08-19): narrative findings carry no
    method and no field endpoints, so every one of them hashed to the SAME
    degenerate key and the surface offered "reject all 552 in this family" over
    a set whose members share nothing but their type. A family offer is only
    honest when the grain names something specific — a method, or a field pair.
    Rows without either stay SINGLE ROWS (family_count absent), which is the
    honest-absence posture the rest of this payload uses.
    """
    def _has(field: str) -> bool:
        return bool(str(mapping.get(field) or "").strip())

    return _has("method") or _has("from_field") or _has("to_field")


def family_key(mapping: dict) -> str:
    """The review FAMILY a proposal belongs to (ADR-0063 D4) — pure, no I/O.

    Grain: ``proposed_type`` + ``method`` + ``from_field``→``to_field`` — the
    natural family of the measured false-proposal class (same method, same
    field pair, same claim shape), so a steward can adjudicate the family in
    one action instead of one row at a time. Endpoint systems are NOT in the
    grain: the same method proposing the same field pair is one family
    whichever direction it was filed from.

    Returns ``""`` when the grain would not discriminate (no method and no
    endpoints — see :func:`_family_grain_is_discriminating`), so those rows are
    never offered as a batch.

    Computed ON READ only. It is an ORDERING/selection aid: nothing in the
    engine acts on this key, and the batch route never selects rows BY it (the
    request always carries the explicit id list — D4).
    """
    def _part(value) -> str:
        return " ".join(str(value or "").strip().lower().split())

    if not _family_grain_is_discriminating(mapping):
        return ""
    return "|".join((
        _part(mapping.get("proposed_type")) or "value_mapping",
        _part(mapping.get("method")),
        f'{_part(mapping.get("from_field"))}->{_part(mapping.get("to_field"))}',
    ))


def family_counts(mappings: list, status: str = "pending") -> dict[str, int]:
    """Per-``family_key`` member count over rows in ``status`` (default the
    actionable pending queue). One pass over the route's already-fetched scan
    — no extra query. Rows in other statuses are skipped, so the count a
    family header shows is exactly what a family action would address. Pure."""
    counts: dict[str, int] = {}
    wanted = str(status or "").strip().lower()
    for m in mappings:
        row_status = str(m.get("status") or "pending").strip().lower()
        if row_status != wanted:
            continue
        key = family_key(m)
        if not key:  # non-discriminating grain — never offered as a family
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


def build_touches(mapping: dict, supersede_count: int = 0) -> dict:
    """The additive ``touches`` payload for the review list (pinned contract).

    ``systems`` = distinct endpoint systems (the route-seed entries this row
    would join); ``concept`` = the shared concept; ``conflict`` = the row's
    ``review_reason`` verbatim ("" when none — the P3 reverse-conflict flag
    and B4's unverified-authority flag both ride ``review_reason``);
    ``supersede_count`` = prior superseded rows under this subject_key."""
    try:
        count = max(0, int(supersede_count or 0))
    except (TypeError, ValueError):
        count = 0
    return {
        "systems": touched_systems(mapping),
        "concept": str(mapping.get("concept") or ""),
        "conflict": str(mapping.get("review_reason") or ""),
        "supersede_count": count,
    }


def provenance_tier(mapping: dict) -> int:
    """R3 provenance tier of a proposal row — 0 user/steward, 1 accepted
    agent proposal, 2 LLM/unknown.

    DELEGATES to ``orchestrator._provenance_tier`` (the single source that
    mirrors the route-seed Cypher CASE) via a lazy import, so the review-badge
    value can never drift from routing semantics. The lazy import is the
    module's ONE sanctioned ``agents.*`` reach (import-time purity preserved;
    parity pinned by ``test_tier_parity_with_orchestrator``)."""
    from .orchestrator import _provenance_tier
    return _provenance_tier(mapping)
