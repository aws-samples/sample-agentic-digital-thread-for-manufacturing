# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Onboarding schema-shape facts (ADR-0063 D6) — derived, never guessed.

Provisioning already READS a system's schema and a bounded set of sample
values. Four physical-shape traps are therefore derivable at connect time
instead of being learned from repeated tool failures later:

1. **Dotted physical column names** — a column whose NAME contains dots.
   HOW to reference one is an engine-generic dialect rule that already lives
   in the connection type's un-displaceable prompt
   (``system_agent._dotted_column_prompt``); it is deliberately NOT restated
   as steward text here. The deployment-specific part — WHICH tables carry
   them — is what this module records.
2. **Serialized-collection columns** — sampled values shaped like serialized
   JSON arrays/objects where an EMPTY collection is a literal token (``[]``)
   rather than NULL/empty. This is the trap that made a sparse column look
   fully populated: ``col IS NOT NULL`` counts every row.
3. **Case-uniform enum vocabularies** — a low-cardinality string column whose
   sampled values are uniformly upper- or lower-case. A predicate written in
   the other case silently returns zero rows (no error, no warning).
4. **Sparsely-populated columns** — present, but populated in a small
   minority of sampled ROWS.

Contract
--------
* Every derivation is a PURE function over ``(schema rows, sampled values)``
  — no connector round-trips, no graph read, no LLM, no AWS. The only IO in
  this module is the ONE orchestrating entrypoint
  (:func:`run_schema_shape_capture`) and the proposal write it delegates to
  ``self_learning.log_proposed_mapping``.
* **Sample semantics are declared, never assumed.** ``sample_kind="rows"``
  means each value is one sampled ROW's value (NULL/empty included), so
  density is measurable; ``sample_kind="distinct"`` means the values are
  de-duplicated and non-null (what the existing provision sampler returns —
  ``SELECT DISTINCT ... WHERE col IS NOT NULL``), so density is NOT
  measurable and :func:`sparse_column_facts` returns nothing. Honest absence
  beats a fabricated ratio (ADR-0053 measurement honesty); measuring density
  would need a COUNT round-trip, which D6 forbids.
* **Derived facts are transient and bounded** — at most :data:`MAX_FACTS`.
  Raw names and samples exist only in this in-process derivation phase.
* **Proposal IO is metadata-only.** Before filing, each fact becomes a bounded
  JSON envelope containing its shape class, bounded counts/ratios, and full
  SHA-256 digests. Every row is an ``insight`` with empty field/value
  endpoints; no raw table, column, sampled value, or secret-shaped text crosses
  the proposal write/log boundary. Insights never auto-accept and accepting
  one writes nothing to the graph.
* **Nothing is written as steward-authored business context.**
* Best-effort by contract: a derivation or filing failure degrades to an
  honest zero-result dict and NEVER fails provisioning.

