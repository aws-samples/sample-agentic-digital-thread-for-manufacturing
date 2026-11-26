# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Identifier-core affinity: discover pattern IdentityRules from sampled values.

Pure functions, no I/O. The onboarding resolver asks an LLM to propose
cross-system identity rules from concept mappings plus a handful of sample
values. That misses the most common real-world join: two systems that key the
same thing with DIFFERENT concept names and DIFFERENT formats — an MES batch
``B-2847`` that another system calls lot reference ``LOT-2026-02847``. Nothing in
the schema says they are the same; the VALUES do.

This pass is evidence-based and deterministic:

1. Every sampled identifier value is reduced to its *digit cores* — runs of
   three or more digits with leading zeros stripped (``LOT-2026-02847`` →
   ``{2026, 2847}``; ``B-2847`` → ``{2847}``).
2. Two fields from different systems are *candidates* when enough of their
   sampled values share a core.
3. ALL sampled values per matched core are kept (ranked, bounded). The
   pattern transform is inferred for each (:func:`agents.resolve.infer_pattern`).
   Within ONE core, the values that fit one shape are merged: the positions
   that vary among them become the ``{*}`` wildcard slot (ADR-0086; several
   revisions of one item). The per-core patterns must then agree exactly
   across cores (a ``{*}`` accepts a core's single literal there), at least
   two agreeing cores AND a quarter of them must be one-to-many, and a tie
   fails closed. A token
   that differs only ACROSS cores is never a wildcard. When that path yields
   no rule, the original representative-pair pass runs unchanged, so every
   one-to-one rule is byte-identical. Coincidental overlaps — a year embedded
   in dates and lot numbers alike — produce inconsistent patterns and are
   rejected by the agreement check, not by a hand-written exclusion list.

A THIRD pass (ADR-0087) runs only when a membership probe is injected and
the two passes above found nothing: an EXACT-agreement rule for two fields
that key the same thing in the SAME format (``source_pattern ==
target_pattern == ""``, method :data:`EXACT_METHOD`, transform identity).
Its gates are all measured: the two fields SHARE at least one MAPS_TO
concept (any role; ``unknown``/``needs_review`` never count), both
columns share one dominant literal value shape (digits ``9``, letters ``A``,
delimiters kept; not all digits, at least two segments), at least
``join_discovery.JOIN_MIN_OVERLAP_COUNT`` exact values are confirmed by the
probe, ``passes_key_cardinality`` holds and neither side is a 1..N
enumeration. A shared concept alone is not evidence (two ``batch`` columns
can hold different numbers); same values alone are not either. The shared
concept, not each field's best identifier concept, is the gate: a run number
mapped ``production-run`` (identifier) AND ``batch`` (correlate) keys the same
thing as a shipment line's ``batch`` column. Absent a shared concept, a pair
of identifier concepts the catalog declares interchangeable across systems
(``catalog.RELATED_IDENTIFIER_CONCEPTS``: a run number mapped
``production-run`` only) agrees too, naming the declaring concept; the
relation is read, never inferred. :class:`ExactPassStats` counts
the pass's outcomes for one bounded summary line.

The result is a rule the steward reviews at the Identity Rules checkpoint
like any other, carrying its evidence (matched pairs, sample sizes) so the
review — and later the trace — can show *why* the graph believes it.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Optional

from .catalog import RELATED_IDENTIFIER_CONCEPTS
from .identifier_translation import WILDCARD, has_wildcard
from .join_discovery import (JOIN_MIN_OVERLAP_COUNT, TRANSFORM_IDENTITY, is_sequential_integer_set,
                             passes_key_cardinality)
from .resolve import infer_pattern

#: Minimum digit-run length that counts as an identifier core.
MIN_CORE_DIGITS = 3
#: Minimum number of value pairs sharing a core before a field pair is considered.
MIN_MATCHED_PAIRS = 3
#: Minimum share of the smaller field's samples that must find a partner.
MIN_COVERAGE = 0.2
#: A side whose sampled values are ALL plain integers (a SERIAL key, a bare
#: numeric code) must itself be covered at least this much: any code that
#: happens to contain N digits "matches" such a column, so a handful of hits
#: against a long integer sequence is chance, not identity.
MIN_INTEGER_SIDE_COVERAGE = 0.5
#: Minimum share of matched pairs the winning pattern pair must explain.
MIN_PATTERN_AGREEMENT = 0.8
#: Confidence written for an affinity rule (W4 calibration may lower it).
AFFINITY_CONFIDENCE = 0.85
#: Provenance method stamped on affinity rules (tier 1: evidence-backed, not steward-confirmed).
AFFINITY_METHOD = "value_core_match"
#: Bound on the sampled values kept per identifier core, per side (the
#: per-core pattern inference is over their product).
MAX_VALUES_PER_CORE = 8
#: A wildcard needs at least this many agreeing cores that are themselves
#: one-to-many: one coincidental sibling is not evidence that a part varies.
MIN_WILDCARD_CORES = 2
#: ...and at least this share of the agreeing cores: two coincidental siblings
#: among 50 or 200 cores are chance, not a part that varies (a year that
#: differs in two lots of fifty never becomes ``{*}``).
MIN_WILDCARD_SHARE = 0.25
#: Bound on a rule's reasoning (persisted as ``origin_context``, cut at 300).
REASONING_CAP = 300
#: Provenance method of an exact-agreement rule (same-format identifiers, ADR-0087).
EXACT_METHOD = "value_exact_match"
#: The transform an exact-agreement join applies: none.
EXACT_TRANSFORM = TRANSFORM_IDENTITY
#: A column's dominant literal shape is held by at least this share of its
#: distinct sampled values (and no other shape ties it).
EXACT_MIN_SHAPE_SHARE = 0.5
#: A join shape has at least this many alphanumeric segments (``AAA-9999``).
EXACT_MIN_SEGMENTS = 2
#: Concept ids that name no concept (never a shared-concept match).
NO_CONCEPT = frozenset({"", "unknown", "needs_review"})

_DIGIT_RUN = re.compile(r"\d+")
_LETTER_RUN = re.compile(r"[A-Za-z]+")
#: Pattern tokens: a placeholder, the wildcard, an alphanumeric run, a separator run.
_TOKEN = re.compile(r"\{\w+\}|\{\*\}|[A-Za-z0-9]+|[^A-Za-z0-9{}]+")
_PLACEHOLDER_TOKEN = re.compile(r"\{\w+\}")
_SEGMENT = re.compile(r"[^\W_]+")


@dataclass(frozen=True)
class SampledField:
    """One field's sampled distinct values plus the metadata a rule needs."""
    system_id: str
    table: str
    field: str
    concept: str
    values: tuple[str, ...] = field(default_factory=tuple)
    #: Schema the values were sampled from (a probe's fallback when the
    #: system has no catalog schema); never part of the field's identity.
    schema: str = field(default="", compare=False)
    #: Every concept the field MAPS_TO (any role, correlates included) — the
    #: exact pass's agreement set (with ``concept``); never part of identity.
    concepts: frozenset[str] = field(default_factory=frozenset, compare=False)
    #: (concept, confidence) pairs of the mapping, when known: which shared
    #: concept an exact rule names. Never part of the field's identity.
    concept_confidence: tuple[tuple[str, float], ...] = field(default=(), compare=False)
    #: A candidate for the EXACT pass only (a correlate-role twin of its
    #: table's identifier concept, ADR-0087 amendment 2026-10-02): the pattern
    #: passes never see it. Never part of the field's identity.
    exact_only: bool = field(default=False, compare=False)

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.field}"


