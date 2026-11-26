# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Generic, connector-agnostic, ORDER-INDEPENDENT cross-system join discovery.

Discovers cross-system identity JOINS from MEASURED value overlap — never from
name/label coincidence — and lays the human-confirmed CROSSWALK foundation for
disjoint-vocabulary entities that *should* relate but share zero values.

Two entry points, both idempotent (dedup rides ``self_learning.log_proposed_mapping``'s
lookup-before-write), so ONBOARDING ORDER AND TIMING NEVER MATTER:

  * :func:`discover_joins_for_system` — run on each onboard: compares the new
    system's identifier-like fields against EVERY already-onboarded system, in
    BOTH directions (new→existing AND existing→new).
  * :func:`discover_all_joins` — a re-runnable sweep over ALL onboarded system
    pairs; callable on demand and after any onboard, so a join between two systems
    onboarded in either order (or a pair that pre-existed) is always found.

A third, SAME-SYSTEM entry point — :func:`discover_intra_system` — runs the same
transform ladder across DIFFERENT tables of ONE system and RETURNS candidates
without writing (the provision caller decides direct-write vs proposal).

The core signal is a NORMALIZATION TRANSFORM LADDER applied to sampled values —
the FIRST rung that clears the overlap threshold wins:

  identity → casefold → strip_affix (a systematic prefix/suffix on the generic
  delimiters ``_-/.`` learned from the data) → token_extract (mine scalar leaves
  out of JSON/struct blobs, reusing ``resolve_evidence.extract_join_tokens``).

On success a ``join_path`` proposal is emitted carrying the winning ``transform``
and (when both systems expose timestamp-typed fields) an optional ``time_key`` for
time-window alignment. When two fields of the SAME concept under an ALREADY-JOINED
parent share ZERO values, the module does NOT silently give up — it emits a
reviewable CROSSWALK candidate (name/token-similarity proposals) instead, which a
human confirms into a ``ValueMapping``.

Everything here is GENERIC — no dataset, system-id, table, column, or model
literals. Heavy logic lives in PURE, offline-testable helpers; IO (graph reads +
value sampling + proposal writes) is isolated behind module-level seams that tests
monkeypatch, and is best-effort so discovery never breaks an onboard.

