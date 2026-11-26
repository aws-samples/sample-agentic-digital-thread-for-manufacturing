# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Metric-definition artifact (ADR-0066 D1) — the PURE half of a
deployment-defined metric: parse, validate, canonicalize, render, diff.

A ``metric_definition`` is a JSON document (contract C1) that a steward files
through the ONE ADR-0003 proposal lifecycle and that becomes a standalone
``(:MetricDefinition)`` graph node when accepted. This module owns the
*document*: nothing here reads the graph, calls an LLM, or touches the network.
Every judgement is COMPUTED — validation, the rendered formula, and the
field-level diff are deterministic functions of the bytes (never LLM-judged).

Division of labour (deliberate, so both doors fail loud):

* :func:`parse_definition` — JSON/shape + INTRA-document semantics (charset,
  closed vocabularies, caps, range rules, the ratio requirement); returns the
  NORMALIZED document. Equal-meaning documents normalize to identical bytes,
  which is what makes the lifecycle's content digest idempotent.
* :func:`validate_definition` — the same shape checks (idempotent) PLUS the
  CROSS-reference rules that need a universe: concept/metric refs, the
  code-metric id collision (C5), the window vocabulary, and cycle detection
  over the reference closure.

Both raise :class:`MetricDefinitionError` naming the offending field (the
``metric_layer._validate_valid_range`` register), never a warning and never a
silently repaired document. ``valid_range``/``max_exclusive`` reproduce the code
registry's rules EXACTLY (two finite bounds, minimum < maximum, inclusive lower,
upper exclusive only when ``max_exclusive``), so a graph-defined metric cannot
mean something a code-defined one could not.

:func:`to_metric` builds the EXISTING frozen ``DirectMetric``/``DerivedMetric``
dataclasses, so ``metric_resolver`` and every metric-layer consumer work
unchanged. ``synonyms``/``grain``/``authoritative_system`` stay definition-side
metadata and never enter a dataclass — the code dataclasses are untouched, so
code metrics remain byte-identical.

``filter`` (ADR-0075, direct only) — a closed, AND-only list of row predicates
over CONCEPT ids — is parsed, canonicalized and rendered by
``metric_filter_document`` (the rule itself: ``metric_predicate_rule``).

Faithful mirror, not an oversight: like ``metric_layer._validate_term``, a
``{"concept": ...}`` leaf validates structurally, but the resolver's evaluator
accepts only metric/scalar leaves — such a definition fails LOUD at resolve
time rather than resolving to a guess.

Import-pure (ADR 0001): stdlib plus two import-pure in-repo modules
(``metric_layer`` for the op-tree types, ``untrusted_text`` for the steward-text
treatment). No I/O, no ``eval``/``exec``, no domain literals (ADR-0044).
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable, Mapping, NoReturn

from . import metric_filter_document as mfd
from .metric_filter_document import (  # noqa: F401  (re-export, ADR-0075)
    filter_from_document, filter_to_document,
)
from .metric_filter_document import render_number as _render_number
from .metric_layer import (
    Aggregation, ConceptRef, DerivedMetric, DirectMetric, Formula, FormulaOp,
    MetricKind, MetricRef, Scalar,
)
from .metric_measure_rule import DurationMeasure, FieldRef
from .untrusted_text import sanitize_untrusted, validate_untrusted


class MetricDefinitionError(Exception):
    """Raised by parse/validate; the message names the offending field."""


# --- Closed vocabularies + caps (C1). Reused verbatim from the code registry:
# this module adds NO formula op and NO aggregation (scope discipline).

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# Concept ids and the denominator-provenance concept id come from the composed
# concept universe (core ∪ graph), which this module does not own — so the shape
# check stays conservative (no whitespace, bounded) and MEMBERSHIP in
# ``known_concepts`` is the real gate, applied in ``validate_definition``.
REF_PATTERN = re.compile(r"^\S{1,64}$")

KINDS: frozenset[str] = frozenset(k.value for k in MetricKind)
AGGREGATIONS: frozenset[str] = frozenset(a.value for a in Aggregation)
FORMULA_OPS: frozenset[str] = frozenset(o.value for o in FormulaOp)

MAX_LABEL_LEN = 128
MAX_SYNONYMS = 8
MAX_SYNONYM_LEN = 64
MAX_UNIT_LEN = 32
MAX_GRAIN_LEN = 128
MAX_AUTHORITATIVE_SYSTEM_LEN = 64
MAX_FORMULA_DEPTH = 6
MAX_FORMULA_NODES = 32
MAX_DOCUMENT_BYTES = 8 * 1024

# Canonical field order (documentation + diff ordering; the wire form is
# sorted-key JSON, so this tuple never affects the digest).
DEFINITION_FIELDS: tuple[str, ...] = (
    "id", "kind", "label", "synonyms", "unit", "concept", "aggregation",
    "measure", "percentiles", "group_by", "filter", "formula", "prefer_stored",
    "denominator_provenance", "valid_range",
    "max_exclusive", "window", "grain", "authoritative_system",
)
_FIELD_SET: frozenset[str] = frozenset(DEFINITION_FIELDS)

