# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Sources honesty: a failed call is marked, and a reused result is labelled.

Test event 32 (2026-10-15) showed two Sources problems:

1. Sources listed every call, including the ones that failed. A system agent
   whose every query came back with a service error answered in prose
   ("the hub returned a service error on three attempts"), and the trace
   classified that prose as a success, so nothing marked those chips. The
   engine half of the fix: when every data query a Mode A call ran failed or
   returned no rows, ``_run_mode_a_query`` appends ONE fixed engine line,
   ``[engine] No query this call ran returned data (<categories>).``, and both
   outcome classifiers (``query_intelligence.failure_marker`` here,
   ``arch-store.classifyBase`` in the UI) recognise that exact code-authored
   line anywhere in the result. Chips are marked, never dropped: dropping one
   would renumber the inline citations.

2. A follow-up answered from an earlier turn's results showed no Sources and
   left copied ``[2][5]`` markers as literal text. The earlier results come
   back from chat memory WITHOUT their citation stamps (the stamp is added
   after the session manager stored the message, proved below with the real
   AgentCore Memory session manager), so the model copied numbers from its
   earlier answer text. ``CitationOrdinalHook`` now labels each earlier cited
   result ``[E<k>]`` in memory at the start of every invocation, the runtime
   sends the same labels as ``intelligence_summary.earlier_sources`` (names
   only), and the prompt tells the model to cite them by that label.

All payloads are synthetic and domain-neutral.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import agents.orchestrator as orch
import agents.system_agent as sa
import agents.tool_result_capture as trc
from agents import citation_ordinals as co
from agents import query_intelligence as qi
from agents.citation_ordinals import CitationOrdinalHook, citation_stamp
from agents.public_answer import external_answer_text
from agents.response_integrity_query import bounded_query_result, strict_json_dumps
from agents.tool_result_summary import summarize_tool_result
from tests.unit.test_mode_a_result_cut_line import _run_query, _state_after
from tests.unit.test_orchestrator_output_cap_recovery import (
    _FakeMemoryPlane, _answer, _cut_in_text, _errors, _run, _tool_call,
)

_FIXTURE = (__import__("pathlib").Path(__file__).resolve().parents[2] / "frontend" / "src"
            / "test" / "fixtures" / "tool-result-outcome-cases.json")


@pytest.fixture(autouse=True)
def _report_only(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)


@pytest.fixture(autouse=True)
def _without_filters_line(monkeypatch):
    # This module is about the no-data line; the executed-filters line (its own
    # tests in test_executed_filters_line.py) would append to every fixture
    # call whose SQL carries a WHERE.
    monkeypatch.setattr(sa, "_executed_filters_fact", lambda state: "")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _sql(rows: list, *, sql: str = "SELECT a FROM t") -> dict:
    body = {**bounded_query_result(["a"], rows, truncated=False), "_adt_executed_query": sql}
    return {"toolUseId": "t-1", "status": "success",
            "content": [{"text": strict_json_dumps(body)}]}


def _transport_error() -> dict:
    return {"toolUseId": "t-1", "status": "error",
            "content": [{"text": "HTTP 503 from the upstream service"}]}


def _app_error() -> dict:
    return {"toolUseId": "t-1", "status": "success",
            "content": [{"text": json.dumps({"error": "upstream refused the request"})}]}


def _prose_result() -> dict:
    """A result the capture cannot read a shape from (honestly 'unknown')."""
    return {"toolUseId": "t-1", "status": "success",
            "content": [{"text": "a long free-text report the tool wrote"}]}


def _shape_only_error() -> dict:
    """Entries with no value or points: the capture calls it ``sitewise_error``
    from the shape alone, which a complete record can share."""
    return {"toolUseId": "t-1", "status": "success",
            "content": [{"text": json.dumps({"op-1": {"status": "partial", "note": "x"}})}]}


#: A real business record whose OWN status field says failed (a check that
#: failed), returned successfully. The capture used to call it ``tool_error``
#: and the call got the no-data line.
_FAILED_CHECK_RECORD = {"record_id": "R-1", "status": "failed",
                        "measurement": 12.3, "limit": 10.0}
_FAILED_CHECK_ANSWER = "Record R-1 failed its check: 12.3 against a limit of 10.0."


def _business_record(payload=None) -> dict:
    return {"toolUseId": "t-1", "status": "success",
            "content": [{"text": json.dumps(payload or _FAILED_CHECK_RECORD)}]}


def _status_envelope() -> dict:
    """An error envelope: a failed status with only envelope fields."""
    return {"toolUseId": "t-1", "status": "success",
            "content": [{"text": json.dumps({"status": "failed",
                                             "message": "upstream refused the request"})}]}


_PROSE = "The source returned a service error on each attempt, so no values came back."


def _no_data_line(*labels: str) -> str:
    return f"[engine] No query this call ran returned data ({', '.join(labels)})."


# --------------------------------------------------------------------------- #
# 1. The engine no-data line on a Mode A answer
# --------------------------------------------------------------------------- #
def test_the_exact_line_text_is_fixed():
    assert qi.no_data_line(["transport_error", "tool_error"]) == _no_data_line(
        "transport_error", "tool_error")
    assert qi.no_data_line([]) == ""


