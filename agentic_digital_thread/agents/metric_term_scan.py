# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0078 D2/D3 — the bounded scan primitives of the DEFINED-metric matcher.

Split from ``metric_term_resolution`` (the ONE vocabulary home, which
re-exports every name here) for the file-size cap. Pure and stdlib-only:

* :data:`MAX_MATCH_INPUT_CHARS` bounds the question scanned; over it the
  matcher raises :class:`MatcherInputTooLong` and the caller fails CLOSED;
* :func:`_scan_defined_forms` is ONE pass over word starts with ONE compiled
  alternation (longest form first) — at most one span per word;
* :func:`_keep_longest_spans` resolves overlaps with a linear occupancy map;
* the imperative / list-join request gates admit a DEFINED single-word form
  in "show me the <term>" / "compare <a> and <b>" requests.
"""
from __future__ import annotations

import functools
import re

#: Hard bound on the question the defined-metric matcher scans (ADR-0078 D3).
#: Over it :func:`defined_metric_terms` RAISES :class:`MatcherInputTooLong` and
#: the caller composes a FORBIDDING ``input_overflow`` block — the matching
#: work is bounded and a long question is never a silent miss.
MAX_MATCH_INPUT_CHARS = 8000

#: Imperative request verbs that make "<verb> [me] [the] <term>" a METRIC
#: REQUEST for a DEFINED single-word form. Generic
#: English only; used for defined-metric admission, never for the unresolved
#: pre-check (there an unknown acronym after "find"/"list" is rarely a metric).
IMPERATIVE_REQUEST_VERBS = frozenset({
    "show", "report", "compare", "give", "list", "tell", "find", "get",
    "display", "calculate", "compute", "plot", "chart", "graph", "trend",
    "summarize", "summarise", "track", "check", "pull", "fetch", "explain",
    "provide",
})
_IMPERATIVE_RE = re.compile(
    r"^(?:(?:please|pls|can you|could you|would you|will you)\s+)*"
    r"(?:" + "|".join(sorted(IMPERATIVE_REQUEST_VERBS)) + r")\s+"
    r"(?:(?:me|us)\s+)?(?:about\s+)?"
    r"(?:(?:the|our|my|a|an|this|that|current|latest|today s|todays)\s+){0,3}$")
#: A defined single-word form LISTED right after an admitted one ("compare
#: <a> and <b>", "report <a>, <b>") joins the same request.
_LIST_JOIN_RE = re.compile(
    r"^\s+(?:(?:and|or|vs|versus|with|against|plus)\s+)?(?:(?:the|our|my)\s+)?$")
#: Context window (chars) handed to the request gates — bounds per-span work.
_CONTEXT_SLICE_CHARS = 160


class MatcherInputTooLong(ValueError):
    """The question exceeds :data:`MAX_MATCH_INPUT_CHARS` (fail CLOSED)."""

    def __init__(self, length: int):
        super().__init__(f"question length {length} exceeds the defined-metric "
                         f"matching bound {MAX_MATCH_INPUT_CHARS}")
        self.length = length


@functools.lru_cache(maxsize=8)
def _forms_pattern(ordered_forms: tuple[str, ...]) -> "re.Pattern[str]":
    """ONE alternation over every form, longest first, with the trailing
    word boundary of the ``_resolves`` idiom. Cached per vocabulary."""
    alternation = "|".join(re.escape(form) for form in ordered_forms)
    return re.compile(r"(?:" + alternation + r")(?![a-z0-9])")


def _word_starts(normalized: str):
    """Offsets where a word starts in NORMALIZED text (the leading boundary)."""
    if normalized:
        yield 0
    for index, char in enumerate(normalized):
        if char == " ":
            yield index + 1


def _scan_defined_forms(normalized: str, forms) -> list[tuple[int, int, str]]:
    """ONE pass over the word starts: at each, the LONGEST form that matches
    there. At most one span per word — work and memory are bounded by the
    (bounded) question, never by forms × occurrences. Pure."""
    ordered = tuple(sorted(forms, key=lambda form: (-len(form), form)))
    if not ordered or not normalized:
        return []
    pattern = _forms_pattern(ordered)
    spans: list[tuple[int, int, str]] = []
    for start in _word_starts(normalized):
        found = pattern.match(normalized, start)
        if found:
            spans.append((start, found.end(), found.group(0)))
    return spans


def _keep_longest_spans(spans) -> list[tuple[int, int, str]]:
    """Overlapping spans keep the LONGEST (ties: the earliest); the survivors
    come back in first-appearance order. Linear occupancy check. Pure."""
    if not spans:
        return []
    occupied = bytearray(max(span[1] for span in spans) + 1)
    kept: list[tuple[int, int, str]] = []
    for span in sorted(spans, key=lambda s: (-(s[1] - s[0]), s[0], s[2])):
        if not any(occupied[span[0]:span[1]]):
            occupied[span[0]:span[1]] = b"\x01" * (span[1] - span[0])
            kept.append(span)
    return sorted(kept, key=lambda s: s[0])