DRY: value tokenization / overlap measurement and multi-source sampling are NOT
re-implemented here — they reuse ``resolve_evidence`` verbatim.
"""
from __future__ import annotations

import difflib
import logging
import re
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Iterable

from . import joinpath_rule_link as _jp_rule
from .miner_contract import MINER_METHOD, MINER_PROVENANCE, MINER_SOURCE
from .stored_value_mapping_memo import operation_scope as _stored_vm_scope
from .resolve_evidence import extract_join_tokens, sample_memo_stats, value_overlap  # noqa: F401 (value_overlap re-exported for tests)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Tunable module constants (generic thresholds — no dataset knowledge).
# --------------------------------------------------------------------------- #
# A handful of shared values can be coincidental; a real join key shares >= 5
# distinct values AND covers a meaningful fraction of the smaller sampled token
# set. (Raised from 2 — live evidence showed degenerate joins entering the queue
# from <=3 shared values.)
JOIN_MIN_OVERLAP_COUNT = 5
JOIN_MIN_RATIO = 0.3

# Post-transform key-set cardinality floor: after a ladder rung normalizes both
# sides, a real join key space still has at least this many distinct values on
# BOTH sides (see :func:`passes_key_cardinality`).
JOIN_MIN_KEY_CARDINALITY = 5

# Generic delimiters a systematic affix rides on (e.g. "WO-123", "SITE_A", "a/b").
_AFFIX_DELIMS = "_-/. "

# Transform ladder labels. Additive/back-compat with the graph's ``jp.transform``
# (an accepted proposal writes exactly this string; readers treat identity/direct
# as a pass-through).
TRANSFORM_IDENTITY = "identity"
TRANSFORM_CASEFOLD = "casefold"
TRANSFORM_TOKEN_EXTRACT = "token_extract"  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
# strip_affix rungs are dynamic: "strip_prefix:<affix>" / "strip_suffix:<affix>".

# Crosswalk (disjoint-vocabulary) candidate generation.
# Floor raised 0.5 → 0.6: live evidence showed zero-overlap nonsense pairs
# (e.g. BNR5↔NRD2, whose incidental fuzzy ratio is exactly 0.5) clearing the
# old floor; 0.6 requires a real token or near-spelling signal. Digit-only
# agreement never counts at all (see :func:`_pair_similarity`).
CROSSWALK_MIN_SIMILARITY = 0.6   # combined token-Jaccard / fuzzy-ratio floor
CROSSWALK_MAX_CANDIDATES = 5     # cap surfaced candidates per field pair

# Value-crosswalk miner (ADR-0058 D4): hard per-run budget on sampled
# identifier FIELD PAIRS (|fields A| x |fields B|). A wide schema pair is
# truncated DETERMINISTICALLY (sorted (table, field) order) at this bound so
# sampling cost stays fixed regardless of schema width; truncation is logged.
# Read at CALL time (not def time) so tests can lower it via monkeypatch.
MINER_MAX_FIELD_PAIRS = 200

# Sequential-integer chance-overlap guard (ADR-0051 P6a): two sampled sides
# that BOTH classify as small sequential-integer (1..N-shaped) sets share
# values by construction, not identity — sequence numbers / row counters /
# positional indices enumerate the same range in every system. Rejected
# regardless of overlap count. Generic shape thresholds, no dataset knowledge.
SEQUENTIAL_INT_MIN_COUNT = 3     # fewer distinct ints can't establish a shape
SEQUENTIAL_INT_MAX_START = 2     # a 1..N (or 0..N / 2..N) enumeration start
SEQUENTIAL_INT_MIN_DENSITY = 0.8  # distinct ints / spanned range coverage

# Identifier-like field name tokens (generic — the shape of an identifier column,
# never a dataset column name).
_IDENTIFIER_NAME_TOKENS = ("id", "code", "key", "name", "no", "num", "ref", "sku",
                           "serial", "tag", "cell", "site", "line", "station")
# Measure-like field name tokens (counts / quantities / rates / sizes). A measure
# is NEVER a plausible join endpoint — this blocklist beats every positive signal
# in :func:`is_identifier_like`, INCLUDING an explicit graph ``role='identifier'``,
# because live evidence showed MAPS_TO role rows leaking measures into discovery
# (a ``*_count`` column is never a join key).
_MEASURE_NAME_TOKENS = ("count", "quantity", "qty", "rate", "size", "total",
                        "sum", "avg", "amount", "pct", "percent")
# Column data-types that never carry a stable join key (free text / blobs).
_NON_IDENTIFIER_TYPE_TOKENS = ("text", "clob", "blob", "json", "bytea")

# Timestamp-typed column detection + event-likeness (generic OT/MES vocabulary).
_TIMESTAMP_TYPE_TOKENS = ("timestamp", "datetime", "date", "time")
# A window BOUNDARY (a range endpoint) → windowed correlation.
_TIME_WINDOW_TOKENS = ("start", "complete", "end", "finish", "open", "close",
                       "begin", "from", "to")
# A point-in-time event → instant correlation.
_TIME_INSTANT_TOKENS = ("event", "updated", "modified", "changed", "logged",
                        "recorded", "occurred", "timestamp", "time", "at")


# =========================================================================== #
# PURE helpers — normalization transform ladder (no IO, fully offline-assertable)
# =========================================================================== #

def _clean_strings(values) -> list[str]:
    """Stripped, non-empty string form of sampled values (order-preserving)."""
    out: list[str] = []
    for v in values or []:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            out.append(s)
    return out


def _overlap(a_tokens: set[str], b_tokens: set[str]) -> dict:
    """Overlap of two token sets: ``{overlap_count, ratio, examples}``.

    ``ratio`` is size-normalized (shared / smaller set), mirroring
    :func:`resolve_evidence.value_overlap`. Pure."""
    shared = a_tokens & b_tokens
    smaller = min(len(a_tokens), len(b_tokens)) or 1
    return {
        "overlap_count": len(shared),
        "ratio": round(len(shared) / smaller, 3),
        "examples": sorted(shared)[:5],
    }


def _clears(overlap: dict, *, min_count: int, min_ratio: float) -> bool:
    """True when a measured overlap clears BOTH thresholds. Pure."""
    return (overlap.get("overlap_count", 0) >= min_count
            and overlap.get("ratio", 0) >= min_ratio)


def _leading_segment(value: str, delims: str) -> str:
    """The leading segment up to and INCLUDING the first delimiter (else '')."""
    for i, ch in enumerate(value):
        if ch in delims:
            return value[:i + 1]
    return ""


def _trailing_segment(value: str, delims: str) -> str:
    """The trailing segment from the last delimiter to the end (else '')."""
    for i in range(len(value) - 1, -1, -1):
        if value[i] in delims:
            return value[i:]
    return ""


def _dominant_affix(values: list[str], *, kind: str, delims: str = _AFFIX_DELIMS) -> str:
    """The systematic prefix/suffix shared by a MAJORITY of ``values`` (else '').

    ``kind`` is ``"prefix"`` or ``"suffix"``. Deterministic: the affix covering the
    most values wins, requiring at least half the values (so a stray coincidental
    segment is never treated as systematic). Pure, generic."""
    seg = _leading_segment if kind == "prefix" else _trailing_segment
    counts: dict[str, int] = {}
    for v in values:
        s = seg(v, delims)
        if s:
            counts[s] = counts.get(s, 0) + 1
    if not counts:
        return ""
    best, n = max(counts.items(), key=lambda kv: (kv[1], len(kv[0])))
    return best if n * 2 >= len(values) else ""


def _strip_prefix_set(values: list[str], affix: str) -> set[str]:
    """Strip ``affix`` from the front of any value that carries it (idempotent)."""
    return {v[len(affix):] if v.startswith(affix) else v for v in values}


def _strip_suffix_set(values: list[str], affix: str) -> set[str]:
    """Strip ``affix`` from the end of any value that carries it (idempotent)."""
    return {v[:-len(affix)] if affix and v.endswith(affix) else v for v in values}


def passes_key_cardinality(a_set, b_set, shared_keys) -> bool:
    """True when POST-TRANSFORM key sets look like a real join key space. Pure.

    ``shared_keys`` is the FULL post-transform shared key set (``a_set & b_set``)
    — NOT a truncated example list. Judging a ``sorted(...)[:5]`` sample is
    digit-first biased (digits sort before letters), so a legitimate mixed key
    set like ``{'1','2','3','7','9','LINE-A'}`` would present an all-numeric
    sample and be wrongly declined.

    Rejects degenerate keys the overlap thresholds alone cannot catch:

      * a tiny key space — fewer than :data:`JOIN_MIN_KEY_CARDINALITY` distinct
        values on EITHER side after normalization;
      * a small-int range — EVERY shared key is a short (``<= 2`` digit)
        numeric, the signature of a stripped affix landing on sequence numbers /
        row counters rather than identifiers (live evidence: ``strip_prefix``
        rungs manufacturing overlap between unrelated id columns on ``1..12``).
        A single non-short-numeric shared key breaks the signature.
    """
    if min(len(a_set or ()), len(b_set or ())) < JOIN_MIN_KEY_CARDINALITY:
        return False
    keys = [str(k) for k in (shared_keys or [])]
    if keys and all(k.isdigit() and len(k) <= 2 for k in keys):
        return False
    return True


def is_sequential_integer_set(values) -> bool:
    """True when sampled values form a small sequential-integer (1..N) shape.

    ADR-0051 P6a chance-overlap guard: an all-integer value set that starts at
    or below :data:`SEQUENTIAL_INT_MAX_START` and covers at least
    :data:`SEQUENTIAL_INT_MIN_DENSITY` of its spanned range is an ENUMERATION
    (sequence numbers, row counters, positional indices) — when BOTH sampled
    sides look like this, shared values are guaranteed by construction and
    carry zero identity signal. A set of real ids that merely happen to be
    numeric (sparse, or starting high) does not match. Pure, generic."""
    vals = _clean_strings(values)
    if len(vals) < SEQUENTIAL_INT_MIN_COUNT or not all(v.isdigit() for v in vals):
        return False
    nums = sorted({int(v) for v in vals})
    if len(nums) < SEQUENTIAL_INT_MIN_COUNT or nums[0] > SEQUENTIAL_INT_MAX_START:
        return False
    density = len(nums) / (nums[-1] - nums[0] + 1)
    return density >= SEQUENTIAL_INT_MIN_DENSITY


def _ladder_rung(label: str, sa: set, sb: set, *, min_count: int,
                 min_ratio: float) -> tuple[str, dict | None]:
    """Evaluate ONE ladder rung → ``(verdict, result)``. Pure.

    Verdicts: ``"win"`` (thresholds AND key-cardinality cleared — result
    carried), ``"abort"`` (thresholds cleared but the post-transform key sets
    are degenerate — the WHOLE ladder declines, per the cleared-but-degenerate
    contract), ``"miss"`` (thresholds not cleared — try the next rung)."""
    ov = _overlap(sa, sb)
    if not _clears(ov, min_count=min_count, min_ratio=min_ratio):
        return "miss", None
    if not passes_key_cardinality(sa, sb, sa & sb):
        return "abort", None
    return "win", {"transform": label, **ov}


def _affix_rungs(a: list[str], b: list[str]) -> list[tuple[str, set, set]]:
    """The dynamic strip_affix rung candidates (label, a-set, b-set). Pure.

    A systematic prefix/suffix learned from EITHER side, applied to BOTH
    (idempotent) — the original ladder-step-3 candidate generation."""
    candidates: list[tuple[str, set, set]] = []
    for side in (a, b):
        p = _dominant_affix(side, kind="prefix")
        if p:
            candidates.append((f"strip_prefix:{p}",
                               _strip_prefix_set(a, p), _strip_prefix_set(b, p)))
        s = _dominant_affix(side, kind="suffix")
        if s:
            candidates.append((f"strip_suffix:{s}",
                               _strip_suffix_set(a, s), _strip_suffix_set(b, s)))
    return candidates


def _token_set(values: list[str]) -> set[str]:
    """Scalar leaves mined out of JSON/struct blobs (token_extract rung). Pure."""
    tokens: set[str] = set()
    for v in values:
        tokens.update(extract_join_tokens(v))
    return tokens


def discover_transform(
    a_values, b_values, *,
    min_count: int = JOIN_MIN_OVERLAP_COUNT,
    min_ratio: float = JOIN_MIN_RATIO,
) -> dict | None:
    """Run the normalization transform ladder; return the FIRST rung that clears.

    Ladder (in order): ``identity`` → ``casefold`` → ``strip_affix`` (a
    systematic prefix/suffix learned from the data) → ``token_extract``
    (JSON/struct scalar-leaf mining). Each rung is judged by
    :func:`_ladder_rung`: a rung that clears the overlap thresholds must ALSO
    pass :func:`passes_key_cardinality` — a cleared-but-degenerate rung
    declines the whole ladder (``None``) rather than falling through.

    ADR-0051 P6a: when BOTH sides classify as small sequential-integer sets
    (:func:`is_sequential_integer_set`), the ladder never runs — shared 1..N
    values are chance overlap, not evidence, regardless of overlap count.

    Returns ``{"transform", "overlap_count", "ratio", "examples"}`` for the
    winning rung, or ``None``. Pure — no IO, no LLM."""
    a = _clean_strings(a_values)
    b = _clean_strings(b_values)
    if not a or not b:
        return None
    if is_sequential_integer_set(a) and is_sequential_integer_set(b):
        return None  # P6a: 1..N enumerations overlap by construction
    rungs: list[tuple[str, set, set]] = [
        (TRANSFORM_IDENTITY, set(a), set(b)),
        (TRANSFORM_CASEFOLD, {v.casefold() for v in a}, {v.casefold() for v in b}),
        *_affix_rungs(a, b),
        (TRANSFORM_TOKEN_EXTRACT, _token_set(a), _token_set(b)),
    ]
    for label, sa, sb in rungs:
        verdict, result = _ladder_rung(
            label, sa, sb, min_count=min_count, min_ratio=min_ratio)
        if verdict == "win":
            return result
        if verdict == "abort":
            return None
    return None


# =========================================================================== #
# PURE helpers — time-key (event-time window alignment)
# =========================================================================== #

def camel_split(name: str) -> str:
    """Insert ``_`` at lower→Upper camelCase boundaries (``DeviceState`` →
    ``Device_State``). Public seam shared with the onboarding writer gate —
    CamelCase compounds must tokenize like their snake_case twins —
    ``CycleCount``/``DeviceState`` evaded the token vocabularies
    because lowering first erases the word boundary. Pure."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name or "")


def _norm_tokens(name: str) -> set[str]:
    """Lowercase word tokens of a field/column name; camelCase boundaries
    split BEFORE lowering (``CycleCount`` → ``{cycle, count}``). Pure."""
    return {t for t in re.split(r"[^a-z0-9]+", camel_split(name).lower()) if t}


def is_timestamp_type(data_type: str | None) -> bool:
    """True when a column data-type reads as a timestamp/date/time type. Generic."""
    dt = (data_type or "").lower()
    return any(tok in dt for tok in _TIMESTAMP_TYPE_TOKENS)