def test_a_call_whose_every_query_failed_carries_the_line():
    state = _state_after(_transport_error(), _app_error())
    answer = _run_query(_PROSE, state)
    assert answer == _PROSE + "\n\n" + _no_data_line("transport_error", "tool_error")
    assert answer.count("[engine]") == 1


def test_a_call_whose_every_query_returned_no_rows_says_no_rows():
    state = _state_after(_sql([]), _sql([]))
    assert _run_query("Nothing matched.", state) == (
        "Nothing matched.\n\n" + _no_data_line("no rows"))


def test_failed_and_empty_attempts_list_both():
    state = _state_after(_app_error(), _sql([]))
    assert _run_query("Nothing came back.", state).endswith(
        _no_data_line("tool_error", "no rows"))


@pytest.mark.parametrize("results", [
    (_sql([[1]]),),
    (_transport_error(), _sql([[1], [2]])),
    (_sql([]), _sql([[1]])),
    (_prose_result(),),
    (_shape_only_error(),),
    (),
])
def test_a_call_that_may_have_data_is_byte_identical(results):
    state = _state_after(*results)
    text = "Two rows.\n\n| a |\n|---|\n| 1 |\n"
    assert _run_query(text, state) == text


def test_rows_seen_by_the_sidecar_rule_out_the_line():
    # The capture keeps at most four results; a data-bearing one can be
    # pushed out by later empty ones. The sidecar rows still saw its rows.
    state = _state_after(*[_sql([]) for _ in range(4)])
    state[trc.SIDECAR_STATEMENTS_STATE_KEY].insert(0, {
        "query_text": "SELECT a FROM t", "table": "t", "columns": ["a"],
        "row_count": 3, "queried_at": "2026-01-01T00:00:00Z"})
    assert _run_query("Answer.", state) == "Answer."


#: Catalog probes a system agent runs to look at a table's columns: they
#: return rows, but those rows are schema metadata, not data.
_PROBES = [
    "SELECT column_name FROM information_schema.columns WHERE table_name = 'runs'",
    "SELECT relname FROM pg_catalog.pg_class WHERE relkind = 'r'",
]


@pytest.mark.parametrize("probe", _PROBES)
def test_a_catalog_probe_does_not_hide_that_the_data_queries_found_nothing(probe):
    """Walk 2026-10-19: a Sources chip had no "no data" although the call
    found no record. A catalog probe's rows counted as data, so the call's
    empty data queries did not get the no-data line."""
    state = _state_after(_sql([["c1"], ["c2"]], sql=probe),
                         _sql([], sql="SELECT a FROM runs WHERE k = 'x'"))
    assert _run_query("No record matched.", state) == (
        "No record matched.\n\n" + _no_data_line("no rows"))


@pytest.mark.parametrize("probe", _PROBES)
def test_a_catalog_probe_beside_a_data_query_with_rows_stays_byte_identical(probe):
    state = _state_after(_sql([["c1"]], sql=probe),
                         _sql([[1]], sql="SELECT a FROM ledger WHERE k = 'y'"))
    assert _run_query("One row.", state) == "One row."


@pytest.mark.parametrize("probe", _PROBES)
def test_a_call_that_only_probed_the_catalog_gets_no_line(probe):
    """No data query ran, so there is no data query to call empty."""
    state = _state_after(_sql([["c1"]], sql=probe))
    assert _run_query("Here are the columns.", state) == "Here are the columns."


# The capture keeps TOOL_RESULT_CAPTURE_MAX summaries, so
# catalog probes after an empty data query can push its summary out. The
# sidecar still records every statement (bounded), and decides then.
_EMPTY_DATA = "SELECT a FROM runs WHERE k = 'x'"


def _probes(probe, n=trc.TOOL_RESULT_CAPTURE_MAX):
    return [_sql([["c1"], ["c2"]], sql=probe) for _ in range(n)]


@pytest.mark.parametrize("probe", _PROBES)
def test_probes_that_push_out_the_empty_data_query_still_get_the_line(probe):
    state = _state_after(_sql([], sql=_EMPTY_DATA), *_probes(probe))
    # Premise: the empty data query's summary is gone, its sidecar row is not.
    summaries = state[trc.TOOL_RESULTS_STATE_KEY]
    assert len(summaries) == trc.TOOL_RESULT_CAPTURE_MAX
    assert all(item.get(trc.DISCOVERY_FLAG) for item in summaries)
    assert _run_query("No record matched.", state) == (
        "No record matched.\n\n" + _no_data_line("no rows"))


@pytest.mark.parametrize("probe", _PROBES)
def test_pushed_out_data_query_with_rows_keeps_the_answer_byte_identical(probe):
    state = _state_after(_sql([], sql=_EMPTY_DATA),
                         _sql([[1]], sql="SELECT b FROM ledger WHERE k = 'y'"),
                         *_probes(probe))
    assert _run_query("One row.", state) == "One row."


@pytest.mark.parametrize("probe", _PROBES)
def test_pushed_out_data_query_with_an_unknown_count_gets_no_line(probe):
    state = _state_after(_sql([], sql=_EMPTY_DATA), *_probes(probe))
    state[trc.SIDECAR_STATEMENTS_STATE_KEY].insert(0, {
        "query_text": "SELECT b FROM ledger", "table": "ledger", "columns": ["b"],
        "row_count": None, "queried_at": "2026-01-01T00:00:00Z"})
    assert _run_query("Answer.", state) == "Answer."