def digit_cores(value: str) -> frozenset[str]:
    """Digit runs with at least :data:`MIN_CORE_DIGITS` SIGNIFICANT digits —
    leading zeros are padding, not identity (``0007`` is the number 7, and
    every zero-padded sequence in every system shares 1..N with every other)."""
    cores = set()
    for run in _DIGIT_RUN.findall(str(value)):
        stripped = run.lstrip("0")
        if len(stripped) >= MIN_CORE_DIGITS:
            cores.add(stripped)
    return frozenset(cores)


def _core_index(values: tuple[str, ...]) -> dict[str, str]:
    """core -> first value carrying it (one representative per core)."""
    index: dict[str, str] = {}
    for value in values:
        for core in digit_cores(value):
            index.setdefault(core, str(value))
    return index


def _all_plain_integers(values: tuple[str, ...]) -> bool:
    """True when every sampled value is a bare digit string (a serial key)."""
    return bool(values) and all(str(v).strip().isdigit() for v in values)


def matched_pairs(source: SampledField, target: SampledField) -> list[tuple[str, str]]:
    """Value pairs (source_value, target_value) that share an identifier core."""
    source_index = _core_index(source.values)
    target_index = _core_index(target.values)
    return [(source_index[core], target_index[core])
            for core in sorted(source_index.keys() & target_index.keys())]


def dominant_pattern(pairs: list[tuple[str, str]]) -> tuple[str, str, int] | None:
    """The (source_pattern, target_pattern) explaining most pairs, with its count.

    Pairs whose transform cannot be inferred (identical formats, or no shared
    digit run) are dropped: a same-format pair is a JoinPath, not a rule."""
    counts: Counter[tuple[str, str]] = Counter()
    for source_value, target_value in pairs:
        inferred = infer_pattern(source_value, target_value)
        if inferred:
            counts[(inferred["source_pattern"], inferred["target_pattern"])] += 1
    if not counts:
        return None
    (source_pattern, target_pattern), count = counts.most_common(1)[0]
    return source_pattern, target_pattern, count


def _value_shape(value: str) -> str:
    """Character-class shape used to rank values (``PRT-5101-R-A`` → ``A-9-A-A``)."""
    return _LETTER_RUN.sub("A", _DIGIT_RUN.sub("9", value))