def _event_score(field_name: str) -> tuple[int, str]:
    """Score a timestamp field's event-likeness → ``(score, semantics)``.

    A window-boundary token (start/complete/…) scores highest and implies
    ``"window"`` semantics; a point-event token (event/updated/timestamp/…) implies
    ``"instant"``. An un-tokenized timestamp still scores 1 (instant). Pure."""
    toks = _norm_tokens(field_name)
    if toks & set(_TIME_WINDOW_TOKENS):
        return (3, "window")
    if toks & set(_TIME_INSTANT_TOKENS):
        return (2, "instant")
    return (1, "instant")


def pick_time_key(a_time_fields, b_time_fields) -> dict | None:
    """Pick the most event-like timestamp pair across two systems.

    ``a_time_fields`` / ``b_time_fields`` are lists of timestamp-typed field names
    (already type-filtered). Returns ``{"from_time_field", "to_time_field",
    "semantics"}`` (``semantics`` = ``"window"`` when EITHER chosen field is a
    window boundary, else ``"instant"``), or ``None`` when either side has no
    timestamp field. Pure — deterministic (ties broken by name)."""
    a = [f for f in (a_time_fields or []) if f]
    b = [f for f in (b_time_fields or []) if f]
    if not a or not b:
        return None
    a_best = sorted(a, key=lambda f: (_event_score(f)[0], f), reverse=True)[0]
    b_best = sorted(b, key=lambda f: (_event_score(f)[0], f), reverse=True)[0]
    semantics = "window" if "window" in (_event_score(a_best)[1],
                                         _event_score(b_best)[1]) else "instant"
    return {"from_time_field": a_best, "to_time_field": b_best, "semantics": semantics}


# =========================================================================== #
# PURE helpers — identifier-like field selection + crosswalk candidate generation
# =========================================================================== #

def is_measure_like(field_name: str) -> bool:
    """True when a field name reads like a MEASURE (count/quantity/rate/size/…).

    A measure is never a plausible join endpoint. This is a NEGATIVE override
    that beats every positive signal in :func:`is_identifier_like` — including an
    explicit graph ``role == 'identifier'`` — because live evidence showed
    MAPS_TO role rows leaking measures into discovery (a ``*_count`` /
    ``*_quantity`` / ``sample_size`` column is never a join key). Pure — no
    dataset literals."""
    return bool(_norm_tokens(field_name) & set(_MEASURE_NAME_TOKENS))


def is_identifier_like(field_name: str, role: str | None = None,
                       data_type: str | None = None) -> bool:
    """True when a field is a plausible cross-system JOIN KEY (generic heuristics).

    Positive signals (any): the graph MAPS_TO ``role == 'identifier'``; a name
    token that reads like an identifier (id/code/key/name/site/line/…). Negative
    overrides: a MEASURE-like name (see :func:`is_measure_like`) is never a join
    key — deliberately beating even an explicit identifier role, since role rows
    leak measures; a free-text/blob data-type never carries a stable join key.
    Pure — no dataset literals."""
    if is_measure_like(field_name):
        # Measure blocklist beats EVERYTHING, including role == 'identifier'.
        return False
    if (data_type or "") and any(tok in (data_type or "").lower()
                                 for tok in _NON_IDENTIFIER_TYPE_TOKENS):
        # Free-text/blob columns are excluded UNLESS explicitly an identifier role.
        if (role or "").lower() != "identifier":
            return False
    if (role or "").lower() == "identifier":
        return True
    return bool(_norm_tokens(field_name) & set(_IDENTIFIER_NAME_TOKENS))


def _pair_similarity(a: str, b: str) -> float:
    """Combined name/token similarity in [0,1]: max(token-Jaccard, fuzzy-ratio).

    Token-Jaccard catches shared normalized tokens (word overlap); the difflib
    fuzzy ratio catches near-spellings. SIMILARITY-FLOOR GUARD (live evidence —
    coincidence-prone crosswalks like '9'↔'9' entering the queue): a pair needs
    an ALPHABETIC ANCHOR. A side with no alphabetic character at all (a bare
    number) can never anchor a crosswalk → 0.0; and digit-only shared tokens
    ('Line 9' vs 'Bay 9' sharing only '9') never count toward the Jaccard — the
    fuzzy ratio alone must then clear the floor. Pure, generic."""
    if not any(c.isalpha() for c in (a or "")) or not any(c.isalpha() for c in (b or "")):
        return 0.0
    at, bt = _norm_tokens(a), _norm_tokens(b)
    shared = at & bt
    # Only an alphabetic shared token counts as word overlap.
    anchored = {t for t in shared if any(c.isalpha() for c in t)}
    jacc = len(shared) / len(at | bt) if anchored and (at or bt) else 0.0
    fuzzy = difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()
    return round(max(jacc, fuzzy), 3)


# ADR-0051 P6b: a trailing positional suffix — optional delimiter + digits.
_POSITIONAL_SUFFIX_RE = re.compile(r"[\s_\-/.]*(\d+)$")


def _positional_stem_suffix(value: str) -> tuple[str, str]:
    """Split a value into (stem, trailing positional digits). Pure.

    ``"AAA-2"`` → ``("AAA", "2")``; ``"Station 12"`` → ``("Station", "12")``;
    no trailing digits → ``(value, "")``."""
    m = _POSITIONAL_SUFFIX_RE.search(value or "")
    if not m:
        return (value or "", "")
    return ((value or "")[:m.start()], m.group(1))


def is_positional_suffix_only_match(
    a: str, b: str, *, min_similarity: float = CROSSWALK_MIN_SIMILARITY,
) -> bool:
    """True when the ONLY similarity signal is a shared positional suffix.

    ADR-0051 P6b: two values ending in the SAME trailing number (``AAA-2`` vs
    ``BBB-2``) agree on POSITION, not identity — position repeats at every
    site/line/station. The pair is a suffix-only match when both carry the
    same numeric suffix AND the remaining stems alone do not clear the
    similarity floor. Pairs whose stems genuinely resemble each other
    (``Milling Line 2`` vs ``Milling Cell 2``) are untouched. Pure, generic."""
    stem_a, suf_a = _positional_stem_suffix(a)
    stem_b, suf_b = _positional_stem_suffix(b)
    if not suf_a or not suf_b:
        return False
    if (suf_a.lstrip("0") or "0") != (suf_b.lstrip("0") or "0"):
        return False
    return _pair_similarity(stem_a, stem_b) < min_similarity


def crosswalk_candidates(
    a_values, b_values, *,
    min_similarity: float = CROSSWALK_MIN_SIMILARITY,
    max_candidates: int = CROSSWALK_MAX_CANDIDATES,
) -> list[dict]:
    """Propose value→value crosswalk pairs by NAME/TOKEN similarity.

    For DISJOINT-vocabulary entities (zero value overlap) that should relate, this
    surfaces the best-matching pairs for HUMAN review — each ``{"from_value",
    "to_value", "similarity"}`` above ``min_similarity``, best first, one target per
    source, capped at ``max_candidates``. Pure, deterministic, generic.

    This is a deliberately-simple first pass; richer BEHAVIORAL/LLM matching is a
    documented follow-on seam (see :func:`behavioral_crosswalk_candidates`)."""
    a = _clean_strings(a_values)
    b = _clean_strings(b_values)
    if not a or not b:
        return []
    scored: list[dict] = []
    used_targets: set[str] = set()
    for av in a:
        best_b, best_s = None, 0.0
        for bv in b:
            if bv in used_targets:
                continue
            if is_positional_suffix_only_match(
                    av, bv, min_similarity=min_similarity):
                continue  # ADR-0051 P6b: shared position is not identity
            s = _pair_similarity(av, bv)
            if s > best_s:
                best_b, best_s = bv, s
        if best_b is not None and best_s >= min_similarity:
            used_targets.add(best_b)
            scored.append({"from_value": av, "to_value": best_b, "similarity": best_s})
    scored.sort(key=lambda c: (c["similarity"], c["from_value"]), reverse=True)
    return scored[:max_candidates]


def behavioral_crosswalk_candidates(*_args, **_kwargs) -> list[dict]:
    """FOLLOW-ON SEAM (NOT IMPLEMENTED): behavioral / LLM crosswalk matching.

    The name/token similarity in :func:`crosswalk_candidates` bridges disjoint
    vocabularies only when the two sides share surface tokens. The richer path —
    correlating entities by BEHAVIOR (co-occurrence in the same time windows under a
    joined parent, event-rate fingerprints) and/or an LLM semantic match over the
    two value populations — belongs here. Intentionally a stub so the accept/reject
    crosswalk PATH is complete and this can be filled in without touching callers.
    Returns ``[]`` today (a no-op, never fabricates a match)."""
    return []


# =========================================================================== #
# PURE helpers — value-pair crosswalk mining (ADR-0058 D2/D3)
# =========================================================================== #

def _has_alpha(value: str) -> bool:
    """True when a value carries at least one alphabetic character. Pure."""
    return any(c.isalpha() for c in (value or ""))


