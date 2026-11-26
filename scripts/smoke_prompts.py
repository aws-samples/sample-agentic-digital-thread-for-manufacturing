#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
smoke_prompts.py — assert the demo actually answers, grounded in real systems.

Sources demo prompts from the deployed app's registry (GET /api/prompts), runs
each through the real agent path (POST /api/ask, SSE), parses the
intelligence_summary, and asserts:

  per-prompt   : the stream completed (done), no error event, a non-empty
                 answer, total_tool_calls >= 1, and system_queries >= 1
                 (the agent grounded the answer in a real system, not the LLM's
                 imagination). ONE computed carve-out (ADR-0054 amendment):
                 an absence-honesty answer (system_queries == 0) passes iff it
                 is graph-grounded (neptune_queries >= 1) AND the shared eval
                 fabrication detector — scoped by word-boundary regex terms
                 derived from the prompt text — finds zero flags. Carve-out
                 passes are annotated distinctly (grounding ==
                 "absence-honesty"), never silent; the carve-out fails closed
                 and phrase markers stay informational only.
  suite-level  : across the run, at least one prompt reached system_queries >= 2
                 and at least one reached neptune_queries >= 1 — proving the
                 cross-system, graph-guided thread works end to end.

Per-prompt grounding is deterministic; the cross-system proof is asserted at the
suite level so a single sparse answer can't flake the gate.

Beyond grounding, the harness also folds in a lightweight regression scorecard
that catches failures a "grounded-looking" run can otherwise mask:

  error taxonomy   : classify_error_labels() maps word-boundary regex patterns
                     found in tool_result
                     summaries + stream errors to canonical labels — including
                     RuntimeUnavailable (the query never reached a live runtime).
  answer signals     : detect_fabrication_signals() remains the compatibility
                       API for all historical phrases, while strict scoring
                       distinguishes memory references ("prior session",
                       "previously confirmed", ...) from genuine outage
                       fallbacks ("currently unavailable", ...).
  latency            : per-question elapsed_seconds (+ suite avg/max).