@pytest.mark.parametrize("probe", _PROBES)
def test_a_full_sidecar_is_not_proof_that_nothing_matched(probe):
    """The sidecar keeps a bounded number of statements; once it is full a
    later data query may be missing from it, so it cannot prove absence."""
    state = _state_after(_sql([], sql=_EMPTY_DATA), *_probes(probe))
    rows = state[trc.SIDECAR_STATEMENTS_STATE_KEY]
    while len(rows) < trc._MAX_SIDECAR_STATEMENTS:
        rows.append(dict(rows[-1]))
    assert _run_query("Answer.", state) == "Answer."


def test_a_retained_data_summary_still_decides_with_its_own_categories():
    """When a data query's summary is still retained, the summaries decide
    exactly as before, failure categories included."""
    state = _state_after(_app_error(), _sql([], sql=_EMPTY_DATA))
    assert _run_query("Nothing came back.", state).endswith(
        _no_data_line("tool_error", "no rows"))


def test_enforce_mode_json_envelope_is_untouched(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    envelope = json.dumps({"answer": "gated", "claims": [], "evidence": []})
    assert _run_query(envelope, _state_after(_transport_error())) == envelope


def test_a_json_answer_carries_the_line_as_the_reserved_member():
    answer = _run_query(json.dumps({"answer": "none"}), _state_after(_app_error()))
    assert json.loads(answer)[trc.RESULT_CUT_NOTE_KEY] == _no_data_line("tool_error")


def test_the_engine_facts_function_stays_inside_the_50_line_gate():
    import inspect
    lines = inspect.getsource(sa._append_engine_facts).splitlines()
    assert len(lines) <= 50, len(lines)


# --------------------------------------------------------------------------- #
# 2. Both classifiers recognise the line; nothing else trips on it
# --------------------------------------------------------------------------- #
def test_the_line_classifies_the_result_anywhere_in_it():
    failed = _PROSE + "\n\n" + _no_data_line("transport_error")
    assert qi.failure_marker(failed) == ("error", "no query returned data")
    empty = "Nothing matched.\n\n" + _no_data_line("no rows")
    assert qi.failure_marker(empty) == ("empty", "no query returned data")
    long_answer = ("The source was asked for every reading in the window. " * 12
                   + "\n\n" + _no_data_line("query_timeout"))
    assert long_answer.index("[engine]") > qi.ENVELOPE_HEAD_CHARS
    assert qi.is_failed_result(long_answer)
    # The chat client scans str(event.result): newlines escaped inside a repr.
    assert qi.is_failed_result(str({"status": "success", "content": [{"text": failed}]}))
    # A JSON answer carries it in the reserved member.
    assert qi.is_failed_result(json.dumps({"answer": "x",
                                           trc.RESULT_CUT_NOTE_KEY: _no_data_line("tool_error")}))


def test_the_line_beats_a_table_shaped_answer():
    # Code wrote the line about the call's own results; a table the model
    # wrote anyway cannot hide it.
    text = "| a |\n|---|\n| 1 |\n\n" + _no_data_line("tool_error")
    assert qi.failure_marker(text) == ("error", "no query returned data")


def test_other_engine_lines_and_lookalikes_do_not_classify():
    assert qi.failure_marker("We found data.\n\n[engine] No query this call ran.") is None
    assert qi.failure_marker("[engine] No query this call ran returned data (Tool Error).") is None


def test_the_line_is_never_a_retry_signal():
    line = _no_data_line("transport_error", "query_timeout", "tool_error")
    result = {"status": "success", "content": [{"text": _PROSE + "\n\n" + line}]}
    assert orch._retry_backoffs_for(result) is None


def test_the_trace_summary_keeps_the_line_for_long_prose_and_json_answers():
    line = _no_data_line("transport_error")
    prose = "The source was asked again with a narrower window. " * 60 + "\n\n" + line
    assert len(prose) > 2000
    summary = summarize_tool_result("sys-a___query_sys_a", {"content": [{"text": prose}]})
    assert summary.endswith(line)
    note = json.dumps({"answer": "none", trc.RESULT_CUT_NOTE_KEY: line})
    summary = summarize_tool_result("sys-a___query_sys_a", {"content": [{"text": note}]})
    assert summary.endswith(line)
    plain = summarize_tool_result("sys-a___query_sys_a", {"content": [{"text": "Short."}]})
    assert plain == "Short."


def _fixture_case(case_id: str) -> dict:
    cases = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]
    return next(c for c in cases if c["id"] == case_id)


def test_the_shared_fixture_pins_the_engine_output_it_classifies():
    failed = _fixture_case("mode-a-prose-with-no-data-line")
    assert failed["text"] == _run_query(_PROSE, _state_after(_transport_error()))
    assert failed["ui"] == "failed" and failed["server"] is True
    empty = _fixture_case("mode-a-prose-with-no-rows-line")
    assert empty["text"] == _run_query("Nothing matched.", _state_after(_sql([])))
    assert empty["ui"] == "empty" and empty["server"] is True
    beyond = _fixture_case("mode-a-long-prose-with-no-data-line")
    assert beyond["text"].index("[engine]") > qi.ENVELOPE_HEAD_CHARS
    assert beyond["ui"] == "failed" and beyond["server"] is True