def _affix_stripper(transform: str | None):
    """Value-normalizer callable for a ``strip_affix`` ladder label, else ``None``.

    Accepts the dynamic rung labels ``strip_prefix:<affix>`` /
    ``strip_suffix:<affix>`` exactly as :func:`discover_transform` emits them
    (and as an accepted ``join_path`` proposal stores them). Idempotent on
    values that do not carry the affix. Pure."""
    t = transform or ""
    if t.startswith("strip_prefix:"):
        affix = t.split(":", 1)[1]
        return (lambda v: v[len(affix):] if affix and v.startswith(affix) else v)
    if t.startswith("strip_suffix:"):
        affix = t.split(":", 1)[1]
        return (lambda v: v[:-len(affix)] if affix and v.endswith(affix) else v)
    return None


def _affix_aligned_pairs(a: list[str], b: list[str],
                         transform: str | None) -> list[dict]:
    """Affix-strip EQUALITY alignment: pairs whose values become equal after the
    given ``strip_affix`` ladder rung (``WO-LINE-A`` ↔ ``LINE-A``). Pure.

    Skips identical pairs (nothing to bridge) and pairs with NO alphabetic
    anchor on either side (two bare numbers agree by chance, never identity —
    the same guard :func:`_pair_similarity` applies). ``support`` is 1.0:
    post-transform equality is exact, not fuzzy."""
    strip = _affix_stripper(transform)
    if strip is None:
        return []
    b_by_key: dict[str, str] = {}
    for bv in b:
        b_by_key.setdefault(strip(bv), bv)
    pairs: list[dict] = []
    for av in a:
        bv = b_by_key.get(strip(av))
        if bv is None or bv == av:
            continue
        if not (_has_alpha(av) or _has_alpha(bv)):
            continue  # digit-only pair: chance agreement, never a crosswalk
        pairs.append({"from_value": av, "to_value": bv,
                      "method": f"affix_strip:{transform}", "support": 1.0})
    return pairs


def value_pair_candidates(
    a_values, b_values, transform: str | None = None, *,
    max_candidates: int = CROSSWALK_MAX_CANDIDATES,
) -> list[dict]:
    """Aligned per-VALUE crosswalk candidates from two bounded samples.

    ADR-0058 D2: the steward-earned crosswalk class is per-value bridges
    (``from_value → to_value``). Two alignment heuristics over the SAME bounded
    samples join discovery already takes — both COMPUTED, zero LLM (D3):

      * affix-strip equality after a ladder rung (:func:`_affix_aligned_pairs`,
        ``support`` 1.0), and
      * near-token pairs from :func:`crosswalk_candidates`
        (``method="token_similarity"``, ``support`` = similarity).

    Refusals (chance-overlap guards carry over): BOTH sides classifying as
    small sequential-integer sets (:func:`is_sequential_integer_set`) yields
    ``[]``; a pair with no alphabetic anchor on either side never aligns;
    identical values are never bridged. Deterministic (sorted best-first by
    ``(-support, from_value, to_value)``), capped at ``max_candidates``. Pure."""
    a = _clean_strings(a_values)
    b = _clean_strings(b_values)
    if not a or not b:
        return []
    if is_sequential_integer_set(a) and is_sequential_integer_set(b):
        return []  # P6a: 1..N enumerations align by construction, not identity
    out = _affix_aligned_pairs(a, b, transform)
    taken_from = {p["from_value"] for p in out}
    taken_to = {p["to_value"] for p in out}
    for c in crosswalk_candidates(a, b, max_candidates=max_candidates):
        if c["from_value"] == c["to_value"]:
            continue
        if c["from_value"] in taken_from or c["to_value"] in taken_to:
            continue
        out.append({"from_value": c["from_value"], "to_value": c["to_value"],
                    "method": "token_similarity",
                    "support": float(c["similarity"])})
    out.sort(key=lambda p: (-p["support"], p["from_value"], p["to_value"]))
    return out[:max_candidates]


def value_crosswalk_evidence(from_label: str, to_label: str, method: str,
                             aligned_count: int, sampled_count: int,
                             examples: list[dict], date_str: str) -> str:
    """The COMPUTED evidence string a mined crosswalk carries (ADR-0058 D3).

    Format: ``cross-system mining <date>: N of M sampled <field A> values align
    with <field B> via <method> (e.g. a -> b; ...)`` — counts and method from
    pure set math, at most FIVE example pairs (the ``_emit_join_path``
    convention: examples in a proposal row are steward-facing data, and
    pending-proposal text is never composed into prompts). Never LLM. Pure."""
    ex = "; ".join(f"{p['from_value']} -> {p['to_value']}"
                   for p in (examples or [])[:5])
    tail = f" (e.g. {ex})" if ex else ""
    return (f"cross-system mining {date_str}: {aligned_count} of "
            f"{sampled_count} sampled {from_label} values align with "
            f"{to_label} via {method}{tail}")


def miner_field_pairs(fields_a, fields_b, *,
                      max_pairs: int | None = None) -> tuple[list, bool]:
    """Deterministic identifier field-pair worklist, budget-truncated. Pure.

    Sorts BOTH sides by ``(table, field)`` before taking the cross product, so
    the pairs a truncation keeps never depend on graph read order.
    ``max_pairs`` defaults to :data:`MINER_MAX_FIELD_PAIRS` at CALL time.
    Returns ``(pairs, truncated)``."""
    if max_pairs is None:
        max_pairs = MINER_MAX_FIELD_PAIRS
    def _key(f):
        return (f.get("table", ""), f.get("field", ""))
    pairs = [(fa, fb) for fa in sorted(fields_a or [], key=_key)
             for fb in sorted(fields_b or [], key=_key)]
    if len(pairs) <= max_pairs:
        return pairs, False
    return pairs[:max_pairs], True


def _canonical_direction(from_system, from_field, from_value,
                         to_system, to_field, to_value) -> tuple:
    """Order two crosswalk sides by ``(system, field)``. Pure.

    Similarity/equality alignment is symmetric, so an A→B and a B→A pass over
    the same field pair describe ONE reviewable bridge — canonical ordering
    makes both passes produce the identical proposal (collapsed by
    ``log_proposed_mapping``'s lookup-before-write dedup)."""
    if (to_system, to_field) < (from_system, from_field):
        return (to_system, to_field, to_value, from_system, from_field, from_value)
    return (from_system, from_field, from_value, to_system, to_field, to_value)


# =========================================================================== #
# IO seams (monkeypatch targets in tests) — best-effort, never fabricate
# =========================================================================== #

#: True inside a strict sweep (:func:`discover_joins_for_system` ``strict``):
#: the read seam raises on a failed read instead of answering ``[]``, so a
#: peer list or a pair's field read that failed is reported, not swept as empty.
_STRICT_READS: ContextVar[bool] = ContextVar("join_discovery_strict_reads", default=False)


def _neptune_query(cypher: str, parameters: dict | None = None) -> list:
    """Module-global Neptune read seam (monkeypatch target in tests). In a
    strict sweep (:data:`_STRICT_READS`) a checked read: a failure raises."""
    if _STRICT_READS.get():
        from .db import neptune_query_checked
        return neptune_query_checked(cypher, parameters) or []
    from .db import neptune_query
    return neptune_query(cypher, parameters)


def _log_proposed_mapping(**kwargs):
    """Module-global proposal seam (monkeypatch target in tests)."""
    from .self_learning import log_proposed_mapping
    return log_proposed_mapping(**kwargs)


def _file_unless_removing(**kwargs):
    """File one proposal through :func:`_log_proposed_mapping`, unless an
    endpoint's removal has started. The peer list (:func:`_onboarded_systems`)
    is read once per sweep and sampling a pair can take minutes, so the
    marker is read again right before each filing; a proposal against a
    system being removed would point at a system that is going away. Answers
    the proposal id, or ``None`` when nothing was filed. A failed marker read
    leaves the catalog's answer (``catalog.removing_system_ids``; the accept
    re-checks the marker)."""
    from .catalog import REMOVING_SYSTEMS_READ, removing_system_ids
    ends = {kwargs.get("from_system"), kwargs.get("to_system")}
    marked = removing_system_ids(_neptune_query(REMOVING_SYSTEMS_READ, {})) & ends
    if marked:
        logger.info("join discovery: %s proposal not filed, %s being removed",
                    kwargs.get("proposed_type"), ", ".join(sorted(marked)))
        return None
    return _log_proposed_mapping(**kwargs)