Each invocation creates one UUID-backed run namespace and sends prompt N with
session ID ``<prefix>-<run-id>-<N>``. This makes every prompt fresh by default,
including across repeated harness invocations, and a fresh turn has no
earlier conversation (and no AgentCore Memory: write_memory=false), so
--strict grades no memory wording on a fresh answer. Memory phrases and
apparent prior-conversation claims ("as we discussed", "earlier in this
conversation") are listed in the report for the rehearsal reviewer to read.
Pass an explicit --run-id (or
SMOKE_RUN_ID) only for intentional deterministic follow-up testing; that mode
keeps memory on, and --strict fails a memory reference there only when the
current turn has system_queries < 1. Outage signals always fail strict
scoring.

These populate a machine-readable JSON report (--report / SMOKE_REPORT),
including run_id and each session_id for provenance. With --strict
(SMOKE_STRICT), RuntimeUnavailable, outage fallback, and a follow-up turn's
ungrounded memory reference extend the gate (a fresh turn's memory and
prior-conversation matches are listed for review only). Without --strict the exit-code contract
remains the grounding gate, including system_queries >= 1 on every prompt —
except the computed absence-honesty carve-out above, which applies in both
modes.

Usage:
  python3 scripts/smoke_prompts.py --base-url https://<dist>.cloudfront.net \
      --password "$ADT_PASSWORD" [--count N] [--timeout 200] \
      [--report /tmp/smoke_report.json] [--strict] [--quality-strict] \
      [--run-id intentional-followup] [--session-prefix smoke] \
      [--prompts-file <golden-set.json>] [--category <name>]
--count defaults to ALL sourced prompts (0/None = all); pass an explicit N to limit.
--prompts-file sources questions from a golden-set-shaped JSON file (the same
file eval_answers.py --golden-set reads) instead of /api/prompts; --category
filters the sourced prompts by their category, case-insensitively. Both are
run-time GROUPING — the deployed prompt library is never modified. --count
applies after the category filter. The suite-level cross-system proof still
applies to whatever subset runs.
Omit --run-id for fresh isolated sessions. Reusing an explicit run ID and prefix
reuses deterministic session IDs for intentional follow-up testing.
Env fallbacks: ADT_BASE_URL, ADT_USERNAME (default ADT), ADT_PASSWORD,
SMOKE_REPORT, SMOKE_STRICT, SMOKE_QUALITY_STRICT, SMOKE_RUN_ID,
SMOKE_SESSION_PREFIX, SMOKE_PROMPTS_FILE, SMOKE_CATEGORY.
Exit 0 = all assertions passed.
"""
import argparse
from dataclasses import dataclass
import http.client
import http.cookiejar
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid

MIN_ANSWER_CHARS = 40

# One transient-network retry per prompt (fresh session id, short backoff).
# A single dropped TLS record once aborted a whole 14-prompt run at prompt 2
# — the backend was healthy; only the client socket died. Deliberately NARROW:
# assertion failures, HTTP error statuses carried in URLError, and stream
# 'error' events are NEVER retried (the gate must not mask real regressions).
TRANSIENT_NETWORK_ERRORS = (
    ssl.SSLError,
    http.client.HTTPException,  # IncompleteRead, chunked-decode failures, ...
    ConnectionError,            # reset / aborted / refused
    TimeoutError,
)
PROMPT_RETRY_BACKOFF_SECONDS = 3.0

# Lane session freshness: the app forwards the signed-in user's access token
# as the AgentCore gateway bearer (ADR-0049), and that token expires ~60 min
# after login. A long run that crosses the boundary hits a mid-turn gateway
# 403 window (observed 2026-09-24: a ~30 s burst every 60 min ate 10 golden
# runs). Both lanes therefore re-login whenever the recorded session is older
# than this, BEFORE every ask — 45 min leaves 15 min of headroom over the
# 60-min token lifetime for the longest observed turn.
SESSION_MAX_AGE_SECONDS = 45 * 60


@dataclass(frozen=True)
class SessionNamespace:
    """Session IDs for one smoke invocation."""

    prefix: str
    run_id: str
    followup_mode: bool

    def session_id(self, prompt_index: int) -> str:
        return f"{self.prefix}-{self.run_id}-{prompt_index}"


def create_session_namespace(
    run_id: str | None = None,
    prefix: str = "smoke",
) -> SessionNamespace:
    """Create one run namespace; an explicit run ID opts into session reuse."""
    explicit_run_id = (run_id or "").strip()
    session_prefix = (prefix or "").strip() or "smoke"
    return SessionNamespace(
        prefix=session_prefix,
        run_id=explicit_run_id or uuid.uuid4().hex,
        followup_mode=bool(explicit_run_id),
    )


def build_report(
    *,
    base_url: str,
    namespace: SessionNamespace,
    totals: dict,
    rows: list,
) -> dict:
    """Build the credential-free machine report with session provenance."""
    questions = [{**row, "run_id": namespace.run_id} for row in rows]
    return {
        "base_url": base_url,
        "run_id": namespace.run_id,
        "session_prefix": namespace.prefix,
        "followup_mode": namespace.followup_mode,
        "totals": totals,
        "questions": questions,
    }

# ── Pure classifiers (offline-testable; no network / no I/O) ─────────────────
#
# Error taxonomy: ordered (compiled regex, label) pairs. Matching is
# case-insensitive WORD-BOUNDARY regex against tool_result summaries and
# stream error text — never bare substrings. CamelCase data VALUES lowercase
# to a single token with no interior boundary (e.g. ``OperatingTimeout`` →
# ``operatingtimeout``), so they cannot classify, while genuine errors
# surface as standalone tokens or the explicitly enumerated AWS compounds
# (``…Exception``/``…Exceeded`` suffix variants). Plural/derived forms of
# genuine error tokens ("3 timeouts", "read timed out", "times/timing out",
# "QueryErrors", "ResourceNotFoundExceptions") classify like their singular
# forms — plural compounds ("OperatingTimeouts") still never do. Each pattern
# maps to exactly one canonical label, so genuine app errors (QueryError,
# COLUMN_NOT_FOUND, ...) are never conflated with the RuntimeUnavailable class (the data plane /
# agent runtime was not reachable, so the query never actually ran and a
# "grounded" verdict would be a false pass). Pinned audit table: ADR-0054 D1.


def _compile_signal(phrase: str):
    """Compile a phrase into a word-boundary regex: \\b edges, \\s+ gaps.

    Each word also tolerates an optional plural ``s`` (D1 plural/derived-form
    coverage: "session errors" / "endpoints not found" classify like their
    singular forms). The \\b edges still exclude camelCase compound data
    values, which lowercase to a single boundary-free token.
    """
    words = [re.escape(word) + ("s?" if not word.endswith("s") else "")
             for word in phrase.split()]
    return re.compile(r"\b" + r"\s+".join(words) + r"\b", re.IGNORECASE)


ERROR_PATTERNS = [
    (re.compile(r"\berrorprocessingresults\b", re.IGNORECASE), "ErrorProcessingResults"),
    (re.compile(r"\bqueryerrors?\b", re.IGNORECASE), "QueryError"),
    # Strands' own reply to a call naming a tool that does not exist is
    # "Unknown tool: <name>" (strands/tools/executors/_executor.py); the colon
    # keeps a row VALUE such as "unknown tool awaiting calibration" in a
    # successful result from counting as a failed call.
    (re.compile(r"\bunknown tool:\s*\S|\bunknown_?tools?\b", re.IGNORECASE), "UnknownTool"),
    (re.compile(r"\bnoschemafound\b", re.IGNORECASE), "NoSchemaFound"),
    (re.compile(r"\btable_not_found\b", re.IGNORECASE), "TABLE_NOT_FOUND"),
    (re.compile(r"\bcolumn_not_found\b", re.IGNORECASE), "COLUMN_NOT_FOUND"),
    (re.compile(r"\bbytesscannedlimit(?:exceeded)?\b", re.IGNORECASE), "BytesScannedLimit"),
    (re.compile(r"\bresourcenotfound(?:exceptions?)?\b", re.IGNORECASE), "ResourceNotFound"),
    (re.compile(r"\baccessdenied(?:exceptions?)?\b|\baccess\s+denied\b", re.IGNORECASE), "AccessDenied"),
    (re.compile(r"\bvalidationexceptions?\b", re.IGNORECASE), "ValidationException"),
    (re.compile(r"\btim(?:e[ds]?|ing)[\s_-]?out\b|\btimeout(?:s|errors?|exceptions?)?\b",
                re.IGNORECASE), "TIMEOUT"),
    # RuntimeUnavailable — the request never reached a live runtime/data plane.
    (_compile_signal("no endpoint or agent found"), "RuntimeUnavailable"),
    (_compile_signal("endpoint not found"), "RuntimeUnavailable"),
    (_compile_signal("endpoint routing error"), "RuntimeUnavailable"),
    (_compile_signal("not responding"), "RuntimeUnavailable"),
    (_compile_signal("unreachable"), "RuntimeUnavailable"),
    (_compile_signal("agent unavailable"), "RuntimeUnavailable"),
    (_compile_signal("session error"), "RuntimeUnavailable"),
]

# Memory-reference phrases, kept distinct from genuine outage/fallback
# language. They are reported on every turn. On a fresh turn neither these nor
# prior-conversation matches (_PRIOR_CONVERSATION_CLAIM) fail strict scoring;
# both are listed for review. See strict_failure_reasons.
MEMORY_REFERENCE_SIGNALS = [
    "prior session",
    "previously confirmed",
    "already on record",
    "already know",
    "from your prior",
    "confirmed data points",
]

# A fresh turn has no earlier conversation, so an answer that claims one may
# have made that history up. This detector LISTS such claims for the rehearsal
# reviewer (row prior_conversation_claims); it never fails a strict turn,
# because a pattern can't reliably separate a claim from a denial or a phrase
# about data. It covers an earlier-time word before a conversation noun
# ("earlier in this conversation", "your prior session", "our last chat"),
# "as we discussed", and "you told me / said / mentioned" with earlier, before
# or previously next to the verb or ending the clause. "Prior" on a data noun
# ("your prior work order") is not listed. Keep it small: it is a reading aid.
_CONVERSATION_DETERMINER = r"(?:your|our|the|this|that|a|an|my)"
_YOU_TOLD = r"you\s+(?:told\s+(?:me|us)|said|mentioned)"
# "turn" is left out: on a plant it is also a production cycle ("the last turn
# of line 4").
_PRIOR_CONVERSATION_CLAIM = re.compile(
    rf"\b(?:(?:from|in|during)\s+{_CONVERSATION_DETERMINER}\s+)?"
    rf"(?:earlier|previous|prior|last)\s+(?:(?:in|during|of)\s+)?"
    rf"(?:{_CONVERSATION_DETERMINER}\s+)?"
    r"(?:session|conversation|chat|discussion)s?\b"
    r"|\bas\s+we(?:['\u2019]ve|\s+have)?\s+discussed\b"
    rf"|\b{_YOU_TOLD}\s+(?:earlier|before|previously)\b"
    rf"|\b{_YOU_TOLD}\b[^.!?;\n]{{0,80}}?\b(?:earlier|before|previously)"
    r"(?=\s*(?:[,.;:!?]|$))"
    rf"|\b(?:earlier|previously),?\s+{_YOU_TOLD}\b",
    re.IGNORECASE)
# "you mentioned earlier in your question": the user's words in the current
# message, which a fresh turn can see.
_CURRENT_MESSAGE_REFERENT = re.compile(
    r"\s+in\s+(?:your|this|the)\s+(?:question|prompt|message|request)\b",
    re.IGNORECASE)

OUTAGE_FALLBACK_SIGNALS = [
    "temporarily unavailable",
    "not responding",
    "unreachable",
    "currently unavailable",
    "endpoint error",
    "currently unreachable",
]

# Compatibility contract: callers of detect_fabrication_signals() continue to
# receive every historically detected signal in the original stable order.
FALLBACK_SIGNALS = MEMORY_REFERENCE_SIGNALS + OUTAGE_FALLBACK_SIGNALS

# Signal matching uses the same D1 word-boundary regex transform as the error
# taxonomy; detectors keep REPORTING the original phrase strings so
# strict-failure text is unchanged.
_MEMORY_REFERENCE_PATTERNS = [(_compile_signal(p), p) for p in MEMORY_REFERENCE_SIGNALS]
_OUTAGE_FALLBACK_PATTERNS = [(_compile_signal(p), p) for p in OUTAGE_FALLBACK_SIGNALS]
_FALLBACK_PATTERNS = _MEMORY_REFERENCE_PATTERNS + _OUTAGE_FALLBACK_PATTERNS


def classify_error_labels(texts) -> list:
    """Pure: canonical error labels found across `texts`.

    `texts` is any iterable of strings — typically the tool_result summaries
    plus stream error strings for one prompt. Matching is case-insensitive
    word-boundary regex (D1 audit table); labels are deduped per text and
    across the whole input, and returned in `ERROR_PATTERNS` order for stable
    output.
    """
    labels = []
    for t in texts or []:
        if not t:
            continue
        low = str(t).lower()
        seen = set()
        for pattern, label in ERROR_PATTERNS:
            if label in seen:
                continue
            if pattern.search(low):
                seen.add(label)
                if label not in labels:
                    labels.append(label)
    return labels


class ScoringDependencyError(RuntimeError):
    """The shared scoring extractor could not be loaded.

    Deliberately NOT an ImportError subclass: the absence-honesty carve-out
    fails closed on ImportError, and absorbing this there would turn a missing
    extractor into one soft carve-out reason instead of stopping the lane.
    """


def _answer_body(answer) -> str:
    """The answer with the reserved ---FOLLOWUPS--- suggestion block removed.

    Delegates to the ONE extractor (``eval_core.answer_body``, re-exported by
    the sibling eval_answers module — imported lazily for the same circular
    reason as ``_eval_module``); a second marker regex here would be exactly
    the pattern drift the single-source detector pins guard against.

    Everything after the marker is generated follow-up QUESTION text: chips
    restate outage wording ("Retry when the endpoint is available?"), memory
    wording ("Compare with what we previously confirmed?") and scoped
    quantities, all of which failed honest answers, and their length padded
    the answer-length gate.

    An unavailable extractor RAISES. Returning the full text is not fail-closed
    for every gate this feeds: chips only ADD characters, so a stub body plus
    one long chip clears MIN_ANSWER_CHARS. Under dependency/version skew the
    lane must exit nonzero, never report a pass it cannot justify.
    """
    text = str(answer or "")
    try:
        extract = _eval_module().answer_body
    except (ImportError, AttributeError) as exc:
        raise ScoringDependencyError(
            "eval_core.answer_body (the shared ---FOLLOWUPS--- extractor) is "
            "unavailable; refusing to score the full answer text") from exc
    return extract(text)


def _detect_signals(answer, patterns) -> list:
    body = _answer_body(answer)
    if not body:
        return []
    low = body.lower()
    return [phrase for pattern, phrase in patterns if pattern.search(low)]


# A memory phrase the answer NEGATES disclaims memory rather than citing it:
# "proposed rather than previously confirmed" (live strict-smoke false
# positive, 2026-10-03) or "not already on record". Only a negator directly in
# front of the phrase counts, so "As previously confirmed" still signals.
_NEGATED_MEMORY_PREFIX = re.compile(
    r"\b(?:not|never|nor|rather\s+than|instead\s+of)\s+$", re.IGNORECASE)


def _cites_memory(pattern, low: str) -> bool:
    for match in pattern.finditer(low):
        before = low[max(0, match.start() - 24):match.start()]
        if not _NEGATED_MEMORY_PREFIX.search(before):
            return True
    return False


def detect_memory_reference_signals(answer) -> list:
    """Pure: memory-reference phrases the final answer cites (not negates)."""
    body = _answer_body(answer)
    if not body:
        return []
    low = body.lower()
    return [phrase for pattern, phrase in _MEMORY_REFERENCE_PATTERNS
            if _cites_memory(pattern, low)]


def detect_prior_conversation_claims(answer) -> list:
    """Pure: phrases that look like claims to know an earlier conversation
    (not directly negated, not references to the current message).

    Separate from :func:`detect_memory_reference_signals`. Listed on each row
    for the rehearsal reviewer, who reads denials ("I do not have your prior
    session") for what they are; never a strict failure."""
    body = _answer_body(answer)
    if not body:
        return []
    low = body.lower()
    return [" ".join(m.group(0).split())
            for m in _PRIOR_CONVERSATION_CLAIM.finditer(low)
            if not _NEGATED_MEMORY_PREFIX.search(low[max(0, m.start() - 24):m.start()])
            and not _CURRENT_MESSAGE_REFERENT.match(low, m.end())]


def detect_outage_fallback_signals(answer) -> list:
    """Pure: outage/fallback phrases present in the final answer."""
    return _detect_signals(answer, _OUTAGE_FALLBACK_PATTERNS)


def detect_fabrication_signals(answer) -> list:
    """Pure compatibility API: all historical fallback signals in `answer`.

    The combined result includes both memory-reference phrases and genuine
    outage/fallback phrases. New scoring code should use the category-specific
    detectors because an intentional follow-up may legitimately cite memory
    after querying a system in the current turn.
    """
    return _detect_signals(answer, _FALLBACK_PATTERNS)


# ── Absence-honesty carve-out (ADR-0054 amendment 2026-08-03) ────────────────
#
# The always-on grounding gate (system_queries >= 1) has ONE computed
# carve-out: an absence answer (system_queries == 0) passes iff it is
# graph-grounded (neptune_queries >= 1) AND the shared ADR-0054 D2
# fabrication detector (scripts/eval_answers.py::fabrication_flags — single
# source, reused via lazy import because eval_answers imports THIS module at
# load time) finds zero flags. Smoke prompts carry no golden `expect` block,
# so the must_not_quantify scope terms are DERIVED FROM THE PROMPT TEXT as
# word-boundary regexes (D1: never bare substrings). The verdict is COMPUTED;
# ABSENCE_HONESTY_MARKERS are recorded informationally and never decide. The
# carve-out FAILS CLOSED when no scope terms are derivable or the detector is
# unavailable, and passes are annotated distinctly — never silent.

_SCOPE_TERM_MIN_LEN = 4
# Generic query/framing words ONLY — never domain literals. These keep the
# derived scope on the ASKED metric words, so honest system counts and
# other-system citations stay out of scope (the D2 false-positive class).
_SCOPE_STOPWORDS = frozenset({
    # interrogatives / auxiliaries / framing
    "what", "which", "where", "when", "whose", "does", "have", "been",
    "show", "list", "give", "tell", "find", "please", "many", "much",
    "there", "their", "them", "they", "your", "this", "that", "these",
    "those", "with", "from", "about", "into", "over", "under", "between",
    "across", "would", "could", "should", "will", "each", "every", "some",
    "most", "also", "than", "then",
    # generic engine/query words — never the asked metric
    "system", "systems", "data", "connected", "onboarded", "status",
    "current", "currently", "today", "overall", "summary", "detail",
    "details",
})
_SCOPE_TOKEN_PATTERN = re.compile(r"[a-z][a-z-]{3,}")
# C1 pre-mask (pinned): an honest count of connected/onboarded systems or
# sources inside a scoped sentence is NOT fabrication. Both word orders are
# covered — attributive ("exactly 2 onboarded systems") and copular ("Only
# **2 systems** are onboarded", live pilot env 2026-08-04) — and token joints
# tolerate markdown emphasis (*/_), which the live answer carried. The number
# must sit directly in a connected/onboarded systems|sources construction;
# bare numbers and metric counts ("14 open work orders are pending") never
# match. Masked on a COPY of the answer inside the carve-out ONLY — the
# shared eval detector is untouched.
# SECURITY: the joint is INTRA-LINE ONLY. A newline-
# tolerant joint let a fabricated scoped value at end-of-line glue onto an
# honest "Systems are connected" heading on the next line and mask the whole
# span (proven exploit). Spaces/tabs and markdown emphasis only — never \n.
_HSC_JOINT = r"[ \t*_]*[ \t][ \t*_]*"  # intra-line joint, markdown-emphasis-tolerant
_HONEST_SYSTEM_COUNT_PATTERN = re.compile(
    r"\b\d+" + _HSC_JOINT + r"(?:connected|onboarded)"
    + _HSC_JOINT + r"(?:systems?|sources?)\b"
    r"|\b\d+" + _HSC_JOINT + r"(?:systems?|sources?)"
    + _HSC_JOINT + r"(?:are|is|were|was)\s+(?:currently\s+)?(?:connected|onboarded)\b",
    re.IGNORECASE)


def derive_prompt_scope_terms(prompt_text: str) -> list:
    """Pure: word-boundary regex scope terms derived from the prompt text.

    The computable analogue of a golden's `must_not_quantify` — smoke prompts
    carry no expect block. Tokens are lowercased, stopword-filtered, lightly
    plural-stemmed, and emitted as ``\\b<stem>s?\\b`` regexes (D1 invariant:
    never bare substrings). Empty result ⇒ the carve-out fails closed.
    """
    terms, seen = [], set()
    for token in _SCOPE_TOKEN_PATTERN.findall((prompt_text or "").lower()):
        if token in _SCOPE_STOPWORDS:
            continue
        stem = token[:-1] if token.endswith("s") and not token.endswith("ss") else token
        if len(stem) < _SCOPE_TERM_MIN_LEN or stem in _SCOPE_STOPWORDS:
            continue
        if stem in seen:
            continue
        seen.add(stem)
        terms.append(rf"\b{re.escape(stem)}s?\b")
    return terms


def _eval_module():
    """Lazily import the sibling eval_answers module (single-source detector).

    Lazy because eval_answers imports THIS module at load time — a top-level
    import would be circular. Mirrors eval_answers._import_smoke for both the
    package context (unit tests: repo root on sys.path) and the script
    context (python3 scripts/smoke_prompts.py).
    """
    try:
        from scripts import eval_answers
        return eval_answers
    except ImportError:
        scripts_dir = os.path.dirname(os.path.abspath(__file__))
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import eval_answers
        return eval_answers


def absence_honesty_carveout(prompt_text, answer, summary) -> dict:
    """Computed absence-honesty verdict for one prompt (pure given imports).

    Engages ONLY when a summary exists and system_queries == 0. Passing
    requires graph grounding (neptune_queries >= 1) AND zero flags from the
    shared eval fabrication detector scoped by prompt-derived terms. Fails
    closed on no derivable scope terms or an unavailable detector.
    ``absence_markers`` is INFORMATIONAL only — never part of the verdict.
    """
    engaged = bool(summary) and summary.get("system_queries", 0) == 0
    detail = {
        "engaged": engaged, "passed": False, "reasons": [],
        "fabrication_flags": [], "absence_markers": [], "scope_term_count": 0,
    }
    if not engaged:
        return detail
    reasons = []
    if summary.get("neptune_queries", 0) < 1:
        reasons.append("neptune_queries < 1 (absence answer not graph-grounded)")
    terms = derive_prompt_scope_terms(prompt_text)
    detail["scope_term_count"] = len(terms)
    if not terms:
        reasons.append("no scope terms derivable from prompt (fail-closed)")
    try:
        eval_mod = _eval_module()
    except ImportError:
        reasons.append("fabrication detector unavailable (fail-closed)")
    else:
        # Informational honesty signal only — the verdict below never reads it.
        detail["absence_markers"] = eval_mod.absence_markers_matched(answer)
        if terms:
            masked_answer = _HONEST_SYSTEM_COUNT_PATTERN.sub(
                " ", _answer_body(answer))
            flags = eval_mod.fabrication_flags(
                {"must_not_quantify": terms}, prompt_text, masked_answer)
            detail["fabrication_flags"] = flags
            if flags:
                reasons.append(
                    f"fabrication detector flagged the absence answer: {flags[:2]}")
    detail["reasons"] = reasons
    detail["passed"] = not reasons
    return detail


def _grounding(system_queries: int, carveout, graph_only: bool = False,
               neptune_queries: int = 0) -> str:
    """Row grounding class: system-grounded | graph-grounded | absence-honesty | ungrounded.

    ``graph-grounded`` is reserved for prompts the golden DECLARES graph-only
    (load_prompts_file → ``graph_only``) that actually hit the graph; it is
    never inferred from the answer.
    """
    if system_queries >= 1:
        return "system-grounded"
    if graph_only and neptune_queries >= 1:
        return "graph-grounded"
    if carveout and carveout.get("engaged") and carveout.get("passed"):
        return "absence-honesty"
    return "ungrounded"


_QUALITY_REJECTION_CODES = (
    "INVALID_TYPED_ENVELOPE",
    "NO_CURRENT_INVOCATION_EVIDENCE",
    "VALUE_NOT_IN_SOURCE",
    "EVIDENCE_REFERENCE_MISSING",
    "CURRENT_EVIDENCE_REQUIRED",
    "CLAIMED_SOURCE_TIME_CONFLICT",
    "COMPARISON_TIMESTAMPS_REQUIRED",
    "GRAPH_RELATIONSHIP_PROOF_REQUIRED",
    "CAUSAL_CLAIMS_UNSUPPORTED",
    "ARITHMETIC_INPUT_UNSUPPORTED",
    "RANKING_INPUT_UNSUPPORTED",
)
_CAUSAL_CLAIM_RE = re.compile(
    r"\b(caused?\b|due to|led to|drives?|resulted in)\b", re.IGNORECASE,
)


# A-lite integrity marker (C3): non-enforcing runtimes append a trailing
# {"response_integrity": {"mode": ...}} event. Reasons carrying this prefix are
# INFORMATIONAL — "enforcement not enabled on this env" is not a quality
# failure — and are excluded from quality-strict failures and the
# quality_failure_questions total.
INTEGRITY_NOT_ENABLED_PREFIX = "integrity enforcement not enabled"
_NON_ENFORCING_MODES = ("report-only", "annotate")


def _enforcement_failure_reasons(reasons: list) -> list:
    """Drop the informational not-enabled reason; keep genuine failures."""
    return [r for r in reasons if not str(r).startswith(INTEGRITY_NOT_ENABLED_PREFIX)]


def quality_failure_reasons(metadata: dict | None) -> list:
    """Pure additive check of server-issued deterministic integrity metadata.

    This verifies gate metadata consistency only; it does not externally audit
    source values. Value audits are a separate fixture-driven concern.

    Mode-aware (C3): metadata carrying ``mode`` in a non-enforcing value with
    no verdict fields means integrity enforcement is not enabled on the env —
    one informational reason, not the strict field cascade. ``mode`` enforce
    or absent (old runtime) keeps the strict checks byte-identical.
    """
    if not isinstance(metadata, dict):
        return ["missing deterministic response-integrity metadata"]
    if metadata.get("mode") in _NON_ENFORCING_MODES and "verdict" not in metadata:
        return [f"{INTEGRITY_NOT_ENABLED_PREFIX} (mode={metadata['mode']})"]
    failures = []
    if metadata.get("verdict") != "accepted":
        failures.append(f"integrity verdict {metadata.get('verdict', 'missing')}")
    if metadata.get("current_turn_evidence") is not True:
        failures.append("no validated current-turn evidence")
    if metadata.get("completeness") != "complete":
        failures.append(f"response completeness {metadata.get('completeness', 'missing')}")
    if not isinstance(metadata.get("current_intent"), bool):
        failures.append("missing deterministic current-intent classification")
    reason_codes = metadata.get("reason_codes")
    if not isinstance(reason_codes, list):
        failures.append("missing deterministic reason codes")
    else:
        failures.extend(f"integrity rejection {code}" for code in reason_codes if code)
    if not isinstance(metadata.get("claim_count"), int) or metadata.get("claim_count", 0) < 1:
        failures.append("no deterministically validated claims")
    return list(dict.fromkeys(failures))


def _env_flag(name: str, environ=None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def strict_failure_reasons(
    *,
    err_labels,
    answer,
    system_queries: int,
    followup_mode: bool,
) -> list:
    """Pure strict-mode extensions beyond the always-on grounding checks.

    Runtime outages and outage/fallback answer language always fail.

    A fresh turn (the default) has no earlier conversation and no AgentCore
    Memory, so memory wording is not graded on it. An answer that seems to
    claim an earlier conversation ("as we discussed", "earlier in this
    conversation": detect_prior_conversation_claims) is listed on the row for
    the rehearsal reviewer to read, not failed: a pattern can't reliably tell
    such a claim from a denial ("I do not have access to your prior session")
    or a phrase about data. Live false positive that started this, a
    rehearsal run on 2026-10-18: an honest gap answer asked the user for "a change you already
    know about" and failed as a memory reference.

    The opt-in follow-up mode (explicit --run-id) continues a conversation, so
    it may name its own earlier turns; any memory phrase fails there only when
    the turn made no system query.
    """
    failures = []
    if "RuntimeUnavailable" in (err_labels or []):
        failures.append("RuntimeUnavailable")

    outage_signals = detect_outage_fallback_signals(answer)
    if outage_signals:
        failures.append(f"outage-fallback {outage_signals}")

    if followup_mode:
        memory_signals = detect_memory_reference_signals(answer)
        if memory_signals and system_queries < 1:
            failures.append("unexpected memory-reference without a current-turn "
                            f"system query {memory_signals}")
    return failures


def _add_grouping_args(ap: argparse.ArgumentParser, env) -> None:
    """Run-time prompt GROUPING flags — selection only, never library edits."""
    ap.add_argument(
        "--prompts-file",
        default=env.get("SMOKE_PROMPTS_FILE", ""),
        help="source prompts from a local golden-set-shaped JSON file "
             "(version 1, questions[] with id/text) instead of the deployed "
             "/api/prompts registry (SMOKE_PROMPTS_FILE). The library is "
             "untouched — this only changes what THIS run asks. The "
             "suite-level cross-system proof still applies to the subset.",
    )
    ap.add_argument(
        "--category",
        default=env.get("SMOKE_CATEGORY", ""),
        help="run only prompts whose category equals this value, "
             "case-insensitively (SMOKE_CATEGORY). Applies to both registry- "
             "and file-sourced prompts; grouping never deletes anything from "
             "the prompt library.",
    )


def build_parser(environ=None) -> argparse.ArgumentParser:
    """Build the CLI parser, accepting an environment mapping for pure tests."""
    env = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(
        description="Demo prompt smoke test with fresh sessions by default",
    )
    ap.add_argument("--base-url", default=env.get("ADT_BASE_URL", ""))
    ap.add_argument("--username", default=env.get("ADT_USERNAME", "ADT"))
    ap.add_argument("--password", default=env.get("ADT_PASSWORD", ""))
    ap.add_argument("--count", type=int, default=None,
                    help="how many prompts to run (default: ALL sourced prompts; "
                         "0 also means all)")
    _add_grouping_args(ap, env)
    ap.add_argument("--timeout", type=int, default=200, help="per-prompt timeout (s)")
    ap.add_argument("--report", default=env.get("SMOKE_REPORT", ""),
                    help="path for the machine-readable JSON report "
                         "(default /tmp/smoke_prompts_report.json)")
    ap.add_argument(
        "--run-id",
        default=env.get("SMOKE_RUN_ID", ""),
        help="explicit reusable run namespace (SMOKE_RUN_ID); omitted creates "
             "a cryptographically random fresh namespace",
    )
    ap.add_argument(
        "--session-prefix",
        default=env.get("SMOKE_SESSION_PREFIX", "smoke"),
        help="session ID prefix (SMOKE_SESSION_PREFIX; default: smoke)",
    )
    ap.add_argument("--no-failed-answers", action="store_true",
                    help="do not keep the full answer text on FAILED rows (default keeps it)")
    ap.add_argument("--strict", action="store_true", default=_env_flag("SMOKE_STRICT", env),
                    help="also fail on RuntimeUnavailable or outage fallback; "
                         "with an explicit --run-id (memory on), also on a "
                         "memory reference without a current-turn system "
                         "query (fresh turns list memory and prior-"
                         "conversation phrases for review, never fail on them)")
    ap.add_argument(
        "--quality-strict", action="store_true",
        default=_env_flag("SMOKE_QUALITY_STRICT", env),
        help="add deterministic response-integrity metadata scoring: verdict, "
             "reason codes, current-turn evidence, intent, and completeness; "
             "does not externally audit values",
    )
    return ap



def _consume_ask_sse(lines) -> dict:
    """Parse only the public scalar/list fields used by smoke scoring."""
    answer, errors, summaries, names = [], [], [], []
    summary, integrity, done, starts, event = {}, {}, False, 0, None
    for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\n")
        if line.startswith("event:"):
            event = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        try:
            data = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if event == "token":
            answer.append(data.get("text", ""))
        elif event == "tool_start":
            starts += 1
        elif event == "tool_result":
            if data.get("summary"):
                summaries.append(data["summary"])
            if data.get("tool") and data["tool"] not in names:
                names.append(data["tool"])
        elif event == "metadata" and "intelligence_summary" in data:
            summary = data["intelligence_summary"]
        elif event == "metadata" and "response_integrity" in data:
            integrity = data["response_integrity"]
        elif event == "error":
            errors.append(data.get("text", "error"))
        elif event == "done":
            done = True
    return {
        "answer": "".join(answer), "summary": summary, "integrity": integrity,
        "errors": errors, "done": done, "tool_summaries": summaries,
        "tool_names": names, "tool_starts": starts,
    }


def ask_body(question: str, session_id: str, followup_mode: bool = False) -> dict:
    """The /api/ask request body for one smoke turn — PURE.

    AgentCore Memory Facts are ACTOR-scoped (``/facts/{actorId}/``), not
    session-scoped, and every smoke turn shares the ``smoke`` actor. Measured
    live: a fresh-session strict run surfaced "seen in prior sessions" —
    Facts written by EARLIER smoke invocations, which the lane's fresh-by-default
    promise never intended. Fresh runs therefore send the server's disable-only
    ``write_memory: false`` so the lane never feeds its own future runs; the
    deliberate ``--run-id`` follow-up mode keeps memory writes on, because
    remembering the prior turn is what that mode tests.
    """
    body = {"question": question, "session_id": session_id, "user_id": "smoke"}
    if not followup_mode:
        body["write_memory"] = False
    return body


class App:
    # Memory posture of every turn this client sends (see ask_body). Class-level
    # default so test doubles that bypass __init__ stay memory-inert.
    followup_mode: bool = False
    # Re-login state (class-level defaults so test doubles that bypass
    # __init__ never re-login): credentials + monotonic login time recorded
    # by login(), consumed by ensure_fresh_session().
    _login_username: str = ""
    _login_password: str = ""
    _login_at: float | None = None

    def __init__(self, base_url: str, followup_mode: bool = False):
        self.base = base_url.rstrip("/")
        self.followup_mode = followup_mode
        cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    def call(self, method: str, path: str, body=None, timeout: int = 60):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        with self.opener.open(req, timeout=timeout) as r:
            raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}

    def login(self, username: str, password: str) -> dict:
        """POST /api/auth/login and record credentials + time for re-login.

        The ONE login path for both lanes (smoke + eval import this). Network
        errors propagate unchanged — exactly like the previous inline
        app.call login. On success the credentials and a monotonic timestamp
        are recorded so ensure_fresh_session() can re-login later in the run.
        """
        result = self.call(
            "POST", "/api/auth/login", {"email": username, "password": password})
        if result.get("status") == "ok":
            self._login_username = username
            self._login_password = password
            self._login_at = time.monotonic()
        return result

    def ensure_fresh_session(self, max_age_seconds: float = SESSION_MAX_AGE_SECONDS) -> bool:
        """Re-login when the recorded session is older than max_age_seconds.

        Called before EVERY ask so a long run never forwards a gateway bearer
        that expires mid-turn (the observed 60-min-boundary 403 burst,
        2026-09-24). No-op (False) when login() was never called (test
        doubles) or the session is still fresh. Failures surface exactly like
        the initial login: network errors propagate unchanged, and a non-ok
        response prints the same ERROR line and raises.
        """
        if self._login_at is None:
            return False
        if time.monotonic() - self._login_at < max_age_seconds:
            return False
        result = self.login(self._login_username, self._login_password)
        if result.get("status") != "ok":
            print("ERROR: login failed")
            raise RuntimeError("re-login failed (session refresh)")
        return True

    def ask_stream(self, question: str, session_id: str, timeout: int) -> dict:
        """POST /api/ask and consume its bounded SSE event stream.

        Re-logs-in first when the recorded session is stale (see
        ensure_fresh_session) — HERE, so retries, unsticks and warmups are
        covered too, not just the lanes' primary asks.
        """
        self.ensure_fresh_session()
        body = ask_body(question, session_id, self.followup_mode)
        request = urllib.request.Request(
            self.base + "/api/ask", data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        start = time.monotonic()
        with self.opener.open(request, timeout=timeout) as response:
            result = _consume_ask_sse(response)
        result["elapsed_seconds"] = round(time.monotonic() - start, 2)
        return result


def fetch_prompts(app: App, count=None) -> list:
    """Short cross-system registry prompts (skip the long report templates).

    ``count`` limits how many are run; ``None`` or ``0`` (the default) runs ALL
    sourced prompts — a partial run can silently skip prompts and even flip the
    suite-level cross-system proof (a 4-prompt run failed the neptune proof
    while the full 6-prompt run passed).
    """
    items = app.call("GET", "/api/prompts")
    if isinstance(items, dict):
        items = items.get("prompts", items.get("items", []))
    short = [
        {"title": e.get("title", ""), "text": e["text"], "category": e.get("category", "")}
        for e in items
        if e.get("text") and len(e["text"]) <= 280
    ]
    return short[:count] if count else short


def load_prompts_file(path: str, count=None) -> list:
    """Source prompts from a local golden-set-shaped JSON file.

    Accepts the same file shape as scripts/eval_answers.py --golden-set
    (``{"version": 1, "questions": [{"id", "text", ...}]}``) so ONE file can
    drive both lanes without drift — the smoke lane reads only id/text/
    category — plus ONE declared expectation, ``graph_only``: a golden whose
    ``expect`` says ``systems_min == 0`` and ``neptune_min >= 1`` is answered
    from the graph BY DESIGN (schema discovery, naming, join keys), so the
    grounding gate grades it on ``neptune_queries`` instead of demanding a
    system query (see assess). Every other eval-only key (markings, reference
    grounding, answer patterns) is ignored. This changes what a run ASKS,
    never what the deployed prompt library contains. No 280-char cap: a file selection is an explicit
    operator choice, unlike the registry skim which skips report templates.

    Raises ValueError on a malformed file (wrong version, no questions, or a
    question missing id/text) — sourcing errors must fail loudly, not run an
    empty suite.
    """
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError(f"prompts file {path}: version must be 1")
    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError(f"prompts file {path}: no questions")
    prompts = []
    for question in questions:
        if not question.get("id") or not question.get("text"):
            raise ValueError(
                f"prompts file {path}: question missing id/text: "
                f"{question!r:.120}")
        prompts.append({
            "title": question["id"],
            "text": question["text"],
            "category": question.get("category", ""),
            "graph_only": _declared_graph_only(question.get("expect")),
        })
    return prompts[:count] if count else prompts


def _declared_graph_only(expect) -> bool:
    """True iff the golden DECLARES a graph-answered question.

    Requires BOTH ``systems_min == 0`` and ``neptune_min >= 1`` — a missing
    expect block, or one that merely omits systems_min, keeps the default
    system-grounding gate (fail-closed: the declaration must be explicit).
    """
    if not isinstance(expect, dict):
        return False
    return expect.get("systems_min") == 0 and (expect.get("neptune_min") or 0) >= 1


def filter_prompts_by_category(prompts: list, category: str) -> list:
    """Keep only prompts whose category equals ``category`` (case-insensitive).

    An empty/blank ``category`` disables filtering (returns the list
    unchanged). Grouping is a run-time selection — the prompt library and the
    sourced list are never mutated.
    """
    wanted = (category or "").strip().lower()
    if not wanted:
        return prompts
    return [p for p in prompts
            if str(p.get("category", "")).strip().lower() == wanted]


def assess(title: str, res: dict, carveout=None, graph_only: bool = False) -> list:
    """Return list of per-prompt failure strings (empty == pass).

    ``graph_only`` (declared by the golden — see load_prompts_file) swaps the
    system-grounding requirement for ``neptune_queries >= 1``; the eval lane
    already grades these goldens that way (systems_min == 0), so without it
    the two lanes disagreed on the same file.

    ``carveout`` (optional) is the computed absence-honesty verdict from
    absence_honesty_carveout(); the 2-arg legacy form behaves identically to
    passing None. A PASSED carve-out suppresses only the system_queries
    failure — the pass is annotated in the row/console, never silent. A
    FAILED engaged carve-out appends its computed reasons alongside the
    grounding failure.
    """
    f = []
    s = res["summary"]
    if not res["done"]:
        f.append("stream did not complete (no done event)")
    if res["errors"]:
        f.append(f"error event(s): {res['errors'][:2]}")
    body_len = len(_answer_body(res["answer"]).strip())
    if body_len < MIN_ANSWER_CHARS:
        f.append(f"answer too short ({body_len} chars)")
    if not s:
        f.append("no intelligence_summary")
    else:
        if s.get("total_tool_calls", 0) < 1:
            f.append("total_tool_calls < 1")
        if s.get("system_queries", 0) < 1:
            if graph_only:
                if s.get("neptune_queries", 0) < 1:
                    f.append("neptune_queries < 1 (declared graph-only prompt "
                             "did not query the graph)")
            elif carveout and carveout.get("engaged") and carveout.get("passed"):
                pass  # absence-honesty carve-out — annotated distinctly upstream
            else:
                f.append("system_queries < 1 (answer not grounded in a system)")
                if carveout and carveout.get("engaged"):
                    f.extend(
                        f"absence-honesty carve-out not satisfied: {reason}"
                        for reason in carveout.get("reasons", []))
    return f


def _request_error_row(title: str, session_id: str, exc: Exception) -> dict:
    label = type(exc).__name__
    return {
        "title": title, "session_id": session_id, "done": False,
        "elapsed_seconds": None, "total_tool_calls": 0, "system_queries": 0,
        "neptune_queries": 0, "answer_len": 0, "systems": [],
        "err_labels": classify_error_labels([label]), "memory_reference_signals": [],
        "prior_conversation_claims": [],
        "outage_fallback_signals": [], "fallback_signals": [],
        "grounding": "ungrounded", "absence_carveout": None,
        "response_integrity": {}, "quality_failure_reasons": [], "errors": [label],
    }


def _result_row(title: str, session_id: str, result: dict, carveout=None,
                graph_only: bool = False) -> dict:
    summary = result["summary"]
    errors = classify_error_labels(result["tool_summaries"] + result["errors"])
    system_queries = summary.get("system_queries", 0)
    return {
        "title": title, "session_id": session_id, "done": result["done"],
        "elapsed_seconds": result["elapsed_seconds"],
        "total_tool_calls": summary.get("total_tool_calls", 0) or result["tool_starts"],
        "system_queries": system_queries,
        "neptune_queries": summary.get("neptune_queries", 0),
        "answer_len": len(_answer_body(result["answer"]).strip()),
        "systems": result["tool_names"],
        "err_labels": errors,
        "memory_reference_signals": detect_memory_reference_signals(result["answer"]),
        "prior_conversation_claims": detect_prior_conversation_claims(result["answer"]),
        "outage_fallback_signals": detect_outage_fallback_signals(result["answer"]),
        "fallback_signals": detect_fabrication_signals(result["answer"]),
        "grounding": _grounding(system_queries, carveout, graph_only,
                                summary.get("neptune_queries", 0)),
        "absence_carveout": carveout if carveout and carveout.get("engaged") else None,
        "response_integrity": result.get("integrity", {}),
        "quality_failure_reasons": quality_failure_reasons(result.get("integrity")),
        "errors": result["errors"],
    }


def _print_prompt_metrics(row: dict) -> None:
    print(f"    tools={row['total_tool_calls']} system_q={row['system_queries']} "
          f"neptune_q={row['neptune_queries']} answer={row['answer_len']}ch "
          f"elapsed={row['elapsed_seconds']}s grounding={row['grounding']}", flush=True)
    labels = (("errors", row["err_labels"]),
              ("memory references", row["memory_reference_signals"]),
              ("prior-conversation claims (for review)",
               row.get("prior_conversation_claims") or []),
              ("outage fallback signals", row["outage_fallback_signals"]))
    for label, values in labels:
        if values:
            print(f"    {label}: {', '.join(values)}", flush=True)
    carveout = row.get("absence_carveout")
    if carveout and carveout.get("absence_markers"):
        print(f"    absence-honesty markers (informational): "
              f"{len(carveout['absence_markers'])} matched", flush=True)


def _extended_prompt_failures(args, namespace, result: dict, row: dict) -> list[str]:
    failures = []
    if args.strict:
        failures.extend(f"{problem} (strict)" for problem in strict_failure_reasons(
            err_labels=row["err_labels"], answer=result["answer"],
            system_queries=row["system_queries"], followup_mode=namespace.followup_mode,
        ))
    if args.quality_strict:
        # Informational not-enabled reasons are a PASS with a note (C3): fail
        # on missing verdict fields only when mode is enforce/absent.
        failures.extend(f"{problem} (quality-strict)"
                        for problem in _enforcement_failure_reasons(
                            row["quality_failure_reasons"]))
    return failures


def _is_transient_network_error(exc: Exception) -> bool:
    """True only for dropped-socket/TLS/timeout failures worth ONE retry.

    HTTP error statuses (urllib.error.HTTPError) are NEVER transient — a 4xx/5xx
    is the backend answering, and the gate must report it. A URLError is
    transient only when its wrapped ``reason`` is itself a transient error
    (connect-phase ssl/socket failures arrive wrapped; mid-stream ones are raw).
    """
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, TRANSIENT_NETWORK_ERRORS):
        return True
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, TRANSIENT_NETWORK_ERRORS)
    return False


_PROMPT_REQUEST_ERRORS = (urllib.error.URLError, TimeoutError, *TRANSIENT_NETWORK_ERRORS)


def _run_prompt(app: App, args, namespace, prompt: dict, index: int, total: int):
    title = prompt["title"] or prompt["text"][:48]
    session_id = namespace.session_id(index)
    print(f"[{index}/{total}] {title}", flush=True)
    try:
        result = app.ask_stream(prompt["text"], session_id, args.timeout)
    except _PROMPT_REQUEST_ERRORS as exc:
        if not _is_transient_network_error(exc):
            print(f"    FAIL — request error: {type(exc).__name__}\n", flush=True)
            return _request_error_row(title, session_id, exc), [f"request error {type(exc).__name__}"]
        # One retry on a dropped socket/TLS record — the backend is typically
        # still healthy. Fresh session id so a half-consumed first attempt can
        # never leak conversation state into the retry's grounding assertions.
        print(f"    transient network error ({type(exc).__name__}) — retrying once", flush=True)
        time.sleep(PROMPT_RETRY_BACKOFF_SECONDS)  # deliberate pacing/backoff delay in a bounded polling loop  # nosemgrep: arbitrary-sleep
        session_id = f"{session_id}-retry"
        try:
            result = app.ask_stream(prompt["text"], session_id, args.timeout)
        except _PROMPT_REQUEST_ERRORS as retry_exc:
            print(f"    FAIL — request error: {type(retry_exc).__name__}\n", flush=True)
            return (_request_error_row(title, session_id, retry_exc),
                    [f"request error {type(retry_exc).__name__}"])
    carveout = absence_honesty_carveout(prompt["text"], result["answer"], result["summary"])
    graph_only = bool(prompt.get("graph_only"))
    row = _result_row(title, session_id, result, carveout, graph_only)
    _print_prompt_metrics(row)
    problems = (assess(title, result, carveout, graph_only)
                + _extended_prompt_failures(args, namespace, result, row))
    if problems:
        for problem in problems:
            print(f"    FAIL — {problem}", flush=True)
        if not getattr(args, "no_failed_answers", False):
            # A verdict without the answer is not diagnosable (live 2026-09-27).
            row = {**row, "answer_full": result.get("answer") or ""}
    elif row["grounding"] == "graph-grounded":
        print("    PASS (declared graph-only — graph-grounded, no system query required)",
              flush=True)
    elif row["grounding"] == "absence-honesty":
        print("    PASS (absence-honesty carve-out — graph-grounded, zero fabrication)",
              flush=True)
    else:
        print("    PASS", flush=True)
    print(flush=True)
    return row, problems


def _cross_system_proof(rows: list[dict]) -> tuple[list[str], int, int]:
    max_system = max((row["system_queries"] for row in rows), default=0)
    max_neptune = max((row["neptune_queries"] for row in rows), default=0)
    failures = []
    print("== suite-level cross-system proof ==", flush=True)
    for name, value, needed in (("system_queries", max_system, 2),
                                ("neptune_queries", max_neptune, 1)):
        if value < needed:
            failures.append(f"no prompt reached {name}>={needed} (max={value})")
            print(f"  FAIL — max {name}={value} (need >={needed})", flush=True)
        else:
            print(f"  PASS — max {name}={value}", flush=True)
    return failures, max_system, max_neptune


def _suite_integrity_mode(rows: list[dict]) -> str | None:
    """The env's integrity mode, surfaced once at suite level (C3).

    First ``mode`` seen in any row's response_integrity metadata; ``None``
    when the runtime predates the marker (no mode ever streamed).
    """
    for row in rows:
        integrity = row.get("response_integrity")
        if isinstance(integrity, dict) and isinstance(integrity.get("mode"), str):
            return integrity["mode"]
    return None


def _report_totals(rows: list[dict], args, failures: list[str], max_system: int,
                   max_neptune: int) -> dict:
    latencies = [row["elapsed_seconds"] for row in rows
                 if row["elapsed_seconds"] is not None]
    count = lambda key: sum(1 for row in rows if row[key])
    return {
        "prompts": len(rows), "total_tool_calls": sum(r["total_tool_calls"] for r in rows),
        "error_rate": round(count("err_labels") / len(rows), 3) if rows else 0.0,
        "runtime_unavailable_count": sum(
            1 for row in rows if "RuntimeUnavailable" in row["err_labels"]),
        "fabrication_fallback_questions": count("fallback_signals"),
        "memory_reference_questions": count("memory_reference_signals"),
        "prior_conversation_claim_questions": sum(
            1 for row in rows if row.get("prior_conversation_claims")),
        "outage_fallback_questions": count("outage_fallback_signals"),
        "avg_latency_seconds": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "max_latency_seconds": max(latencies) if latencies else None,
        "max_system_queries": max_system, "max_neptune_queries": max_neptune,
        "absence_honesty_passes": sum(
            1 for row in rows if row.get("grounding") == "absence-honesty"),
        "strict": args.strict, "quality_strict": args.quality_strict,
        "integrity_mode": _suite_integrity_mode(rows),
        "quality_failure_questions": sum(
            1 for row in rows
            if _enforcement_failure_reasons(row["quality_failure_reasons"])),
        "passed": not failures,
    }


def _write_report(args, namespace, rows: list[dict], totals: dict) -> None:
    report = build_report(base_url=args.base_url, namespace=namespace,
                          totals=totals, rows=rows)
    path = args.report or os.path.join("/tmp", "smoke_prompts_report.json")  # nosec B108 - fixed report path documented in --help; report contains no secrets
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
        print(f"\n== report written: {path} ==", flush=True)
    except OSError as exc:
        print(f"\n== WARN: report write failed ({type(exc).__name__}) ==", flush=True)
    print(f"  totals: tool_calls={totals['total_tool_calls']} error_rate={totals['error_rate']} "
          f"runtime_unavailable={totals['runtime_unavailable_count']} "
          f"fabrication_questions={totals['fabrication_fallback_questions']} "
          f"avg_latency={totals['avg_latency_seconds']}s "
          f"max_latency={totals['max_latency_seconds']}s", flush=True)


def _source_prompts(app: App, args) -> tuple[list, str]:
    """Source the run's prompts: file or registry, then category, then count.

    ``--count`` applies AFTER the category filter, so a limit never truncates
    the group selection. Returns (prompts, source_label); raises the file
    loader's ValueError unchanged (fail loudly, never run an empty suite
    silently).
    """
    if args.prompts_file:
        prompts = load_prompts_file(args.prompts_file)
        label = f"file {args.prompts_file}"
    else:
        prompts = fetch_prompts(app)
        label = "/api/prompts"
    if args.category:
        prompts = filter_prompts_by_category(prompts, args.category)
        label += f" (category={args.category})"
    if args.count:
        prompts = prompts[:args.count]
    return prompts, label


def main() -> int:
    args = build_parser().parse_args()
    namespace = create_session_namespace(args.run_id, args.session_prefix)
    if not args.base_url or not args.password:
        print("ERROR: --base-url and --password (or ADT_BASE_URL/ADT_PASSWORD) required")
        return 2
    app = App(args.base_url, followup_mode=namespace.followup_mode)
    login = app.login(args.username, args.password)
    if login.get("status") != "ok":
        print("ERROR: login failed")
        return 2
    try:
        prompts, source_label = _source_prompts(app, args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: prompt sourcing failed: {exc}")
        return 2
    if not prompts:
        print(f"ERROR: no prompts sourced from {source_label}")
        return 2
    mode = "explicit follow-up reuse" if namespace.followup_mode else "fresh isolated sessions"
    print(f"== smoke: {len(prompts)} prompts from {source_label}; "
          f"run_id={namespace.run_id}; {mode} ==\n", flush=True)
    rows, failures = [], []
    for index, prompt in enumerate(prompts, 1):
        row, problems = _run_prompt(app, args, namespace, prompt, index, len(prompts))
        rows.append(row)
        failures.extend(f"{row['title']}: {problem}" for problem in problems)
    proof_failures, max_system, max_neptune = _cross_system_proof(rows)
    failures.extend(proof_failures)
    totals = _report_totals(rows, args, failures, max_system, max_neptune)
    _write_report(args, namespace, rows, totals)
    if failures:
        print(f"\n== RESULT: FAIL ({len(failures)}) ==", flush=True)
        for failure in failures:
            print(f"  - {failure}", flush=True)
        return 1
    print(f"\n== RESULT: PASS — {len(prompts)} prompts grounded + cross-system proven ==", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