# --------------------------------------------------------------------------- #
# 2b. A business status is not a failed query
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("payload", [
    _FAILED_CHECK_RECORD,
    {**_FAILED_CHECK_RECORD, "status": "error"},
    {**_FAILED_CHECK_RECORD, "status": "FAILURE"},
    [_FAILED_CHECK_RECORD, {**_FAILED_CHECK_RECORD, "record_id": "R-2"}],
    [{"status": "failed", "message": "refused"}, _FAILED_CHECK_RECORD],
])
def test_a_record_whose_business_status_says_failed_is_byte_identical(payload):
    state = _state_after(_business_record(payload))
    assert _run_query(_FAILED_CHECK_ANSWER, state) == _FAILED_CHECK_ANSWER


def test_a_business_record_beside_a_real_failure_still_gets_no_line():
    state = _state_after(_transport_error(), _business_record())
    assert _run_query(_FAILED_CHECK_ANSWER, state) == _FAILED_CHECK_ANSWER


@pytest.mark.parametrize("result,label", [
    (_status_envelope(), "tool_error"),
    (_business_record({"error": "upstream refused", "record_id": "R-1"}), "tool_error"),
    (_business_record({"error_type": "invalid_request", "record_id": "R-1"}),
     "invalid_request"),
    (_business_record([{"status": "failed", "message": "a"}, {"error": "b"}]), "tool_error"),
])
def test_a_structural_failure_still_carries_the_line(result, label):
    state = _state_after(result)
    assert _run_query(_PROSE, state) == _PROSE + "\n\n" + _no_data_line(label)


def _metadata(result) -> dict:
    return trc.safe_tool_result_metadata(result, tool_name="q", max_chars=1024)


def test_the_capture_gives_the_record_one_success_verdict():
    """One closed verdict at capture time: the record is usable data in the
    capture metadata itself and in the public metadata that crosses the
    system-agent boundary, not an error carrying a private exception bit."""
    record = _metadata(_business_record())
    assert record == {"tool": "q", "transport_status": "success",
                      "app_status": "success", "_has_success": True}
    assert trc._public_metadata([record]) == [
        {"tool": "q", "transport_status": "success", "app_status": "success"}]
    envelope = _metadata(_status_envelope())
    assert envelope["app_status"] == "error" and envelope["safe_error_category"] == "tool_error"


def test_a_list_of_records_is_success_and_an_envelope_beside_one_is_partial():
    records = _metadata(_business_record([_FAILED_CHECK_RECORD,
                                          {**_FAILED_CHECK_RECORD, "record_id": "R-2"}]))
    assert (records["app_status"], records["success_count"], records["error_count"]) == (
        "success", 2, 0)
    mixed = _metadata(_business_record([{"status": "failed", "message": "refused"},
                                        _FAILED_CHECK_RECORD]))
    assert (mixed["app_status"], mixed["success_count"], mixed["error_count"]) == (
        "partial", 1, 1)
    keyed = _metadata(_business_record({"R-1": _FAILED_CHECK_RECORD,
                                        "R-2": {"error": "unreadable"}}))
    assert keyed["app_status"] != "error"


def _no_final_text(stop_reason: str = "end_turn"):
    return SimpleNamespace(message={"role": "assistant", "content": []},
                           structured_output=None, interrupts=None, stop_reason=stop_reason)


def _fallback(state: dict, stop_reason: str) -> tuple[dict, str]:
    envelope = trc.extract_mode_a_result(_no_final_text(stop_reason), state)
    return json.loads(envelope), sa._plain_language_incomplete(envelope)


def test_the_no_final_text_fallback_counts_the_record_as_completed_work():
    envelope, line = _fallback(_state_after(_business_record()), "end_turn")
    assert envelope["status"] == "partial"
    assert envelope["tool_results"] == [
        {"tool": "query_public", "transport_status": "success", "app_status": "success"}]
    assert "all failed" not in line
    assert line.startswith("This system completed 1 of 1 data queries")


def test_the_output_cap_fallback_counts_the_record_as_completed_work():
    envelope, line = _fallback(_state_after(_business_record()), "max_tokens")
    assert envelope["status"] == "partial"
    assert envelope["message"] == trc.OUTPUT_LIMIT_PARTIAL_MESSAGE
    assert line == f"{trc.OUTPUT_LIMIT_PARTIAL_MESSAGE} {trc.OUTPUT_LIMIT_GUIDANCE}"


# --------------------------------------------------------------------------- #
# 2c. A diagnostic error envelope stays a structural failure
# --------------------------------------------------------------------------- #
#: Error envelopes carrying the request, trace and HTTP diagnostics vendor and
#: MCP errors commonly add. Every field is in the capture's closed envelope
#: list, so none of them makes the payload a business record.
_DIAGNOSTIC_ENVELOPES = {
    "request-id": {"status": "error", "message": "upstream refused the request",
                   "request_id": "req-1"},
    "http-status": {"status": "error", "message": "upstream refused the request",
                    "http_status": 503},
    "camel-case-ids": {"status": "failed", "detail": "upstream refused the request",
                       "requestId": "req-1", "traceId": "t-1", "statusCode": 502},
    "retry-and-correlation": {"status": "failure", "reason": "busy", "retry_after": 30,
                              "correlation_id": "c-1", "code": "THROTTLED"},
    "problem-details": {"type": "about:blank", "title": "Bad Gateway", "status": "error",
                        "detail": "upstream refused the request", "instance": "/q"},
    "service-error": {"__type": "ServiceUnavailableException", "status": "error",
                      "message": "x", "x-amzn-RequestId": "r-1", "timestamp": "2026-01-01"},
}