def _system_sample_meta(system_id: str) -> tuple[str, str, dict]:
    """Resolve ``(connection_type, schema, connection_config)`` for sampling.

    Mirrors ``concept_suggest._peer_meta`` (DRY intent) — catalog entry first, then
    the node connection resolver for the non-secret Athena config. Best-effort."""
    from .catalog import catalog
    te = catalog.get(system_id) or {}
    cfg = te.get("connection_config") or {}
    if not cfg:
        try:
            from .connection_resolver import resolve_connection
            rc = resolve_connection(system_id) or {}
            cfg = {k: rc[k] for k in ("workgroup", "database", "catalog",
                                      "output_location", "region") if rc.get(k)}
        except Exception:  # noqa: BLE001 - best-effort connection lookup
            cfg = {}
    return (te.get("connection_type", "PostgreSQL"), te.get("schema", ""), cfg)


def _sample(system_id: str, table: str, field: str) -> list[str]:
    """Sample distinct values for a field (best-effort, reuses resolve_evidence)."""
    from .resolve_evidence import sample_field_values
    ct, schema, cfg = _system_sample_meta(system_id)
    return sample_field_values(system_id=system_id, connection_type=ct, schema=schema,
                               table=table, field=field, connection_config=cfg)


def _probe(system_id: str, table: str, field: str, values) -> "list[str] | None":
    """Which ``values`` exist in ``system_id``'s ``table.field`` (IO seam).

    ``None`` = the source cannot be probed; ``[]`` = probed, none found."""
    from .resolve_evidence import probe_field_values
    ct, schema, _cfg = _system_sample_meta(system_id)
    return probe_field_values(system_id, ct, schema, table, field, values)


# --------------------------------------------------------------------------- #
# Containment probe (sparse foreign keys)
# --------------------------------------------------------------------------- #
# Independent samples cannot see a SPARSE foreign key (a child table that
# references a small part of a large parent key space) — live-found
# 2026-09-24: ERP ``lot_mes_tracking.work_order_id`` references MES
# ``work_orders.work_order_id`` exactly, yet two arbitrary 200-value samples
# shared nothing and no MES↔ERP join was ever proposed. When the ladder finds
# nothing for a pair that plausibly names the same key, probe each side's
# sample for MEMBERSHIP in the other side instead.

#: A containment hit on high-entropy keys (UUIDs, long mixed tokens) is not
#: chance, so the ratio floor relaxes — a child sample drawn across several
#: sites legitimately finds only the probed site's share in the parent.
CONTAINMENT_MIN_RATIO_HIGH_ENTROPY = 0.1
#: Minimum length for a key to count as high-entropy.
_HIGH_ENTROPY_MIN_LEN = 12


def _column(field: str) -> str:
    return (field or "").split(".")[-1].strip().lower()


def containment_probe_eligible(fa: dict, fb: dict) -> bool:
    """Probe only pairs that plausibly name the SAME key — identical column
    name or a shared mapped concept — bounding probe cost. Pure."""
    if _column(fa.get("field", "")) and _column(fa.get("field", "")) == _column(fb.get("field", "")):
        return True
    return bool(fa.get("concept")) and fa.get("concept") == fb.get("concept")


def _high_entropy(values) -> bool:
    """True when every value is a long, non-numeric token (UUID-like). Pure."""
    vals = list(values)
    return bool(vals) and all(
        len(v) >= _HIGH_ENTROPY_MIN_LEN and not v.isdigit() for v in vals)


def judge_containment(sample, found, *, min_count: int = JOIN_MIN_OVERLAP_COUNT,
                      min_ratio: float = JOIN_MIN_RATIO) -> "dict | None":
    """Verdict for one containment probe: ``found`` ⊆ ``sample`` are the
    sampled values that EXIST on the other side. Returns the identity-transform
    overlap dict or ``None``. Pure — same count, cardinality and P6a gates as
    the ladder; the ratio floor relaxes only for high-entropy keys."""
    s = set(_clean_strings(sample))
    f = set(_clean_strings(found)) & s
    if not s or not f or is_sequential_integer_set(s):
        return None
    ratio = round(len(f) / len(s), 3)
    floor = CONTAINMENT_MIN_RATIO_HIGH_ENTROPY if _high_entropy(f) else min_ratio
    if len(f) < min_count or ratio < floor:
        return None
    if not passes_key_cardinality(s, f, f):
        return None
    return {"transform": TRANSFORM_IDENTITY, "overlap_count": len(f),
            "ratio": ratio, "examples": sorted(f)[:5], "method": "containment_probe"}


def _containment_transform(system_a, fa, a_vals, system_b, fb, b_vals, probe=None) -> "dict | None":
    """Probe b's sample in a, then a's sample in b; first verdict wins."""
    probe = probe or _probe
    for sample, sid, f in ((b_vals, system_a, fa), (a_vals, system_b, fb)):
        found = probe(sid, f["table"], f["field"], sample)
        if found is None:
            continue
        verdict = judge_containment(sample, found)
        if verdict:
            return verdict
    return None


# =========================================================================== #
# IO orchestration — graph reads (best-effort)
# =========================================================================== #

def _onboarded_systems() -> list[str]:
    """All onboarded data-system ids that expose schema fields (excludes the graph).

    Order-independent: a plain set of ids, sorted for deterministic sweeps. Excludes
    the knowledge-graph target itself (the rules engine is not a joinable source)
    and every system being removed or half-removed (``catalog.removed_ids``: the
    graph marker or this process's catalog state): its schema stays in the graph
    until its removal cascade reaches the graph step, and a join found against it
    would name a system that is on its way out."""
    from .catalog import REMOVAL_STARTED_COLUMN, removed_ids
    from .proposal_scoring import is_knowledge_graph_system
    rows = _neptune_query(
        "MATCH (s:System)-[:HAS_SCHEMA]->(:SchemaEntry)-[:HAS_FIELD]->(:Field) "
        "RETURN DISTINCT s.id AS id, " + REMOVAL_STARTED_COLUMN, {}
    ) or []
    ids = {r.get("id") for r in rows if r.get("id")} - removed_ids(rows, "id")
    return sorted(i for i in ids if not is_knowledge_graph_system(i))


def _identifier_fields(system_id: str) -> list[dict]:
    """Identifier-like fields for a system → ``[{table, field, concept, role, data_type}]``.

    Reads Field nodes (with any MAPS_TO concept/role and the physical data_type),
    keeps only fields :func:`is_identifier_like` accepts. Deduped per (table, field),
    preferring the identifier-role row. Best-effort → ``[]``."""
    rows = _neptune_query(
        "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry)-[:HAS_FIELD]->(f:Field) "
        "OPTIONAL MATCH (f)-[r:MAPS_TO]->(c:Concept) "
        "RETURN se.name AS table, f.name AS field, c.id AS concept, r.role AS role, "
        "f.data_type AS data_type",
        {"sid": system_id},
    ) or []
    by_key: dict[tuple[str, str], dict] = {}
    for r in rows:
        tbl, fld = r.get("table"), r.get("field")
        if not tbl or not fld:
            continue
        if not is_identifier_like(fld, r.get("role"), r.get("data_type")):
            continue
        rec = {"table": tbl, "field": fld, "concept": r.get("concept") or "",
               "role": r.get("role") or "", "data_type": r.get("data_type") or ""}
        key = (tbl, fld)
        cur = by_key.get(key)
        if cur is None or ((rec["role"] or "").lower() == "identifier"
                           and (cur["role"] or "").lower() != "identifier"):
            by_key[key] = rec
    return list(by_key.values())


def _timestamp_fields(system_id: str) -> list[str]:
    """Timestamp-typed field names for a system (for time_key). Best-effort → ``[]``."""
    rows = _neptune_query(
        "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(:SchemaEntry)-[:HAS_FIELD]->(f:Field) "
        "RETURN f.name AS field, f.data_type AS data_type",
        {"sid": system_id},
    ) or []
    out: list[str] = []
    seen: set[str] = set()
    for r in rows:
        fld, dt = r.get("field"), r.get("data_type")
        if fld and fld not in seen and (is_timestamp_type(dt)
                                        or _norm_tokens(fld) & set(_TIME_INSTANT_TOKENS)
                                        or _norm_tokens(fld) & set(_TIME_WINDOW_TOKENS)):
            seen.add(fld)
            out.append(fld)
    return out


def _has_established_join(system_a: str, system_b: str) -> bool:
    """True when an accepted JoinPath already connects the two systems (either dir).

    This is the "already-joined parent" gate for surfacing a crosswalk candidate:
    two systems only warrant a crosswalk when they are ALREADY related. A
    JoinPath whose IdentityRule is switched off does not count
    (:mod:`joinpath_rule_link`). Best-effort."""
    rows = _neptune_query(
        "MATCH (jp:JoinPath) WHERE ((jp.from_system = $a AND jp.to_system = $b) "
        "OR (jp.from_system = $b AND jp.to_system = $a)) "
        "AND " + _jp_rule.rule_active("jp") + " "
        + _jp_rule.drop_if_rule_inactive() + "RETURN count(jp) AS n",
        {"a": system_a, "b": system_b},
    ) or []
    try:
        return bool(rows) and int(rows[0].get("n") or 0) > 0
    except (TypeError, ValueError):
        return False