# Fields that belong to exactly one kind. A document carrying the other kind's
# field is a MISTAKE, not something to ignore: a direct metric with a formula
# would silently drop the formula on ``to_metric``.
_DIRECT_ONLY: tuple[str, ...] = ("concept", "aggregation", "filter",
                                 "measure", "percentiles", "group_by")
_DERIVED_ONLY: tuple[str, ...] = (
    "formula",
    "prefer_stored",
    "denominator_provenance",
)

# Arity mirrors ``metric_resolver._apply_op`` so validation never admits a
# formula the deterministic evaluator would reject at resolve time.
_FIXED_ARITY: dict[str, int] = {
    FormulaOp.RATIO.value: 2,
    FormulaOp.SCALE.value: 2,
}

_LEAF_KEYS: tuple[str, ...] = ("metric", "concept", "value")


def _fail(metric_id: Any, field: str, problem: str) -> NoReturn:
    subject = metric_id if isinstance(metric_id, str) and metric_id else "<unidentified>"
    raise MetricDefinitionError(
        f"metric_definition {subject!r}: field {field!r} {problem}"
    )


# --------------------------------------------------------------------------- #
# Parse + normalize (shape + intra-document semantics)
# --------------------------------------------------------------------------- #

def parse_definition(raw: str | Mapping[str, Any]) -> dict:
    """Parse and shape-check a C1 document; return its NORMALIZED form.

    ``raw`` is the canonical JSON string (the wire/graph form) or an equivalent
    mapping. Raises :class:`MetricDefinitionError` naming the offending field.
    """
    if isinstance(raw, str):
        try:
            loaded = json.loads(raw)
        except ValueError as exc:
            _fail("", "document", f"is not valid JSON: {exc}")
    elif isinstance(raw, Mapping):
        loaded = dict(raw)
    else:
        _fail("", "document", "must be a JSON object or a JSON object string")
    if not isinstance(loaded, dict):
        _fail("", "document", "must be a JSON object")
    return _normalize(loaded)


def canonical_json(doc: Mapping[str, Any]) -> str:
    """The canonical byte form: normalized document, sorted keys, no spaces.

    This is the string that is digested, stored in ``definition_json``, and
    byte-compared on readback — so it must be a pure function of MEANING.
    """
    return _dump(_normalize(doc))