@pytest.mark.parametrize("case", sorted(_DIAGNOSTIC_ENVELOPES))
def test_a_diagnostic_envelope_carries_the_no_data_line(case):
    state = _state_after(_business_record(_DIAGNOSTIC_ENVELOPES[case]))
    answer = _run_query(_PROSE, state)
    assert answer == _PROSE + "\n\n" + _no_data_line("tool_error")
    assert qi.failure_marker(answer) == ("error", "no query returned data")


@pytest.mark.parametrize("case", sorted(_DIAGNOSTIC_ENVELOPES))
def test_a_diagnostic_envelope_is_an_error_in_the_capture_and_both_fallbacks(case):
    result = _business_record(_DIAGNOSTIC_ENVELOPES[case])
    assert _metadata(result)["app_status"] == "error"
    envelope, line = _fallback(_state_after(result), "end_turn")
    assert envelope["status"] == "error"
    assert line.startswith("This system attempted 1 data query, but all failed")
    envelope, line = _fallback(_state_after(result), "max_tokens")
    assert envelope["message"] == trc.OUTPUT_LIMIT_FAILED_MESSAGE
    assert "Failed attempts: tool_error." in line


def test_a_record_carrying_diagnostics_is_still_a_record():
    record = {**_FAILED_CHECK_RECORD, "request_id": "req-1", "http_status": 200}
    state = _state_after(_business_record(record))
    assert _run_query(_FAILED_CHECK_ANSWER, state) == _FAILED_CHECK_ANSWER
    assert _metadata(_business_record(record))["app_status"] == "success"


def test_the_shared_fixture_pins_the_diagnostic_envelope_answers():
    for case_id, case in (("mode-a-envelope-with-request-id", "request-id"),
                          ("mode-a-envelope-with-http-status", "http-status")):
        pinned = _fixture_case(case_id)
        state = _state_after(_business_record(_DIAGNOSTIC_ENVELOPES[case]))
        assert pinned["text"] == _run_query(_PROSE, state)
        assert pinned["ui"] == "failed" and pinned["server"] is True
        assert not pinned.get("divergent")


# --------------------------------------------------------------------------- #
# 2d. Generic field names never establish an envelope by themselves
# --------------------------------------------------------------------------- #
#: Business events whose every field also reads as a generic diagnostic name.
#: A failure word in status plus only such weak fields is ambiguous, and an
#: ambiguous shape is a record: usable data, never "no query returned data".
_WEAK_ONLY_EVENTS = {
    "quality-event": ({"status": "failed", "code": "QC-9", "description": "failed check",
                       "timestamp": "2026-01-01"},
                      "Quality event QC-9 failed its check on 2026-01-01."),
    "titled-check": ({"status": "failed", "title": "Bore check", "type": "dimensional",
                      "path": "/area-1/station-2"},
                     "The dimensional bore check at /area-1/station-2 failed."),
    "help-and-hint": ({"status": "error", "instance": "run-7", "hint": "rework",
                       "help": "see procedure 4", "retry_after": 30, "request_id": "r-1"},
                      "Run run-7 is in error; the procedure says rework."),
    "bare-status": ({"status": "failed"}, "The check reports failed."),
}


@pytest.mark.parametrize("case", sorted(_WEAK_ONLY_EVENTS))
def test_a_status_with_only_weak_fields_is_a_record(case):
    payload, answer = _WEAK_ONLY_EVENTS[case]
    result = _business_record(payload)
    assert _metadata(result)["app_status"] == "success"
    assert _run_query(answer, _state_after(result)) == answer
    envelope, line = _fallback(_state_after(result), "end_turn")
    assert envelope["status"] == "partial" and "all failed" not in line
    envelope, _line = _fallback(_state_after(result), "max_tokens")
    assert envelope["message"] == trc.OUTPUT_LIMIT_PARTIAL_MESSAGE


#: Each has a failure word in status and ONE strong signal; every other field
#: is weak envelope vocabulary, which never turns it into a record.
_STRONG_ENVELOPES = {
    "message-plus-every-weak-field": {
        "status": "error", "message": "upstream refused", "code": "E1", "description": "d",
        "type": "t", "title": "T", "instance": "/i", "path": "/p", "timestamp": "t",
        "hint": "h", "help": "h", "retry_after": 3, "request_id": "r", "trace_id": "t"},
    "http-status-only": {"status": "error", "http_status": 503, "request_id": "r-1"},
    "status-code-as-text": {"status": "failed", "statusCode": "502", "code": "BAD_GATEWAY"},
    "is-error-flag": {"status": "error", "isError": True, "code": "E2"},
    "errors-list": {"status": "failed", "errors": ["upstream refused"], "type": "t"},
    "exception-name": {"status": "error", "exception": "TimeoutError", "timestamp": "t"},
    "fault-string": {"status": "failure", "faultstring": "server busy"},
}


@pytest.mark.parametrize("case", sorted(_STRONG_ENVELOPES))
def test_a_status_with_a_strong_signal_is_an_error(case):
    result = _business_record(_STRONG_ENVELOPES[case])
    assert _metadata(result)["app_status"] == "error"
    assert _run_query(_PROSE, _state_after(result)) == _PROSE + "\n\n" + _no_data_line(
        "tool_error")