# =========================================================================== #
# IO orchestration — pair + sweep discovery (idempotent, order-independent)
# =========================================================================== #

def _qualified(table: str, field: str) -> str:
    """``table.field`` label, avoiding a doubled table segment. Pure."""
    t, f = (table or "").strip(), (field or "").strip()
    if not t:
        return f
    if not f or f == t or f.startswith(t + "."):
        return f
    return f"{t}.{f}"


def _emit_join_path(from_system, from_field, to_system, to_field, concept,
                    transform, overlap, time_key) -> str | None:
    """Emit a ``join_path`` proposal carrying the transform + optional time_key."""
    ratio = overlap.get("ratio", 0) or 0
    confidence = round(min(0.95, 0.6 + 0.35 * float(ratio)), 3)
    kind = ("containment-probe join" if overlap.get("method") == "containment_probe"
            else "value-overlap join")
    return _file_unless_removing(
        from_system=from_system, from_field=from_field, from_value="",
        to_system=to_system, to_field=to_field, to_value="",
        concept=concept, confidence=confidence,
        query_context=(f"Auto-analysis: {kind} ({transform}, "
                       f"{overlap.get('overlap_count', 0)} shared e.g. "
                       f"{overlap.get('examples', [])})"),
        proposed_type="join_path", transform=transform, time_key=time_key,
        source=MINER_SOURCE, method=MINER_METHOD,
        provenance=MINER_PROVENANCE,
    )


def _emit_crosswalk(from_system, from_field, to_system, to_field, concept,
                    candidate) -> str | None:
    """Emit a reviewable ``crosswalk`` candidate (confirmed → ValueMapping).

    DIRECTION-CANONICAL: name/token similarity is symmetric, so an A→B and a
    B→A sweep pass over the same field pair describe ONE reviewable bridge —
    the sides are ordered by system id before emitting, making both passes
    produce the identical proposal (collapsed by ``log_proposed_mapping``'s
    lookup-before-write dedup) instead of two mirrored queue entries."""
    (from_system, from_field, from_value, to_system, to_field, to_value) = (
        _canonical_direction(from_system, from_field, candidate["from_value"],
                             to_system, to_field, candidate["to_value"]))
    return _file_unless_removing(
        from_system=from_system, from_field=from_field,
        from_value=from_value,
        to_system=to_system, to_field=to_field, to_value=to_value,
        concept=concept,
        confidence=round(float(candidate.get("similarity", 0.0)), 3),
        query_context=(f"Auto-analysis: crosswalk candidate (name/token similarity "
                       f"{candidate.get('similarity')}) — disjoint values under a "
                       f"joined parent; confirm to persist a ValueMapping"),
        proposed_type="crosswalk",
        source=MINER_SOURCE, method=MINER_METHOD,
        provenance=MINER_PROVENANCE,
    )


def _emit_value_crosswalk(from_system, from_field, to_system, to_field, concept,
                          candidate, evidence: str) -> str | None:
    """Emit ONE mined value bridge as a reviewable ``crosswalk`` proposal.

    ADR-0058 D2: the SAME grain as :func:`_emit_crosswalk` (value_mapping-shaped,
    never auto-accepted, direction-canonicalized via
    :func:`_canonical_direction`); the ``query_context`` carries the COMPUTED
    evidence string built by :func:`value_crosswalk_evidence` — never LLM text.
    Confidence is the candidate's alignment ``support``."""
    (from_system, from_field, from_value, to_system, to_field, to_value) = (
        _canonical_direction(from_system, from_field, candidate["from_value"],
                             to_system, to_field, candidate["to_value"]))
    return _file_unless_removing(
        from_system=from_system, from_field=from_field, from_value=from_value,
        to_system=to_system, to_field=to_field, to_value=to_value,
        concept=concept,
        confidence=round(float(candidate.get("support", 0.0)), 3),
        query_context=evidence,
        proposed_type="crosswalk",
        source=MINER_SOURCE, method=MINER_METHOD,
        provenance=MINER_PROVENANCE,
    )


def _bump_usage(refs) -> None:
    """Module-global usage-ledger seam (ADR-0052 — the writer ships with its
    telemetry). Best-effort: a metrics bump never fails a mining run."""
    try:
        from .artifact_usage import bump_artifact_usage
        bump_artifact_usage(refs)
    except Exception as e:  # noqa: BLE001 - telemetry is best-effort
        logger.debug("miner usage bump skipped: %s", e)


def _pair_transform(system_a, fa, a_vals, system_b, fb, b_vals, probe=None) -> "dict | None":
    """The ladder verdict, else — for a same-key-named pair — the containment
    probe (sparse foreign keys). Best-effort: a probe error means no join.
    ``probe`` is the sweep memo's probe (:class:`_SweepMemo`), else :func:`_probe`."""
    transform = discover_transform(a_vals, b_vals)
    if transform is not None or not containment_probe_eligible(fa, fb):
        return transform
    try:
        return _containment_transform(system_a, fa, a_vals, system_b, fb, b_vals, probe=probe)
    except Exception as e:  # noqa: BLE001 - probing is best-effort
        logger.info("containment probe skipped (%s.%s↔%s.%s): %s",
                    fa["table"], fa["field"], fb["table"], fb["field"], e)
        if _STRICT_READS.get():       # a strict sweep reports a failed probe
            raise
        return None


