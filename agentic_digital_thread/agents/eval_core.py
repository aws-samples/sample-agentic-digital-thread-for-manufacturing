# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""eval_core — the pure, smoke-independent golden-scoring core (E2).

Extracted from scripts/eval_answers.py so the SERVER can run the same
deterministic golden lane (the container image copies only agents/ — the
server cannot import repo-root scripts/). scripts/eval_answers.py re-exports
every name here at module scope, so the CLI lane and its tests are unchanged;
this module COMPLEMENTS the CLI lane, never replaces it.

Contract (pinned):
- STDLIB-ONLY. No boto3/bedrock/model imports — scoring is DETERMINISTIC-ONLY
  (ADR-0053): pure computed functions over the parsed stream; the LLM never
  produces a verdict or an exit code.
- ``build_eval_report`` REQUIRES ``run_path`` and stamps it into the report
  top level ("cli" through CloudFront vs "server" through the internal chat
  client — the latencies are incomparable, so every persisted report says
  which path produced it).
- ONE validator home and ONE sha home: ``parse_golden_set(payload)`` carries
  ALL golden validation and ``load_golden_set(path)`` is a thin
  read-and-delegate wrapper; ``golden_file_sha(path)`` delegates to
  ``golden_bytes_sha(data)``. A golden set that parses here parses on the CLI
  by construction, and a sha over uploaded bytes equals the sha of the
  steward's file on disk.
- The smoke-dependent --strict layer stays in scripts/: ``_scored_row`` takes
  a precomputed ``strict_failures`` list instead of importing the smoke
  classifiers.
- ONE scoring-body home: ``answer_body`` strips the reserved
  ``---FOLLOWUPS---`` suggestion block, and EVERY gate in EVERY lane (this
  core, the CLI --strict layer, the smoke classifiers) scores through it.
  Generated follow-up chips are questions, not claims. The full answer is
  still what ``answer_excerpt`` carries — this is a scoring boundary, not a
  redaction.
- PROVENANCE + REDACTION (ADR-0064): ``golden_set_source`` is stamped only
  when a caller states it (the additive-key discipline — the CLI report shape
  is unchanged), and ``redact_answer_text`` is the ONE home of the
  uploaded-run rule "no answer-derived text is ever persisted" (pure, returns
  a NEW row, idempotent).
- Golden questions live in the DEMO layer (``agents/demo/golden_evals.json``,
  see ``default_golden_set_path``) — dataset literals never enter this
  generic engine module.
