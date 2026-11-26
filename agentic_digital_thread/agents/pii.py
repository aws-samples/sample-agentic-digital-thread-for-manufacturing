# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Best-effort, deterministic PII helpers — import-light and offline-testable.

Two concerns, both dataset-agnostic (no dataset literals):

  * ``PII_PATTERNS`` / ``redact_pii`` — structured-identifier redaction lifted
    VERBATIM from the orchestrator's memory-write hardening (SSN / email / phone /
    Luhn-valid standalone card numbers — a non-card-shaped digit run that ends a
    letter-led, non-card-keyword ``PREFIX-`` identifier is never a card). The
    orchestrator re-imports these so there is ONE
    source of truth (see ``orchestrator._redact_pii``). This covers structured
    identifiers only, NOT names or other unstructured PII (best-effort).
  * ``column_looks_pii`` — a NAME heuristic used when surfacing sample values in a
    schema bundle, so a column whose NAME implies it holds personal data never has
    its raw values rendered (rendered as ``[redacted]`` instead). Generic token
    matching — no dataset-specific column names.

This module deliberately imports nothing heavy (no strands / MCP / boto3) so it is
importable from both the app and the orchestrator runtime and unit-testable offline.
"""

import re
from collections.abc import Callable

# ---------------------------------------------------------------------------
# Structured-identifier redaction (verbatim from orchestrator memory-write path)
# ---------------------------------------------------------------------------
# Bedrock Guardrails (incl. PII anonymization) protect the model's RESPONSES, not
# values surfaced into a prompt/schema bundle or persisted to memory. We redact
# structured identifiers deterministically: SSN / email / phone / credit-card-shaped
# numbers. This is best-effort and does NOT cover names or unstructured PII.
# Card candidate: a STANDALONE run of 13-16 digits; a single space or dash may
# sit only BETWEEN digits (never leading/trailing), and no word character may
# touch either end.
_CARD_CANDIDATE = re.compile(r"(?<![A-Za-z0-9_])\d(?:[ -]?\d){12,15}(?![A-Za-z0-9_])")
# A record identifier the digits would be the tail of: a letter-led,
# hyphen-joined chain ending in "-" (``WO-``, ``RUN-2026-``, ``lot_A1-``).
# Possessive repeats (the final "-" is never part of a segment); its one
# caller passes at most _ID_PREFIX_LOOKBACK (64) characters.
_ID_PREFIX_TAIL = re.compile(r"[A-Za-z][A-Za-z0-9]*+(?:-[A-Za-z0-9]++)*+-\Z")
# Enough look-back for any realistic identifier prefix; keeps the check O(1).
_ID_PREFIX_LOOKBACK = 64
# A prefix segment that labels the digits AS a card never exempts them.
_CARD_KEYWORDS = frozenset({
    "card", "cc", "visa", "mastercard", "mc", "amex", "discover",
    "pan", "acct", "account", "number", "no", "num",
})
# How cards are actually written: 4-4-4-4 (space or dash), Amex 4-6-5, or one
# ungrouped 15-16 digit run. Identifier tails (e.g. 8-5 ``20260928-20644``) are not.
_CARD_SHAPED = re.compile(r"\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}|\d{4}[ -]\d{6}[ -]\d{5}|\d{15,16}")


def _luhn_ok(digits: str) -> bool:
    """Luhn checksum over a pure digit string (the card-number check digit)."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _is_identifier_tail(match: re.Match) -> bool:
    """True only when the run is the non-card-shaped tail of a non-card prefix.

    Observed: ``WO-20260928-20644`` has 13 digits after its prefix and was
    masked in a final answer, and such a tail can be Luhn-valid by chance. But a
    prefix must not smuggle a card through (``card-4111…``, ``a1b2-4111…``), so
    the skip needs (a) a letter-led ``PREFIX-`` chain, (b) no card keyword in
    any of its segments, and (c) a run that is not written the way cards are.
    """
    start = match.start()
    before = match.string[max(0, start - _ID_PREFIX_LOOKBACK):start]
    prefix = _ID_PREFIX_TAIL.search(before)
    if prefix is None:
        return False
    if any(seg.lower() in _CARD_KEYWORDS for seg in prefix.group(0).split("-")):
        return False
    return _CARD_SHAPED.fullmatch(match.group(0)) is None


def _redact_card(match: re.Match) -> str:
    """Replace a Luhn-valid card candidate unless it is an identifier tail."""
    digits = re.sub(r"[ -]", "", match.group(0))
    if not _luhn_ok(digits) or _is_identifier_tail(match):
        return match.group(0)
    return "[REDACTED_CARD]"


PII_PATTERNS: list[tuple[re.Pattern, "str | Callable[[re.Match], str]"]] = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
    (re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)"), "[REDACTED_PHONE]"),
    (_CARD_CANDIDATE, _redact_card),
]


def redact_pii(text: str) -> str:
    """Redact structured PII identifiers from a string (best-effort, see PII_PATTERNS)."""
    if not text or not isinstance(text, str):
        return text
    # Email before phone so the digits in an email aren't partially phone-matched.
    for pattern, replacement in PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ---------------------------------------------------------------------------
# Column-NAME PII heuristic (used to drop sample values for personal-data columns)
# ---------------------------------------------------------------------------
# Snake_case is the dominant convention, so we tokenize on non-alphanumerics and
# match whole tokens (avoids false positives like "dashboard" -> "card"). A short
# substring list catches compact / camelCase forms (first_name, ccNum, etc.).
# Generic — no dataset-specific names.
_PII_TOKENS = {
    "ssn", "email", "mail", "phone", "mobile", "telephone", "fax",
    "dob", "birth", "birthdate", "birthday", "passport", "card", "address", "cc",
}
_PII_SUBSTRINGS = (
    "ssn", "email", "passport",
    "first_name", "last_name", "full_name", "middle_name",
    "firstname", "lastname", "fullname",
    "cc_num", "ccnum", "credit_card", "creditcard",
)


def column_looks_pii(name) -> bool:
    """Return True when a column NAME implies it holds personal data.

    Used to suppress sample-value surfacing for such columns (rendered as
    ``[redacted]``). Best-effort name heuristic only — the value-level
    :func:`redact_pii` still runs on every surfaced sample regardless.
    """
    if not name:
        return False
    n = str(name).strip().lower()
    if not n:
        return False
    tokens = {t for t in re.split(r"[^a-z0-9]+", n) if t}
    if tokens & _PII_TOKENS:
        return True
    return any(sub in n for sub in _PII_SUBSTRINGS)