def _core_groups(values: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    """core -> the values carrying it, deduped and bounded per core. Values of
    the field's commonest shapes are kept first (then by value), so rare-shaped
    siblings that happen to sort early never crowd the fitting ones out."""
    distinct = {str(v) for v in values}
    shapes = Counter(_value_shape(v) for v in distinct)
    groups: dict[str, list[str]] = {}
    for value in sorted(distinct, key=lambda v: (-shapes[_value_shape(v)], v)):
        for core in digit_cores(value):
            groups.setdefault(core, []).append(value)
    return {core: tuple(found[:MAX_VALUES_PER_CORE]) for core, found in groups.items()}


def matched_groups(source: SampledField, target: SampledField
                   ) -> list[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    """(core, source values, target values) for every shared core, sorted by
    core; each side ranked by field-wide shape frequency, then value —
    independent of the samples' row order."""
    source_groups = _core_groups(source.values)
    target_groups = _core_groups(target.values)
    return [(core, source_groups[core], target_groups[core])
            for core in sorted(source_groups.keys() & target_groups.keys())]


def _tokens(pattern: str) -> list[str]:
    return _TOKEN.findall(pattern)


def _skeleton(pattern: str) -> tuple[str, ...] | None:
    """Pattern shape: ``P`` placeholder, ``A`` alphanumeric run, separators
    verbatim (``PRT-{id}-R-A`` → ``(A,-,P,-,A,-,A)``); None if unparseable."""
    tokens = _tokens(pattern)
    if "".join(tokens) != pattern:
        return None
    return tuple("P" if _PLACEHOLDER_TOKEN.fullmatch(t) else "A" if t.isalnum() else t
                 for t in tokens)


def _unambiguous(tokens: list[str]) -> bool:
    """A merged pattern keeps an alphanumeric literal and never glues ``{*}``
    to a placeholder (``{id}{*}`` cannot say where the core ends)."""
    glued = any(WILDCARD in (left, right)
                and any(_PLACEHOLDER_TOKEN.fullmatch(t) for t in (left, right))
                for left, right in zip(tokens, tokens[1:]))
    return not glued and any(t.isalnum() for t in tokens)


def merge_patterns(patterns: list[str]) -> str | None:
    """Merge same-shape patterns token by token: agreeing tokens stay literal,
    a differing alphanumeric token becomes ``{*}``. None when the shapes
    differ, a non-alphanumeric token differs, or the result is ambiguous."""
    shapes = {_skeleton(p) for p in patterns}
    if len(shapes) != 1 or None in shapes:
        return None
    merged = []
    for column in zip(*(_tokens(p) for p in patterns)):
        if len(set(column)) == 1:
            merged.append(column[0])
        elif all(t.isalnum() for t in column):
            merged.append(WILDCARD)
        else:
            return None
    if WILDCARD in merged and not _unambiguous(merged):
        return None
    return "".join(merged)


def _group_votes(group) -> dict[tuple, list[tuple[str, str, str, str]]]:
    """Shape key -> the (source, target, source_pattern, target_pattern) fits
    over the group's bounded source × target product."""
    _core, source_values, target_values = group
    votes: dict[tuple, list[tuple[str, str, str, str]]] = {}
    for source_value in source_values:
        for target_value in target_values:
            inferred = infer_pattern(source_value, target_value)
            if not inferred:
                continue
            sp, tp = inferred["source_pattern"], inferred["target_pattern"]
            key = (_skeleton(sp), _skeleton(tp))
            if None not in key:
                votes.setdefault(key, []).append((source_value, target_value, sp, tp))
    return votes


@dataclass(frozen=True)
class _CoreMerge:
    """ONE core's values that fit one shape, and their merged pattern pair."""
    core: str
    sources: tuple[str, ...]
    targets: tuple[str, ...]
    source_pattern: str
    target_pattern: str

    @property
    def one_to_many(self) -> bool:
        return len(self.sources) > 1 or len(self.targets) > 1

    @property
    def has_wildcard(self) -> bool:
        return has_wildcard(self.source_pattern) or has_wildcard(self.target_pattern)


def _core_merges(group) -> tuple[_CoreMerge, ...]:
    """Per shape that fits within ONE core, the fitting values merged: the
    wildcard slot is exactly the positions that vary among that core's values."""
    merges = []
    for _key, fits in sorted(_group_votes(group).items()):
        source_pattern = merge_patterns(sorted({fit[2] for fit in fits}))
        target_pattern = merge_patterns(sorted({fit[3] for fit in fits}))
        if source_pattern is not None and target_pattern is not None:
            merges.append(_CoreMerge(group[0], tuple(sorted({fit[0] for fit in fits})),
                                     tuple(sorted({fit[1] for fit in fits})),
                                     source_pattern, target_pattern))
    return tuple(merges)


def _agrees(candidate: str, pattern: str) -> bool:
    """Exact token agreement, except a ``{*}`` in the candidate accepts one
    alphanumeric token there (a core with a single value at that position)."""
    wanted, got = _tokens(candidate), _tokens(pattern)
    return len(wanted) == len(got) and all(
        w == g or (w == WILDCARD and g.isalnum()) for w, g in zip(wanted, got))


def _support(candidate: tuple[str, str], merges_by_core) -> tuple[_CoreMerge, ...]:
    """The first merge of each core that agrees with the candidate pattern pair."""
    source_pattern, target_pattern = candidate
    agreeing = []
    for merges in merges_by_core:
        match = next((m for m in merges if _agrees(source_pattern, m.source_pattern)
                      and _agrees(target_pattern, m.target_pattern)), None)
        if match is not None:
            agreeing.append(match)
    return tuple(agreeing)


def required_one_to_many(agreeing: int) -> int:
    """One-to-many cores a wildcard needs among ``agreeing`` cores: the
    :data:`MIN_WILDCARD_CORES` floor or :data:`MIN_WILDCARD_SHARE` of them."""
    return max(MIN_WILDCARD_CORES, math.ceil(MIN_WILDCARD_SHARE * agreeing))


@dataclass(frozen=True)
class _Family:
    """The agreed wildcard pattern pair and the per-core values supporting it."""
    source_pattern: str
    target_pattern: str
    per_core: tuple[_CoreMerge, ...]

    @property
    def agreeing(self) -> int:
        return len(self.per_core)

    @property
    def max_src(self) -> int:
        return max(len(c.sources) for c in self.per_core)

    @property
    def max_tgt(self) -> int:
        return max(len(c.targets) for c in self.per_core)

    def supported(self, n: int) -> bool:
        many = sum(1 for c in self.per_core if c.one_to_many)
        return (self.agreeing >= MIN_MATCHED_PAIRS and self.agreeing / n >= MIN_PATTERN_AGREEMENT
                and many >= required_one_to_many(self.agreeing))


def _winning_family(groups) -> _Family | None:
    """The ONE supported wildcard pattern pair, else None. Candidates are only
    patterns some core's own values produced (a token that differs only ACROSS
    cores never becomes ``{*}``); support is counted per core. Two supported
    candidates with equal support fail closed; order-independent throughout."""
    merges_by_core = [_core_merges(group) for group in groups]
    candidates = sorted({(m.source_pattern, m.target_pattern)
                         for merges in merges_by_core for m in merges if m.has_wildcard})
    families = [_Family(*candidate, _support(candidate, merges_by_core)) for candidate in candidates]
    supported = sorted((f for f in families if f.supported(len(groups))), key=lambda f: -f.agreeing)
    if not supported or (len(supported) > 1 and supported[1].agreeing == supported[0].agreeing):
        return None
    return supported[0]


def _value_list(values: tuple[str, ...], shown: int = 3) -> str:
    extra = len(values) - shown
    return ", ".join(values[:shown]) + (f" +{extra} more" if extra > 0 else "")


def _wildcard_reasoning(source: SampledField, target: SampledField, n: int, family: _Family) -> str:
    """Evidence text; the multiplicity clause goes first (origin_context is cut at 300)."""
    many_target = family.max_tgt > 1
    many = [c for c in family.per_core if len(c.targets if many_target else c.sources) > 1]
    example_core = min(many, key=lambda c: (-len(c.targets if many_target else c.sources), int(c.core)))
    srcs, tgts = example_core.sources, example_core.targets
    example = (f"{srcs[0]} ↔ {_value_list(tgts)}" if many_target
               else f"{_value_list(srcs)} ↔ {tgts[0]}")
    return (f"{family.agreeing} of {n} sampled identifier cores share one transform; "
            f"{len(many)} of {n} cores match several {'target' if many_target else 'source'} "
            f"values (up to {family.max_tgt if many_target else family.max_src}), so the part "
            f"that varies is a {WILDCARD} wildcard (e.g. {example}); "
            f"{len(source.values)} and {len(target.values)} distinct values sampled")


def rule_concept(source: SampledField, target: SampledField) -> str:
    """The concept an affinity rule between two fields names, the same
    whichever is the source (ADR-0087 amendment 2026-10-18): a concept both
    MAPS_TO, else a catalog-related identifier pair's declaring concept
    (:func:`agreed_concept`), else the lexically first of the two fields'
    own concepts (ids that name no concept left out)."""
    agreed = agreed_concept(source, target)
    if agreed is not None:
        return agreed[0]
    named = sorted({source.concept, target.concept} - NO_CONCEPT)
    return named[0] if named else min(str(source.concept or ""), str(target.concept or ""))


def _rule_dict(source: SampledField, target: SampledField,
               source_pattern: str, target_pattern: str, reasoning: str) -> dict:
    return {
        "concept": rule_concept(source, target),
        "source_field": source.qualified, "source_table": source.table,
        "source_pattern": source_pattern,
        "target_system": target.system_id,
        "target_field": target.qualified, "target_table": target.table,
        "target_pattern": target_pattern,
        "confidence": AFFINITY_CONFIDENCE,
        "method": AFFINITY_METHOD,
        "reasoning": reasoning,
    }


def _wildcard_rule(source: SampledField, target: SampledField, n: int, family: _Family) -> dict:
    """A one-to-many rule: the agreed pattern pair, the varying part ``{*}``."""
    return _rule_dict(source, target, family.source_pattern, family.target_pattern,
                      _wildcard_reasoning(source, target, n, family))


def _legacy_rule(source: SampledField, target: SampledField, pairs: list[tuple[str, str]]) -> dict | None:
    """The one-to-one pass over one representative pair per core (unchanged)."""
    winner = dominant_pattern(pairs)
    if winner is None:
        return None
    source_pattern, target_pattern, agreeing = winner
    if agreeing < MIN_MATCHED_PAIRS or agreeing / len(pairs) < MIN_PATTERN_AGREEMENT:
        return None
    example_source, example_target = next(
        (s, t) for s, t in pairs
        if (infer_pattern(s, t) or {}).get("source_pattern") == source_pattern)
    return _rule_dict(source, target, source_pattern, target_pattern, (
        f"{agreeing} of {len(pairs)} sampled value pairs share an identifier core "
        f"under one transform ({example_source} ↔ {example_target}); "
        f"{len(source.values)} and {len(target.values)} distinct values sampled"))


def _passes_gates(source: SampledField, target: SampledField, pairs: list[tuple[str, str]]) -> bool:
    """Match-count, coverage and integer-side coverage gates (unchanged)."""
    smaller = min(len(source.values), len(target.values))
    if len(pairs) < MIN_MATCHED_PAIRS or len(pairs) / smaller < MIN_COVERAGE:
        return False
    for side in (source, target):
        if _all_plain_integers(side.values) and len(pairs) / len(side.values) < MIN_INTEGER_SIDE_COVERAGE:
            return False
    return True


#: ``probe(field, values)`` -> which of ``values`` exist EXACTLY in ``field``'s
#: column, or None when the column cannot be probed.
MembershipProbe = Callable[[SampledField, tuple], Optional[list]]


def literal_shape(value) -> str:
    """Literal value shape: digits ``9``, ASCII letters ``A``, everything
    else kept (``RUN-20260921-91005`` -> ``AAA-99999999-99999``)."""
    return "".join("9" if c.isascii() and c.isdigit() else "A" if c.isascii() and c.isalpha() else c
                   for c in str(value).strip())


def dominant_shape(values) -> str | None:
    """The literal shape of at least :data:`EXACT_MIN_SHAPE_SHARE` of the
    distinct values; None when there is no such majority or it is tied."""
    distinct = {str(v).strip() for v in values if str(v).strip()}
    if not distinct:
        return None
    (shape, count), *rest = Counter(literal_shape(v) for v in distinct).most_common(2)
    if count / len(distinct) < EXACT_MIN_SHAPE_SHARE or (rest and rest[0][1] == count):
        return None
    return shape


def is_join_shape(shape: str) -> bool:
    """A shape an exact join may ride on: at least :data:`EXACT_MIN_SEGMENTS`
    alphanumeric segments and not all digits (a bare number is chance)."""
    return (len(_SEGMENT.findall(shape or "")) >= EXACT_MIN_SEGMENTS
            and any(c.isalnum() and c != "9" for c in shape))


def concept_set(sampled: SampledField) -> frozenset[str]:
    """Every concept the field names (``concept`` plus ``concepts``), less
    the ids that name no concept."""
    return frozenset((sampled.concepts | {sampled.concept}) - NO_CONCEPT)


def shared_concept(source: SampledField, target: SampledField) -> str | None:
    """The concept both fields MAPS_TO, else None; several -> the highest
    combined confidence of both fields' mappings, tie lexically first. When
    either field has no confidence for every shared concept, lexically first.
    Symmetric: either field may be the source (ADR-0087 amendment 2026-10-18)."""
    shared = concept_set(source) & concept_set(target)
    if not shared:
        return None
    scores = [dict(side.concept_confidence) for side in (source, target)]
    if not all(shared <= set(score) for score in scores):
        return min(shared)
    return min(shared, key=lambda c: (-sum(float(score[c] or 0.0) for score in scores), c))


def related_concept(source: SampledField, target: SampledField) -> tuple[str, str] | None:
    """(declaring concept, the other side's concept) of a catalog-declared
    related identifier pair (``catalog.RELATED_IDENTIFIER_CONCEPTS``) the two
    fields span, in either direction; several -> lexically first. Else None."""
    src, tgt = concept_set(source), concept_set(target)
    spanned = sorted((declaring, concept) for concept, declaring in RELATED_IDENTIFIER_CONCEPTS
                     if (concept in src and declaring in tgt) or (declaring in src and concept in tgt))
    return spanned[0] if spanned else None


def agreed_concept(source: SampledField, target: SampledField) -> tuple[str, str] | None:
    """(the concept the rule names, its reasoning label): a SHARED concept
    first, else a catalog-related identifier pair (labelled with the related
    concept so the steward sees the agreement was not a shared id)."""
    shared = shared_concept(source, target)
    if shared is not None:
        return shared, shared
    related = related_concept(source, target)
    if related is None:
        return None
    declaring, other = related
    return declaring, f"{declaring} (catalog-related {other})"


def _exact_shape(source: SampledField, target: SampledField) -> str | None:
    """The shared dominant join shape when the cheap (probe-free) shape gates hold."""
    if is_sequential_integer_set(source.values) or is_sequential_integer_set(target.values):
        return None
    shape = dominant_shape(source.values)
    if shape is None or not is_join_shape(shape) or dominant_shape(target.values) != shape:
        return None
    return shape


def exact_shared(source: SampledField, target: SampledField,
                 probe: MembershipProbe, probed_only: bool = False) -> frozenset[str] | None:
    """Values present EXACTLY in both columns: the sample intersection plus
    each side's sampled values the probe finds in the other column. None when
    either probe cannot answer (unconfirmed is not evidence). ``probed_only``
    counts only the values the probe confirms in the other column, never the
    bare sample intersection (the provision step's re-measurement: a sample
    may be older than the column)."""
    src, tgt = {str(v) for v in source.values}, {str(v) for v in target.values}
    in_target = probe(target, tuple(sorted(src)))
    in_source = probe(source, tuple(sorted(tgt)))
    if in_target is None or in_source is None:
        return None
    confirmed = (src & {str(v) for v in in_target}) | (tgt & {str(v) for v in in_source})
    return frozenset(confirmed if probed_only else (src & tgt) | confirmed)


def _exact_reasoning(source: SampledField, target: SampledField, shape: str, shared,
                     concept: str) -> str:
    text = (f"{len(shared)} exact values of {source.qualified} exist in {target.system_id} "
            f"{target.qualified} (membership-probed); both columns shape {shape}; "
            f"concept {concept}; {len(source.values)} and {len(target.values)} "
            f"distinct values sampled")
    return text[:REASONING_CAP]


# The exact inverse of the shape clause :func:`_exact_reasoning` writes. Kept
# beside it so the two can only drift together (pinned by a round-trip test).
# The reasoning is persisted as the rule's ``origin_context`` (IdentityRule and
# JoinPath), which the route seed reads back as ``provenance``; the shape is the
# one measured fact that says what an identifier of this join LOOKS like.
_SHAPE_CLAUSE = re.compile(r"both columns shape (\S+);")


def exact_shape_from_reasoning(text) -> str | None:
    """The measured join shape an exact rule's reasoning records, or None
    when the text is not an exact rule's (a pattern rule's provenance, an
    LLM note, a missing value)."""
    if not isinstance(text, str):
        return None
    match = _SHAPE_CLAUSE.search(text)
    return match.group(1) if match else None


# The concept clause :func:`_exact_reasoning` writes after the shape clause:
# the agreed concept id, then ``;`` or the label of a catalog-related pair.
_CONCEPT_CLAUSE = re.compile(r"both columns shape \S+; concept ([A-Za-z0-9][A-Za-z0-9_-]{0,127})[;( ]")


def exact_concept_from_reasoning(text) -> str | None:
    """The concept an exact rule's reasoning records it joins on, or None
    when the text is not an exact rule's or names none."""
    if not isinstance(text, str):
        return None
    match = _CONCEPT_CLAUSE.search(text)
    return match.group(1) if match else None


# The whole concept clause, the catalog-related label (``agreed_concept``)
# included: ``concept <agreed>;`` or ``concept <agreed> (catalog-related <other>);``.
_CONCEPTS_CLAUSE = re.compile(
    r"both columns shape \S+; concept ([A-Za-z0-9][A-Za-z0-9_-]{0,127})"
    r"(?: \(catalog-related ([A-Za-z0-9][A-Za-z0-9_-]{0,127})\))?;")


def exact_concepts_from_reasoning(text) -> tuple[str, str] | None:
    """``(agreed concept, the catalog-related concept or "")`` an exact rule's
    reasoning records, or None when the text is not an exact rule's. For a
    catalog-related agreement (``agreed_concept``) the agreed concept is the
    pair's declaring concept and the related one the pair's other concept;
    one column of the join maps each."""
    if not isinstance(text, str):
        return None
    match = _CONCEPTS_CLAUSE.search(text)
    return (match.group(1), match.group(2) or "") if match else None


#: Outcomes of :func:`exact_verdict` for a considered pair (``ineligible`` —
#: same system or no values — is never considered).
EXACT_EMITTED, EXACT_REFUSED_CONCEPT, EXACT_REFUSED_SHAPE, EXACT_REFUSED_PROBE = (
    "emitted", "concept", "shape", "probe")


def exact_verdict(source: SampledField, target: SampledField,
                  probe: MembershipProbe, probed_only: bool = False) -> tuple[dict | None, str]:
    """(rule, outcome): the exact-agreement rule and ``emitted`` when every
    measured gate holds, else (None, the gate that refused): ``concept`` (no
    shared or catalog-related concept), ``shape`` (no shared join shape, or a 1..N enumeration),
    ``probe`` (unconfirmed, below the floor, or key cardinality).
    ``probed_only``: :func:`exact_shared` counts only probe-confirmed values."""
    if source.system_id == target.system_id or not source.values or not target.values:
        return None, "ineligible"
    agreed = agreed_concept(source, target)
    if agreed is None:
        return None, EXACT_REFUSED_CONCEPT
    concept, label = agreed
    shape = _exact_shape(source, target)
    if shape is None:
        return None, EXACT_REFUSED_SHAPE
    shared = exact_shared(source, target, probe, probed_only=probed_only)
    if (shared is None or len(shared) < JOIN_MIN_OVERLAP_COUNT
            or not passes_key_cardinality(set(source.values), set(target.values), shared)):
        return None, EXACT_REFUSED_PROBE
    rule = _rule_dict(source, target, "", "", _exact_reasoning(source, target, shape, shared, label))
    return {**rule, "concept": concept, "method": EXACT_METHOD, "transform": EXACT_TRANSFORM,
            "shared_count": len(shared)}, EXACT_EMITTED


def exact_rule(source: SampledField, target: SampledField, probe: MembershipProbe) -> dict | None:
    """The exact-agreement rule (same format, identity transform) when every
    measured gate holds, else None (see the module docstring)."""
    return exact_verdict(source, target, probe)[0]


@dataclass
class ExactPassStats:
    """Counts (never values) of one affinity run's exact-agreement pass:
    ``qualified`` rules before :func:`cap_exact_rules`, ``emitted`` after."""
    considered: int = 0
    refused_concept: int = 0
    refused_shape: int = 0
    refused_probe: int = 0
    qualified: int = 0
    emitted: int = 0

    def record(self, outcome: str) -> None:
        counter = {EXACT_EMITTED: "qualified", EXACT_REFUSED_CONCEPT: "refused_concept",
                   EXACT_REFUSED_SHAPE: "refused_shape", EXACT_REFUSED_PROBE: "refused_probe"}.get(outcome)
        if counter:
            self.considered += 1
            setattr(self, counter, getattr(self, counter) + 1)

    def summary(self) -> str:
        return (f"exact pass: {self.considered} pair(s) considered; refused "
                f"concept={self.refused_concept} shape={self.refused_shape} "
                f"probe/cardinality={self.refused_probe}; qualified={self.qualified} "
                f"emitted={self.emitted}")


def _pattern_rule(source: SampledField, target: SampledField) -> dict | None:
    """The wildcard path, then the one-to-one pass exactly as before. That
    pass can never itself be a tie: its winner must hold
    MIN_PATTERN_AGREEMENT (> 0.5) of the pairs."""
    pairs = matched_pairs(source, target)
    if not _passes_gates(source, target, pairs):
        return None
    groups = matched_groups(source, target)
    family = _winning_family(groups)
    if family is None:
        return _legacy_rule(source, target, pairs)
    return _wildcard_rule(source, target, len(groups), family)


def affinity_rule(source: SampledField, target: SampledField,
                  probe: MembershipProbe | None = None,
                  stats: ExactPassStats | None = None) -> dict | None:
    """A resolver-shaped rule dict when the two fields' values agree, else None.

    The pattern passes run first (unchanged); only when they yield nothing AND
    a ``probe`` is injected does the exact-agreement pass run (its outcome
    counted into ``stats`` when given). A pair with an ``exact_only`` side
    skips the pattern passes: only the exact pass may join it."""
    if source.system_id == target.system_id or not source.values or not target.values:
        return None
    rule = None if source.exact_only or target.exact_only else _pattern_rule(source, target)
    if rule is None and probe is not None:
        rule, outcome = exact_verdict(source, target, probe)
        if stats is not None:
            stats.record(outcome)
    return rule


def _is_exact_rule(rule: dict) -> bool:
    return rule.get("method") == EXACT_METHOD


def _endpoints(rule: dict, source_system: str) -> tuple:
    """The rule's two ``(system, table, field)`` endpoints, sorted: the same
    tuple whichever endpoint is the source."""
    return tuple(sorted(((source_system, str(rule.get("source_table") or ""), rule["source_field"]),
                         (rule["target_system"], rule["target_table"], rule["target_field"]))))


def _capped(entries: list[tuple[dict, str]]) -> list[dict]:
    """:func:`cap_exact_rules` over ``(rule, source system)`` entries."""
    best: dict[tuple, tuple] = {}
    for rule, sid in entries:
        if not _is_exact_rule(rule):
            continue
        pair = _endpoints(rule, sid)
        rank = (-rule["shared_count"], str(rule.get("concept") or ""))
        if pair not in best or rank < best[pair][0]:
            best[pair] = (rank, id(rule))
    kept = {rule_id for _rank, rule_id in best.values()}
    return [rule for rule, _sid in entries if not _is_exact_rule(rule) or id(rule) in kept]


def cap_exact_rules(rules: list[dict], source_system: str = "") -> list[dict]:
    """At most ONE exact rule per pair of physical columns (ADR-0087
    amendment 2026-10-18): the same two ``(system, table, field)`` endpoints,
    in either order, are one join, whichever side sampled them and however
    often (a column with several identifier-role edges is sampled once per
    edge). The kept one has the most shared values, tie broken on its
    concept. Two DIFFERENT columns are never merged here, even with equal
    values: a one-to-one extension table holds exactly its base table's keys, so
    sampled values cannot prove two columns are aliases, and each pair passed
    its own probe. Every pair is the same from either side, so either side's
    resolve keeps the same rules. Every other rule and the order of the kept
    ones are unchanged. ``source_system`` names the system the rules'
    sources belong to."""
    return _capped([(rule, source_system) for rule in rules])


def find_affinity_rules(new_fields: list[SampledField],
                        existing_fields: list[SampledField],
                        probe: MembershipProbe | None = None,
                        stats: ExactPassStats | None = None) -> list[dict]:
    """Every (new field, existing field) pair whose sampled values agree
    (exact-agreement rules only with a ``probe``, capped by :func:`cap_exact_rules`;
    the exact pass's outcome counts go into ``stats`` when given)."""
    entries = []
    for source in new_fields:
        for target in existing_fields:
            rule = affinity_rule(source, target, probe, stats)
            if rule:
                entries.append((rule, source.system_id))
    capped = _capped(entries)
    if stats is not None:
        stats.emitted += sum(1 for rule in capped if _is_exact_rule(rule))
    return capped


#: The methods of the rules the affinity pass builds from sampled fields: their
#: ``<side>_table`` and ``<side>_field`` name a physical column exactly.
_SAMPLED_METHODS = frozenset({AFFINITY_METHOD, EXACT_METHOD})


def _sampled_columns(rules) -> dict:
    """``{(side, system): {(table, column)}}``: every physical column a
    sampled rule names on either side, dotted or not, by its exact table and
    whole column. The source system is ``""`` (every rule of one merge is the
    resolved system's); a target column is its target system's. Pure."""
    out: dict = {}
    for rule in rules or []:
        if not isinstance(rule, dict) or rule.get("method") not in _SAMPLED_METHODS:
            continue
        for side in ("source", "target"):
            table = str(rule.get(f"{side}_table") or "")
            field = str(rule.get(f"{side}_field") or "")
            column = field[len(table) + 1:] if table and field.startswith(table + ".") else ""
            if column:
                system = str(rule.get("target_system") or "") if side == "target" else ""
                out.setdefault((side, system), set()).add((table, column))
    return {key: frozenset(cols) for key, cols in out.items()
            if any("." in column for _table, column in cols)}


#: Marks a merge-key end whose spelling names more than one sampled column:
#: it pairs with no column (provision rejects such a source spelling).
_AMBIGUOUS_END = "\x00ambiguous:"


def _end_key(rule: dict, side: str, sampled: dict) -> str:
    """One end of a rule's merge key. When the end's system has a sampled
    dotted column (:func:`_sampled_columns`), the end's spelling (with its
    ``<side>_table``) is read by ``dotted_columns.read_column``, the reading
    provision binds a rule's source with: one dotted column keys as
    ``table.column``, so two columns that end the same way stay two pairs and
    every spelling of one column is one pair; a spelling naming more than
    one column pairs with nothing. Every other end keys on the last segment
    of its field, as without dotted columns."""
    from . import dotted_columns
    field = str(rule.get(f"{side}_field", ""))
    leaf = field.split(".")[-1]
    system = str(rule.get("target_system") or "") if side == "target" else ""
    columns = sampled.get((side, system))
    if not columns:
        return leaf
    reading = dotted_columns.read_column(str(rule.get(f"{side}_field") or ""), columns,
                                         str(rule.get(f"{side}_table") or ""))
    if reading.status == dotted_columns.ONE:
        return reading.qualified
    if reading.status == dotted_columns.AMBIGUOUS:
        return _AMBIGUOUS_END + field
    return leaf


def _merge_key(rule: dict, sampled: dict | None = None) -> tuple[str, str, str]:
    """The field pair a rule is merged on: ``(source, target system, target)``,
    each end the last segment of its field, or the whole dotted column it
    names among the ``sampled`` columns (:func:`_end_key`)."""
    sampled = sampled or {}
    return (_end_key(rule, "source", sampled),
            str(rule.get("target_system", "")),
            _end_key(rule, "target", sampled))


def _is_wildcard_rule(rule: dict) -> bool:
    return has_wildcard(rule.get("source_pattern")) or has_wildcard(rule.get("target_pattern"))


def _replacing(rule: dict, replaced: dict) -> dict:
    """A copy of the wildcard rule whose evidence names the LLM constant it
    replaced, within :data:`REASONING_CAP` (the multiplicity clause stays first)."""
    side = "target_pattern" if has_wildcard(rule.get("target_pattern")) else "source_pattern"
    note = f"; replaced proposed constant {str(replaced.get(side) or 'direct match')[:80]}"
    reasoning = str(rule.get("reasoning") or "")
    if len(reasoning) + len(note) > REASONING_CAP:
        reasoning = reasoning[:REASONING_CAP - len(note) - 1].rstrip() + "…"
    return {**rule, "reasoning": reasoning + note}


#: A pattern made ONLY of these placeholders captures the whole value and
#: emits it unchanged (``*``, ``{*}``, ``{id}``): it transforms nothing.
_PURE_PLACEHOLDER = re.compile(r"(?:\*|\{\*\}|\{id\})+")


def _pattern_text(pattern) -> str:
    return str(pattern or "").strip()


def patterns_add_nothing(source_pattern, target_pattern) -> bool:
    """True when an LLM pattern pair is identity-shaped — it matches exactly
    the values a direct match does: both empty, the SAME text on both sides
    (``AB-*``/``AB-*``, ``{id}``/``{id}``), or each side empty or a pure
    placeholder (``*``, ``{*}``, ``{id}``). A pair that rewrites the value
    (``AB-{id}-REV-{*}`` → ``{id}``) adds something and returns False. Pure."""
    src, tgt = _pattern_text(source_pattern), _pattern_text(target_pattern)
    if src == tgt:
        return True
    return all(not p or _PURE_PLACEHOLDER.fullmatch(p) for p in (src, tgt))


def _add_nothing_reason(replaced: dict) -> str:
    """Why an exact rule replaced ``replaced`` (its patterns add nothing)."""
    src, tgt = _pattern_text(replaced.get("source_pattern")), _pattern_text(replaced.get("target_pattern"))
    if not (src or tgt):
        return "LLM patterns empty"
    return f"LLM patterns {src[:40]!r}->{tgt[:40]!r} identity-shaped"


def _replacement(rule: dict, replaced: dict) -> dict | None:
    """What an affinity rule puts in place of the LLM rule for its pair: a
    wildcard rule (with a note), or an exact rule over an LLM claim whose
    patterns add nothing (:func:`patterns_add_nothing` — the probe-measured
    evidence of the SAME claim, ADR-0087); else None."""
    if _is_wildcard_rule(rule):
        return _replacing(rule, replaced)
    if _is_exact_rule(rule) and patterns_add_nothing(replaced.get("source_pattern"),
                                                     replaced.get("target_pattern")):
        return rule
    return None


def _physical_key(rule: dict) -> tuple:
    """Both endpoints of a rule exactly as sampled: source table and field,
    target system, table and field. Two affinity rules are one rule only when
    every part is equal (a column name shared by two tables is two columns)."""
    return tuple(str(rule.get(k) or "") for k in
                 ("source_table", "source_field", "target_system", "target_table", "target_field"))


def _table_name(table) -> str:
    """A table name as the merge compares it: case-insensitive, without a
    schema prefix (``ERP_DATA.Customers`` is ``customers``)."""
    return str(table or "").strip().lower().split(".")[-1]


def _rule_table(rule: dict, side: str) -> str:
    """The table a rule names on ``side``: its ``<side>_table``, else the part
    of a table-qualified field before the column, as :func:`_table_name`
    compares it; ``""`` when it names none."""
    field = str(rule.get(f"{side}_field") or "")
    return _table_name(rule.get(f"{side}_table") or (field.rsplit(".", 1)[0] if "." in field else ""))


def _named_table(rule: dict, side: str, sampled: dict) -> str:
    """:func:`_rule_table` of an LLM rule, or ``""`` when the end keys a whole
    sampled dotted column (:func:`_end_key`), which already names its table."""
    field = str(rule.get(f"{side}_field") or "")
    if _end_key(rule, side, sampled) != field.split(".")[-1]:
        return ""
    return _rule_table(rule, side)


def _paired_llm(llm_rules: list[dict], positions: dict, rule: dict, sampled: dict) -> list[int]:
    """Indexes of the LLM rules an affinity rule pairs with: same field pair
    (:func:`_merge_key`) and, on each side where the LLM rule names a table,
    that table. A rule naming this pair's tables comes before one that names
    none (the leaf-name fallback), then in LLM order."""
    pairs = []
    for index in positions.get(_merge_key(rule, sampled), ()):
        named = [_named_table(llm_rules[index], side, sampled) for side in ("source", "target")]
        if all(not n or n == _rule_table(rule, side)
               for n, side in zip(named, ("source", "target"))):
            pairs.append((not any(named), index))
    return [index for _fallback, index in sorted(pairs)]


def _llm_positions(llm_rules: list[dict], sampled: dict) -> dict:
    positions: dict[tuple[str, str, str], list[int]] = {}
    for index, rule in enumerate(llm_rules):
        positions.setdefault(_merge_key(rule, sampled), []).append(index)
    return positions


def exact_replacement_notes(llm_rules: list[dict], merged: list[dict]) -> list[str]:
    """One ``source -> system.target (why)`` note per LLM rule
    :func:`merge_affinity_rules` replaced with an exact rule."""
    llm = [r for r in llm_rules if not _is_exact_rule(r)]
    sampled = _sampled_columns(merged)
    positions = _llm_positions(llm, sampled)
    notes = []
    for rule in merged:
        if not _is_exact_rule(rule):
            continue
        pairs = _paired_llm(llm, positions, rule, sampled)
        if pairs:
            notes.append(f"{rule.get('source_field')} -> {rule.get('target_system')}."
                         f"{rule.get('target_field')} ({_add_nothing_reason(llm[pairs[0]])})")
    return notes


def merge_affinity_rules(llm_rules: list[dict], affinity_rules: list[dict]) -> list[dict]:
    """LLM rules first; an affinity rule is added only for a field pair the LLM
    did not already propose (the LLM's own pattern wins when both exist) —
    EXCEPT a wildcard affinity rule, which replaces the LLM rule for its pair
    in place and says so in its reasoning: the sample proved the part the LLM
    held constant varies (ADR-0086), and an exact rule, which replaces an
    LLM rule whose patterns add nothing (empty or identity-shaped, ADR-0087). Duplicate LLM rules for that pair are
    ALL replaced: the first keeps its position, the rest are dropped. A pair
    on a dotted column the affinity rules sampled is that whole column
    (:func:`_merge_key`).

    An affinity rule pairs with an LLM rule on the same field pair only where
    the LLM rule's tables allow it (:func:`_paired_llm`): an LLM rule that
    names a table is a claim about that table, so it never wins over, or is
    replaced by, an affinity rule on another table; one that names none pairs
    on the leaf names. Affinity rules are deduplicated against each other on
    their full physical endpoints (:func:`_physical_key`), and each is judged
    against the LLM rule it pairs with as proposed, so the rules kept do not
    depend on the order the affinity rules arrive in (ADR-0087 amendment
    2026-10-18)."""
    merged: list[dict | None] = list(llm_rules)
    sampled = _sampled_columns(affinity_rules)
    positions = _llm_positions(llm_rules, sampled)
    added, taken = set(), set()
    for rule in affinity_rules:
        physical = _physical_key(rule)
        if physical in added:
            continue
        pairs = _paired_llm(llm_rules, positions, rule, sampled)
        if not pairs:
            added.add(physical)
            merged.append(rule)
            continue
        replacement = _replacement(rule, llm_rules[pairs[0]])
        if replacement is None:
            continue                      # the LLM's own rule for this pair wins
        added.add(physical)
        free = [index for index in pairs if index not in taken]
        if not free:
            merged.append(replacement)
            continue
        merged[free[0]] = replacement
        for index in free[1:]:
            merged[index] = None
        taken.update(free)
    return [rule for rule in merged if rule is not None]
