# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for the pure classifiers in scripts/smoke_prompts.py.

These exercise the error-taxonomy and fabrication-fallback detectors with no
network — they are pure functions over strings. scripts/ lives at the repo root
(outside the agentic_digital_thread PYTHONPATH), so we add the repo root to sys.path
and import `scripts.smoke_prompts` as a namespace package (no __init__.py
required, no restructuring of the script).
"""
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.smoke_prompts import (  # noqa: E402
    assess,
    classify_error_labels,
    detect_fabrication_signals,
    detect_memory_reference_signals,
    detect_outage_fallback_signals,
    detect_prior_conversation_claims,
    _request_error_row,
    quality_failure_reasons,
    strict_failure_reasons,
)


def test_runtime_unavailable_detected():
    # A tool_result summary / stream error that means the query never reached a
    # live runtime must classify as RuntimeUnavailable.
    for text in [
        "No endpoint or agent found for this request",
        "gateway reported the endpoint not responding",
        "endpoint routing error while dispatching",
        "target agent unavailable",
        "session error: stream closed",
        "runtime unreachable",
    ]:
        assert "RuntimeUnavailable" in classify_error_labels([text]), text


def test_column_not_found_detected():
    labels = classify_error_labels(["Athena failed: COLUMN_NOT_FOUND: no such column"])
    assert "COLUMN_NOT_FOUND" in labels


def test_app_errors_not_flagged_as_runtime_unavailable():
    # Genuine app/query errors must NOT be conflated with the transient
    # RuntimeUnavailable class — they mean the runtime WAS reached and returned
    # a real error.
    labels = classify_error_labels([
        "QueryError: bad SQL",
        "TABLE_NOT_FOUND for orders",
        "COLUMN_NOT_FOUND",
        "ValidationException: invalid parameter",
    ])
    assert "RuntimeUnavailable" not in labels
    for expected in ("QueryError", "TABLE_NOT_FOUND", "COLUMN_NOT_FOUND", "ValidationException"):
        assert expected in labels


def test_error_labels_deduped_and_stable():
    labels = classify_error_labels([
        "QueryError first",
        "QueryError again",
        "COLUMN_NOT_FOUND here",
    ])
    assert labels.count("QueryError") == 1
    # ERROR_PATTERNS order: QueryError precedes COLUMN_NOT_FOUND.
    assert labels.index("QueryError") < labels.index("COLUMN_NOT_FOUND")


def test_error_labels_empty_for_clean_input():
    assert classify_error_labels(["rows returned successfully", ""]) == []
    assert classify_error_labels([]) == []
    assert classify_error_labels(None) == []


def test_strands_unknown_tool_reply_is_an_error():
    """Strands answers a call to a tool name that does not exist with
    "Unknown tool: <name>" (strands/tools/executors/_executor.py). The pattern
    only matched the joined class-name spelling, so smoke reported 0 errors on
    a run where the model twice called an invented tool."""
    for text in ("Unknown tool: sys-a___query_sys_a_2",
                 "Error: Unknown tool: explore_graph_2",
                 "unknown_tool sys-a___query_sys_a_2",
                 "UnknownTool"):
        assert classify_error_labels([text]) == ["UnknownTool"], text
    assert classify_error_labels(["the tool is unknown to us"]) == []


def test_business_data_mentioning_an_unknown_tool_is_not_an_error():
    """Tool summaries carry row values; a successful read of a row that says
    "unknown tool" must not count as a failed call."""
    for text in ("1 record found: unknown tool awaiting calibration",
                 "rows: [['Unknown tool', 'bin 4']]"):
        assert classify_error_labels([text]) == [], text


def test_fabrication_signals_detected():
    answer = (
        "Drawing from your prior conversation, the value was previously confirmed "
        "and is already on record. The system is currently unavailable."
    )
    signals = detect_fabrication_signals(answer)
    for expected in ("from your prior", "previously confirmed", "already on record",
                     "currently unavailable"):
        assert expected in signals


def test_fabrication_signals_clean_answer_empty():
    clean = (
        "The ERP shows 3 open work orders and the MES reports a 92% yield for "
        "line 4, cross-referenced via the knowledge graph."
    )
    assert detect_fabrication_signals(clean) == []
    assert detect_fabrication_signals("") == []
    assert detect_fabrication_signals(None) == []


def test_memory_references_are_separate_from_outage_fallbacks():
    answer = (
        "From your prior session, this was previously confirmed and already on "
        "record, but the endpoint is currently unavailable."
    )

    memory = detect_memory_reference_signals(answer)
    outage = detect_outage_fallback_signals(answer)
    combined = detect_fabrication_signals(answer)

    assert "prior session" in memory
    assert "from your prior" in memory
    assert "previously confirmed" in memory
    assert "already on record" in memory
    assert memory and outage == ["currently unavailable"]
    assert set(combined) == set(memory + outage)


def test_strict_memory_reference_fails_only_an_ungrounded_followup():
    # A fresh smoke turn has no earlier conversation (and no AgentCore Memory:
    # tests/unit/test_smoke_fresh_turns_memory_free.py), so memory wording
    # isn't graded as a memory reference there. Only the opt-in --run-id
    # follow-up mode continues a conversation; there a memory reference still
    # needs a current-turn system query.
    answer = "As previously confirmed by QA, there are three orders."

    fresh = strict_failure_reasons(
        err_labels=[], answer=answer, system_queries=1, followup_mode=False,
    )
    fresh_no_query = strict_failure_reasons(
        err_labels=[], answer=answer, system_queries=0, followup_mode=False,
    )
    followup = strict_failure_reasons(
        err_labels=[], answer=answer, system_queries=1, followup_mode=True,
    )
    followup_no_query = strict_failure_reasons(
        err_labels=[], answer=answer, system_queries=0, followup_mode=True,
    )

    assert fresh == []
    # A fresh turn with no system query is the always-on grounding gate's
    # failure, not a memory one.
    assert not any("memory-reference" in reason for reason in fresh_no_query)
    assert followup == []
    assert any("memory-reference" in reason for reason in followup_no_query)


def test_a_fresh_turn_claiming_a_prior_conversation_is_listed_not_failed():
    # A fresh turn has no earlier conversation, so an answer that says it
    # knows something from one may have made that history up. Pattern
    # matching can't tell a claim from a denial or a data phrase reliably, so
    # strict smoke lists the match for the rehearsal reviewer and does not
    # fail on it.
    for answer in ("From your prior session, three orders were previously confirmed.",
                   "As discussed in a prior session, there are three orders."):
        reasons = strict_failure_reasons(
            err_labels=[], answer=answer, system_queries=1, followup_mode=False,
        )
        assert reasons == [], answer
        assert detect_prior_conversation_claims(answer), answer


def test_a_negated_prior_conversation_phrase_passes_a_fresh_turn():
    assert strict_failure_reasons(
        err_labels=[], answer="These figures were measured now, not from your "
        "prior chats.", system_queries=1, followup_mode=False,
    ) == []


def test_a_followup_may_name_its_own_earlier_turn():
    assert strict_failure_reasons(
        err_labels=[], answer="From your prior session, three orders closed.",
        system_queries=1, followup_mode=True,
    ) == []


def test_fresh_strict_scoring_ignores_the_live_already_know_answer():
    # Live strict-smoke false positive (rehearsal, 2026-10-18, "Engineering Change
    # Impact on Maintenance"): an honest gap answer asked the user for "a
    # change you already know about" and failed as a memory reference.
    answer = ("PLM holds no engineering-change records. If you have a part "
              "number or a change you already know about, share it and I'll "
              "trace it.")
    assert strict_failure_reasons(
        err_labels=[], answer=answer, system_queries=1, followup_mode=False,
    ) == []


def test_fresh_strict_scoring_still_fails_outages():
    answer = "As previously confirmed, the endpoint is currently unavailable."
    reasons = strict_failure_reasons(
        err_labels=["RuntimeUnavailable"], answer=answer, system_queries=1,
        followup_mode=False,
    )
    assert "RuntimeUnavailable" in reasons
    assert any("outage-fallback" in reason for reason in reasons)
    assert not any("memory-reference" in reason for reason in reasons)


import pytest  # noqa: E402

# Verbatim from a live strict-smoke run (2026-10-03): the answer DISCLAIMS
# memory, so it is not a memory reference.
_NEGATED_LIVE = (
    "*Equipment-to-line correspondences below were inferred from CMMS "
    "facility/area text and MES line names (no stored mapping existed yet), "
    "so they are proposed rather than previously confirmed.*"
)


@pytest.mark.parametrize("answer", [
    _NEGATED_LIVE,
    "These links were not previously confirmed by a steward.",
    "The join is new and not already on record.",
    "This is measured now, instead of previously confirmed values.",
])
def test_negated_memory_phrase_is_not_a_memory_reference(answer):
    assert detect_memory_reference_signals(answer) == []
    assert strict_failure_reasons(
        err_labels=[], answer=answer, system_queries=1, followup_mode=False,
    ) == []


def test_negation_is_scoped_to_the_cited_occurrence():
    answer = ("As previously confirmed, three orders closed. The new join "
              "is proposed rather than previously confirmed.")
    assert detect_memory_reference_signals(answer) == ["previously confirmed"]


def test_negated_phrase_stays_in_the_compatibility_signal_list():
    assert "previously confirmed" in detect_fabrication_signals(_NEGATED_LIVE)


def _fresh_reasons(answer):
    return strict_failure_reasons(
        err_labels=[], answer=answer, system_queries=1, followup_mode=False,
    )


# A fresh turn has no earlier conversation. Pattern matching can't reliably
# tell a claim to one from a denial or a phrase about data (every narrowing of
# the denial rule either missed denials or excused real claims), so strict
# smoke never fails on it. It lists the
# matches for the rehearsal reviewer instead (row prior_conversation_claims,
# a "for review" line, and a count in the totals).
@pytest.mark.parametrize("answer", [
    "Earlier in this conversation, you told me three orders were open.",
    "As we discussed, three orders are open.",
    "You told me earlier that three orders were open.",
    "Three orders were open, as you mentioned before.",
    "Previously, you said three orders were open.",
    "In our previous chat, three orders were open.",
    "As noted in the last session, three orders are open.",
    "Without a matching PLM record, as we discussed, three orders remain open.",
    "I cannot query MES as we discussed earlier.",
])
def test_a_fresh_prior_conversation_claim_is_listed_and_not_failed(answer):
    assert detect_prior_conversation_claims(answer), answer
    assert _fresh_reasons(answer) == [], answer


@pytest.mark.parametrize("answer", [
    "I cannot access your prior session, so I used the current ERP result.",
    "I do not have access to your prior session, so I queried ERP.",
    "Without access to the prior conversation, I queried ERP.",
    "The result was calculated from your prior work order and the current one.",
    "As you mentioned earlier in your question, line 4 is down.",
    "Scrap rose on the last turn of line 4.",
])
def test_an_honest_fresh_answer_never_fails_strict(answer):
    assert _fresh_reasons(answer) == [], answer


@pytest.mark.parametrize("answer", [
    # "prior" on a data noun.
    "The result was calculated from your prior work order and the current one.",
    # The user's own words in the current message, which a fresh turn sees.
    "As you mentioned earlier in your question, line 4 is down.",
    "You said earlier in this message that line 4 is down, and MES agrees.",
    # "Turn" as a production cycle, not a chat turn.
    "Scrap rose on the last turn of line 4.",
    # A direct negator.
    "These figures were measured now, not from your prior chats.",
    # Time words about data.
    "You said the order closed before Friday, but ERP shows it open.",
    "You mentioned line 4, so I checked its previous shift.",
    "The previous work order on that line closed earlier today.",
    "Check with your QA lead about the prior lot before release.",
])
def test_data_phrases_and_the_current_message_are_not_listed(answer):
    assert detect_prior_conversation_claims(answer) == [], answer


def test_a_fresh_claim_is_on_the_row_printed_for_review_and_counted(capsys):
    args = SimpleNamespace(strict=True, quality_strict=False, timeout=5)
    namespace = create_session_namespace()
    prompt = {"title": "Open orders", "text": "How many orders are open?"}
    summary = {"total_tool_calls": 3, "system_queries": 2, "neptune_queries": 1}
    answer = "As we discussed, three orders are open in ERP. " + "x" * 80
    row, problems = _run_prompt(_FakeApp(_stream_result(answer, summary)),
                                args, namespace, prompt, 1, 1)
    assert row["prior_conversation_claims"] == ["as we discussed"]
    assert not any("prior-conversation" in p for p in problems)
    out = capsys.readouterr().out
    assert "prior-conversation claims (for review): as we discussed" in out
    totals = report_totals([row], args, [], 2, 1)
    assert totals["prior_conversation_claim_questions"] == 1
    error_row = _request_error_row("q", "s", RuntimeError("x"))
    assert error_row["prior_conversation_claims"] == []


def test_followup_memory_still_requires_a_current_turn_system_query():
    answer = "As previously confirmed, the result is still three orders."
    strict_reasons = strict_failure_reasons(
        err_labels=[], answer=answer, system_queries=0, followup_mode=True,
    )
    base_reasons = assess("follow-up", {
        "done": True,
        "errors": [],
        "answer": "x" * 80,
        "summary": {"total_tool_calls": 1, "system_queries": 0},
    })

    assert any("current-turn system query" in reason for reason in strict_reasons)
    assert any("system_queries < 1" in reason for reason in base_reasons)


def test_outage_fallback_always_fails_strict_followup_scoring():
    reasons = strict_failure_reasons(
        err_labels=[],
        answer="The endpoint is currently unavailable.",
        system_queries=2,
        followup_mode=True,
    )

    assert any("outage-fallback" in reason for reason in reasons)


def test_quality_strict_accepts_complete_deterministic_metadata():
    metadata = {
        "verdict": "accepted",
        "reason_codes": [],
        "current_turn_evidence": True,
        "completeness": "complete",
        "learning_allowed": True,
        "claim_count": 2,
        "current_intent": True,
    }
    assert quality_failure_reasons(metadata) == []


def test_quality_strict_rejects_missing_or_negative_deterministic_fields():
    reasons = quality_failure_reasons({
        "verdict": "rejected",
        "reason_codes": ["ARITHMETIC_INPUT_UNSUPPORTED"],
        "current_turn_evidence": False,
        "completeness": "partial",
        "claim_count": 0,
    })
    assert "integrity verdict rejected" in reasons
    assert "no validated current-turn evidence" in reasons
    assert "response completeness partial" in reasons
    assert "missing deterministic current-intent classification" in reasons
    assert "integrity rejection ARITHMETIC_INPUT_UNSUPPORTED" in reasons
    assert "no deterministically validated claims" in reasons


def test_quality_strict_requires_metadata_and_does_not_claim_external_value_audit():
    assert quality_failure_reasons(None) == [
        "missing deterministic response-integrity metadata"
    ]
    # A metadata pass is intentionally independent of arbitrary answer prose or
    # expected values; external value verification belongs to a separate audit.
    assert quality_failure_reasons({
        "verdict": "accepted", "reason_codes": [],
        "current_turn_evidence": True, "completeness": "complete",
        "learning_allowed": True, "claim_count": 1, "current_intent": False,
    }) == []


def test_request_error_row_contains_category_not_exception_text():
    secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"  # gitleaks:allow — intentional fake token (never-mint/classifier fixture)  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
    row = _request_error_row("question", "session", RuntimeError(secret))
    rendered = str(row)
    assert row["errors"] == ["RuntimeError"]
    assert secret not in rendered


# ---------------------------------------------------------------------------
# Mode-aware scorecard (C3) — the A-lite marker distinguishes "enforcement not
# enabled on this env" from "integrity failed"
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from scripts.smoke_prompts import (  # noqa: E402
    INTEGRITY_NOT_ENABLED_PREFIX,
    _extended_prompt_failures,
    _report_totals,
    _suite_integrity_mode,
)

_FULL_ENFORCE_METADATA = {
    "verdict": "accepted", "reason_codes": [], "current_turn_evidence": True,
    "completeness": "complete", "learning_allowed": True, "claim_count": 2,
    "current_intent": True,
}


def test_quality_reasons_mode_aware_table():
    cases = [
        # None → unchanged missing-metadata reason.
        (None, ["missing deterministic response-integrity metadata"]),
        # Marker without verdict fields → single informational reason.
        ({"mode": "report-only"},
         ["integrity enforcement not enabled (mode=report-only)"]),
        ({"mode": "annotate"},
         ["integrity enforcement not enabled (mode=annotate)"]),
        # Full accepted enforce metadata (with the additive mode key) → [].
        ({**_FULL_ENFORCE_METADATA, "mode": "enforce"}, []),
    ]
    for metadata, expected in cases:
        assert quality_failure_reasons(metadata) == expected


def test_quality_reasons_enforce_mode_missing_verdict_keeps_strict_cascade():
    reasons = quality_failure_reasons({"mode": "enforce"})
    assert "integrity verdict missing" in reasons
    assert "no validated current-turn evidence" in reasons
    assert "no deterministically validated claims" in reasons
    assert not any(r.startswith(INTEGRITY_NOT_ENABLED_PREFIX) for r in reasons)


def test_quality_reasons_mode_absent_old_runtime_unchanged():
    # {} (metadata event never arrived / old runtime) keeps today's strict
    # field checks byte-identical — never the informational reason.
    reasons = quality_failure_reasons({})
    assert "integrity verdict missing" in reasons
    assert "response completeness missing" in reasons
    assert not any(r.startswith(INTEGRITY_NOT_ENABLED_PREFIX) for r in reasons)


def test_quality_strict_passes_report_only_env_with_marker():
    args = SimpleNamespace(strict=False, quality_strict=True)
    namespace = SimpleNamespace(followup_mode=False)
    row = {"quality_failure_reasons":
           ["integrity enforcement not enabled (mode=report-only)"]}
    assert _extended_prompt_failures(args, namespace, {}, row) == []


def test_quality_strict_still_fails_enforce_env_missing_verdicts():
    args = SimpleNamespace(strict=False, quality_strict=True)
    namespace = SimpleNamespace(followup_mode=False)
    row = {"quality_failure_reasons": quality_failure_reasons({"mode": "enforce"})}
    failures = _extended_prompt_failures(args, namespace, {}, row)
    assert failures
    assert all(f.endswith("(quality-strict)") for f in failures)


def _totals_row(reasons, integrity=None):
    return {
        "total_tool_calls": 1, "err_labels": [], "fallback_signals": [],
        "memory_reference_signals": [], "outage_fallback_signals": [],
        "elapsed_seconds": 1.0, "quality_failure_reasons": reasons,
        "response_integrity": integrity or {},
    }


def test_report_totals_surface_integrity_mode_and_skip_informational_rows():
    args = SimpleNamespace(strict=False, quality_strict=False)
    rows = [
        _totals_row(["integrity enforcement not enabled (mode=report-only)"],
                    integrity={"mode": "report-only"}),
        _totals_row(["integrity verdict missing"], integrity={}),
    ]
    totals = _report_totals(rows, args, [], 2, 1)
    assert totals["integrity_mode"] == "report-only"
    # Informational-only rows are not quality failures; genuine ones count.
    assert totals["quality_failure_questions"] == 1


def test_suite_integrity_mode_none_for_old_runtimes():
    assert _suite_integrity_mode([_totals_row([], integrity={})]) is None


# ---------------------------------------------------------------------------
# D1 word-boundary taxonomy (ADR-0054) — pinned regression fixtures. These
# strings are the contract: camelCase data VALUES must never classify, while
# genuine error tokens (incl. enumerated AWS suffix compounds) must.
# ---------------------------------------------------------------------------

import pytest  # noqa: E402


@pytest.mark.parametrize("text", [
    # The live pilot-env false positive — a data value, not an error.
    "8 events: OperatingTimeout",
    # Compound data values never classify (AccessGranted / OperatingTimeout /
    # ResourceNotFoundFlag lowercase to single tokens with no interior boundary).
    "status column values: AccessGranted, OperatingTimeout, ResourceNotFoundFlag",
])
def test_data_value_compounds_never_classify(text):
    assert classify_error_labels([text]) == []


@pytest.mark.parametrize("text", [
    "query timeout after 60s",
    "read timed out",
    "TimeoutError: gateway",
])
def test_timeout_genuine_forms_classify(text):
    assert classify_error_labels([text]) == ["TIMEOUT"]


@pytest.mark.parametrize("text", [
    # Plural/derived forms of the genuine error token (the MED-A gap).
    "3 timeouts",
    "timeouts observed during the run",
    "the gateway request times out after 60s",
    "requests kept timing out",
    "TimeoutExceptions raised on both calls",
    "TimeoutErrors from the gateway",
])
def test_timeout_plural_and_derived_forms_classify(text):
    assert classify_error_labels([text]) == ["TIMEOUT"]


@pytest.mark.parametrize("text", [
    # Plural compound data values must STILL never classify — no interior
    # word boundary survives lowercasing.
    "OperatingTimeouts",
    "12 events: OperatingTimeouts recorded",
    "status values: AccessDeniedFlags, ResourceNotFoundMarkers, QueryErrorCodes",
])
def test_plural_compound_data_values_never_classify(text):
    assert classify_error_labels([text]) == []


@pytest.mark.parametrize("text,expected", [
    # Plural forms of the other needles (same MED-A audit).
    ("two QueryErrors while planning", ["QueryError"]),
    ("multiple ResourceNotFoundExceptions from SiteWise", ["ResourceNotFound"]),
    ("AccessDeniedExceptions on both targets", ["AccessDenied"]),
    ("ValidationExceptions for each parameter", ["ValidationException"]),
])
def test_plural_error_tokens_classify(text, expected):
    assert classify_error_labels([text]) == expected


@pytest.mark.parametrize("text", [
    # Plural forms of the RuntimeUnavailable phrases (_compile_signal s?).
    "session errors on every call",
    "endpoints not found for both systems",
    "agents unavailable across the fleet",
])
def test_runtime_unavailable_plural_phrases_classify(text):
    assert classify_error_labels([text]) == ["RuntimeUnavailable"]


def test_signal_detectors_cover_plural_forms_report_original_phrase():
    # Detectors match plural forms but keep REPORTING the original phrase
    # strings so strict-failure text is unchanged.
    assert detect_outage_fallback_signals("endpoint errors on retry") == [
        "endpoint error"]
    assert detect_memory_reference_signals("from prior sessions") == [
        "prior session"]


@pytest.mark.parametrize("text,expected", [
    ("BytesScannedLimitExceeded: query aborted", ["BytesScannedLimit"]),
    ("An error occurred (AccessDeniedException) when calling", ["AccessDenied"]),
])
def test_aws_exception_suffix_variants_classify(text, expected):
    assert classify_error_labels([text]) == expected


@pytest.mark.parametrize("text", [
    "No endpoint or agent found for path",
    "endpoint not found",
    "endpoint routing error",
    "target is not responding",
    "runtime unreachable",
    "agent unavailable",
    "session error: stream closed",
])
def test_runtime_unavailable_phrases_survive(text):
    # Multi-word RuntimeUnavailable phrases keep working under the regex
    # transform (\b edges, \s+ between words).
    assert classify_error_labels([text]) == ["RuntimeUnavailable"]


def test_labels_dedup_and_stable_order():
    # Two RuntimeUnavailable phrases in one text → exactly one label.
    assert classify_error_labels(["Agent unavailable — session error"]) == [
        "RuntimeUnavailable"
    ]
    # Per-text output follows ERROR_PATTERNS order (QueryError before TIMEOUT
    # regardless of position in the text); cross-input dedup keeps one label.
    labels = classify_error_labels([
        "query timeout after 60s hit QueryError: bad SQL",
        "read timed out",
    ])
    assert labels == ["QueryError", "TIMEOUT"]


# ---------------------------------------------------------------------------
# Absence-honesty carve-out (ADR-0054 amendment 2026-08-03) — mutation-kill
# pins. Each test names the mutation it kills. The carve-out verdict is
# COMPUTED (shared eval fabrication detector + query counts); phrase markers
# are informational only and never decide in either direction.
# ---------------------------------------------------------------------------

import scripts.eval_answers as eval_answers  # noqa: E402
import scripts.smoke_prompts as smoke_prompts  # noqa: E402
from scripts.smoke_prompts import (  # noqa: E402
    _grounding,
    _report_totals as report_totals,
    _result_row,
    _run_prompt,
    absence_honesty_carveout,
    create_session_namespace,
    derive_prompt_scope_terms,
)

_PROBE_PROMPT = ("What is the current maintenance backlog and which work "
                 "orders are overdue?")
# Condensed from the live pilot-env answer (2026-08-03) — including the honest
# "exactly 2 onboarded systems" count inside a maintenance-scoped sentence
# that triggered contingency C1 (the pinned honest-system-count pre-mask).
_HONEST_ABSENCE = (
    "I can see the graph has exactly 2 onboarded systems — and neither is a "
    "maintenance/CMMS system. A maintenance management or CMMS system is not "
    "connected to this Digital Thread. Neither system holds work orders, "
    "maintenance backlog, or overdue maintenance tasks. The authority rules "
    "cover concepts like batch and machine-state — there is no work-order or "
    "maintenance concept mapped to any onboarded system."
)
_FABRICATED_ABSENCE = (
    "No CMMS is onboarded, but there are 14 overdue preventive maintenance "
    "work orders in the backlog."
)
_ABSENT_SUMMARY = {"total_tool_calls": 2, "system_queries": 0, "neptune_queries": 2}


def test_carveout_passes_live_honest_absence_answer():
    # The headline behavior: the pilot env's Maintenance Backlog Probe answer — graph-
    # grounded, zero fabrication — passes via the carve-out, distinctly
    # classified, with an empty assess().
    cv = absence_honesty_carveout(_PROBE_PROMPT, _HONEST_ABSENCE, _ABSENT_SUMMARY)
    assert cv["engaged"] is True
    assert cv["passed"] is True
    assert cv["reasons"] == []
    assert cv["fabrication_flags"] == []
    assert _grounding(0, cv) == "absence-honesty"
    res = {"done": True, "errors": [], "answer": _HONEST_ABSENCE,
           "summary": _ABSENT_SUMMARY}
    assert assess("probe", res, cv) == []


def test_carveout_fabricated_quantity_still_fails():
    # Kills M1: disabling the fabrication check inside the carve-out.
    cv = absence_honesty_carveout(_PROBE_PROMPT, _FABRICATED_ABSENCE, _ABSENT_SUMMARY)
    assert cv["engaged"] is True
    assert cv["passed"] is False
    assert any("fabricated-quantity" in f for f in cv["fabrication_flags"])
    res = {"done": True, "errors": [], "answer": _FABRICATED_ABSENCE,
           "summary": _ABSENT_SUMMARY}
    failures = assess("probe", res, cv)
    assert any("system_queries < 1" in f for f in failures)
    assert any("carve-out not satisfied" in f for f in failures)


def test_carveout_requires_graph_grounding():
    # Kills M2: dropping the neptune_queries >= 1 requirement.
    summary = {"total_tool_calls": 1, "system_queries": 0, "neptune_queries": 0}
    cv = absence_honesty_carveout(_PROBE_PROMPT, _HONEST_ABSENCE, summary)
    assert cv["engaged"] is True
    assert cv["passed"] is False
    assert any("not graph-grounded" in r for r in cv["reasons"])


def test_zero_neptune_zero_system_still_fails_assess():
    # Kills M14: a zero-neptune AND zero-system answer sneaking through.
    summary = {"total_tool_calls": 1, "system_queries": 0, "neptune_queries": 0}
    cv = absence_honesty_carveout(_PROBE_PROMPT, _HONEST_ABSENCE, summary)
    res = {"done": True, "errors": [], "answer": _HONEST_ABSENCE,
           "summary": summary}
    failures = assess("probe", res, cv)
    assert failures
    assert any("system_queries < 1" in f for f in failures)
    assert _grounding(0, cv) == "ungrounded"


def test_carveout_never_engages_when_system_grounded():
    # Kills M3: the carve-out engaging at system_queries > 0. Even a
    # fabricated answer text is irrelevant — the detector never runs.
    grounded = {"total_tool_calls": 3, "system_queries": 2, "neptune_queries": 1}
    cv = absence_honesty_carveout(_PROBE_PROMPT, _FABRICATED_ABSENCE, grounded)
    assert cv["engaged"] is False
    assert cv["passed"] is False
    assert cv["fabrication_flags"] == []
    assert _grounding(2, cv) == "system-grounded"


def test_markers_are_informational_only_both_directions():
    # Kills M4: gating the verdict on ABSENCE_HONESTY_MARKERS in EITHER
    # direction. (a) An honest paraphrase hitting ZERO markers still passes;
    # (b) a marker-bearing fabricated answer still fails.
    paraphrase = (
        "The knowledge graph maps zero maintenance concepts; work orders and "
        "backlog live in a CMMS, and this digital thread lacks one entirely."
    )
    assert eval_answers.absence_markers_matched(paraphrase) == []
    cv_honest = absence_honesty_carveout(_PROBE_PROMPT, paraphrase, _ABSENT_SUMMARY)
    assert cv_honest["passed"] is True
    assert cv_honest["absence_markers"] == []

    marker_bearing_fabrication = (
        "No records for maintenance exist in most systems, yet there are 14 "
        "overdue work orders in the backlog."
    )
    assert eval_answers.absence_markers_matched(marker_bearing_fabrication)
    cv_fab = absence_honesty_carveout(
        _PROBE_PROMPT, marker_bearing_fabrication, _ABSENT_SUMMARY)
    assert cv_fab["passed"] is False
    assert cv_fab["absence_markers"]  # recorded — informational only


def test_carveout_fails_closed_without_scope_terms():
    # Kills M6: failing OPEN when no scope terms are derivable.
    cv = absence_honesty_carveout("What is the status?", _HONEST_ABSENCE,
                                  _ABSENT_SUMMARY)
    assert cv["engaged"] is True
    assert cv["passed"] is False
    assert cv["scope_term_count"] == 0
    assert any("fail-closed" in r for r in cv["reasons"])


def test_scope_terms_are_word_boundary_regexes():
    # Kills M7: bare-substring scoping (D1 violation). Every derived term is
    # \b-anchored, and a camelCase compound data value never puts a sentence
    # in scope (no interior boundary survives lowercasing).
    terms = derive_prompt_scope_terms(_PROBE_PROMPT)
    assert terms
    for term in terms:
        assert term.startswith(r"\b") and term.endswith(r"s?\b"), term
    cv = absence_honesty_carveout(
        _PROBE_PROMPT, "12 events: PreventiveMaintenanceFlag recorded.",
        _ABSENT_SUMMARY)
    assert cv["fabrication_flags"] == []
    assert cv["passed"] is True


def test_carveout_reuses_eval_detector_single_source(monkeypatch):
    # Kills M8: a private re-implementation of the detector. The carve-out
    # must call scripts.eval_answers.fabrication_flags with the prompt-derived
    # terms as must_not_quantify.
    calls = {}

    def recorder(expect, question_text, answer):
        calls["expect"] = expect
        calls["question"] = question_text
        return []

    monkeypatch.setattr(eval_answers, "fabrication_flags", recorder)
    cv = absence_honesty_carveout(_PROBE_PROMPT, _HONEST_ABSENCE, _ABSENT_SUMMARY)
    assert cv["passed"] is True
    assert calls["expect"] == {
        "must_not_quantify": derive_prompt_scope_terms(_PROBE_PROMPT)}
    assert calls["question"] == _PROBE_PROMPT


def test_carveout_not_engaged_without_summary():
    # Kills M9: rescuing a summary-less result.
    cv = absence_honesty_carveout(_PROBE_PROMPT, _HONEST_ABSENCE, {})
    assert cv["engaged"] is False
    assert cv["passed"] is False
    res = {"done": True, "errors": [], "answer": _HONEST_ABSENCE, "summary": {}}
    assert any("no intelligence_summary" in f for f in assess("probe", res, cv))


def test_carveout_invented_identifier_fails():
    # Kills M10: an invented identifier ("WO-4821" — absent from the prompt)
    # passing the carve-out.
    answer = ("No CMMS is onboarded; the only overdue maintenance work order "
              "in the graph metadata is WO-4821.")
    cv = absence_honesty_carveout(_PROBE_PROMPT, answer, _ABSENT_SUMMARY)
    assert cv["passed"] is False
    assert any("invented-identifier" in f for f in cv["fabrication_flags"])


def test_assess_two_arg_legacy_behavior_unchanged():
    # Kills M11: breaking the 2-arg legacy form (byte-identical to
    # carveout=None; existing follow-up memory test also pins this).
    res = {"done": True, "errors": [], "answer": _HONEST_ABSENCE,
           "summary": _ABSENT_SUMMARY}
    legacy = assess("probe", res)
    assert legacy == assess("probe", res, None)
    assert any("system_queries < 1" in f for f in legacy)


def test_carveout_detector_import_failure_fails_closed(monkeypatch):
    # Kills M12: swallowing a detector ImportError as a pass.
    def boom():
        raise ImportError("eval_answers unavailable")

    monkeypatch.setattr(smoke_prompts, "_eval_module", boom)
    cv = absence_honesty_carveout(_PROBE_PROMPT, _HONEST_ABSENCE, _ABSENT_SUMMARY)
    assert cv["engaged"] is True
    assert cv["passed"] is False
    assert any("fail-closed" in r for r in cv["reasons"])


def test_honest_other_system_counts_do_not_flag():
    # Kills M13: over-broad scoping flagging honest counts about OTHER
    # systems (the D2 false-positive class the scoped detector eliminates).
    answer = ("Lake Athena returned 3847 production segments and MES shows 12 "
              "active batches. However, no maintenance system is connected, "
              "so the backlog and overdue work orders cannot be reported.")
    cv = absence_honesty_carveout(_PROBE_PROMPT, answer, _ABSENT_SUMMARY)
    assert cv["passed"] is True
    assert cv["fabrication_flags"] == []


def test_honest_onboarded_system_count_masked_in_carveout_only():
    # C1 pin (both directions): the live answer honestly counts onboarded
    # systems INSIDE a maintenance-scoped sentence — masked on a COPY inside
    # the carve-out only. The shared eval detector stays untouched (the same
    # text still flags when scored directly), and the mask NEVER rescues a
    # fabricated metric count.
    sentence = ("The graph has exactly 2 onboarded systems and neither holds "
                "maintenance backlog data.")
    cv = absence_honesty_carveout(_PROBE_PROMPT, sentence, _ABSENT_SUMMARY)
    assert cv["passed"] is True
    assert cv["fabrication_flags"] == []
    direct = eval_answers.fabrication_flags(
        {"must_not_quantify": derive_prompt_scope_terms(_PROBE_PROMPT)},
        _PROBE_PROMPT, sentence)
    assert any("fabricated-quantity" in f for f in direct)
    fab = absence_honesty_carveout(_PROBE_PROMPT, _FABRICATED_ABSENCE,
                                   _ABSENT_SUMMARY)
    assert fab["passed"] is False


def test_honest_copular_system_count_masked():
    # C1 pin, copular word order (live pilot env 2026-08-04 rerun): the agent
    # phrased its honest count as "Only **2 systems** are onboarded" —
    # N systems ARE onboarded, with markdown bold at the token joints. Kills
    # the mutation that narrows the mask back to the attributive-only
    # "N onboarded systems" word order (the exact regression the verifier
    # caught live: the carve-out flagged this honest count as
    # fabricated-quantity and strict smoke failed).
    live = ("Only **2 systems** are onboarded in this environment, and "
            "neither covers maintenance or work orders: no maintenance "
            "backlog can be reported.")
    cv = absence_honesty_carveout(_PROBE_PROMPT, live, _ABSENT_SUMMARY)
    assert cv["engaged"] is True
    assert cv["passed"] is True
    assert cv["fabrication_flags"] == []
    # The shared eval detector still flags the raw text when scored directly
    # — the mask lives in the carve-out COPY only.
    direct = eval_answers.fabrication_flags(
        {"must_not_quantify": derive_prompt_scope_terms(_PROBE_PROMPT)},
        _PROBE_PROMPT, live)
    assert any("fabricated-quantity" in f for f in direct)


def test_copular_fabricated_metric_count_still_fails():
    # Companion pin: the broadened copular mask must NOT over-mask a
    # genuinely fabricated metric count in the same word order. Kills the
    # mutation that widens the mask beyond connected/onboarded
    # systems|sources constructions (e.g. masking any "N <noun> are ..."
    # sentence).
    fabricated = ("No CMMS is onboarded, but 14 open work orders are pending "
                  "in the maintenance backlog.")
    cv = absence_honesty_carveout(_PROBE_PROMPT, fabricated, _ABSENT_SUMMARY)
    assert cv["engaged"] is True
    assert cv["passed"] is False
    assert any("fabricated-quantity" in f for f in cv["fabrication_flags"])
    # And the sneakier variant that borrows the mask's own verb: a metric
    # noun ("work orders") in the copular onboarded construction never
    # matches the systems|sources-anchored pattern.
    sneaky = ("14 work orders are onboarded in the backlog and 3 remain "
              "overdue for maintenance.")
    cv2 = absence_honesty_carveout(_PROBE_PROMPT, sneaky, _ABSENT_SUMMARY)
    assert cv2["passed"] is False
    assert any("fabricated-quantity" in f for f in cv2["fabrication_flags"])


def test_mask_joint_never_crosses_lines():
    # SECURITY pin: a newline-tolerant joint let a
    # fabricated scoped value at end-of-line glue onto an honest "Systems
    # are connected" heading on the NEXT line, masking the whole span.
    # Kills the mutation that re-widens _HSC_JOINT to [\s*_]*\s[\s*_]*.
    exploit = ("Maintenance backlog total: **47**\n"
               "**Systems** are currently connected: ERP, MES.")
    cv = absence_honesty_carveout(_PROBE_PROMPT, exploit, _ABSENT_SUMMARY)
    assert cv["engaged"] is True
    assert cv["passed"] is False
    assert any("fabricated-quantity" in f for f in cv["fabrication_flags"])
    # Control: the same honest copular sentence on ONE line still masks —
    # the fix must not regress the legitimate intra-line exemption.
    honest = "Only **2 systems** are currently connected: Lake Athena, Telem SiteWise."
    cv2 = absence_honesty_carveout(_PROBE_PROMPT, honest, _ABSENT_SUMMARY)
    assert cv2["passed"] is True


class _FakeApp(smoke_prompts.App):
    """Bypasses App.__init__ — the class-level login defaults keep it inert."""

    def __init__(self, result):
        self._result = result

    def ask_stream(self, question, session_id, timeout):
        return dict(self._result)


def _stream_result(answer, summary):
    return {
        "answer": answer, "summary": summary,
        "integrity": {"mode": "report-only"}, "errors": [], "done": True,
        "tool_summaries": [], "tool_names": ["knowledge_graph"],
        "tool_starts": 2, "elapsed_seconds": 1.2,
    }


def test_run_prompt_annotates_carveout_pass(capsys):
    # Kills M5: a silent, unannotated carve-out pass. Integration through the
    # real _run_prompt: row grounding, absence_carveout detail, the distinct
    # console PASS line, and the additive totals counter — while a grounded
    # row stays "system-grounded".
    args = SimpleNamespace(strict=True, quality_strict=False, timeout=5)
    namespace = create_session_namespace()
    prompt = {"title": "Maintenance Backlog Probe", "text": _PROBE_PROMPT}
    app = _FakeApp(_stream_result(_HONEST_ABSENCE, _ABSENT_SUMMARY))

    row, problems = _run_prompt(app, args, namespace, prompt, 1, 1)

    assert problems == []
    assert row["grounding"] == "absence-honesty"
    assert row["absence_carveout"]["passed"] is True
    out = capsys.readouterr().out
    assert "PASS (absence-honesty carve-out — graph-grounded, zero fabrication)" in out
    assert "grounding=absence-honesty" in out

    grounded_summary = {"total_tool_calls": 3, "system_queries": 2,
                        "neptune_queries": 1}
    grounded_result = _stream_result("x" * 80, grounded_summary)
    grounded_row = _result_row(
        "grounded", "s-1", grounded_result,
        absence_honesty_carveout(_PROBE_PROMPT, "x" * 80, grounded_summary))
    assert grounded_row["grounding"] == "system-grounded"
    assert grounded_row["absence_carveout"] is None

    totals = report_totals([row, grounded_row], args, [], 2, 2)
    assert totals["absence_honesty_passes"] == 1

    error_row = _request_error_row("q", "s", RuntimeError("x"))
    assert error_row["grounding"] == "ungrounded"
    assert error_row["absence_carveout"] is None


# ---------------------------------------------------------------------------
# FOLLOWUPS scoring boundary (smoke lane).
#
# ``---FOLLOWUPS---`` is a reserved protocol marker: everything after it is
# generated suggestion-question text the frontend renders as chips. Those
# chips are QUESTIONS, not claims — but they routinely restate outage
# wording ("Retry when the endpoint is available?"), memory wording
# ("Compare with what we found earlier?") and scoped quantities. Scoring them
# flagged honest answers and padded the answer-length gate. Every
# answer-dependent smoke gate scores eval_core.answer_body only; the ONE
# extractor is shared with the eval lane (no second regex).
# ---------------------------------------------------------------------------

_SMOKE_FOLLOWUPS = "---FOLLOWUPS---"


def _chipped(body: str, *chips: str) -> str:
    return "\n".join([body, _SMOKE_FOLLOWUPS, *chips])


def test_smoke_uses_the_shared_core_extractor():
    # Same object as the eval lane's — a copy here would be pattern drift.
    from scripts import eval_answers as _ea
    import scripts.smoke_prompts as _sp

    assert _sp._eval_module().answer_body is _ea.eval_core.answer_body
    assert _ea.FOLLOWUPS_MARKER == _SMOKE_FOLLOWUPS


def test_outage_wording_only_in_a_chip_never_signals():
    answer = _chipped(
        "The line bound 1200 units, read from the run records.",
        "Q1: Retry if the endpoint is currently unavailable?")
    assert detect_outage_fallback_signals(answer) == []
    assert strict_failure_reasons(err_labels=[], answer=answer,
                                  system_queries=2,
                                  followup_mode=False) == []
    # In the BODY it still signals (the detector is not weakened).
    assert detect_outage_fallback_signals(
        _chipped("The endpoint is currently unavailable.", "Q1: Retry?")) != []


def test_memory_wording_only_in_a_chip_never_signals():
    answer = _chipped("Three orders closed on that line, per the run records.",
                      "Q1: Compare with what we previously confirmed?")
    assert detect_memory_reference_signals(answer) == []
    assert detect_fabrication_signals(answer) == []
    assert strict_failure_reasons(err_labels=[], answer=answer,
                                  system_queries=2,
                                  followup_mode=False) == []
    assert detect_memory_reference_signals(
        _chipped("As previously confirmed, three orders closed.",
                 "Q1: More?")) != []


def test_answer_length_gate_counts_the_body_not_the_chips():
    # Kills the padding escape: chips are long, the answer is a stub.
    stub = _chipped("Not sure.", "Q1: " + "x" * 200, "Q2: " + "y" * 200)
    res = {"done": True, "errors": [], "answer": stub,
           "summary": {"total_tool_calls": 2, "system_queries": 2}}
    failures = assess("probe", res)
    assert any("answer too short" in f for f in failures)


def test_carveout_fabrication_only_in_a_chip_never_flags():
    chipped_honest = _chipped(
        _HONEST_ABSENCE,
        "Q1: Compare with the 14 overdue maintenance work orders?")
    cv = absence_honesty_carveout(_PROBE_PROMPT, chipped_honest,
                                 _ABSENT_SUMMARY)
    assert cv["engaged"] is True
    assert cv["fabrication_flags"] == []
    assert cv["passed"] is True
    res = {"done": True, "errors": [], "answer": chipped_honest,
           "summary": _ABSENT_SUMMARY}
    assert assess("probe", res, cv) == []
    # Fabrication in the BODY still fails the carve-out.
    body_fab = _chipped(_FABRICATED_ABSENCE, "Q1: More?")
    assert absence_honesty_carveout(
        _PROBE_PROMPT, body_fab, _ABSENT_SUMMARY)["passed"] is False


def _break_extractor(monkeypatch):
    """Make the shared extractor unavailable (dependency/version skew)."""
    def boom():
        raise ImportError("eval_answers unavailable")

    monkeypatch.setattr(smoke_prompts, "_eval_module", boom)


def test_missing_shared_extractor_is_a_hard_scoring_error(monkeypatch):
    """An unavailable extractor must NOT degrade to scoring the full text.

    Returning the full answer here reads as "fail closed" but is not: the
    chips only ever ADD characters, so the answer-length gate gets WEAKER.
    The lane must stop instead of reporting a pass it cannot justify.
    """
    _break_extractor(monkeypatch)
    with pytest.raises(smoke_prompts.ScoringDependencyError):
        smoke_prompts._answer_body(_chipped("Not sure.", "Q1: " + "x" * 200))


def test_chipped_stub_cannot_pass_the_length_gate_without_the_extractor(monkeypatch):
    """Mutation-resistant: an exact reproduction of the gap.

    Body "No." + a 200-char chip measured 224 chars and assessed clean when
    the extractor raised ImportError. Either the gate fails the stub or the
    lane errors — never an empty assessment.
    """
    stub = _chipped("No.", "Q1: " + "R" * 200)
    res = {"done": True, "errors": [], "answer": stub,
           "summary": {"total_tool_calls": 2, "system_queries": 2}}
    # With the extractor present the stub is correctly TOO SHORT.
    assert any("answer too short" in f for f in assess("probe", res))
    _break_extractor(monkeypatch)
    with pytest.raises(smoke_prompts.ScoringDependencyError):
        assess("probe", res)


def test_scoring_dependency_error_is_not_swallowed_as_an_import_error():
    """`except ImportError` handlers must not absorb the scoring error.

    The carve-out fails closed on ImportError; if the hard scoring error were
    an ImportError subclass, a missing extractor would silently reduce to
    "carve-out not satisfied" instead of stopping the lane.
    """
    assert not issubclass(smoke_prompts.ScoringDependencyError, ImportError)


def test_cli_strict_classifiers_receive_body_only_text(monkeypatch):
    """The CLI --strict layer must hand the smoke classifiers the BODY.

    scripts/eval_answers.strict_question_failures is the seam; capturing the
    argument kills a fix applied only inside eval_core (the classifiers are
    smoke-side and would still see the chips).
    """
    from scripts import eval_answers as _ea

    seen = []

    def _capture(*, err_labels, answer, system_queries, followup_mode):
        seen.append(answer)
        return []

    monkeypatch.setattr(_ea.smoke, "strict_failure_reasons", _capture)
    body = "The line bound 1200 units."
    result = {"answer": _chipped(body, "Q1: Retry when unavailable?"),
              "tool_summaries": [], "errors": []}
    _ea.strict_question_failures(result, {"system_queries": 2})
    assert seen == [body]


# ---------------------------------------------------------------------------
# Payload-free /api/ask keepalive comments are transport-only.
# ---------------------------------------------------------------------------

from scripts.smoke_prompts import _consume_ask_sse  # noqa: E402


def _wire_lines(records):
    return [line.encode() for record in records for line in record.splitlines(True)]


def test_keepalive_comments_are_semantically_transparent_to_smoke_scoring():
    semantic = [
        'event: tool_start\n',
        'data: {"tool":"erp_query","tool_id":"t1","input":{}}\n\n',
        'event: tool_result\n',
        'data: {"tool":"erp_query","tool_id":"t1","summary":"3 rows"}\n\n',
        'event: token\n',
        'data: {"text":"grounded answer"}\n\n',
        'event: metadata\n',
        'data: {"intelligence_summary":{"total_tool_calls":1,"system_queries":1}}\n\n',
        'event: metadata\n',
        'data: {"response_integrity":{"verdict":"accepted","mode":"enforce"}}\n\n',
        'event: done\n',
        'data: {"text":"grounded answer"}\n\n',
    ]
    with_comments = [
        semantic[0], semantic[1], ': keepalive\n\n',
        semantic[2], semantic[3], ': keepalive\n\n',
        *semantic[4:8], ': keepalive\n\n', *semantic[8:],
    ]

    expected = _consume_ask_sse(_wire_lines(semantic))
    actual = _consume_ask_sse(_wire_lines(with_comments))

    assert actual == expected
    assert actual == {
        "answer": "grounded answer",
        "summary": {"total_tool_calls": 1, "system_queries": 1},
        "integrity": {"verdict": "accepted", "mode": "enforce"},
        "errors": [], "done": True, "tool_summaries": ["3 rows"],
        "tool_names": ["erp_query"], "tool_starts": 1,
    }


def test_keepalive_comment_neither_masks_error_nor_completes_truncated_stream():
    truncated = [
        'event: error\n',
        'data: {"text":"Query failed."}\n\n',
        ': keepalive\n\n',
    ]
    result = _consume_ask_sse(_wire_lines(truncated))

    assert result["errors"] == ["Query failed."]
    assert result["done"] is False
    reasons = assess("truncated", result)
    assert "stream did not complete (no done event)" in reasons
    assert any(reason.startswith("error event(s):") for reason in reasons)