def _dump(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _normalize(doc: Mapping[str, Any]) -> dict:
    """Shape-check + canonicalize one document (no external references).

    Kept thin on purpose: one helper per field GROUP, so a new field lands in
    exactly one place and every clause stays readable.
    """
    if not isinstance(doc, Mapping):
        _fail("", "document", "must be a JSON object")
    metric_id = _normalize_id(doc.get("id"))
    _reject_unknown_fields(metric_id, doc)
    kind = _normalize_kind(metric_id, doc.get("kind"))

    out: dict[str, Any] = {"id": metric_id, "kind": kind}
    out.update(_normalize_naming(metric_id, doc))
    out.update(_normalize_kind_fields(metric_id, kind, doc))
    out.update(_normalize_bounds(metric_id, doc))
    out.update(_normalize_advisory(metric_id, doc))

    ordered = {name: out[name] for name in DEFINITION_FIELDS if name in out}
    _check_size(metric_id, ordered)
    return ordered


def _reject_unknown_fields(metric_id: str, doc: Mapping[str, Any]) -> None:
    unknown = sorted(set(doc) - _FIELD_SET)
    if unknown:
        _fail(metric_id, unknown[0],
              "is not a metric-definition field; the document shape is closed "
              f"(allowed: {', '.join(DEFINITION_FIELDS)})")


def _normalize_kind(metric_id: str, kind: Any) -> str:
    if not isinstance(kind, str) or kind not in KINDS:
        _fail(metric_id, "kind", f"must be one of {sorted(KINDS)}")
    return kind


def _normalize_naming(metric_id: str, doc: Mapping[str, Any]) -> dict:
    return {
        "label": _clean_text(metric_id, "label", doc.get("label"),
                             max_len=MAX_LABEL_LEN, required=True),
        "synonyms": _normalize_synonyms(metric_id, doc.get("synonyms")),
        "unit": _clean_text(metric_id, "unit", doc.get("unit"),
                            max_len=MAX_UNIT_LEN),
    }


def _normalize_kind_fields(metric_id: str, kind: str,
                           doc: Mapping[str, Any]) -> dict:
    """The kind-exclusive half. A field belonging to the OTHER kind is a
    mistake, not something to ignore: a direct definition carrying a formula
    would silently drop it on ``to_metric``."""
    wrong_kind = _DERIVED_ONLY if kind == MetricKind.DIRECT.value else _DIRECT_ONLY
    for field in wrong_kind:
        if not _absent(doc.get(field)):
            _fail(metric_id, field, f"is not valid on a {kind!r} definition")
    if kind == MetricKind.DIRECT.value:
        return _normalize_direct_fields(metric_id, doc)
    return _normalize_derived_fields(metric_id, doc)


def _normalize_direct_fields(metric_id: str, doc: Mapping[str, Any]) -> dict:
    aggregation = doc.get("aggregation")
    if not isinstance(aggregation, str) or aggregation not in AGGREGATIONS:
        _fail(metric_id, "aggregation",
              "is required on a direct definition and must be one of "
              f"{sorted(AGGREGATIONS)}")
    out: dict[str, Any] = {
        "concept": _normalize_ref(metric_id, "concept", doc.get("concept")),
        "aggregation": aggregation,
    }
    out.update(_normalize_measure_fields(metric_id, doc))
    if not _absent(doc.get("group_by")):  # a {concept, column} field ref
        out["group_by"] = _normalize_field_ref(
            metric_id, "group_by", doc["group_by"])
    if not _absent(doc.get("filter")):  # ADR-0075; failures name filter[i].<field>
        out["filter"] = mfd.normalize_filter(
            doc["filter"], aggregation,
            fail=lambda field, problem: _fail(metric_id, field, problem),
            ref=lambda field, value: _normalize_ref(metric_id, field, value))
    return out


_MEASURE_KEYS: frozenset[str] = frozenset(
    {"kind", "start", "end", "unit", "clamp_negative"})
_FIELD_REF_KEYS: frozenset[str] = frozenset({"concept", "column"})


def _normalize_field_ref(metric_id: str, field: str, raw: Any) -> dict:
    """Shape-check a ``{concept, column}`` field ref; return its canonical
    form. The column is EXACT as stored (a dotted physical column is ONE
    identifier, ADR-0076 D4), only shape is checked here; the MAPS_TO check is
    the resolver's, fail-closed."""
    if not isinstance(raw, Mapping):
        _fail(metric_id, field, "must be a {concept, column} object")
    extra = sorted(str(k) for k in set(raw) - _FIELD_REF_KEYS)
    if extra:
        _fail(metric_id, f"{field}.{extra[0]}",
              "is not a field-ref field (only 'concept', 'column')")
    concept = _normalize_ref(metric_id, f"{field}.concept", raw.get("concept"))
    column = raw.get("column")
    if not isinstance(column, str) or not column.strip():
        _fail(metric_id, f"{field}.column", "must be a non-empty column name")
    return {"concept": concept, "column": column.strip()}


def _normalize_measure_fields(metric_id: str, doc: Mapping[str, Any]) -> dict:
    """Parse the duration ``measure`` + ``percentiles`` pair.

    ``measure`` and ``percentiles`` come together: a percentile is valid only
    with a duration measure, and a duration measure is only ever read as a
    percentile. Values are shape/charset-checked here (the closed rule lives in
    ``metric_measure_rule``, applied at ``to_metric`` and layer-validate time);
    the field-ref concepts are MEMBERSHIP-checked in ``validate_definition``."""
    has_measure = not _absent(doc.get("measure"))
    has_percentiles = not _absent(doc.get("percentiles"))
    if not has_measure and not has_percentiles:
        return {}
    if has_percentiles and not has_measure:
        _fail(metric_id, "percentiles",
              "are valid only with a duration measure")
    if has_measure and not has_percentiles:
        _fail(metric_id, "percentiles",
              "a duration measure requires a non-empty percentiles list")
    return {
        "measure": _normalize_measure(metric_id, doc["measure"]),
        "percentiles": _normalize_percentiles(metric_id, doc["percentiles"]),
    }


def _normalize_measure(metric_id: str, raw: Any) -> dict:
    if not isinstance(raw, Mapping):
        _fail(metric_id, "measure", "must be a duration measure object")
    extra = sorted(str(k) for k in set(raw) - _MEASURE_KEYS)
    if extra:
        _fail(metric_id, f"measure.{extra[0]}",
              f"is not a measure field (only {sorted(_MEASURE_KEYS)})")
    if raw.get("kind") != "duration":
        _fail(metric_id, "measure", "kind must be 'duration'")
    from .metric_measure_rule import DURATION_UNITS
    unit = raw.get("unit")
    if unit not in DURATION_UNITS:
        _fail(metric_id, "measure", f"unit must be one of {list(DURATION_UNITS)}")
    clamp = raw.get("clamp_negative", True)
    if not isinstance(clamp, bool):
        _fail(metric_id, "measure", "clamp_negative must be a boolean")
    if clamp is not True:  # this measure kind always clamps at 0
        _fail(metric_id, "measure",
              "clamp_negative must be true (negative durations are clamped "
              "at 0 by this metric kind)")
    return {
        "kind": "duration",
        "start": _normalize_field_ref(metric_id, "measure.start", raw.get("start")),
        "end": _normalize_field_ref(metric_id, "measure.end", raw.get("end")),
        "unit": unit,
        "clamp_negative": clamp,
    }


def _normalize_percentiles(metric_id: str, raw: Any) -> list[float]:
    from .metric_measure_rule import check_percentiles
    if not isinstance(raw, (list, tuple)):
        _fail(metric_id, "percentiles", "must be a list of 1..5 numbers")
    values: list[float] = []
    for entry in raw:
        if isinstance(entry, bool) or not isinstance(entry, (int, float)):
            _fail(metric_id, "percentiles",
                  "each percentile must be a number strictly between 0 and 1")
        values.append(float(entry))
    problem = check_percentiles(tuple(values))
    if problem:
        _fail(metric_id, "percentiles", problem)
    return sorted(values)


def _normalize_derived_fields(metric_id: str, doc: Mapping[str, Any]) -> dict:
    if _absent(doc.get("formula")):
        _fail(metric_id, "formula", "is required on a derived definition")
    out: dict[str, Any] = {"formula": _normalize_formula(
        metric_id, doc.get("formula"), path="formula", depth=1,
        counter={"nodes": 0})}
    prefer_stored = doc.get("prefer_stored")
    if not _absent(prefer_stored):
        out["prefer_stored"] = _normalize_metric_ref(
            metric_id, "prefer_stored", prefer_stored)
    provenance = doc.get("denominator_provenance")
    if not _absent(provenance):
        out["denominator_provenance"] = _normalize_ref(
            metric_id, "denominator_provenance", provenance)
        if not _contains_ratio(out["formula"]):
            _fail(metric_id, "denominator_provenance",
                  "requires a ratio formula operation")
    return out


def _normalize_bounds(metric_id: str, doc: Mapping[str, Any]) -> dict:
    valid_range = doc.get("valid_range")
    max_exclusive = doc.get("max_exclusive", False)
    if not isinstance(max_exclusive, bool):
        _fail(metric_id, "max_exclusive", "must be a boolean")
    out: dict[str, Any] = {}
    if not _absent(valid_range):
        out["valid_range"] = _normalize_valid_range(metric_id, valid_range)
    elif max_exclusive:
        _fail(metric_id, "max_exclusive", "requires valid_range")
    out["max_exclusive"] = max_exclusive
    return out


def _normalize_advisory(metric_id: str, doc: Mapping[str, Any]) -> dict:
    """``window`` (vocabulary-checked in ``validate_definition``) plus the two
    advisory fields, which carry NO resolution authority."""
    out: dict[str, Any] = {}
    window = doc.get("window")
    if not _absent(window):
        out["window"] = _normalize_ref(metric_id, "window", window)
    grain = _clean_text(metric_id, "grain", doc.get("grain"),
                        max_len=MAX_GRAIN_LEN)
    if grain:
        out["grain"] = grain
    system = _clean_text(metric_id, "authoritative_system",
                         doc.get("authoritative_system"),
                         max_len=MAX_AUTHORITATIVE_SYSTEM_LEN)
    if system:
        out["authoritative_system"] = system
    return out


def _check_size(metric_id: str, ordered: Mapping[str, Any]) -> None:
    size = len(_dump(ordered).encode("utf-8"))
    if size > MAX_DOCUMENT_BYTES:
        _fail(metric_id, "document",
              f"canonical JSON is {size} bytes, over the "
              f"{MAX_DOCUMENT_BYTES}-byte cap")


def _absent(value: Any) -> bool:
    """Optional fields: ``None`` and ``""`` mean 'not supplied'."""
    return value is None or (isinstance(value, str) and value.strip() == "")


def _normalize_id(raw: Any) -> str:
    if not isinstance(raw, str) or not ID_PATTERN.match(raw):
        _fail("", "id",
              "must match the metric-id charset "
              f"{ID_PATTERN.pattern!r} (lowercase, digits, hyphens; "
              "1-64 chars)")
    return raw


def _clean_text(metric_id: str, field: str, value: Any, *, max_len: int,
                required: bool = False) -> str:
    """Sanitize + validate one steward-authored text field (ADR-0054 posture:
    hostile text is REJECTED at write, never truncated, never fenced-away)."""
    if value is None:
        value = ""
    sanitized = sanitize_untrusted(value)
    problem = validate_untrusted(
        sanitized, max_len=max_len, field=f"'{field}'",
        label=f"metric definition {field}")
    if problem:
        reason, detail = problem
        _fail(metric_id, field, f"rejected ({reason}): {detail}")
    if required and not sanitized:
        _fail(metric_id, field, "must be a non-empty string")
    return sanitized


def _normalize_synonyms(metric_id: str, raw: Any) -> list[str]:
    if raw is None or raw == "":
        return []
    if not isinstance(raw, (list, tuple)):
        _fail(metric_id, "synonyms", "must be a list of strings")
    if len(raw) > MAX_SYNONYMS:
        _fail(metric_id, "synonyms",
              f"carries {len(raw)} entries, over the {MAX_SYNONYMS} cap")
    out: list[str] = []
    for index, entry in enumerate(raw):
        cleaned = _clean_text(metric_id, f"synonyms[{index}]", entry,
                              max_len=MAX_SYNONYM_LEN, required=True)
        if cleaned in out:
            _fail(metric_id, "synonyms",
                  f"repeats an entry at position {index}")
        out.append(cleaned)
    return out


def _normalize_ref(metric_id: str, field: str, raw: Any) -> str:
    """Shape-check a REQUIRED id-shaped reference (callers gate on absence)."""
    if _absent(raw):
        _fail(metric_id, field, "must be a non-empty id")
    if not isinstance(raw, str):
        _fail(metric_id, field, "must be a string id")
    value = sanitize_untrusted(raw)
    if not REF_PATTERN.match(value):
        _fail(metric_id, field,
              "must be a non-empty id of at most 64 characters with no "
              "whitespace")
    return value


def _normalize_metric_ref(metric_id: str, field: str, raw: Any) -> str:
    if not isinstance(raw, str) or not ID_PATTERN.match(raw):
        _fail(metric_id, field,
              f"must be a metric id matching {ID_PATTERN.pattern!r}")
    return raw


def _normalize_valid_range(metric_id: str, raw: Any) -> list[float]:
    """EXACTLY the code registry's rule (``_validate_valid_range``): a
    two-number pair of finite bounds with minimum < maximum."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        _fail(metric_id, "valid_range", "must be a two-number list")
    lower, upper = raw
    if (
        isinstance(lower, bool)
        or isinstance(upper, bool)
        or not isinstance(lower, (int, float))
        or not isinstance(upper, (int, float))
        or not math.isfinite(float(lower))
        or not math.isfinite(float(upper))
        or lower >= upper
    ):
        _fail(metric_id, "valid_range",
              "must contain finite bounds with minimum < maximum")
    return [float(lower), float(upper)]


def _normalize_formula(metric_id: str, node: Any, *, path: str, depth: int,
                       counter: dict) -> dict:
    if depth > MAX_FORMULA_DEPTH:
        _fail(metric_id, path,
              f"exceeds the maximum formula depth of {MAX_FORMULA_DEPTH}")
    if not isinstance(node, Mapping):
        _fail(metric_id, path, "must be an {op, inputs} object")
    unknown = sorted(set(node) - {"op", "inputs"})
    if unknown:
        _fail(metric_id, f"{path}.{unknown[0]}",
              "is not a formula-node field (only 'op' and 'inputs')")
    op = node.get("op")
    if not isinstance(op, str) or op not in FORMULA_OPS:
        _fail(metric_id, f"{path}.op",
              f"must be one of the closed formula operations {sorted(FORMULA_OPS)}")
    inputs = node.get("inputs")
    if not isinstance(inputs, (list, tuple)) or not inputs:
        _fail(metric_id, f"{path}.inputs",
              "must be a non-empty list of formula terms")
    expected = _FIXED_ARITY.get(op)
    if expected is not None and len(inputs) != expected:
        _fail(metric_id, f"{path}.inputs",
              f"must carry exactly {expected} terms for the {op!r} operation, "
              f"got {len(inputs)}")
    if op == FormulaOp.WEIGHTED_SUM.value and len(inputs) % 2 != 0:
        _fail(metric_id, f"{path}.inputs",
              f"must carry an even number of terms (value/weight pairs) for "
              f"the {op!r} operation, got {len(inputs)}")

    _count_node(metric_id, path, counter)
    terms: list[dict] = []
    for index, term in enumerate(inputs):
        terms.append(_normalize_term(metric_id, term,
                                     path=f"{path}.inputs[{index}]",
                                     depth=depth, counter=counter))
    return {"op": op, "inputs": terms}


def _normalize_term(metric_id: str, term: Any, *, path: str, depth: int,
                    counter: dict) -> dict:
    if not isinstance(term, Mapping):
        _fail(metric_id, path,
              "must be a formula term object "
              f"({', '.join('{' + k + '}' for k in _LEAF_KEYS)} or a nested "
              "{op, inputs})")
    if "op" in term or "inputs" in term:
        return _normalize_formula(metric_id, term, path=path, depth=depth + 1,
                                  counter=counter)
    present = [key for key in _LEAF_KEYS if key in term]
    extra = sorted(set(term) - set(_LEAF_KEYS))
    if extra:
        _fail(metric_id, f"{path}.{extra[0]}",
              "is not a formula-term field (only 'metric', 'concept', "
              "'value', or a nested {op, inputs})")
    if len(present) != 1:
        _fail(metric_id, path,
              "must carry exactly one of 'metric', 'concept', or 'value'")
    _count_node(metric_id, path, counter)
    key = present[0]
    if key == "metric":
        return {"metric": _normalize_metric_ref(
            metric_id, f"{path}.metric", term["metric"])}
    if key == "concept":
        return {"concept": _normalize_ref(
            metric_id, f"{path}.concept", term["concept"])}
    value = term["value"]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        _fail(metric_id, f"{path}.value", "must be a finite number")
    return {"value": float(value)}


def _count_node(metric_id: str, path: str, counter: dict) -> None:
    counter["nodes"] += 1
    if counter["nodes"] > MAX_FORMULA_NODES:
        _fail(metric_id, "formula",
              f"exceeds the maximum of {MAX_FORMULA_NODES} formula nodes "
              f"(at {path})")


def _contains_ratio(node: Any) -> bool:
    """Whether a normalized op-tree contains a ratio operation."""
    if not isinstance(node, Mapping) or "op" not in node:
        return False
    if node.get("op") == FormulaOp.RATIO.value:
        return True
    return any(_contains_ratio(term) for term in node.get("inputs") or ())


# --------------------------------------------------------------------------- #
# Cross-reference validation
# --------------------------------------------------------------------------- #

def validate_definition(
    doc: Mapping[str, Any],
    *,
    known_concepts: Iterable[str],
    known_metric_ids: Iterable[str],
    code_metric_ids: Iterable[str],
    window_vocab: Iterable[str] | None = None,
    definitions: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Validate one document against a reference universe. ``None`` on success.

    Fails loud with :class:`MetricDefinitionError` naming the offending field.
    Re-runs the shape checks first, so a caller that validates never has to
    trust that its input was already parsed.

    ``window_vocab`` is a PARAMETER rather than an import so this module stays
    import-pure and vocabulary-agnostic (the caller passes
    ``metric_dimension.WINDOW_VOCAB``). Supplying none is fail-CLOSED: a
    document that declares a window is then rejected, never admitted unchecked.

    ``definitions`` are the other graph-resident documents by id; supplying
    them extends cycle detection from self-reference to the full reference
    closure. Code metrics are acyclic by pin, so a reference that leaves the
    supplied set terminates the walk.
    """
    norm = _normalize(doc)
    metric_id = norm["id"]
    concepts = frozenset(known_concepts)

    if metric_id in frozenset(code_metric_ids):
        _fail(metric_id, "id",
              "collides with a code-defined metric id; code definitions always "
              "win, so overriding one is a repository change, not a proposal")

    if norm["kind"] == MetricKind.DIRECT.value:
        _validate_direct_refs(norm, concepts)
    else:
        _validate_derived_refs(norm, concepts=concepts,
                              metric_ids=frozenset(known_metric_ids),
                              definitions=definitions)

    window = norm.get("window")
    if window is not None and window not in frozenset(window_vocab or ()):
        _fail(metric_id, "window",
              f"{window!r} is not in the supported window vocabulary "
              f"{sorted(frozenset(window_vocab or ()))}")


def _validate_direct_refs(norm: Mapping[str, Any],
                          concepts: frozenset[str]) -> None:
    """The measure concept, every predicate concept AND every duration-measure
    field-ref concept must be KNOWN (core ∪ graph): a reference to an unknown
    concept is a typo, not a pending binding (ADR-0075 D6 /)."""
    if norm["concept"] not in concepts:
        _fail(norm["id"], "concept",
              f"{norm['concept']!r} is not a known concept in this "
              "deployment")
    for index, clause in enumerate(norm.get("filter") or ()):
        if clause["concept"] not in concepts:
            _fail(norm["id"], f"filter[{index}].concept",
                  f"{clause['concept']!r} is not a known concept in this "
                  "deployment")
    measure = norm.get("measure")
    if measure:
        for role in ("start", "end"):
            concept = measure[role]["concept"]
            if concept not in concepts:
                _fail(norm["id"], f"measure.{role}.concept",
                      f"{concept!r} is not a known concept in this deployment")
    group_by = norm.get("group_by")
    if group_by:
        if not measure:
            _fail(norm["id"], "group_by",
                  "is valid only with a duration measure")
        if group_by["concept"] not in concepts:
            _fail(norm["id"], "group_by.concept",
                  f"{group_by['concept']!r} is not a known concept in this "
                  "deployment")


def _validate_derived_refs(norm: Mapping[str, Any], *,
                           concepts: frozenset[str],
                           metric_ids: frozenset[str],
                           definitions: Mapping[str, Mapping[str, Any]] | None,
                           ) -> None:
    """Reference resolution + cycle detection for a derived definition."""
    metric_id = norm["id"]
    if norm.get("prefer_stored") == metric_id:
        _fail(metric_id, "prefer_stored",
              "references its own metric id — a definition cycle")
    if metric_id in _collect_formula_metric_refs(norm):
        _fail(metric_id, "formula",
              "references its own metric id — a definition cycle")
    _validate_terms(metric_id, norm["formula"], path="formula",
                    concepts=concepts, metric_ids=metric_ids)
    prefer_stored = norm.get("prefer_stored")
    if prefer_stored is not None and prefer_stored not in metric_ids:
        _fail(metric_id, "prefer_stored",
              f"metric ref {prefer_stored!r} does not resolve")
    provenance = norm.get("denominator_provenance")
    if provenance is not None and provenance not in concepts:
        _fail(metric_id, "denominator_provenance",
              f"{provenance!r} is not a known concept in this deployment")
    cycle = _detect_cycle(norm, definitions)
    if cycle:
        _fail(metric_id, "formula",
              "closes a definition cycle: " + " -> ".join(cycle))


def _validate_terms(metric_id: str, node: Mapping[str, Any], *, path: str,
                    concepts: frozenset[str],
                    metric_ids: frozenset[str]) -> None:
    for index, term in enumerate(node.get("inputs") or ()):
        term_path = f"{path}.inputs[{index}]"
        if "op" in term:
            _validate_terms(metric_id, term, path=term_path,
                            concepts=concepts, metric_ids=metric_ids)
        elif "concept" in term:
            if term["concept"] not in concepts:
                _fail(metric_id, f"{term_path}.concept",
                      f"{term['concept']!r} is not a known concept in this "
                      "deployment")
        elif "metric" in term:
            if term["metric"] not in metric_ids:
                _fail(metric_id, f"{term_path}.metric",
                      f"metric ref {term['metric']!r} does not resolve")


def _metric_refs(doc: Mapping[str, Any]) -> frozenset[str]:
    """Every metric id one document references (formula refs + prefer_stored).

    Lenient by design: it walks whatever shape it is given so the cycle walk can
    traverse peer documents without re-validating (and without raising) on them.
    """
    refs: set[str] = set()
    prefer_stored = doc.get("prefer_stored") if isinstance(doc, Mapping) else None
    if isinstance(prefer_stored, str) and prefer_stored:
        refs.add(prefer_stored)
    _collect_metric_refs(doc.get("formula") if isinstance(doc, Mapping) else None,
                         refs, depth=0)
    return frozenset(refs)


def _collect_formula_metric_refs(doc: Mapping[str, Any]) -> frozenset[str]:
    """Metric ids referenced by the formula op-tree ALONE (no prefer_stored),
    so a self-reference can be reported against the field that carries it."""
    refs: set[str] = set()
    _collect_metric_refs(doc.get("formula"), refs, depth=0)
    return frozenset(refs)


def _collect_metric_refs(node: Any, refs: set[str], *, depth: int) -> None:
    if depth > MAX_FORMULA_DEPTH + 1 or not isinstance(node, Mapping):
        return
    ref = node.get("metric")
    if isinstance(ref, str) and ref:
        refs.add(ref)
    inputs = node.get("inputs")
    if isinstance(inputs, (list, tuple)):
        for term in inputs:
            _collect_metric_refs(term, refs, depth=depth + 1)


def _detect_cycle(doc: Mapping[str, Any],
                  definitions: Mapping[str, Mapping[str, Any]] | None,
                  ) -> list[str]:
    """The reference path that returns to ``doc['id']``, or ``[]`` when acyclic.

    Computed, bounded, and deterministic: references are walked in sorted order
    and every id is expanded at most once per path.
    """
    universe: dict[str, Mapping[str, Any]] = dict(definitions or {})
    start = doc["id"]
    universe[start] = doc

    def walk(current: str, path: list[str], visiting: frozenset[str]) -> list[str]:
        for ref in sorted(_metric_refs(universe.get(current) or {})):
            if ref == start:
                return path + [ref]
            if ref in visiting or ref not in universe:
                continue
            found = walk(ref, path + [ref], visiting | {ref})
            if found:
                return found
        return []

    return walk(start, [start], frozenset({start}))


# --------------------------------------------------------------------------- #
# Dataclass construction
# --------------------------------------------------------------------------- #

def to_metric(doc: Mapping[str, Any]) -> DirectMetric | DerivedMetric:
    """Build the EXISTING frozen metric dataclass for one definition.

    The result is indistinguishable from a hand-built registry entry, which is
    what lets ``metric_resolver``/``card_layer``/the dimension machinery consume
    graph-defined metrics with zero edits. ``synonyms``, ``grain`` and
    ``authoritative_system`` are definition-side metadata and deliberately do
    NOT enter the dataclass.
    """
    norm = _normalize(doc)
    valid_range = norm.get("valid_range")
    bounds = None if valid_range is None else (valid_range[0], valid_range[1])
    if norm["kind"] == MetricKind.DIRECT.value:
        return DirectMetric(
            id=norm["id"],
            concept=norm["concept"],
            aggregation=Aggregation(norm["aggregation"]),
            label=norm["label"],
            unit=norm["unit"],
            measure=_measure_from_document(norm.get("measure")),
            percentiles=(tuple(norm["percentiles"])
                         if norm.get("percentiles") is not None else None),
            group_by=(FieldRef(norm["group_by"]["concept"],
                               norm["group_by"]["column"])
                      if norm.get("group_by") is not None else None),
            filter=filter_from_document(norm.get("filter")),
            window=norm.get("window"),
            valid_range=bounds,
            max_exclusive=norm["max_exclusive"],
        )
    return DerivedMetric(
        id=norm["id"],
        label=norm["label"],
        unit=norm["unit"],
        formula=_to_formula(norm["formula"]),
        prefer_stored=norm.get("prefer_stored"),
        window=norm.get("window"),
        valid_range=bounds,
        max_exclusive=norm["max_exclusive"],
        denominator_provenance=norm.get("denominator_provenance"),
    )


def _measure_from_document(measure: "Mapping[str, Any] | None") -> "DurationMeasure | None":
    """Build the frozen ``DurationMeasure`` for a normalized ``measure`` block, ``None`` when absent."""
    if not measure:
        return None
    return DurationMeasure(
        kind=measure["kind"],
        start=FieldRef(measure["start"]["concept"], measure["start"]["column"]),
        end=FieldRef(measure["end"]["concept"], measure["end"]["column"]),
        unit=measure["unit"],
        clamp_negative=measure["clamp_negative"],
    )


def _to_formula(node: Mapping[str, Any]) -> Formula:
    return Formula(FormulaOp(node["op"]),
                   tuple(_to_term(term) for term in node["inputs"]))


def _to_term(term: Mapping[str, Any]):
    if "op" in term:
        return _to_formula(term)
    if "metric" in term:
        return MetricRef(term["metric"])
    if "concept" in term:
        return ConceptRef(term["concept"])
    return Scalar(term["value"])


# --------------------------------------------------------------------------- #
# Rendering + diff (computed, never LLM-judged)
# --------------------------------------------------------------------------- #

def render_formula(doc: Mapping[str, Any]) -> str:
    """A deterministic, human-readable one-line rendering of the definition.

    Derived: nested ``op(term, term)`` with bare metric ids, ``concept:<id>``
    for concept leaves (the two namespaces stay distinguishable) and trimmed
    numeric literals. Direct: ``aggregation(concept:<id>)`` plus, when a
    ``filter`` is declared, `` where <concept> eq 'X' and <concept> in (...)``.
    """
    norm = _normalize(doc)
    if norm["kind"] == MetricKind.DIRECT.value:
        group_by = norm.get("group_by")
        return _render_direct(norm.get("measure"), norm.get("percentiles"),
                              norm["aggregation"], norm["concept"],
                              group_by["column"] if group_by else None, norm.get("filter"))
    return _render_node(norm["formula"])


def render_metric(metric: DirectMetric) -> str:
    """:func:`render_formula` for a BUILT direct metric (the served layer
    holds dataclasses, not documents): the same text the definition it was
    built from renders as, so a lineage that carries it names the definition
    in the one register."""
    measure = None
    if metric.measure is not None:
        m = metric.measure
        measure = {"start": {"column": m.start.column}, "end": {"column": m.end.column},
                   "unit": m.unit, "clamp_negative": m.clamp_negative}
    aggregation = getattr(metric.aggregation, "value", metric.aggregation)
    return _render_direct(measure, metric.percentiles, aggregation, metric.concept,
                          getattr(metric.group_by, "column", None),
                          mfd.filter_to_document(metric.filter))


def _render_direct(measure, percentiles, aggregation: str, concept: str,
                   group_by_column: str | None, filter_clauses) -> str:
    """The one direct-metric rendering shared by both entry points."""
    if measure:
        head = _render_measure(measure, percentiles or ())
    else:
        head = f"{aggregation}(concept:{concept})"
    if group_by_column:
        head += f" by {group_by_column}"
    return head + mfd.render_filter(filter_clauses)


def _render_measure(measure: Mapping[str, Any], percentiles) -> str:
    """``p90, p99 of duration(start_ts -> complete_ts) in hours``, a plain-
    words rendering of the duration-percentile measure. Clamp is implied
    by the measure's own clamp flag; stated when off."""
    ps = ", ".join(f"p{_render_number(p * 100)}" for p in percentiles)
    start = measure["start"]["column"]
    end = measure["end"]["column"]
    clamp = "" if measure.get("clamp_negative", True) else " (negatives kept)"
    return (f"{ps} of duration({start} -> {end}) in {measure['unit']}{clamp}")


def _render_node(node: Mapping[str, Any]) -> str:
    inner = ", ".join(_render_term(term) for term in node["inputs"])
    return f"{node['op']}({inner})"


def _render_term(term: Mapping[str, Any]) -> str:
    if "op" in term:
        return _render_node(term)
    if "metric" in term:
        return str(term["metric"])
    if "concept" in term:
        return f"concept:{term['concept']}"
    return _render_number(term["value"])


def diff_definitions(current: Mapping[str, Any] | None,
                     proposed: Mapping[str, Any]) -> dict:
    """Structural, field-level diff between the current and proposed document.

    Pure and deterministic (sorted keys, canonical values) — the steward's
    review surface, never an LLM verdict. ``current is None`` means the
    proposal adds a definition that does not exist yet.
    """
    after = _normalize(proposed)
    before = None if current is None else _normalize(current)
    base: Mapping[str, Any] = {} if before is None else before

    added: dict[str, Any] = {}
    removed: dict[str, Any] = {}
    changed: dict[str, dict] = {}
    for field in DEFINITION_FIELDS:
        in_before, in_after = field in base, field in after
        if in_after and not in_before:
            added[field] = after[field]
        elif in_before and not in_after:
            removed[field] = base[field]
        elif in_before and in_after and _dump({field: base[field]}) != _dump(
                {field: after[field]}):
            changed[field] = {"from": base[field], "to": after[field]}

    return {
        "is_new": before is None,
        "added": added,
        "removed": removed,
        "changed": changed,
        "changed_fields": sorted(set(added) | set(removed) | set(changed)),
        "formula": {
            "from": None if before is None else render_formula(before),
            "to": render_formula(after),
        },
    }