class _SampleCache:
    """Per-pair memo over :func:`_sample` (best-effort: a failure caches ``[]``;
    in a strict sweep it raises)."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, str], list[str]] = {}

    def get(self, sid: str, tbl: str, fld: str) -> list[str]:
        key = (sid, tbl, fld)
        if key not in self._cache:
            try:
                self._cache[key] = _sample(sid, tbl, fld) or []
            except Exception as e:  # noqa: BLE001 - sampling best-effort
                logger.info("join_discovery sample skipped (%s %s.%s): %s", sid, tbl, fld, e)
                if _STRICT_READS.get():   # a strict sweep reports a failed sample
                    raise
                self._cache[key] = []
        return self._cache[key]


class _SweepMemo:
    """One sweep's memo over the IO both directions of a pair repeat.

    A sweep runs ``discover_pair(a, b)`` and ``discover_pair(b, a)``. What a
    pair costs is IO: the containment probes and the field-list graph reads
    (event 28, 2026-10-06: one MES's phase-2 resolve spent 214 s in 1390
    probes, half of them the echo); the transform ladder itself is pure. The
    memo therefore keeps each probe (system, table, field, sample) and each
    system's field lists for the sweep, and re-runs the pure ladder for each
    direction. A VERDICT is never shared between directions: the ladder and
    the containment probe are order-sensitive (the affix rungs try the first
    argument's affix first, a cleared-but-degenerate rung aborts the whole
    ladder, and the containment probe tries the second argument's sample
    first), so one direction can win on a transform the other never reaches,
    or abort where the other clears. Each direction proposes exactly what it
    proposed without the memo; a probe both directions need runs once."""

    def __init__(self) -> None:
        self._probes: dict[tuple, "list[str] | None"] = {}
        self._fields: dict[str, list[dict]] = {}
        self._time_fields: dict[str, list[str]] = {}

    def probe(self, system_id: str, table: str, field: str, values) -> "list[str] | None":
        key = (system_id, table, field, tuple(values))
        if key not in self._probes:
            self._probes[key] = _probe(system_id, table, field, values)
        return self._probes[key]

    def transform(self, system_a, fa, a_vals, system_b, fb, b_vals) -> "dict | None":
        return _pair_transform(system_a, fa, a_vals, system_b, fb, b_vals, probe=self.probe)

    def fields(self, system_id: str) -> list[dict]:
        if system_id not in self._fields:
            self._fields[system_id] = _identifier_fields(system_id)
        return self._fields[system_id]

    def time_fields(self, system_id: str) -> list[str]:
        if system_id not in self._time_fields:
            self._time_fields[system_id] = _timestamp_fields(system_id)
        return self._time_fields[system_id]


def _try_join_field_pair(system_a, fa, a_vals, system_b, fb, b_vals, time_key,
                         memo: "_SweepMemo | None" = None):
    """Measure ONE field pair; emit its ``join_path`` proposal when it clears.
    Returns ``(cleared, proposal_id)`` — ``cleared`` is True whenever the pair
    passed every gate, even if the emit was deduped/gated (id ``None``)."""
    if not a_vals or not b_vals:
        return False, None
    transform = ((memo or _SweepMemo()).transform(system_a, fa, a_vals, system_b, fb, b_vals))
    if transform is None:
        return False, None
    # Defensive emit gate: BOTH endpoints must still read as identifiers
    # and NEVER as measures — protects against graph rows carrying stale
    # roles (role='identifier' leaking onto *_count / *_quantity fields).
    if not all(is_identifier_like(f["field"], f.get("role"), f.get("data_type"))
               and not is_measure_like(f["field"]) for f in (fa, fb)):
        return False, None
    return True, _emit_join_path(
        from_system=system_a, from_field=_qualified(fa["table"], fa["field"]),
        to_system=system_b, to_field=_qualified(fb["table"], fb["field"]),
        concept=fa["concept"] or fb["concept"] or "",
        transform=transform["transform"], overlap=transform, time_key=time_key,
    ) or None


def _seed_crosswalks(system_a, system_b, unjoined, concept_pairs, samples) -> list:
    """Crosswalk seed: a shared concept whose fields NEVER joined AND whose
    systems are ALREADY related (a parent JoinPath) → name-similarity
    candidates. Returns the emitted proposal ids."""
    ids: list = []
    if not unjoined or not _has_established_join(system_a, system_b):
        return ids
    for concept in unjoined:
        for fa, fb in concept_pairs.get(concept, []):
            a_vals = samples.get(system_a, fa["table"], fa["field"])
            b_vals = samples.get(system_b, fb["table"], fb["field"])
            for cand in crosswalk_candidates(a_vals, b_vals):
                mid = _emit_crosswalk(
                    from_system=system_a,
                    from_field=_qualified(fa["table"], fa["field"]),
                    to_system=system_b,
                    to_field=_qualified(fb["table"], fb["field"]),
                    concept=concept, candidate=cand,
                )
                if mid:
                    ids.append(mid)
    return ids


# Each filing checks the pair's stored ValueMappings; one read serves them all
# (``stored_value_mapping_memo``).
@_stored_vm_scope()
def discover_pair(system_a: str, system_b: str, memo: "_SweepMemo | None" = None) -> dict:
    """Discover joins/crosswalks between an ORDERED pair (``a`` → ``b``).

    Compares every identifier-like field of ``a`` against every identifier-like
    field of ``b`` via the transform ladder (plus the containment probe for
    same-key-named pairs — sparse foreign keys); emits a ``join_path``
    proposal for each pair that clears the threshold (carrying transform +
    time_key). For a SHARED concept whose fields produced NO join AND whose
    systems are ALREADY joined, emits reviewable crosswalk candidates instead.

    Returns ``{"joins": [ids], "crosswalks": [ids]}``. Idempotent (dedup via
    ``log_proposed_mapping``). Best-effort: any per-field IO error is isolated so
    one bad field never aborts the pair. A sweep passes ``memo`` so the reverse
    direction reuses this direction's field lists and probes (:class:`_SweepMemo`);
    alone, the call measures for itself exactly as before."""
    result = {"joins": [], "crosswalks": []}
    if not system_a or not system_b or system_a == system_b:
        return result
    memo = memo or _SweepMemo()
    fields_a = memo.fields(system_a)
    fields_b = memo.fields(system_b)
    if not fields_a or not fields_b:
        return result
    time_key = pick_time_key(memo.time_fields(system_a), memo.time_fields(system_b))
    samples = _SampleCache()
    # Track shared-concept field pairs that did NOT produce a join (crosswalk seed).
    concept_joined: dict[str, bool] = {}
    concept_pairs: dict[str, list[tuple[dict, dict]]] = {}
    for fa in fields_a:
        a_vals = samples.get(system_a, fa["table"], fa["field"])
        for fb in fields_b:
            shared_concept = fa["concept"] and fa["concept"] == fb["concept"]
            if shared_concept:
                concept_joined.setdefault(fa["concept"], False)
                concept_pairs.setdefault(fa["concept"], []).append((fa, fb))
            b_vals = samples.get(system_b, fb["table"], fb["field"])
            cleared, mid = _try_join_field_pair(
                system_a, fa, a_vals, system_b, fb, b_vals, time_key, memo)
            if mid:
                result["joins"].append(mid)
            if cleared and shared_concept:
                concept_joined[fa["concept"]] = True
    unjoined = [c for c, joined in concept_joined.items() if not joined]
    result["crosswalks"].extend(
        _seed_crosswalks(system_a, system_b, unjoined, concept_pairs, samples))
    return result


def discover_intra_system(system_id: str) -> dict:
    """Discover SAME-SYSTEM join candidates across DIFFERENT tables (G1 engine).

    The intra-system analogue of :func:`discover_pair` (which stays
    cross-system-only — its same-system guard is deliberate): compares every
    identifier-like field of ``system_id`` against every OTHER-table
    identifier-like field of the SAME system via the transform ladder.
    Same-TABLE pairs are skipped — interchangeable identifiers on one row are a
    different relation (same-row identifier sets, handled at provision), not a
    table-to-table join.

    Returns candidates WITHOUT writing anything::

        {"pairs": [{"from_table", "from_field", "to_table", "to_field",
                    "transform", "overlap_count", "overlap_ratio", "examples"}]}

    The provision caller decides direct-write vs proposal — this function never
    calls ``log_proposed_mapping``. The SAME defensive emit gate as
    :func:`discover_pair` applies (both endpoints must read as identifiers and
    NEVER as measures, protecting against stale graph roles). Deterministic
    (fields sorted; each unordered table pair examined once) and best-effort:
    a per-field sampling error is isolated so one bad field never aborts the
    system."""
    result: dict = {"pairs": []}
    if not system_id:
        return result
    fields = _identifier_fields(system_id)
    if len(fields) < 2:
        return result

    sample_cache: dict[tuple[str, str], list[str]] = {}

    def _cached_sample(tbl, fld):
        key = (tbl, fld)
        if key not in sample_cache:
            try:
                sample_cache[key] = _sample(system_id, tbl, fld) or []
            except Exception as e:  # noqa: BLE001 - sampling best-effort
                logger.info("intra-system sample skipped (%s %s.%s): %s",
                            system_id, tbl, fld, e)
                sample_cache[key] = []
        return sample_cache[key]

    ordered = sorted(fields, key=lambda f: (f["table"], f["field"]))
    for i, fa in enumerate(ordered):
        for fb in ordered[i + 1:]:
            if fa["table"] == fb["table"]:
                # Same-row identifier sets are a provision-time concern, not a join.
                continue
            a_vals = _cached_sample(fa["table"], fa["field"])
            b_vals = _cached_sample(fb["table"], fb["field"])
            if not a_vals or not b_vals:
                continue
            transform = discover_transform(a_vals, b_vals)
            if transform is None:
                continue
            # Defensive emit gate (same as discover_pair): BOTH endpoints must
            # still read as identifiers and NEVER as measures — protects against
            # graph rows carrying stale roles.
            if not all(is_identifier_like(f["field"], f.get("role"), f.get("data_type"))
                       and not is_measure_like(f["field"]) for f in (fa, fb)):
                continue
            result["pairs"].append({
                "from_table": fa["table"], "from_field": fa["field"],
                "to_table": fb["table"], "to_field": fb["field"],
                "transform": transform["transform"],
                "overlap_count": transform["overlap_count"],
                "overlap_ratio": transform["ratio"],
                "examples": transform["examples"],
            })
    return result


def _make_cached_sampler(tag: str):
    """Per-run memoized best-effort sampler over the :func:`_sample` seam.

    One bad field never aborts a run (a sampling error caches ``[]``), and a
    field is sampled at most ONCE per run — the same bound-cost discipline as
    ``discover_pair``'s inline cache (``_SAMPLE_LIMIT`` values per field)."""
    cache: dict[tuple[str, str, str], list[str]] = {}

    def _cached(sid: str, tbl: str, fld: str) -> list[str]:
        key = (sid, tbl, fld)
        if key not in cache:
            try:
                cache[key] = _sample(sid, tbl, fld) or []
            except Exception as e:  # noqa: BLE001 - sampling best-effort
                logger.info("%s sample skipped (%s %s.%s): %s", tag, sid, tbl, fld, e)
                cache[key] = []
        return cache[key]
    return _cached


def _mine_field_pair(system_a: str, fa: dict, system_b: str, fb: dict,
                     sampler, day: str) -> list[str]:
    """Mine ONE identifier-field pair → filed ``crosswalk`` proposal ids.

    Mining basis (ADR-0058 D2): the pair either clears the transform ladder
    (joined-adjacent — the highest-signal place for value bridges) or shares a
    concept (disjoint vocabularies that *should* relate). The defensive emit
    gate from ``discover_pair`` applies: BOTH endpoints must still read as
    identifiers and NEVER as measures (stale graph roles). IO via seams."""
    if not all(is_identifier_like(f["field"], f.get("role"), f.get("data_type"))
               and not is_measure_like(f["field"]) for f in (fa, fb)):
        return []
    a_vals = sampler(system_a, fa["table"], fa["field"])
    b_vals = sampler(system_b, fb["table"], fb["field"])
    if not a_vals or not b_vals:
        return []
    shared_concept = bool(fa.get("concept")) and fa.get("concept") == fb.get("concept")
    transform = discover_transform(a_vals, b_vals)
    if transform is None and not shared_concept:
        return []
    cands = value_pair_candidates(a_vals, b_vals,
                                  (transform or {}).get("transform"))
    if not cands:
        return []
    from_label = _qualified(fa["table"], fa["field"])
    to_label = _qualified(fb["table"], fb["field"])
    sampled = len(_clean_strings(a_vals))
    ids: list[str] = []
    for cand in cands:
        evidence = value_crosswalk_evidence(
            from_label, to_label, cand["method"], len(cands), sampled, cands, day)
        mid = _emit_value_crosswalk(
            system_a, from_label, system_b, to_label,
            fa.get("concept") or fb.get("concept") or "", cand, evidence)
        if mid:
            ids.append(mid)
    return ids