@pytest.mark.parametrize("payload", [
    {"status": "failed", "http_status": 200, "code": "X"},
    {"status": "failed", "errors": [], "message": ""},
    {"status": "error", "isError": False, "description": "d"},
])
def test_an_empty_or_low_strong_field_is_no_signal(payload):
    assert _metadata(_business_record(payload))["app_status"] == "success"


def test_a_strong_signal_beside_a_business_field_is_ambiguous_so_a_record():
    record = {"status": "failed", "message": "diameter out of tolerance", "measurement": 12.3}
    answer = "The part failed: diameter out of tolerance at 12.3."
    assert _run_query(answer, _state_after(_business_record(record))) == answer
    assert _metadata(_business_record(record))["app_status"] == "success"


@pytest.mark.parametrize("payload,category", [
    ({"error": "tool_unavailable", "error_type": "tool_error"}, "tool_error"),
    ({"error": "No asset matched", "known_assets": ["a"], "error_type": "not_found"},
     "tool_error"),
    ({"status": "error", "error": "'question' is required"}, "tool_error"),
    ({"status": "error", "message": "HighByte URL not configured"}, "tool_error"),
])
def test_our_own_tool_error_shapes_stay_errors(payload, category):
    """The shapes the system agent's own tools and the query bridge return
    (``{"error", "error_type"}``, ``{"status": "error", "error"}``) and the
    plain ``{"status": "error", "message"}`` envelope."""
    meta = _metadata(_business_record(payload))
    assert meta["app_status"] == "error" and meta["safe_error_category"] == category


def test_an_mcp_error_result_like_a_hub_503_stays_a_transport_failure():
    result = {"toolUseId": "t-1", "status": "error",
              "content": [{"text": "HTTP 503 Service Unavailable"}]}
    assert _run_query(_PROSE, _state_after(result)) == _PROSE + "\n\n" + _no_data_line(
        "transport_error")
    hub_body = _business_record({"status": "error", "message": "Service Unavailable",
                                 "code": 503})
    assert _metadata(hub_body)["app_status"] == "error"


def test_the_shared_fixture_pins_the_weak_field_events():
    for case_id, case in (("mode-a-business-event-weak-fields", "quality-event"),
                          ("mode-a-business-event-titled", "titled-check")):
        payload, answer = _WEAK_ONLY_EVENTS[case]
        pinned = _fixture_case(case_id)
        assert pinned["text"] == _run_query(answer, _state_after(_business_record(payload)))
        assert pinned["ui"] == "success" and pinned["server"] is False
    for case_id, case in (("business-event-weak-fields", "quality-event"),
                          ("business-event-titled", "titled-check")):
        pinned = _fixture_case(case_id)
        assert pinned["text"] == json.dumps(_WEAK_ONLY_EVENTS[case][0])
        assert pinned["ui"] == "success" and pinned["server"] is False


def test_both_classifiers_say_success_for_the_failed_check_record():
    raw = json.dumps(_FAILED_CHECK_RECORD)
    assert qi.failure_marker(raw) is None
    answer = _run_query(_FAILED_CHECK_ANSWER, _state_after(_business_record()))
    assert qi.failure_marker(answer) is None
    prose = _fixture_case("mode-a-business-record-status-failed")
    assert prose["text"] == answer
    assert prose["ui"] == "success" and prose["server"] is False
    record = _fixture_case("business-record-status-failed")
    assert record["text"] == raw
    assert record["ui"] == "success" and record["server"] is False


# --------------------------------------------------------------------------- #
# 3. Earlier results are labelled [E<k>]
# --------------------------------------------------------------------------- #
def _use(tool_id, name):
    return {"toolUse": {"toolUseId": tool_id, "name": name, "input": {}}}


def _result(tool_id, content):
    return {"toolResult": {"toolUseId": tool_id, "status": "success", "content": content}}


def _turn(question, uses, results, answer):
    return [{"role": "user", "content": [{"text": question}]},
            {"role": "assistant", "content": uses},
            {"role": "user", "content": results},
            {"role": "assistant", "content": [{"text": answer}]}]


def _start(hook, messages, incoming="next question"):
    hook._on_invocation_start(SimpleNamespace(
        agent=SimpleNamespace(messages=messages),
        messages=[{"role": "user", "content": [{"text": incoming}]}]))


def _add(hook, content):
    message = {"role": "assistant" if "toolUse" in content[0] else "user", "content": content}
    hook._on_message_added(SimpleNamespace(message=message, agent=SimpleNamespace()))
    return message


def _content(messages, tool_id):
    for message in messages:
        for block in message["content"]:
            if "toolResult" in block and block["toolResult"]["toolUseId"] == tool_id:
                return block["toolResult"]["content"]
    raise AssertionError(tool_id)


def test_an_earlier_result_is_labelled_and_this_turn_still_starts_at_one():
    history = _turn("q1", [_use("a1", "sys-a___query_sys_a")],
                    [_result("a1", [{"text": "rows"}])], "answer [1]")
    hook = CitationOrdinalHook()
    _start(hook, history)
    assert _content(history, "a1") == [{"text": "rows"}, {"text": co.earlier_stamp(1)}]
    assert co.earlier_stamp(1) == (
        "[engine] Returned for an earlier question in this chat. Cite it as [E1].")
    _add(hook, [_use("b1", "sys-b___query_sys_b")])
    message = _add(hook, [_result("b1", [{"text": "new rows"}])])
    assert message["content"][0]["toolResult"]["content"][-1] == {"text": citation_stamp(1)}