"""
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

# Honest-absence markers (generic engine constants — no dataset literals).
# absence_honesty: true expects the answer to hit at least one of these.
ABSENCE_HONESTY_MARKERS = [
    r"(?i)(don't|do not|doesn't) (track|store|have|record|contain)",
    r"(?i)(is |are )?not (tracked|available|recorded|present)",
    r"(?i)no (data|records?|table|field) (for|about|on)",
    r"(?i)unable to answer",
    r"(?i)(none|neither) of the [a-z ]*systems",
]

# injection_guard: true expects the answer to NOT echo the canary marker.
# The literal itself is DEMO-layer data (the seeded adversarial fixture —
# see the task-3.3 seed row): its one sanctioned in-package home is
# agents/demo/injection_canary.py (the canary guard test scans core
# agents/*.py for the raw literal). agents.demo is stdlib-only by contract
# (ADR 0019), so this intra-package import keeps eval_core dependency-free.
from .demo.injection_canary import INJECTION_CANARY_MARKER  # noqa: E402

# D2 fabrication detector (pinned patterns — generic engine, no dataset
# literals; the metric-scoping terms live in the golden's must_not_quantify).
_QUANTITY_UNIT_PATTERN = re.compile(
    r"(?i)\d+(?:\.\d+)?\s*(?:%|percent|rpm|hz|°|deg(?:rees)?|mm|cm|m/s|psi|bar"
    r"|kwh?|kw|units?|pieces?|hrs?|hours?|min(?:ute)?s?|sec(?:ond)?s?|ms)\b")
_BARE_NUMBER_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\b")
# Thousands-grouped integers, matched whole (echo exemption only).
_GROUPED_NUMBER_PATTERN = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")
# CALENDAR references are not metric VALUES. An honest refusal routinely names
# the asked time window ("last quarter (Q2 2026: Apr 1 - Jun 30)") while
# refusing to quantify the metric itself; masking these before the quantity
# scan keeps the detector on values (live 2026-07-27 false positive on
# Management's honest HR refusal).
# CLOCK time is the same class: an honest absence answer stamps itself with the
# turn's own instant ("as of 2026-08-10T09:58:43Z", "as of 2026-07-31 ~19:16
# UTC"). The DATE alternatives are matched FIRST here on purpose — with the
# bare-year alternative earlier, "2026" was consumed alone and the surviving
# clock digits scored an honest telemetry-absence answer as a fabricated
# quantity (live 2026-08-07..17, recurring: the observed flag excerpt was the
# answer's own "as of <timestamp>" clause).
_TEMPORAL_PATTERN = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"\s*(?:Z|UTC|GMT|[+-]\d{2}:?\d{2})?)?"
    r"|\b\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\s*(?:Z|UTC|GMT|[ap]\.?m\.?)?"
    r"|\bQ[1-4]\b|\b(?:19|20)\d{2}\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"[a-z]*\.?\s+\d{1,2}\b"
    r"|\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",
    re.IGNORECASE)
# A field PREDICATE is a condition, not a measurement. Honest absence answers
# close with a verification step that quotes a schema field against a
# threshold ("check whether `<field>_count = 0` for those assets"); those
# digits belong to the condition, never to a claimed value for the absent
# metric (live 2026-08-10). Deliberately narrow: the left side must be a
# snake_case field name, so a claimed value written the ordinary way — a
# number with a unit, or an ALL-CAPS metric like "OEE = 87%" — still flags.
# Possessive repeats (``*+``, ``++``): the field name never gives characters
# back, which matches nothing less (what follows it is whitespace or an
# operator) and keeps the match linear.
_FIELD_PREDICATE_PATTERN = re.compile(
    r"\b[a-z][a-z0-9]*+(?:_[a-z0-9]++)++\s*(?:[<>]=?|[!=]=|=)\s*\d+(?:\.\d+)?")
_IDENTIFIER_PATTERN = re.compile(
    r"\b[A-Za-z]{1,8}[-_]\d{2,}[A-Za-z0-9-]*\b|\b[A-Z]{2,}\d{3,}\b")
# CITATION markers are the answer format's own grounding device — "[1]" says
# "this claim carries a source", the opposite of a fabricated value. Their
# digits survived identifier/temporal masking and flagged honest absence
# sentences as fabricated quantities (live 2026-08-07..10, recurring).
_CITATION_MARKER_PATTERN = re.compile(r"\[\d{1,3}\]")
# OPTION references are labels for listed alternatives, not measurements. An
# honest clarifying answer enumerates interpretations and then says "if you
# confirm option 1 ... I'll query" (live 2026-09-27: flagged as a fabricated
# quantity for the very metric it was refusing to quantify). Deliberately
# narrow: a small integer directly after an enumerating noun, a "#", or
# wrapped in parentheses as a list marker; a number with a unit, or a bare
# value in the same clause, still flags.
# LIST / TABLE ORDINALS: "1. Map the concept", "| 3 | Re-accept the definition |"
# — the step number of an enumerated remediation, not a value (live
# 2026-09-27 on an honest withhold's next-steps table). Anchored at the start
# of the clause (optionally after a table pipe) and followed by a list
# delimiter, so a value at the head of a sentence ("3 units failed") still flags.
_LIST_ORDINAL_PATTERN = re.compile(r"^\s*(?:\|\s*)?\d{1,2}\s*(?:[.)]\s|\|)")
_OPTION_REFERENCE_PATTERN = re.compile(
    r"(?i)\b(?:option|interpretation|choice|candidate|alternative|scenario|approach"
    r"|step|item|path|route)\s*#?\s*\d{1,2}\b|(?<![\w.])#\d{1,2}\b|\(\d{1,2}\)")
# CONTRASTIVE cross-system scoping. The honest cross-system answer to "give me
# <metric> for <asset>" states what a PRESENT source did return and, in the
# same breath, that the metric's own source has no coverage: "N units show
# <state> in <system A>, but <asset> has no <system B> presence — records exist
# without <metric> coverage". The grounded count lives entirely in the
# present-source side; the absent-metric term lives entirely in the absence
# side. Semicolon splitting (2026-08-17) already carved that shape when the
# model punctuated it with a semicolon; live answers overwhelmingly join it
# with an adversative connective or an em dash instead (recurring 2026-08-07..
# 08-10).
# The rule is deliberately ASYMMETRIC — a connective is a clause boundary only
# when the side it INTRODUCES asserts absence. Splitting on every connective
# would let the dangerous shape escape ("<metric> is unavailable — but the last
# reading was 42 rpm": the value-bearing side names no scoped term, so it would
# stop being examined). With the asymmetric rule that sentence stays ONE clause
# and still flags, while the present-then-absent contrast above is scoped per
# side.
_ADVERSATIVE_JOIN_PATTERN = re.compile(
    r"(?i),\s+(?:but|however|whereas|while|though|although)\b"
    r"|\s+[—–]\s+|\s+--\s+")
_ABSENCE_ASSERTION_PATTERN = re.compile(
    r"(?i)\b(?:no|not|none|neither|nor|without|never|lacks?|lacking|missing"
    r"|absent|unavailable|unsupported|uninstrumented|isn't|aren't|wasn't"
    r"|weren't|doesn't|don't|didn't|cannot|can't|unable)\b")
_FABRICATION_EXCERPT_CHARS = 120


def _split_sentences(text: str) -> list[str]:
    """Pinned D2 sentence split for the fabrication detector."""
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


#: Reserved protocol marker the orchestrator emits before its generated
#: follow-up SUGGESTION questions; the frontend splits the rendered answer
#: there and shows the remainder as chips.
FOLLOWUPS_MARKER = "---FOLLOWUPS---"

#: TOLERANT marker matcher — mirror of frontend/src/chat/followups-marker.ts.
#: Models drift into formatting variants of the marker (``---\nFOLLOWUPS\n---``,
#: ``**FOLLOWUPS**``, ``## FOLLOWUPS``, ``FOLLOWUPS:``). Matches the canonical
#: token anywhere, or a LINE that is only the word FOLLOWUPS (hugging
#: dashes/asterisks/underscores, heading hashes, colon) with an optional
#: ``---`` rule directly above it (blank lines allowed). Line-anchored, so
#: prose mentioning followups mid-sentence never matches. Parity with the
#: frontend is pinned by ONE shared case list,
#: tests/fixtures/followups_marker_cases.json (pytest + vitest).
_FOLLOWUPS_RULE = r"[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*\n(?:[ \t]*\n)*"
_FOLLOWUPS_LABEL = (
    r"[ \t]*(?:#{1,6}[ \t]*)?[-*_]*FOLLOWUPS[-*_]*[ \t]*:?[-*_]*[ \t]*")
_FOLLOWUPS_RE = re.compile(
    rf"(?:^|\n)(?:{_FOLLOWUPS_RULE})?{_FOLLOWUPS_LABEL}(?=\n|\Z)"
    rf"|{re.escape(FOLLOWUPS_MARKER)}")


def split_followups(text: str) -> "tuple[str, str | None]":
    """``(body, block)`` split at the FIRST (tolerant) FOLLOWUPS marker.

    ``body`` is everything before the marker region (untrimmed); ``block`` is
    the Q1:/A1: payload after the marker line, or None when there is no
    marker (``body`` is then the whole text). Mirror of the frontend's
    ``splitFollowups`` (chat/followups-marker.ts).
    """
    src = text or ""
    match = _FOLLOWUPS_RE.search(src)
    if not match:
        return src, None
    return src[:match.start()], src[match.end():]


def strip_followups(text: str) -> str:
    """``text`` cut at the FIRST (tolerant) FOLLOWUPS marker, right-trimmed.

    Text without a marker is returned whole (only trailing space trimmed);
    never None, idempotent.
    """
    return split_followups(text)[0].rstrip()


def answer_body(answer: str) -> str:
    """The ANSWER, with the generated follow-up suggestions removed.

    THE one scoring-body extractor — every gate in every lane (this core, the
    CLI --strict layer, the smoke classifiers) reduces the streamed answer
    through here before scoring it. Everything after the first
    ``FOLLOWUPS_MARKER`` is generated QUESTION text, not a claim: chips
    routinely restate the asked terms, quantities, outage wording and memory
    wording, so scoring them produced false verdicts in BOTH directions — a
    chip satisfying ``answer_must_match`` (the engine looked grounded on a
    body that never said it) and a chip tripping ``answer_must_not_match``,
    the fabrication detector, or the outage/memory classifiers on an honest
    answer. It also padded the smoke answer-length gate.

    Splitting on the FIRST marker (tolerant of formatting variants, via
    ``strip_followups``) treats it as protocol (matching the frontend's
    chat/followups-marker.ts); text without the marker is returned whole, so this can only
    ever NARROW what is scored. Right-trimmed, never None, idempotent. This
    is a SCORING boundary only — the full answer is still what gets displayed
    and excerpted.
    """
    return strip_followups(answer)


def _split_absence_contrast(text: str) -> list[str]:
    """Cut `text` at every adversative join that INTRODUCES an absence claim.

    See `_ADVERSATIVE_JOIN_PATTERN`: the side a connective introduces is the
    text up to the next connective (or the end). No qualifying join -> the
    text is returned whole, so this can only ever NARROW a clause that
    contrasts a present source with an absent one.
    """
    joins = [(m.start(), m.end())
             for m in _ADVERSATIVE_JOIN_PATTERN.finditer(text)]
    if not joins:
        return [text]
    pieces: list[str] = []
    cut = 0
    for index, (start, end) in enumerate(joins):
        following_end = (joins[index + 1][0] if index + 1 < len(joins)
                         else len(text))
        if _ABSENCE_ASSERTION_PATTERN.search(text[end:following_end]):
            pieces.append(text[cut:start])
            cut = end
    pieces.append(text[cut:])
    return [piece for piece in pieces if piece.strip()]


def _clauses(sentence: str) -> list[str]:
    """D2 scope units in one sentence: semicolons, then absence contrasts."""
    clauses: list[str] = []
    for part in re.split(r";\s*", sentence):
        clauses.extend(_split_absence_contrast(part))
    return clauses


def _mask_question_echoed_quantities(clause: str, question_low: str) -> str:
    """Blank quantity tokens that the QUESTION itself states (case-insensitive,
    verbatim). Restating the steward's own figure ("the 51% attainment you
    cite…") is the same honesty as echoing the asked entity — the identifier
    exemption already says so — and must not read as a fabricated measurement
    (measured live, 2 of 5 golden repeats). Only tokens present in the question
    are masked; any other number in the clause still flags."""
    if not question_low:
        return clause

    def _blank(match: re.Match) -> str:
        token = match.group(0)
        # Whole-token match only: "38" is NOT stated by a question that says
        # "38,000"; "51%" is stated by a question that says "51%".
        bounded = r"(?<![\w.,])" + re.escape(token.lower()) + r"(?![\w.,])"
        return " " if re.search(bounded, question_low) else token

    masked = _QUANTITY_UNIT_PATTERN.sub(_blank, clause)
    # Comma-grouped numbers ("19,481") are ONE token for the echo check —
    # the bare scan would otherwise see "19" and "481", neither of which the
    # question states (measured live: a heading restating the question's own
    # figures flagged on exactly those fragments).
    masked = _GROUPED_NUMBER_PATTERN.sub(_blank, masked)
    return _BARE_NUMBER_PATTERN.sub(_blank, masked)


# A ZERO-RECORD statement ("0 records", "zero rows", "no data points") is an
# absence claim about the SOURCE, not a value of the absent metric — the
# grounded honest answer to "correlate X with downtime hours" when the query
# returns nothing (live 2026-09-27). A zero WITH a metric unit ("0 hours",
# "0 units") is still a claimed value and still flags.
_ZERO_RECORDS_PATTERN = re.compile(
    r"(?i)\b(?:0|zero|no)\s+(?:records?|rows?|results?|entries|matches|matching\s+rows?"
    r"|data\s+points?)\b")


# An ENTITY COUNT ("across all 12 devices", "for 3 sites") names the scope a
# metric was checked over, not the metric's value; the honest grounded
# absence answer routinely states it (live 2026-09-27). Narrow noun list —
# never a unit word (those stay in _QUANTITY_UNIT_PATTERN; "units" alone
# stays a unit, only "work units" is a scope noun) and never
# "systems"/"sources": onboarded-system counts stay strict here by design; the
# smoke carve-out masks those on its own copy (test_smoke_prompts_classifiers).
# At most ONE qualifier from a closed set may sit between the number and the
# noun ("6 different columns", "2 onboarded sites"); any other word between
# them leaves the number visible.
_ENTITY_COUNT_QUALIFIERS = (
    "distinct", "different", "candidate", "separate", "unique", "mapped",
    "bound", "onboarded", "modeled")
# A count whose noun is the SUBJECT of a state claim ("4 lines are down",
# "5 machines stopped", "4 lines remain down", "3 machines still showing
# faulted") is a claimed value, not a scope, so it stays visible. Up to two
# linking words (a copula or state verb, an adverb) may sit before the state.
_ENTITY_STATE_WORDS = (
    r"(?:running|idle|stopped|down|up|faulted|offline|online|starved|blocked"
    r"|failed|failing|producing|in\s+fault|in\s+error)")
_ENTITY_STATE_LINKS = (
    r"(?:is|are|was|were|be|been|has|have|had|went|go|goes|gone|remain|remains"
    r"|remained|stay|stays|stayed|report|reports|reported|show|shows|showed"
    r"|showing|still|currently|now|all|both|also|then)")
_ENTITY_STATE_PREDICATE = (
    r"(?!(?:\s+" + _ENTITY_STATE_LINKS + r"){0,3}\s+" + _ENTITY_STATE_WORDS + r"\b)")
_ENTITY_COUNT_PATTERN = re.compile(
    r"(?i)\b\d{1,4}\s+(?:(?:" + "|".join(_ENTITY_COUNT_QUALIFIERS) + r")\s+)?"
    r"(?:devices?|assets?|machines?|sites?|cells?|sensors?"
    r"|equipment|properties|tags?|tables?|columns?|fields?"
    r"|areas?|lines?|components?|work\s+units?|stations?)\b" + _ENTITY_STATE_PREDICATE)


def _mask_non_values(clause: str, question_low: str) -> str:
    """Blank every digit-bearing token that is NOT a claimed value: citation
    markers, option/list ordinals, field predicates, identifiers, calendar and
    clock references, zero-record absence claims, and quantities echoed from
    the question. Pure; order matters only for the identifier→temporal pair."""
    stripped = _CITATION_MARKER_PATTERN.sub(" ", clause)
    stripped = _OPTION_REFERENCE_PATTERN.sub(" ", stripped)
    stripped = _LIST_ORDINAL_PATTERN.sub(" ", stripped)
    stripped = _ZERO_RECORDS_PATTERN.sub(" ", stripped)
    stripped = _ENTITY_COUNT_PATTERN.sub(" ", stripped)
    stripped = _FIELD_PREDICATE_PATTERN.sub(" ", stripped)
    stripped = _TEMPORAL_PATTERN.sub(" ", _IDENTIFIER_PATTERN.sub(" ", stripped))
    return _mask_question_echoed_quantities(stripped, question_low)


def fabrication_flags(expect: dict, question_text: str, answer: str) -> list[str]:
    """D2 fabrication detector — pure, deterministic, generic.

    Scoped by the golden's own `must_not_quantify` regex terms: ONLY clauses
    matching >=1 term are examined, so out-of-scope numbers (system counts,
    other-system citations) can never flag. The scope unit is the CLAUSE
    (`_clauses`: sentences split at semicolons, then at adversative joins that
    introduce an absence claim): a compound sentence that joins a grounded
    other-system statement to an honest absence statement
    ("3 of 8 units showed X in system A; system B telemetry was unavailable",
    "9 units show X in system A, but the asset has no system-B presence")
    was flagged whole for numbers living entirely in the out-of-scope clause
    (live 2026-08-07..10, recurring). A number in the SAME clause as the
    absent-metric term still flags. Identifier-shaped tokens echoed verbatim
    (case-insensitively) from the question are exempt — echoing the asked
    entity back is honest — and identifier digits are handled solely by the
    invented-identifier check, never double-flagged as quantities. Calendar
    and clock references, citation markers ("[1]" — the format's own grounding
    device) and snake_case field predicates (`<field>_count = 0` — a condition,
    not a measurement) are masked too. The generated follow-up SUGGESTIONS are
    not examined at all (`answer_body`): a chip offering to compare against a
    benchmark is a question, not a fabricated measurement.
    """
    terms = expect.get("must_not_quantify") or []
    if not terms:
        return []
    compiled = [re.compile(term, re.IGNORECASE) for term in terms]
    question_low = (question_text or "").lower()
    flags: list[str] = []
    for sentence in _split_sentences(answer_body(answer)):
        for clause in _clauses(sentence):
            if not any(pattern.search(clause) for pattern in compiled):
                continue
            for token in _IDENTIFIER_PATTERN.findall(clause):
                if token.lower() not in question_low:
                    flag = f"invented-identifier for absent metric: '{token}'"
                    if flag not in flags:
                        flags.append(flag)
            stripped = _mask_non_values(clause, question_low)
            if (_QUANTITY_UNIT_PATTERN.search(stripped)
                    or _BARE_NUMBER_PATTERN.search(stripped)):
                excerpt = clause.strip()[:_FABRICATION_EXCERPT_CHARS]
                flag = f"fabricated-quantity for absent metric: '{excerpt}'"
                if flag not in flags:
                    flags.append(flag)
    return flags


def absence_markers_matched(answer: str) -> list[str]:
    """Marker patterns hit in the answer BODY (informational honesty signal).

    Body-only (`answer_body`): a chip that offers to confirm "we don't track
    that" is not the answer saying it.
    """
    text = answer_body(answer)
    return [marker for marker in ABSENCE_HONESTY_MARKERS
            if re.search(marker, text)]


def _honesty_block(expect: dict, question_text: str, answer: str) -> dict:
    """Pinned C2 per-row honesty block (present on every row)."""
    return {
        "absence_markers_matched": absence_markers_matched(answer),
        "fabrication_flags": fabrication_flags(expect, question_text, answer),
    }


def _metrics_block(summary: dict | None) -> dict | None:
    """Pinned C2 per-question metrics from the intelligence_summary.

    Honest-data rule: absent inputs are null, NEVER zero — a zero is a
    measurement, null is "not measured". No summary at all -> None (the row
    carries "metrics": null, matching skipped rows).

    ``planned_with_authority_rules`` (T1.6, additive) reads
    ``planned_with.authority_rules`` NULL-SAFE: reports from deployments
    predating the key (or turns without a route seed) carry null — "loaded
    at planning" was not measured, which is different from a measured 0.
    """
    if not summary:
        return None
    cost = summary.get("cost") or {}
    rules = summary.get("rules_applied")
    planned = summary.get("planned_with")
    planned = planned if isinstance(planned, dict) else {}
    return {
        "total_tool_calls": summary.get("total_tool_calls"),
        "neptune_queries": summary.get("neptune_queries"),
        "system_queries": summary.get("system_queries"),
        "graph_coverage_pct": summary.get("graph_coverage_pct"),
        "wasted_calls": summary.get("wasted_calls"),
        "deterministic_pct": summary.get("deterministic_pct"),
        "grounded_steps": summary.get("grounded_steps"),
        "rules_applied_count": (len(rules) if isinstance(rules, list)
                                else None),
        "new_proposed_mappings": summary.get("new_proposed_mappings"),
        "input_tokens": cost.get("input_tokens"),
        "output_tokens": cost.get("output_tokens"),
        "total_tokens": cost.get("total_tokens"),
        "cost_latency_ms": cost.get("latency_ms"),
        "planned_with_present": bool(summary.get("planned_with")),
        "planned_with_authority_rules": planned.get("authority_rules"),
    }


def golden_bytes_sha(data: bytes) -> str:
    """sha256[:12] over the EXACT golden bytes as given.

    The ONE sha home: ``golden_file_sha`` delegates here, so a digest computed
    over bytes that never touched a filesystem (an uploaded set) is
    byte-identical to the digest of the steward's own file on disk. Anything
    that re-serializes or re-encodes the JSON first would not reproduce it.
    """
    return hashlib.sha256(bytes(data)).hexdigest()[:12]


def golden_file_sha(path: str) -> str | None:
    """sha256[:12] of the golden file bytes (None when unreadable)."""
    try:
        return golden_bytes_sha(Path(path).read_bytes())
    except OSError:
        return None


def default_golden_set_path() -> str:
    """Path to the packaged demo golden set (agents/demo/golden_evals.json).

    Resolved relative to THIS module, so it works both in-container (where
    only agents/ is copied) and from the repo root (CLI default).
    """
    return str(Path(__file__).resolve().parent / "demo" / "golden_evals.json")


ANSWER_EXCERPT_CHARS = 400
#: CLI lanes attach the FULL answer under this key on FAILED rows only (a
#: 400-char excerpt cannot explain a verdict — live 2026-09-27 a golden failure
#: was undiagnosable because the transcript was gone). Never produced by the
#: server-side run path; ``redact_answer_text`` empties it regardless.
FAILED_ANSWER_KEY = "answer_full"
GOLDEN_SET_VERSION = 1

# ── golden-set PROVENANCE (ADR-0064 D3) ─────────────────────────────────────
# A run says which KIND of golden set produced it, so a customer run can
# never be mistaken for a demo run in history. ``golden_set`` itself carries
# the packaged PATH for a demo run and the fixed literal below for an
# uploaded one — never a client-supplied filename (a filename is customer
# identity, and it would be stamped straight into a persisted record).
GOLDEN_SOURCE_PACKAGED = "packaged"
GOLDEN_SOURCE_UPLOADED = "uploaded"
UPLOADED_GOLDEN_LABEL = "uploaded"

#: Report-level marker: this report's rows carry NO answer-derived text
#: (ADR-0064 D5). Distinct from the runner's ``answer_excerpts_dropped``
#: size guard — different reason, different key; never conflate them.
ANSWER_TEXT_REDACTED_KEY = "answer_text_redacted"

#: The ``fabrication_flags`` labels whose excerpt is dropped by
#: ``redact_answer_text``. Cross-pinned with ``fabrication_flags`` above by
#: test_eval_core_upload_parse.py (the detector's real output must reduce to
#: exactly these strings) — the labels are engine constants, the excerpt is
#: answer text.
FABRICATION_FLAG_LABELS = (
    "fabricated-quantity for absent metric",
    "invented-identifier for absent metric",
)

# The ``: '`` boundary is what ``fabrication_flags`` puts between a label and
# its answer excerpt; ``(server error frames: `` is the transport row's.
_FLAG_EXCERPT_SEPARATOR = ": '"
_ERROR_FRAMES_MARKER = " (server error frames: "
_ERROR_EVENTS_PREFIX = "error event(s):"


_LEAK_CLASSES = ("fact", "semantic")
_TREATMENT_PHASES = ("query-time", "map-time")


def _validate_golden_markings(question: dict, require: bool = False) -> None:
    """Validate the additive improvable/leak_class/treatment_phase markings.

    Legacy goldens carrying NONE of the three keys load unchanged (the
    unmodified demo golden set predates the markings; `improvable` defaults
    true). A golden that opts into the marking scheme (any key present) and
    is improvable (explicit or defaulted) MUST carry a valid `leak_class`
    AND `treatment_phase` (H.5 leak policy / L4-5 denominator scoping) —
    the markings live IN the golden file, never in a side channel.

    When the FILE opts in via top-level `require_markings: true`, the
    lenient unmarked-question escape closes: every question must carry
    valid markings (explicit `improvable: false`, or improvable with both
    `leak_class` and `treatment_phase`). Without that, a pilot-env golden that
    omitted all three keys would silently load as improvable with no leak
    class and no treatment phase — escaping the H.5 leak policy and the
    L4-5 denominator scoping (a silent corruption path in a
    pre-registered experiment).
    """
    if not any(key in question for key in
               ("improvable", "leak_class", "treatment_phase")):
        if require:
            raise ValueError(
                f"golden {question.get('id')}: file sets require_markings "
                "but question carries none of improvable/leak_class/"
                "treatment_phase")
        return
    qid = question.get("id")
    improvable = question.get("improvable", True)
    if not isinstance(improvable, bool):
        raise ValueError(f"golden {qid}: improvable must be a bool")
    leak = question.get("leak_class")
    if leak is not None and leak not in _LEAK_CLASSES:
        raise ValueError(f"golden {qid}: leak_class must be one of "
                         f"{_LEAK_CLASSES}, got {leak!r}")
    phase = question.get("treatment_phase")
    if phase is not None and phase not in _TREATMENT_PHASES:
        raise ValueError(f"golden {qid}: treatment_phase must be one of "
                         f"{_TREATMENT_PHASES}, got {phase!r}")
    if improvable and leak is None:
        raise ValueError(f"golden {qid}: improvable golden requires leak_class")
    if improvable and phase is None:
        raise ValueError(
            f"golden {qid}: improvable golden requires treatment_phase")


_UTF8_BOM = b"\xef\xbb\xbf"


def _golden_text(data: bytes) -> str:
    """Decode golden bytes as strict UTF-8 (no BOM) or raise honestly.

    A BOM is REJECTED, never stripped, and a decode failure is never
    "repaired" by a lenient codec: either would mean the bytes that were
    validated are not the bytes the sha binds, so the digest shown to the
    steward would not match the digest of their file. Both messages carry the
    sha of the bytes AS GIVEN so the steward can compare it against
    ``golden_file_sha`` on their own copy.
    """
    sha = golden_bytes_sha(data)
    if data.startswith(_UTF8_BOM):
        raise ValueError(
            "golden set bytes start with a UTF-8 BOM — re-save the file as "
            "UTF-8 without a BOM (the BOM is not stripped: that would change "
            f"the bytes the sha binds); sha256[:12] as given: {sha}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            "golden set bytes are not valid UTF-8 "
            f"({exc.reason} at byte {exc.start}) — re-save the file as UTF-8; "
            f"sha256[:12] as given: {sha}") from exc


def parse_golden_set(payload) -> list[dict]:
    """Validate a golden set and return its questions — the ONE validator home.

    ``payload`` is the raw golden JSON ``bytes`` (an upload), its decoded
    ``str``, or the already-parsed ``dict``. ``load_golden_set`` is a thin
    read-and-delegate wrapper over this function, so a set that parses
    server-side runs on the CLI BY CONSTRUCTION rather than by convention
    (ADR-0061 D2 / the golden file schema stays the sole interchange format).
    Raises ``ValueError`` (``json.JSONDecodeError`` for unparseable JSON —
    itself a ``ValueError``) when malformed.
    """
    data = payload
    if isinstance(data, (bytes, bytearray)):
        data = _golden_text(bytes(data))
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, dict) or data.get("version") != GOLDEN_SET_VERSION:
        raise ValueError(f"golden set version must be {GOLDEN_SET_VERSION}")
    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("golden set has no questions")
    require_markings = data.get("require_markings", False)
    if not isinstance(require_markings, bool):
        raise ValueError("require_markings must be a bool when present")
    seen_ids = set()
    for question in questions:
        qid = question.get("id")
        if not qid or not question.get("text") or not isinstance(
                question.get("expect"), dict):
            raise ValueError(f"golden question missing id/text/expect: {question!r:.120}")
        if qid in seen_ids:
            raise ValueError(f"duplicate golden question id: {qid}")
        seen_ids.add(qid)
        _validate_golden_markings(question, require=require_markings)
        _env_equals_gate(question)
        _connected_gate(question)
        _story_fields_gate(question)
    return questions


def load_golden_set(path: str) -> list[dict]:
    """Load and validate the golden-set file; raise ValueError when malformed.

    THIN wrapper: read the file's bytes → ``parse_golden_set``. The file is
    read as BYTES (not decoded text) so the CLI lane and an uploaded run walk
    the identical decode + validate path — see ``parse_golden_set``. Filesystem
    errors (missing/unreadable path) still surface as ``OSError``.
    """
    return parse_golden_set(Path(path).read_bytes())


def _env_equals_gate(question: dict) -> "dict | None":
    """The question's ``skip_unless_env_equals`` gate, shape-checked: absent
    ⇒ ``None``; anything but a non-empty dict of str -> str RAISES (the ONE
    shape rule, shared by :func:`parse_golden_set` and :func:`should_skip`)."""
    gate = question.get("skip_unless_env_equals")
    if gate is None:
        return None
    if not isinstance(gate, dict) or not gate or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in gate.items()):
        raise ValueError(f"golden {question.get('id')}: skip_unless_env_equals "
                         "must be a non-empty dict of str -> str")
    return gate


def _env_equals_skip(question: dict, env) -> "str | None":
    """``skip_unless_env_equals: {VAR: value}`` — skip unless every
    ``env[VAR]`` equals ``value`` (trimmed, case-insensitive). A malformed
    gate RAISES rather than silently running or skipping the question."""
    gate = _env_equals_gate(question)
    if gate is None:
        return None
    for var, value in gate.items():
        if env.get(var, "").strip().lower() != value.strip().lower():
            return f"skip_unless_env_equals {var} != {value}"
    return None


def _connected_gate(question: dict) -> "list[str] | None":
    """The question's ``requires_connected_types`` gate, shape-checked:
    absent ⇒ ``None``; anything but a non-empty list of non-empty str
    RAISES (shared by :func:`parse_golden_set` and :func:`should_skip`)."""
    gate = question.get("requires_connected_types")
    if gate is None:
        return None
    if not isinstance(gate, list) or not gate or not all(
            isinstance(t, str) and t.strip() for t in gate):
        raise ValueError(f"golden {question.get('id')}: requires_connected_types "
                         "must be a non-empty list of system types")
    return gate


def _connected_skip(question: dict, connected_types) -> "str | None":
    """``requires_connected_types: [TYPE, ...]`` — skip while the deployment
    has no ONBOARDED system of a listed type (case-insensitive). A workshop
    account starts with its CMMSs registered but not onboarded (onboarding
    them is a module), so a maintenance golden asked before that step is a
    precondition miss, not an answer failure. ``connected_types`` ``None``
    means the state is unknown: the question RUNS (never a silent skip)."""
    gate = _connected_gate(question)
    if gate is None or connected_types is None:
        return None
    have = {str(t).strip().lower() for t in connected_types}
    missing = [t for t in gate if t.strip().lower() not in have]
    if missing:
        return f"requires_connected_types {','.join(missing)} not onboarded"
    return None


#: ``story_fields`` names: lower-case identifiers, each written ``{name}`` in
#: the question text.
_STORY_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
#: Any identifier-shaped ``{name}`` in a question text, declared or not
#: (other braces, such as a JSON example, are left alone).
_STORY_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _story_placeholders(text: str) -> set:
    """The names of the identifier-shaped ``{name}`` tokens in *text*."""
    return set(_STORY_PLACEHOLDER_RE.findall(text or ""))


def _story_fields_gate(question: dict) -> "dict | None":
    """The question's ``story_fields`` map, shape-checked: absent ⇒ ``None``;
    anything but a non-empty dict of name -> dotted answer-key path whose
    names are exactly the ``{name}`` tokens in the text (each one written, and
    no undeclared one left to reach the model bare) RAISES (shared by
    :func:`parse_golden_set` and :func:`fill_story_fields`)."""
    fields = question.get("story_fields")
    if fields is None:
        return None
    text = question.get("text") or ""
    if not isinstance(fields, dict) or not fields or not all(
            isinstance(name, str) and _STORY_FIELD_NAME_RE.match(name)
            and isinstance(path, str) and path.strip()
            for name, path in fields.items()) or _story_placeholders(text) != set(fields):
        raise ValueError(f"golden {question.get('id')}: story_fields must be a "
                         "non-empty dict of name -> answer-key path, naming every "
                         "{name} in the text and no other")
    return fields


def _story_value(story, path: str) -> "str | None":
    """The non-blank string at dotted *path* in *story*, else ``None``."""
    node = story
    for key in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, str) and node.strip() else None


def fill_story_fields(question: dict, read_story) -> "tuple[dict, str | None]":
    """Fill ``story_fields`` into the question text from the deployed story's
    answer key, so a golden can name this account's run the way a workshop
    page has the attendee paste it, without a run id in the file.

    ``read_story()`` returns the answer key (the body ``GET
    /api/admin/workshop/story`` serves) or ``None``; it is called only when
    the question declares fields. Returns ``(question with the text filled,
    None)``, or ``(question, skip reason)`` when there is no key, it lacks a
    value, or a ``{name}`` is still in the text after filling (one a story
    value brought in), so the model is never asked a bare ``{name}``. The text
    is filled in one pass, so a value is never itself filled. The input is
    never mutated."""
    fields = _story_fields_gate(question)
    if fields is None:
        return question, None
    story = read_story()
    if not isinstance(story, dict):
        return question, "story_fields: no story answer key in this lane or deployment"
    values = {}
    for name, path in fields.items():
        values[name] = _story_value(story, path)
        if values[name] is None:
            return question, f"story_fields {name}: {path} not in the story answer key"
    text = _STORY_PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], question["text"])
    left = sorted(_story_placeholders(text))
    if left:
        return question, f"story_fields: {{{left[0]}}} still in the question after filling"
    return {**question, "text": text}, None


def should_skip(question: dict, environ=None, no_seed_fixtures: bool = False,
                connected_types=None):
    """Return a skip reason string, or None when the question must run.

    ``connected_types`` is the set of system types the deployment has
    ONBOARDED (``None`` = unknown; gated questions then run)."""
    env = os.environ if environ is None else environ
    gate = question.get("skip_unless_env")
    if gate and env.get(gate, "").strip().lower() not in ("1", "true", "yes", "on"):
        return f"skip_unless_env {gate} not set"
    equals_reason = _env_equals_skip(question, env)
    if equals_reason:
        return equals_reason
    if question.get("requires_seed") and no_seed_fixtures:
        return "requires_seed skipped (--no-seed-fixtures)"
    return _connected_skip(question, connected_types)


def _expect_min_failures(expect: dict, summary: dict) -> list[str]:
    """Deterministic minimum-grounding gates over the intelligence_summary."""
    failures = []
    s = summary or {}
    checks = (
        ("systems_min", "system_queries", s.get("system_queries", 0)),
        ("neptune_min", "neptune_queries", s.get("neptune_queries", 0)),
        ("rules_applied_min", "rules_applied", len(s.get("rules_applied") or [])),
    )
    for key, label, actual in checks:
        needed = int(expect.get(key, 0) or 0)
        if needed < 1:
            continue
        if not summary:
            failures.append(f"no intelligence_summary (needed {label} >= {needed})")
        elif actual < needed:
            failures.append(f"{label} {actual} < {needed}")
    return failures


def _expect_answer_failures(expect: dict, answer: str,
                            question_text: str = "") -> list[str]:
    """Deterministic regex gates over the final answer text.

    The CONTENT gates score the answer BODY only (`answer_body` — generated
    follow-up suggestions are questions, not claims, and scoring them let a
    chip both satisfy a required pattern and trip a forbidden one).

    ``injection_guard`` deliberately keeps scanning the FULL text: the canary
    is an exfiltration signal rather than a content claim, so echoing it into
    a suggestion chip is still a leak. Narrowing that one to the body would
    open an escape hatch in a security gate.
    """
    failures = []
    full_text = answer or ""
    text = answer_body(answer)
    for pattern in expect.get("answer_must_match") or []:
        if not re.search(pattern, text):
            failures.append(f"answer missing required pattern {pattern!r}")
    for pattern in expect.get("answer_must_not_match") or []:
        if re.search(pattern, text):
            failures.append(f"answer matched forbidden pattern {pattern!r}")
    if expect.get("absence_honesty"):
        if expect.get("must_not_quantify"):
            # D2: the fabrication detector gates; phrase markers demote to the
            # informational honesty block (never gating).
            failures.extend(fabrication_flags(expect, question_text, text))
        elif not any(re.search(marker, text)
                     for marker in ABSENCE_HONESTY_MARKERS):
            # Legacy semantics: goldens without must_not_quantify unchanged.
            failures.append("answer lacks an honest-absence marker")
    if expect.get("injection_guard") and re.search(INJECTION_CANARY_MARKER,
                                                   full_text):
        failures.append("answer echoed the injection canary")
    return failures


def score_question(expect: dict, summary: dict, answer: str, errors: list,
                   question_text: str = ""):
    """PURE deterministic scorer -> (verdict, failures). No LLM is consulted."""
    failures = []
    if errors:
        failures.append(f"error event(s): {errors[:2]}")
    failures.extend(_expect_min_failures(expect, summary))
    failures.extend(_expect_answer_failures(expect, answer, question_text))
    return ("pass" if not failures else "fail"), failures


def _skipped_row(question: dict, reason: str) -> dict:
    """Report row for a skipped golden (never gates)."""
    return {
        "id": question["id"], "session_id": None, "verdict": "skipped",
        "skip_reason": reason, "failures": [], "systems_queried": 0,
        "neptune_queries": 0, "elapsed_seconds": None, "answer_excerpt": "",
        "honesty": {"absence_markers_matched": [], "fabrication_flags": []},
        "metrics": None,
    }


def _scored_row(question: dict, session_id: str, result: dict,
                strict_failures: list[str] | None = None) -> dict:
    """Score one consumed stream into the pinned per-question report row.

    ``strict_failures`` is the injection seam for the CLI's smoke-dependent
    --strict layer (scripts/eval_answers.py computes it there and passes the
    precomputed list) — this module never imports the smoke classifiers.
    """
    if not result.get("done"):
        return _transport_failure_row(question, session_id, result)
    summary = result.get("summary") or {}
    answer = result.get("answer", "")
    verdict, failures = score_question(
        question["expect"], summary, answer, result.get("errors", []),
        question_text=question.get("text", ""))
    if strict_failures:
        failures = failures + list(strict_failures)
        verdict = "fail"
    return {
        "id": question["id"], "session_id": session_id, "verdict": verdict,
        "failures": failures,
        "systems_queried": summary.get("system_queries", 0),
        "neptune_queries": summary.get("neptune_queries", 0),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "answer_excerpt": (answer or "").strip()[:ANSWER_EXCERPT_CHARS],
        "honesty": _honesty_block(
            question["expect"], question.get("text", ""), answer),
        "metrics": _with_identifier_miss_calls(
            _metrics_block(result.get("summary")), result),
    }


#: Opening of the fenced ADR-0048 IDENTIFIER-MISS block a system agent emits
#: (```…\n  IDENTIFIER-MISS:). Matched against the public tool_result
#: summaries only — never the answer, never tool payloads.
_IDENTIFIER_MISS_FENCE_RE = re.compile(r"```[^\n]*\n[ \t]*IDENTIFIER-MISS:")


def _with_identifier_miss_calls(metrics: dict | None, result: dict) -> dict | None:
    """Add the COMPUTED ``identifier_miss_calls`` count to a metrics block.

    Counts tool results whose public summary opens a fenced IDENTIFIER-MISS
    block — a delegation the target could not resolve and the orchestrator
    had to recover from. Measured-only: ABSENT when the stream carried no
    ``tool_summaries`` (e.g. a server-side run that does not collect them),
    never a guessed zero.
    """
    summaries = result.get("tool_summaries")
    if metrics is None or not isinstance(summaries, list):
        return metrics
    return {**metrics, "identifier_miss_calls": sum(
        1 for text in summaries
        if isinstance(text, str) and _IDENTIFIER_MISS_FENCE_RE.search(text))}


def _transport_failure_row(question: dict, session_id: str,
                           result: dict) -> dict:
    """Report row for a stream that STARTED but never delivered its `done`
    frame (client-side socket timeout mid-stream, server error frame, or a
    dropped connection).

    Such repeats are NOT scored as CONTENT failures ("no intelligence_summary"
    plus one "missing required pattern" per expected pattern) — that would
    misattribute an undelivered answer as a wrong answer and poison majority
    verdicts and A/B diffs. The verdict
    stays "fail" (a turn the user never got IS a failure) but the failure is
    named as transport, exactly once, and the row carries a machine-readable
    marker so rollups can separate content regressions from delivery problems.
    """
    errors = result.get("errors") or []
    detail = f" (server error frames: {errors[:2]})" if errors else ""
    return {
        "id": question["id"], "session_id": session_id, "verdict": "fail",
        "failures": ["incomplete stream — no done frame (transport, not "
                     "content)" + detail],
        "transport_failure": True,
        "systems_queried": 0, "neptune_queries": 0,
        "elapsed_seconds": result.get("elapsed_seconds"),
        "answer_excerpt": (result.get("answer") or "").strip()[:ANSWER_EXCERPT_CHARS],
        "honesty": {"absence_markers_matched": [], "fabrication_flags": []},
        "metrics": None,
    }


def _redacted_error_detail(count: "int | None") -> str:
    """``n=`` is the MEASURED error-event count, or ``?`` when the caller did
    not measure it — never an invented number."""
    shown = "?" if count is None else int(count)
    return f"error event(s) (redacted, n={shown})"


def _redacted_flag(flag: str) -> str:
    """A fabrication flag reduced to its LABEL (the excerpt is answer text).

    A flag whose label is not one of ``FABRICATION_FLAG_LABELS`` is returned
    UNCHANGED — a steward-authored pattern that happens to contain the same
    boundary must never be truncated.
    """
    if not isinstance(flag, str):
        return flag
    label, sep, _ = flag.partition(_FLAG_EXCERPT_SEPARATOR)
    return label if sep and label in FABRICATION_FLAG_LABELS else flag


def _redacted_failure(entry: str, error_count: "int | None") -> str:
    """One failure string with every answer/stream-derived fragment removed.

    Deterministic failure strings survive VERBATIM — they are the steward's
    own authored expectations and the diagnostic value of the whole lane
    (ADR-0064 D5). Only two shapes carry text the model or the transport
    produced: a fabrication flag's excerpt (dropped to its label) and a
    stream-``errors`` detail (replaced by a measured count).
    """
    if not isinstance(entry, str):
        return entry
    flagged = _redacted_flag(entry)
    if flagged != entry:
        return flagged
    if entry.startswith(_ERROR_EVENTS_PREFIX):
        return _redacted_error_detail(error_count)
    head, sep, _ = entry.partition(_ERROR_FRAMES_MARKER)
    if sep:
        return f"{head} ({_redacted_error_detail(error_count)})"
    return entry


def redact_answer_text(row: dict, *, error_count: "int | None" = None) -> dict:
    """Return a NEW row carrying NO answer-derived text (ADR-0064 D5).

    Applied to an UPLOADED golden set's rows BEFORE they enter the report, so
    the application's own table never becomes a durable store of customer
    operational prose: ``answer_excerpt`` empties, fabrication flags drop to
    their labels, and a stream-``errors`` detail becomes a count. Verdicts,
    ids, session ids, metrics, skip reasons and the deterministic
    (steward-authored) failure strings are untouched.

    Pure and idempotent: the input row is never mutated and a second pass is
    a no-op. ``error_count`` is the MEASURED number of error events for this
    question; without it the replacement says ``n=?`` rather than inventing
    one.
    """
    if not isinstance(row, dict):
        return row
    redacted = dict(row)
    if "answer_excerpt" in redacted:
        redacted["answer_excerpt"] = ""
    if FAILED_ANSWER_KEY in redacted:
        redacted[FAILED_ANSWER_KEY] = ""
    honesty = redacted.get("honesty")
    if isinstance(honesty, dict):
        flags: list = []
        for flag in honesty.get("fabrication_flags") or []:
            label = _redacted_flag(flag)
            if label not in flags:
                flags.append(label)
        redacted["honesty"] = {**honesty, "fabrication_flags": flags}
    failures = redacted.get("failures")
    if isinstance(failures, list):
        redacted["failures"] = [_redacted_failure(entry, error_count)
                                for entry in failures]
    redacted[ANSWER_TEXT_REDACTED_KEY] = True
    return redacted


def _suite_totals(rows: list[dict]) -> dict:
    """Suite-level totals: pass/fail/skipped counts + latency stats."""
    latencies = [row["elapsed_seconds"] for row in rows
                 if isinstance(row.get("elapsed_seconds"), (int, float))]
    verdicts = [row["verdict"] for row in rows]
    return {
        "pass": verdicts.count("pass"),
        "fail": verdicts.count("fail"),
        "skipped": verdicts.count("skipped"),
        "total": len(rows),
        "avg_latency_seconds": (round(sum(latencies) / len(latencies), 2)
                                if latencies else None),
        "max_latency_seconds": max(latencies) if latencies else None,
    }


def _campaign_totals(rows: list[dict]) -> dict:
    """Content, delivery and validity totals for repeated campaigns only."""
    campaign_rows = [row for row in rows if "measurement_valid" in row]
    content = [row.get("content_verdict") for row in campaign_rows]
    return {
        "content_pass": content.count("pass"),
        "content_fail": content.count("fail"),
        "content_unavailable": content.count(None),
        "measurement_valid": all(
            row.get("measurement_valid") is True for row in campaign_rows),
        "delivered_count": sum(
            int(row.get("delivered_count") or 0) for row in campaign_rows),
        "transport_failure_count": sum(
            int(row.get("transport_failure_count") or 0)
            for row in campaign_rows),
        "invalid_question_count": sum(
            row.get("measurement_valid") is False for row in campaign_rows),
    }


def _report_suite(rows: list[dict], repeats: int) -> dict:
    """Legacy suite totals plus campaign-only measurement dimensions."""
    suite = _suite_totals(rows)
    if repeats > 1:
        suite.update(_campaign_totals(rows))
    return suite


def build_eval_report(*, base_url: str, namespace, golden_set: str,
                      strict: bool, rows: list[dict], run_path: str,
                      variant: str = "",
                      golden_set_sha: str | None = None,
                      golden_set_source: str | None = None,
                      repeats: int = 1, no_memory: bool = False,
                      memory_suppression: str = "marker",
                      unsticks: list | None = None,
                      trend: dict | None = None) -> dict:
    """Build the credential-free machine report.

    ``run_path`` is required because CLI/server latencies are incomparable.
    Feature keys remain additive: the repeats=1 CLI shape stays unchanged;
    server reports stamp their explicit packaged/uploaded source.
    """
    if not isinstance(run_path, str) or not run_path:
        raise ValueError("run_path is required and must be a non-empty str "
                         '(e.g. "cli" or "server")')
    report = {
        "base_url": base_url,
        "run_id": namespace.run_id,
        "session_prefix": namespace.prefix,
        "golden_set": golden_set,
        "strict": strict,
        "run_path": run_path,
        "variant": variant or None,
        "golden_set_sha": golden_set_sha,
        "suite": _report_suite(rows, repeats),
        "questions": rows,
    }
    if golden_set_source:
        report["golden_set_source"] = golden_set_source
    if repeats > 1:
        report["repeats"] = repeats
    if no_memory:
        report["no_memory"] = True
        report["memory_suppression"] = memory_suppression
        report["unsticks"] = unsticks or []
    if trend is not None:
        report["trend"] = trend
    return report


def suite_exit_code(rows: list[dict]) -> int:
    """Exit 1 for content failure OR an invalid completed measurement.

    Skips never gate.  A repeated campaign may retain a passing content
    majority while transport loss or state instability invalidates the
    measurement; that is nonzero without rewriting content as failed.
    """
    invalid = any(row.get("measurement_valid") is False for row in rows)
    content_failed = any(row.get("verdict") == "fail" for row in rows)
    return 1 if invalid or content_failed else 0


# ── Late-arrival golden-authoring guard (ADR-0063 track 4) ──────────────────
#
# MEASURED LIVE: three goldens scored FAIL while the orchestrator was right.
# Their expectations were exact point counts derived by hand on one date, but
# the source table receives LATE-ARRIVING rows for windows that have already
# closed (proven by comparing each row's ingestion timestamp to its event
# timestamp: rows for a closed week kept landing days later). A point-value
# assertion over a still-accruing window is structurally unstable — it drifts
# forever, and every drift reads as a model regression that is not one.
#
# What this section does about it, and what it deliberately does NOT do:
#
# * It is ADVICE TO A HUMAN AUTHOR. ADR-0062 D5 forbids a GENERATED
#   ``answer_must_match`` and there is no code path here that emits one:
#   these helpers return plain data that rides the NON-GATING ``hints`` block
#   (scoring reads ``question["expect"]`` only, so a hint can neither pass nor
#   fail a question). No ``decided_by``, no steward identity, no verdict.
# * The accrual verdict is TRI-STATE and evidence-only: "accruing",
#   "settled", or ABSENT (``None``/no keys) when the inputs cannot tell.
#   Absent is never rendered as ``False`` — an unmeasured window must not read
#   as a settled one (the honest-data rule, ADR-0045).
# * Everything is PURE: stdlib arithmetic and string formatting, no I/O, no
#   clock, no randomness. Two calls over the same inputs are byte-identical.

#: Keys carrying MEASURED evidence about whether a window is still accruing.
#: ``late_arrival_rows`` is the direct measurement (rows whose ingestion
#: instant is later than the window's close); the timestamp pair is the
#: fallback for callers that measured the extremes instead of the count.
ACCRUAL_EVIDENCE_KEYS = ("late_arrival_rows", "window_closed_at",
                         "latest_ingested_at")

#: The observed magnitude a numeric expectation would be written against.
OBSERVED_COUNT_KEY = "observed_count"

ACCRUAL_ACCRUING = "accruing"
ACCRUAL_SETTLED = "settled"
STABILITY_EXACT = "exact"
STABILITY_TOLERANT = "stability-tolerant"

_TOLERANT_NOTE = (
    "this window appears to still be accruing (rows landed after it closed) "
    "— prefer a magnitude band or named categorical codes over an exact count")
_SETTLED_NOTE = (
    "no late-arriving rows observed for this window — an exact count is "
    "defensible; re-check the ingestion-vs-event timestamps if the source "
    "starts backfilling")

#: Leaf keys of :func:`suggest_count_band`'s band block.
COUNT_BAND_KEYS = frozenset({"label", "low", "high", "unit", "paste"})

#: EVERY leaf key these helpers can emit. Exported so a wiring layer extends
#: its recursive leaf allowlist by ONE import instead of re-listing the keys
#: (the ADR-0062 draft allowlist is the load-bearing export boundary).
STABILITY_ADVICE_KEYS = frozenset({
    "accrual_signal", "expectation_stability", "stability_note",
    "suggested_band"}) | COUNT_BAND_KEYS


def _measured_count(value) -> "int | None":
    """A measured non-negative count, or ``None`` when not measured.

    Bools are not counts and floats are not row counts — both read as
    unmeasured rather than being coerced into a fabricated integer.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _parsed_instant(value):
    """An ISO-8601 instant, or ``None`` when the input cannot be parsed."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def accrual_signal(observed: dict) -> "str | None":
    """Is the measured window still accruing? Evidence-only, tri-state.

    Returns :data:`ACCRUAL_ACCRUING`, :data:`ACCRUAL_SETTLED`, or ``None``
    when the inputs cannot tell — ``None`` is ABSENCE, not "settled". A
    naive/aware timestamp mix is also "cannot tell": comparing them would be
    a guess about the missing offset.
    """
    observed = observed if isinstance(observed, dict) else {}
    late = _measured_count(observed.get("late_arrival_rows"))
    if late is not None:
        return ACCRUAL_ACCRUING if late > 0 else ACCRUAL_SETTLED
    closed = _parsed_instant(observed.get("window_closed_at"))
    latest = _parsed_instant(observed.get("latest_ingested_at"))
    if closed is None or latest is None:
        return None
    try:
        return ACCRUAL_ACCRUING if latest > closed else ACCRUAL_SETTLED
    except TypeError:
        return None


def _band_unit(value: int) -> "tuple[str, int]":
    """The magnitude suffix and its divisor for a band bound."""
    for suffix, divisor in (("B", 1_000_000_000), ("M", 1_000_000),
                            ("K", 1_000)):
        if value >= divisor:
            return suffix, divisor
    return "", 1


def _scaled_label(value: int, unit: str, divisor: int) -> str:
    """``5200000 -> "5.2M"``; at most one decimal, trailing zeros stripped."""
    if not unit:
        return str(value)
    text = f"{value / divisor:.1f}".rstrip("0").rstrip(".")
    return f"{text}{unit}"


def suggest_count_band(count) -> "dict | None":
    """A pasteable magnitude band for one observed count. Pure, no I/O.

    The band is the two-significant-digit bucket the count falls in, so the
    label and the bounds always agree: ``5222180`` -> ``5.2M``, half-open
    ``[5200000, 5300000)``. ``None`` for an unmeasured input — this helper
    suggests, it never invents a magnitude.
    """
    value = _measured_count(count)
    if value is None:
        return None
    step = max(1, 10 ** (len(str(value)) - 2))
    low = (value // step) * step
    high = low + step
    unit, divisor = _band_unit(low)
    label = _scaled_label(low, unit, divisor)
    return {
        "label": label,
        "low": low,
        "high": high,
        "unit": unit,
        "paste": f"{label}-scale band: at least {low:,} and under {high:,}",
    }


def stability_advice(observed: dict) -> dict:
    """NON-GATING advice for the HUMAN writing a numeric expectation.

    ``observed`` is a counter block (the ADR-0062 ``hints`` shape, optionally
    carrying the :data:`ACCRUAL_EVIDENCE_KEYS` and
    :data:`OBSERVED_COUNT_KEY`). Returns ``{}`` when the evidence cannot tell
    whether the window is accruing — silence, never a fabricated verdict and
    never a generated assertion.
    """
    observed = observed if isinstance(observed, dict) else {}
    signal = accrual_signal(observed)
    if signal is None:
        return {}
    if signal == ACCRUAL_SETTLED:
        return {"accrual_signal": signal,
                "expectation_stability": STABILITY_EXACT,
                "stability_note": _SETTLED_NOTE}
    advice = {"accrual_signal": signal,
              "expectation_stability": STABILITY_TOLERANT,
              "stability_note": _TOLERANT_NOTE}
    band = suggest_count_band(observed.get(OBSERVED_COUNT_KEY))
    if band is not None:
        advice["suggested_band"] = band
    return advice


def with_stability_advice(hints: dict, observed: "dict | None" = None) -> dict:
    """A NEW hints block carrying the advice; inputs are never mutated.

    ``observed`` defaults to ``hints`` (the counters and the evidence usually
    arrive together). Adds nothing when the evidence cannot tell.
    """
    base = dict(hints) if isinstance(hints, dict) else {}
    source = observed if isinstance(observed, dict) else base
    merged = dict(base)
    merged.update(stability_advice(source))
    return merged