@_stored_vm_scope()
def mine_value_crosswalks(system_a: str, system_b: str) -> dict:
    """Mine per-VALUE crosswalk candidates between two onboarded systems.

    ADR-0058 D2/D4: from the SAME bounded samples join discovery takes (200
    values per field, cached per run), compute value-pair alignments
    (:func:`value_pair_candidates`) for identifier-field pairs that clear the
    transform ladder or share a concept, and file each as a pending
    ``crosswalk`` proposal carrying a COMPUTED evidence string — zero LLM.
    Bounded: ≤ :data:`MINER_MAX_FIELD_PAIRS` field pairs per run (deterministic
    truncation, logged) and ≤ :data:`CROSSWALK_MAX_CANDIDATES` candidates per
    pair. Idempotent — re-runs collapse to dup_pending via
    ``log_proposed_mapping``'s lookup-before-write dedup. Best-effort: a bad
    field pair is isolated, never aborting the run. Returns
    ``{"crosswalks": [ids], "field_pairs": N, "truncated": bool}``."""
    result: dict = {"crosswalks": [], "field_pairs": 0, "truncated": False}
    if not system_a or not system_b or system_a == system_b:
        return result
    pairs, truncated = miner_field_pairs(_identifier_fields(system_a),
                                         _identifier_fields(system_b))
    result["truncated"] = truncated
    if truncated:
        logger.info("mine_value_crosswalks(%s, %s): field pairs truncated to %d",
                    system_a, system_b, MINER_MAX_FIELD_PAIRS)
    sampler = _make_cached_sampler("miner")
    day = datetime.now(timezone.utc).date().isoformat()
    for fa, fb in pairs:
        result["field_pairs"] += 1
        try:
            result["crosswalks"].extend(
                _mine_field_pair(system_a, fa, system_b, fb, sampler, day))
        except Exception as e:  # noqa: BLE001 - isolate a bad field pair
            logger.info("miner field pair skipped (%s.%s ↔ %s.%s): %s",
                        fa.get("table"), fa.get("field"),
                        fb.get("table"), fb.get("field"), e)
    if result["crosswalks"]:
        _bump_usage([("miner", "value_crosswalk")])
    return result


def discover_joins_for_system(system_id: str, skip_peers: Iterable[str] = (),
                              strict: bool = False) -> dict:
    """Run discovery for a NEWLY-onboarded system against ALL existing systems.

    BIDIRECTIONAL and order-independent: for every other onboarded system it runs
    both ``new→existing`` and ``existing→new``, so a join is found regardless of
    which system was onboarded first. Best-effort; idempotent. Returns
    ``{"joins": [ids], "crosswalks": [ids]}`` (aggregated).

    ``skip_peers``: onboarded systems whose pair with ``system_id`` this sweep
    leaves out because another sweep over the SAME unchanged graph covers it
    (the workshop seed's coordinated phase 2, where every system's resolve
    sweeps while no provision runs: ``Phase2JoinSweep`` ``once``). That sweep
    runs both directions of the pair with the same inputs, so it files the
    same proposals this one would; filed twice, the second is an identical
    re-observation (``dup_pending``, or one co-reference in the staging
    buffer). Empty (every other caller): every peer, as before.

    With ``strict`` (or a non-empty ``skip_peers``) no other sweep covers
    this one's peers: a direction that raises is tried once more, then
    reported (``unswept`` peers; ``incomplete`` True when any peer, or the
    peer list, was not swept). A pair's owner in a two-system lap skips
    nobody, so the caller says ``strict`` explicitly. Its graph reads are
    checked (:data:`_STRICT_READS`), and so are its source samples and
    containment probes (``resolve_evidence.checked_source_reads``): a peer
    list, a pair's field read, a sample or a probe that fails raises through
    :func:`discover_pair` and is retried, then reported, never measured as an
    empty graph or column."""
    skipped = {str(s) for s in skip_peers or ()}
    sole_owner = bool(strict) or bool(skipped)
    if not sole_owner:
        return _discover_joins(system_id, skipped, False)
    from .resolve_evidence import checked_source_reads
    token = _STRICT_READS.set(True)
    try:
        with checked_source_reads():
            return _discover_joins(system_id, skipped, True)
    finally:
        _STRICT_READS.reset(token)


def _discover_joins(system_id: str, skipped: set, sole_owner: bool) -> dict:
    """:func:`discover_joins_for_system`'s sweep."""
    agg: dict = {"joins": [], "crosswalks": []}
    if not system_id:
        return agg
    try:
        others = [s for s in _onboarded_systems() if s != system_id and s not in skipped]
    except Exception as e:  # noqa: BLE001 - best-effort
        logger.info("discover_joins_for_system system list failed (%s): %s", system_id, e)
        if sole_owner:
            agg.update(unswept=[], incomplete=True)
        return agg
    started = time.monotonic()
    before = sample_memo_stats()
    unswept = _sweep_peers(system_id, others, agg, attempts=2 if sole_owner else 1)
    if sole_owner:
        agg.update(unswept=unswept, incomplete=bool(unswept))
    after = sample_memo_stats()
    # One line per sweep: the seed's phase-2 resolve spent minutes here on a
    # workshop account, so the stretch must be readable from the log.
    logger.info("join discovery %s: %d peer(s) (%d left to another sweep), %.1fs, samples "
                "fetched %d, memo hits %d, %d join(s), %d crosswalk(s)", system_id,
                len(others), len(skipped), time.monotonic() - started,
                after["fetched"] - before["fetched"],
                after["hits"] - before["hits"], len(agg["joins"]), len(agg["crosswalks"]))
    return agg


def _sweep_peers(system_id: str, others: list, agg: dict, *, attempts: int) -> list[str]:
    """Both directions of each pair ``(system_id, other)`` into ``agg``, one
    memo for the sweep (both directions share each probe and field list);
    returns the peers with a direction that raised on every attempt."""
    memo = _SweepMemo()
    unswept: list[str] = []
    for other in others:
        for a, b in ((system_id, other), (other, system_id)):
            r = _sweep_direction(a, b, memo, attempts=attempts)
            if r is None:
                if other not in unswept:
                    unswept.append(other)
                continue
            agg["joins"].extend(r.get("joins", []))
            agg["crosswalks"].extend(r.get("crosswalks", []))
    return unswept


def _sweep_direction(a: str, b: str, memo: "_SweepMemo", *, attempts: int) -> "dict | None":
    """``discover_pair(a, b, memo)``, tried up to ``attempts`` times; ``None``
    when every attempt raised (logged)."""
    for attempt in range(1, attempts + 1):
        try:
            return discover_pair(a, b, memo)
        except Exception as e:  # noqa: BLE001 - isolate a bad pair
            logger.info("discover_pair skipped (%s→%s, attempt %d/%d): %s", a, b, attempt,
                        attempts, e)
    return None


def discover_all_joins() -> dict:
    """Re-runnable sweep over ALL onboarded system pairs (both directions).

    Callable on demand and after any onboard, so ONBOARDING ORDER/TIMING NEVER
    MATTERS — a join between two pre-existing systems is discovered on the next
    sweep even if neither was just onboarded. Deterministic (sorted systems),
    idempotent (dedup via ``log_proposed_mapping``). Returns aggregated ids."""
    agg = {"joins": [], "crosswalks": []}
    try:
        systems = _onboarded_systems()
    except Exception as e:  # noqa: BLE001 - best-effort
        logger.info("discover_all_joins system list failed: %s", e)
        return agg
    memo = _SweepMemo()
    for i, a in enumerate(systems):
        for b in systems[i + 1:]:
            for x, y in ((a, b), (b, a)):
                try:
                    r = discover_pair(x, y, memo)
                except Exception as e:  # noqa: BLE001 - isolate a bad pair
                    logger.info("discover_pair skipped (%s→%s): %s", x, y, e)
                    continue
                agg["joins"].extend(r.get("joins", []))
                agg["crosswalks"].extend(r.get("crosswalks", []))
    return agg
