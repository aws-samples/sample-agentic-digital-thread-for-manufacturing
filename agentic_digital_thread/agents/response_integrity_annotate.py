# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Annotate-mode answer verification (ADR-0047).

``annotate`` keeps the model-authored answer as the TRANSPORT and uses the
integrity capture layer as an AUDIT: the server deterministically verifies the
answer text against the trusted observations captured from this invocation's
tool results — no model cooperation (no typed claim drafts, no locator
conventions) required.

Two outputs:

1. A grounding sidecar (``checked`` / ``verified`` / ``unverified`` / ``score``)
   for shadow metrics, UI badges, and the smoke scorecard.
2. A fabrication verdict: an answer that asserts factual tokens while ZERO
   trusted observations exist for the turn is BLOCKED (the one failure mode
   worth hard-blocking — the model wrote numbers although no tool returned
   data). Any turn WITH evidence passes through unchanged; unverified tokens
   only lower the score, they never block.

Pure and offline-testable: the only coupling is the read-only capture state.
"""
from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .response_integrity_capture import _state

logger = logging.getLogger(__name__)

# Numeric literals in prose: 1234 / 1,234 / 12.5 / 93.5% / -3.2 — but not
# list markers ("1. ") or fragments inside words/identifiers.
_NUMBER_RE = re.compile(
    r"(?<![\w.\-])(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+\.\d+|-?\d+)(?!\.?\d)(?![\w])"
)
# Identifier-ish tokens: letters+digits mixed (NRT6, BWU3, WO-1042, MKE-FILL-001).
_IDENTIFIER_RE = re.compile(
    r"\b(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z][A-Za-z0-9_-]{1,38}\b"
)
# Trivial numbers that appear in prose structure, never worth verifying.
_TRIVIAL_NUMBERS = {"0", "1", "2"}
# Unit-ish suffixed tokens ("24h", "7d", "30s", "5min") are duration prose,
# not source identifiers.
_UNIT_TOKEN_RE = re.compile(r"^\d+(h|hr|hrs|d|day|days|s|sec|min|m|ms|x)$", re.I)

_BLOCKED_MESSAGE = (
    "No supporting data was returned by this system for this question, so an "
    "answer was withheld rather than risk stating unverified figures. "
    "Reason: the connected system produced no readable evidence this turn "
    "(it may be unavailable, empty for the requested filter, or mid-refresh). "
    "This is a deterministic server-side integrity decision for this turn, "
    "NOT a transient error: retrying the same query will return the same "
    "result, so report this honestly instead of retrying."
)


def _canonical_number(token: str) -> str | None:
    try:
        value = Decimal(token.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return format(value.normalize(), "f")


def _observation_value_index(observations: list[Any]) -> tuple[set[str], set[str]]:
    """Build (numeric canonicals, casefolded strings) from captured observations."""
    numbers: set[str] = set()
    strings: set[str] = set()
    for obs in observations:
        value = getattr(obs, "canonical_value", None)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float, Decimal)):
            canonical = _canonical_number(str(value))
            if canonical is not None:
                numbers.add(canonical)
            continue
        if isinstance(value, str) and value:
            strings.add(value.casefold())
            canonical = _canonical_number(value)
            if canonical is not None:
                numbers.add(canonical)
        entity = getattr(obs, "entity_label", "")
        if isinstance(entity, str) and entity:
            strings.add(entity.casefold())
    return numbers, strings


def _factual_tokens(text: str, question: str) -> tuple[list[str], list[str]]:
    """Extract (numeric, identifier) tokens asserting facts in the answer.

    Tokens already present in the QUESTION are excluded — the model restating
    the user's own constraint ("last 7 days") asserts nothing new.
    """
    question_fold = (question or "").casefold()
    numbers: list[str] = []
    for match in _NUMBER_RE.finditer(text or ""):
        token = match.group(0)
        if token.lstrip("-") in _TRIVIAL_NUMBERS or token in question_fold:
            continue
        numbers.append(token)
    identifiers: list[str] = []
    for match in _IDENTIFIER_RE.finditer(text or ""):
        token = match.group(0)
        if _UNIT_TOKEN_RE.match(token) or token.casefold() in question_fold:
            continue
        identifiers.append(token)
    return numbers, identifiers


def grounding_report(text: str, question: str, invocation_state: dict) -> dict:
    """Deterministic text-vs-observations grounding report (pure, bounded)."""
    from .response_integrity_contracts import Completeness

    state = _state(invocation_state)
    observations = list(state.observations)
    numbers, identifiers = _factual_tokens(text, question)
    obs_numbers, obs_strings = _observation_value_index(observations)

    verified = 0
    unverified: list[str] = []
    for token in numbers:
        canonical = _canonical_number(token)
        if canonical is not None and canonical in obs_numbers:
            verified += 1
        else:
            unverified.append(token)
    for token in identifiers:
        fold = token.casefold()
        if fold in obs_strings or any(fold in value for value in obs_strings):
            verified += 1
        else:
            unverified.append(token)

    checked = len(numbers) + len(identifiers)
    # Did ANY tool this turn return a readable payload? Distinguishes
    # "capture could not PROVE the data" (annotate, never block) from
    # "no data existed at all" (fabrication candidate). A payload that was
    # seen but proof-rejected still appends COMPLETE/PARTIAL to completeness;
    # an errored tool skips capture entirely and an empty result appends
    # UNAVAILABLE only.
    payload_seen = any(
        item in (Completeness.COMPLETE, Completeness.PARTIAL)
        for item in state.completeness
    ) or bool(observations)
    return {
        "mode": "annotate",
        "checked": checked,
        "verified": verified,
        "unverified": unverified[:20],
        "score": round(verified / checked, 3) if checked else None,
        "observations": len(observations),
        "tool_calls": state.tool_calls,
        "payload_seen": payload_seen,
        "reason_codes": [reason.value for reason in state.reason_codes],
    }


def annotate_answer(text: str, question: str, invocation_state: dict) -> tuple[str, dict]:
    """Return ``(answer_or_block, grounding_sidecar)`` for annotate mode.

    The answer passes through UNCHANGED unless it asserts factual tokens while
    NO tool this turn returned a readable payload — the true-fabrication case
    (the model wrote figures although no source produced data) — in which case
    an honest structured refusal replaces it. A payload the capture layer saw
    but could not PROVE (projection/lineage limits) never blocks: unverified
    tokens with seen payloads only lower the score (models paraphrase,
    reformat and derive; that is annotation territory, not blocking territory).
    """
    report = grounding_report(text, question, invocation_state)
    report["blocked"] = False
    if report["checked"] > 0 and not report["payload_seen"]:
        report["blocked"] = True
        logger.warning(
            "annotate: BLOCKED zero-evidence factual answer "
            "(checked=%d tool_calls=%d reasons=%s)",
            report["checked"], report["tool_calls"], report["reason_codes"],
        )
        blocked = json.dumps({
            # NOT "unavailable": that word is the orchestrator's RETRYABLE
            # transient vocabulary (its error contract instructs 3 escalating
            # retries of the SAME query), so a deterministic integrity verdict
            # presented as "unavailable" was re-queried up to 3x per system,
            # burning turns and thinning the synthesis — measured on the pilot
            # golden set as "both systems returned unavailable, retrying".
            # The verdict is terminal for the turn, so it says so.
            "status": "withheld",
            "retryable": False,
            "answer": _BLOCKED_MESSAGE,
            "claims": [],
            "evidence": [],
            "reason_codes": list(dict.fromkeys(
                [*report["reason_codes"], "NO_CURRENT_INVOCATION_EVIDENCE",
                 "FABRICATION_BLOCKED"])),
        }, ensure_ascii=False)
        return blocked, report
    logger.info(
        "annotate: grounding score=%s verified=%d/%d observations=%d",
        report["score"], report["verified"], report["checked"],
        report["observations"],
    )
    return text, report