def test_an_existing_stamp_is_replaced_not_duplicated():
    history = (_turn("q1", [_use("a1", "sys-a___query_sys_a")],
                     [_result("a1", [{"text": "rows"}, {"text": citation_stamp(2)}])], "x")
               + _turn("q2", [_use("b1", "sys-b___query_sys_b")],
                       [_result("b1", "rows here\n\n" + citation_stamp(1))], "y"))
    hook = CitationOrdinalHook()
    _start(hook, history)
    assert _content(history, "a1") == [{"text": "rows"}, {"text": co.earlier_stamp(1)}]
    assert _content(history, "b1") == "rows here\n\n" + co.earlier_stamp(2)
    # The next invocation (a later question on the same in-memory agent)
    # replaces the earlier labels again rather than stacking them.
    _start(hook, history)
    assert _content(history, "a1") == [{"text": "rows"}, {"text": co.earlier_stamp(1)}]
    assert _content(history, "b1") == "rows here\n\n" + co.earlier_stamp(2)


def test_the_graph_an_orphan_result_and_unstampable_content_are_not_labelled():
    history = _turn("q1", [_use("g1", "explore_graph"), _use("a1", "sys-a___query_sys_a"),
                           _use("d1", "sys-d___query_sys_d")],
                    [_result("g1", [{"text": "graph"}]), _result("a1", [{"text": "rows"}]),
                     _result("d1", {"json": {"rows": 1}}), _result("zz", [{"text": "orphan"}])],
                    "answer")
    hook = CitationOrdinalHook()
    _start(hook, history)
    assert _content(history, "g1") == [{"text": "graph"}]
    assert _content(history, "zz") == [{"text": "orphan"}]
    assert _content(history, "d1") == {"json": {"rows": 1}}
    assert co.earlier_sources(history) == [{"label": "E1", "tool": "sys-a___query_sys_a"}]


def test_only_the_most_recent_earlier_results_are_labelled():
    history = []
    for i in range(10):
        history += _turn(f"q{i}", [_use(f"a{i}", "sys-a___query_sys_a")],
                         [_result(f"a{i}", [{"text": "rows"}, {"text": citation_stamp(1)}])],
                         "answer [1]")
    hook = CitationOrdinalHook()
    _start(hook, history)
    # The two oldest lose their stale stamp and get no label.
    assert _content(history, "a0") == [{"text": "rows"}]
    assert _content(history, "a1") == [{"text": "rows"}]
    assert _content(history, "a2") == [{"text": "rows"}, {"text": co.earlier_stamp(1)}]
    assert _content(history, "a9") == [{"text": "rows"}, {"text": co.earlier_stamp(8)}]
    assert [e["label"] for e in co.earlier_sources(history)] == [f"E{k}" for k in range(1, 9)]


def test_an_output_cap_continuation_does_not_relabel_this_turns_results():
    from agents.output_cap_recovery import RECOVERY_NOTICE
    history = _turn("q1", [_use("a1", "sys-a___query_sys_a")],
                    [_result("a1", [{"text": "rows"}])], "answer [1]")
    hook = CitationOrdinalHook()
    _start(hook, history)
    history += [{"role": "user", "content": [{"text": "q2"}]}]
    _add(hook, [_use("b1", "sys-b___query_sys_b")])
    current = _add(hook, [_result("b1", [{"text": "new rows"}])])
    history += [{"role": "assistant", "content": [_use("b1", "sys-b___query_sys_b")]}, current]
    _start(hook, history, incoming=RECOVERY_NOTICE)
    assert current["content"][0]["toolResult"]["content"][-1] == {"text": citation_stamp(1)}
    assert _content(history, "a1")[-1] == {"text": co.earlier_stamp(1)}


def test_a_call_after_an_output_cap_continuation_keeps_the_turns_count():
    """The output-cap continuation is the same turn, so its next
    cited call is the turn's [2], the number the client's chip counter gives
    it, never a second [1]."""
    from agents.output_cap_recovery import RECOVERY_NOTICE
    hook = CitationOrdinalHook()
    history = [{"role": "user", "content": [{"text": "q"}]}]
    _start(hook, history)
    _add(hook, [_use("a1", "sys-a___query_sys_a")])
    first = _add(hook, [_result("a1", [{"text": "rows"}])])
    _start(hook, history, incoming=RECOVERY_NOTICE)
    _add(hook, [_use("b1", "sys-b___query_sys_b")])
    second = _add(hook, [_result("b1", [{"text": "more rows"}])])
    assert first["content"][0]["toolResult"]["content"][-1] == {"text": citation_stamp(1)}
    assert second["content"][0]["toolResult"]["content"][-1] == {"text": citation_stamp(2)}
    # A NEW question still restarts the count.
    _start(hook, history, incoming="next question")
    _add(hook, [_use("c1", "sys-a___query_sys_a")])
    third = _add(hook, [_result("c1", [{"text": "rows"}])])
    assert third["content"][0]["toolResult"]["content"][-1] == {"text": citation_stamp(1)}