Proposal type choice
--------------------
Metadata cannot safely address a raw ``table.column`` endpoint. All persisted
shape facts therefore use the existing ``insight`` grain. A future steward
workflow may redeem digests against transient or separately authorized schema
data, but this writer never stores the raw values needed for a field mutation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Bounds (output is capped by construction — a wide schema can never flood the
# review queue or a prompt).
MAX_ENVELOPE_CHARS = 1600
MAX_METADATA_COUNT = 1_000_000
MAX_METADATA_DIGESTS = 8
# --------------------------------------------------------------------------- #
MAX_FACTS = 12            # facts derived per system, per run
MAX_FACT_CHARS = 420      # rendered length of one fact's text
MAX_NAMES_PER_FACT = 5    # dotted names / vocabulary members named in the text
MAX_VALUE_CHARS = 60      # per-value truncation inside rendered text

# Minimum sampled values before ANY claim about a column's values is made: a
# one-value sample states nothing about uniformity or density.
MIN_SAMPLE_VALUES = 3
# A vocabulary bigger than this is free text, not an enum.
MAX_ENUM_DISTINCT = 8
# "Small minority" — populated in at most a third of the sampled rows.
SPARSE_MAX_RATIO = 0.34

# Literal tokens that encode an EMPTY serialized collection.
EMPTY_COLLECTION_TOKENS = ("[]", "{}")

SAMPLE_KIND_ROWS = "rows"          # per-row values, NULL/empty preserved
SAMPLE_KIND_DISTINCT = "distinct"  # de-duplicated, non-null values

KIND_DOTTED = "dotted_columns"
KIND_SERIALIZED = "serialized_collection"
KIND_CASE_ENUM = "case_uniform_enum"
KIND_SPARSE = "sparse_column"

# Emission order: the traps that silently corrupt a COUNT come first.
_KIND_RANK = {KIND_SERIALIZED: 0, KIND_CASE_ENUM: 1, KIND_SPARSE: 2,
              KIND_DOTTED: 3}

# --------------------------------------------------------------------------- #
# PURE value helpers
# --------------------------------------------------------------------------- #
def _as_text(value) -> str | None:
    """``None`` for a NULL sample; otherwise the value's stripped text."""
    return None if value is None else str(value).strip()


def looks_serialized_collection(value) -> bool:
    """True when a sampled value's text is a bracketed serialized collection
    (``[...]`` or ``{...}``), empty ones included. Shape only — never parsed,
    never trusted as JSON. Pure."""
    text = _as_text(value)
    if not text or len(text) < 2:
        return False
    return (text[0], text[-1]) in (("[", "]"), ("{", "}"))


def is_empty_collection_token(value) -> bool:
    """True when a sampled value IS an empty serialized collection (``[]`` /
    ``{}``, whitespace inside tolerated). Pure."""
    text = _as_text(value)
    if not text or not looks_serialized_collection(text):
        return False
    return f"{text[0]}{text[-1]}" in EMPTY_COLLECTION_TOKENS and not text[1:-1].strip()


def is_populated(value) -> bool:
    """True when a sampled value carries content. NULL, empty/whitespace text
    and an EMPTY serialized collection all count as UNPOPULATED — the last one
    is the trap: it is neither NULL nor ``''``, so a naive populated-count
    reports it as data. Pure."""
    text = _as_text(value)
    if not text:
        return False
    return not is_empty_collection_token(text)


def _letter_case(values: list) -> str:
    """``"upper"`` / ``"lower"`` when every letter-bearing value is uniformly
    cased, else ``""``. A value with no cased letters (digits, codes) neither
    proves nor breaks uniformity. Pure."""
    cased = [v for v in values if v.lower() != v.upper()]
    if not cased:
        return ""
    if all(v == v.upper() for v in cased):
        return "upper"
    if all(v == v.lower() for v in cased):
        return "lower"
    return ""


def _column_names(meta) -> list[str]:
    """Column names from one ``load_system_schema`` table entry (``{"columns":
    [...]}``, or a bare column list). Pure."""
    columns = meta.get("columns") if isinstance(meta, dict) else meta
    names: list[str] = []
    for column in columns or []:
        name = column.get("name") if isinstance(column, dict) else column
        if isinstance(name, (tuple, list)):
            name = name[0] if name else ""
        if name:
            names.append(str(name))
    return names


# --------------------------------------------------------------------------- #
# THE FOUR DETECTORS — pure, offline, exhaustively testable
# --------------------------------------------------------------------------- #
def dotted_column_facts(schema: dict) -> list[dict]:
    """One TABLE-grained fact per table carrying dotted physical column names.

    Table-grained by ADR-0063 D6: the dialect rule (how to reference a dotted
    name) is engine-generic prompt text; the deployment-specific fact is WHICH
    tables carry them. ``schema`` is the already-fetched
    ``schema_check.load_system_schema`` shape. Pure."""
    facts: list[dict] = []
    for table, meta in sorted((schema or {}).items()):
        dotted = sorted(n for n in _column_names(meta) if "." in n)
        if dotted:
            facts.append({"kind": KIND_DOTTED, "table": str(table), "column": "",
                          "dotted": dotted[:MAX_NAMES_PER_FACT],
                          "dotted_total": len(dotted)})
    return facts


def serialized_collection_facts(samples: dict) -> list[dict]:
    """Columns whose sampled values are serialized collections AND whose EMPTY
    collection is a literal token rather than NULL/empty.

    Qualifies only when EVERY sampled value is a bracketed collection (a column
    that also yields NULL/empty is representable the ordinary way, so it is NOT
    this trap) and at least one of them is the empty token. Pure."""
    facts: list[dict] = []
    for table, column, values in _iter_columns(samples):
        if any(_as_text(v) in (None, "") for v in values):
            continue  # emptiness IS expressible as NULL/'' here — not the trap
        if not all(looks_serialized_collection(v) for v in values):
            continue
        empty = [v for v in values if is_empty_collection_token(v)]
        if not empty:
            continue
        facts.append({
            "kind": KIND_SERIALIZED, "table": table, "column": column,
            "empty_token": f"{_as_text(empty[0])[0]}{_as_text(empty[0])[-1]}",
            "populated": sum(1 for v in values if is_populated(v)),
            "sampled": len(values),
            "value_digests": [_sha(v) for v in values[:MAX_METADATA_DIGESTS]],
        })
    return facts


def case_uniform_enum_facts(samples: dict) -> list[dict]:
    """Low-cardinality string columns whose sampled values are uniformly
    upper- or lower-case (a predicate in the other case matches zero rows).

    Requires at least :data:`MIN_SAMPLE_VALUES` sampled values, 2..
    :data:`MAX_ENUM_DISTINCT` distinct ones (a single value is a constant, not
    a vocabulary), and letter-case uniformity. Serialized collections and
    letter-free values are not vocabularies. Pure."""
    facts: list[dict] = []
    for table, column, values in _iter_columns(samples):
        texts = [_as_text(v) for v in values]
        texts = [t for t in texts if t and not looks_serialized_collection(t)]
        if len(texts) < MIN_SAMPLE_VALUES:
            continue
        distinct = sorted(set(texts))
        if not 2 <= len(distinct) <= MAX_ENUM_DISTINCT:
            continue
        case = _letter_case(distinct)
        if not case:
            continue
        facts.append({"kind": KIND_CASE_ENUM, "table": table, "column": column,
                      "case": case, "vocabulary": distinct[:MAX_NAMES_PER_FACT],
                      "distinct": len(distinct),
                      "value_digests": [_sha(v) for v in distinct[:MAX_METADATA_DIGESTS]]})
    return facts


def sparse_column_facts(samples: dict, sample_kind: str = SAMPLE_KIND_DISTINCT) -> list[dict]:
    """Columns populated in a small minority of sampled ROWS.

    Returns ``[]`` for ``sample_kind="distinct"`` — de-duplicated non-null
    values carry NO density information, and inventing a ratio from them would
    be a fabricated number (honest absence instead). Under
    ``sample_kind="rows"``: at least :data:`MIN_SAMPLE_VALUES` sampled rows, and
    a populated share above zero (zero populated is "no data in the sample", a
    different claim) but at most :data:`SPARSE_MAX_RATIO`. An EMPTY serialized
    collection counts as UNPOPULATED (:func:`is_populated`). Pure."""
    if sample_kind != SAMPLE_KIND_ROWS:
        return []
    facts: list[dict] = []
    for table, column, values in _iter_columns(samples):
        if len(values) < MIN_SAMPLE_VALUES:
            continue
        populated = sum(1 for v in values if is_populated(v))
        if not 0 < populated <= SPARSE_MAX_RATIO * len(values):
            continue
        facts.append({"kind": KIND_SPARSE, "table": table, "column": column,
                      "populated": populated, "sampled": len(values)})
    return facts


def _iter_columns(samples: dict):
    """Deterministic ``(table, column, values)`` walk of a
    ``{table: {column: [values]}}`` sample map. Pure."""
    for table, columns in sorted((samples or {}).items()):
        if not isinstance(columns, dict):
            continue
        for column, values in sorted(columns.items()):
            if isinstance(values, (list, tuple)) and values:
                yield str(table), str(column), list(values)


# --------------------------------------------------------------------------- #
# Rendering + type routing (pure)
# --------------------------------------------------------------------------- #
def _names(values: list) -> str:
    """Bounded, quoted rendering of a name/vocabulary list. Pure."""
    return ", ".join(f"'{str(v)[:MAX_VALUE_CHARS]}'" for v in values)


def fact_text(fact: dict) -> str:
    """The steward-readable sentence for one derived fact, capped at
    :data:`MAX_FACT_CHARS`. Pure — states only what was derived, and never
    restates the engine-generic dialect rule as steward text."""
    kind = fact.get("kind")
    table, column = fact.get("table", ""), fact.get("column", "")
    if kind == KIND_DOTTED:
        text = (
            f"{fact.get('dotted_total', 0)} physical column name(s) in table "
            f"'{table}' contain dots: {_names(fact.get('dotted') or [])}. Each "
            f"is ONE physical column whose name includes the dots (the "
            f"connection type's dialect rule covers how to reference it); this "
            f"records which table carries them.")
    elif kind == KIND_SERIALIZED:
        token = fact.get("empty_token", "[]")
        text = (
            f"'{table}.{column}' holds serialized collections and stores an "
            f"EMPTY collection as the literal token {token} — not NULL and not "
            f"''. Count populated rows against the token (col <> '{token}'); "
            f"an IS NOT NULL count reports every row as populated. "
            f"{fact.get('populated', 0)} of {fact.get('sampled', 0)} sampled "
            f"value(s) were non-empty.")
    elif kind == KIND_CASE_ENUM:
        text = (
            f"'{table}.{column}' carries a case-uniform vocabulary "
            f"({fact.get('case', '')}-case, {fact.get('distinct', 0)} distinct "
            f"sampled value(s)): {_names(fact.get('vocabulary') or [])}. A "
            f"predicate written in another case returns zero rows silently — "
            f"match the observed case or fold both sides.")
    elif kind == KIND_SPARSE:
        text = (
            f"'{table}.{column}' was populated in {fact.get('populated', 0)} of "
            f"{fact.get('sampled', 0)} sampled row(s) — a small minority. An "
            f"empty result from a filter on it means sparse population, not "
            f"absent data; an empty serialized collection counts as unpopulated.")
    else:
        return ""
    return text[:MAX_FACT_CHARS]


def derive_schema_shape_facts(
    schema: dict, samples: dict, sample_kind: str = SAMPLE_KIND_DISTINCT,
) -> list[dict]:
    """All four detectors over one system's already-read schema and samples,
    deterministically ordered and capped at :data:`MAX_FACTS`. Pure."""
    facts = (serialized_collection_facts(samples)
             + case_uniform_enum_facts(samples)
             + sparse_column_facts(samples, sample_kind)
             + dotted_column_facts(schema))
    facts.sort(key=lambda f: (_KIND_RANK.get(f.get("kind"), 9),
                              f.get("table", ""), f.get("column", "")))
    return facts[:MAX_FACTS]


# --------------------------------------------------------------------------- #
# Transient, bounded sample stash — the "no new connector round-trips" seam.
#
# Provisioning ALREADY samples values (``onboard._collect_provision_samples``)
# to bake them into the agent's schema render. The post-provision hook runs
# after that, in the same job thread, so it can DERIVE from the same values
# instead of asking the connector again. The stash is transient (popped on
# read), bounded (a handful of systems, capped tables/columns/values), holds
# no PII-named column (the upstream sampler skips those) and is never
# persisted anywhere.
# --------------------------------------------------------------------------- #
_MAX_STASHED_SYSTEMS = 4
_MAX_STASHED_TABLES = 40
_MAX_STASHED_COLUMNS = 60
_MAX_STASHED_VALUES = 8
_MAX_STASHED_VALUE_CHARS = 200

_sample_stash: dict = {}


def _bounded_samples(samples: dict) -> dict:
    """Cap a sample map to the stash bounds. Pure."""
    out: dict = {}
    for table, columns in list((samples or {}).items())[:_MAX_STASHED_TABLES]:
        if not isinstance(columns, dict):
            continue
        kept = {}
        for column, values in list(columns.items())[:_MAX_STASHED_COLUMNS]:
            if isinstance(values, (list, tuple)) and values:
                kept[str(column)] = [
                    (None if v is None else str(v)[:_MAX_STASHED_VALUE_CHARS])
                    for v in list(values)[:_MAX_STASHED_VALUES]]
        if kept:
            out[str(table)] = kept
    return out


def remember_samples(system_id: str, samples: dict,
                     sample_kind: str = SAMPLE_KIND_DISTINCT) -> None:
    """Stash a provision run's sample values for the post-provision derivation.
    Best-effort and never raises — a stash failure only costs the facts."""
    try:
        bounded = _bounded_samples(samples)
        if not system_id or not bounded:
            return
        while len(_sample_stash) >= _MAX_STASHED_SYSTEMS:
            _sample_stash.pop(next(iter(_sample_stash)), None)
        _sample_stash[str(system_id)] = (bounded, sample_kind)
    except Exception as exc:  # noqa: BLE001 - best-effort by contract
        logger.debug("schema-shape sample stash skipped: %s", type(exc).__name__)


def take_samples(system_id: str) -> tuple:
    """POP this system's stashed samples: ``(samples, sample_kind)``, or
    ``({}, "distinct")`` when nothing was stashed."""
    return _sample_stash.pop(str(system_id), ({}, SAMPLE_KIND_DISTINCT))


# --------------------------------------------------------------------------- #


def _sha(value) -> str:
    data = str(value if value is not None else "").encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _bounded_count(value) -> int:
    try:
        return max(0, min(int(value or 0), MAX_METADATA_COUNT))
    except (TypeError, ValueError):
        return 0


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(max(0.0, min(numerator / denominator, 1.0)), 4)


def _safe_digests(values) -> list[str]:
    out: list[str] = []
    for value in list(values or [])[:MAX_METADATA_DIGESTS]:
        text = str(value)
        out.append(text if re.fullmatch(r"sha256:[0-9a-f]{64}", text)
                   else _sha(text))
    return out


def shape_metadata(fact: dict) -> dict:
    """Metadata-only persisted shape: classes, bounds, ratios, and SHA-256."""
    supplied_kind = str(fact.get("kind") or "")
    kind = supplied_kind if supplied_kind in _KIND_RANK else "unknown"
    metadata = {"version": 1, "shape_class": kind,
                "table_digest": _sha(fact.get("table"))}
    if fact.get("column"):
        metadata["column_digest"] = _sha(fact.get("column"))
    if kind == KIND_DOTTED:
        metadata["column_count"] = _bounded_count(fact.get("dotted_total"))
        metadata["column_digests"] = [
            _sha(value) for value in (fact.get("dotted") or [])[:MAX_METADATA_DIGESTS]]
    elif kind in (KIND_SERIALIZED, KIND_SPARSE):
        sampled = _bounded_count(fact.get("sampled"))
        populated = min(_bounded_count(fact.get("populated")), sampled)
        metadata.update({"sample_count": sampled, "populated_count": populated,
                         "populated_ratio": _ratio(populated, sampled)})
        if kind == KIND_SERIALIZED:
            metadata["empty_value_digest"] = _sha(fact.get("empty_token"))
    elif kind == KIND_CASE_ENUM:
        metadata["distinct_count"] = _bounded_count(fact.get("distinct"))
        letter_case = str(fact.get("case") or "")
        metadata["letter_case"] = (
            letter_case if letter_case in {"upper", "lower"} else "unknown")
    digests = _safe_digests(fact.get("value_digests"))
    if digests:
        metadata["value_digests"] = digests
    return metadata


def proposal_envelope(fact: dict) -> str:
    """Canonical bounded JSON persisted in query_context; never raw samples."""
    text = json.dumps({"schema_shape": shape_metadata(fact)},
                      sort_keys=True, separators=(",", ":"))
    return text[:MAX_ENVELOPE_CHARS]
# Filing — through the EXISTING proposal lifecycle only
# --------------------------------------------------------------------------- #
def file_schema_shape_facts(system_id: str, facts: list) -> dict:
    """File metadata-only insights; raw schema/sample material never crosses IO."""
    from . import self_learning
    counts = {"filed": 0, "field_annotation": 0, "insight": 0}
    for fact in facts or []:
        kind = str(fact.get("kind") or "")
        if kind not in _KIND_RANK:
            continue
        mapping_id = self_learning.log_proposed_mapping(
            system_id, "", "", system_id, "", "", "schema-shape", 0.9,
            proposal_envelope(fact), proposed_type="insight", source="agent",
            method="onboarding-schema-shape", reason=kind,
            provenance="onboarding-schema-shape",
        )
        if mapping_id:
            counts["filed"] += 1
            counts["insight"] += 1
    return counts


def run_schema_shape_capture(system_id: str) -> dict:
    """Orchestrating entrypoint: already-read schema + stashed samples →
    derived facts → filed proposals. NEVER raises — any failure degrades to an
    honest zero-result dict, so provisioning is never affected (the
    ``coverage_gap_analyzer.run_coverage_gap_check`` boundary)."""
    zero = {"facts": 0, "filed": 0, "field_annotation": 0, "insight": 0}
    try:
        from .schema_check import load_system_schema
        schema = load_system_schema(system_id) or {}
        samples, sample_kind = take_samples(system_id)
        facts = derive_schema_shape_facts(schema, samples, sample_kind)
        if not facts:
            return zero
        result = {"facts": len(facts), **file_schema_shape_facts(system_id, facts)}
        logger.info("[SCHEMA-SHAPE] %s: %d fact(s) derived, %d filed",
                    system_id, result["facts"], result["filed"])
        return result
    except Exception as exc:  # noqa: BLE001 - a shape derivation must never break its caller
        logger.warning("schema-shape capture failed (non-critical): category=%s",
                       type(exc).__name__)
        return zero