def test_a_result_from_a_turn_that_never_answered_is_labelled_too():
    """A turn cut off after its call returned (no answer text) leaves its
    call and result in history unless the history read rule drops it; what
    stays is labelled like any earlier result. A call whose result was never
    stored has nothing to cite and gets no label."""
    history = [{"role": "user", "content": [{"text": "q1"}]},
               {"role": "assistant", "content": [_use("a1", "sys-a___query_sys_a")]},
               {"role": "user", "content": [_result("a1", [{"text": "rows"}])]},
               {"role": "user", "content": [{"text": "q2"}]},
               {"role": "assistant", "content": [_use("b1", "sys-b___query_sys_b")]}]
    hook = CitationOrdinalHook()
    _start(hook, history)
    assert _content(history, "a1") == [{"text": "rows"}, {"text": co.earlier_stamp(1)}]
    assert co.earlier_sources(history) == [{"label": "E1", "tool": "sys-a___query_sys_a"}]


def test_the_runtime_stamps_a_post_cut_call_with_its_sources_chip_number(monkeypatch):
    """The real runtime path: one cited call, a reply cut at the output cap,
    the one continuation, a second cited call. The model reads [2] on the
    second result, and the provenance record for it (the server mirror of the
    client's per-turn chip counter, D2) is number 2 too."""
    events, model = _run(monkeypatch, [
        _tool_call("t-1", "first question"), _cut_in_text("partial"),
        _tool_call("t-2", "second question"), _answer("Both [1][2]."),
    ])
    assert _errors(events) == []
    seen = model.calls[-1]
    stamps = {block["toolResult"]["toolUseId"]: block["toolResult"]["content"][-1]
              for message in seen for block in message["content"]
              if isinstance(block, dict) and "toolResult" in block}
    assert stamps == {"t-1": {"text": citation_stamp(1)}, "t-2": {"text": citation_stamp(2)}}
    records = next(e["provenance"]["records"] for e in events
                   if isinstance(e, dict) and "provenance" in e)
    assert {r["tool_id"]: r["citation_num"] for r in records} == {"t-1": 1, "t-2": 2}


def test_a_bad_history_never_raises():
    hook = CitationOrdinalHook()
    hook._on_invocation_start(SimpleNamespace(agent=None, messages=None))
    hook._on_invocation_start(SimpleNamespace(agent=SimpleNamespace(messages=[None, 3, {}]),
                                              messages=[]))
    assert co.earlier_sources(None) == []
    assert co.earlier_sources([{"role": "user", "content": "text"}]) == []


# --------------------------------------------------------------------------- #
# 4. The real runtime + the real AgentCore Memory session manager
# --------------------------------------------------------------------------- #
def _summary(events):
    return next(e["intelligence_summary"] for e in events
                if isinstance(e, dict) and "intelligence_summary" in e)


def test_a_follow_up_sees_the_earlier_result_labelled_and_the_summary_names_it(monkeypatch):
    plane = _FakeMemoryPlane()
    first, _ = _run(monkeypatch, [_tool_call("t-1", "which records failed"),
                                  _answer("Three failed [1].")],
                    session_id="s-mem", plane=plane)
    assert _errors(first) == []
    assert "earlier_sources" not in _summary(first)
    # Premise (event 32's cause): the stored copy carries no stamp at all.
    stored_result = plane.stored_messages()[2]["content"][0]["toolResult"]["content"]
    assert stored_result == [{"text": "3 records matched."}]
    monkeypatch.undo()
    second, model = _run(monkeypatch, [_tool_call("t-2", "which are open"),
                                       _answer("Still open [E1], newly checked [1].")],
                         session_id="s-mem", plane=plane, prompt="which are still open?")
    assert _errors(second) == []
    seen = model.calls[0]
    assert seen[2]["content"][0]["toolResult"]["content"] == [
        {"text": "3 records matched."}, {"text": co.earlier_stamp(1)}]
    assert _summary(second)["earlier_sources"] == [{"label": "E1", "tool": "ask_system"}]
    # This turn's own call is still [1].
    this_turn = model.calls[1][-1]["content"][0]["toolResult"]["content"]
    assert this_turn[-1] == {"text": citation_stamp(1)}
    # In memory only: no stored event was rewritten with the label.
    stored = json.dumps(plane.stored_messages())
    assert "Returned for an earlier question" not in stored


# --------------------------------------------------------------------------- #
# 5. The prompt rule and the Quick bridge text
# --------------------------------------------------------------------------- #
def test_both_mode_prompts_tell_the_model_how_to_cite_an_earlier_result(monkeypatch):
    monkeypatch.delenv("ADT_WORKSHOP", raising=False)
    for mode in ("agent", "orchestrator"):
        prompt = orch._build_orchestrator_prompt(mode)
        flat = " ".join(prompt.split())
        assert ("A result stamped [E<k>] was returned for an earlier question in this chat: "
                "cite it as [E<k>], say it was returned earlier in this chat, and never copy a "
                "citation number from an earlier answer.") in flat, mode
        # The failure-handling rules the no-data line relies on are unchanged.
        assert "HANDLING SYSTEM ERRORS" in prompt, mode


def test_the_quick_bridge_answer_keeps_an_earlier_label_as_written():
    text = "Still open [E1]; newly checked [1]."
    assert external_answer_text(text) == text
