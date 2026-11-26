# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Telemetry-coverage OUT-OF-BAND wiring (system runtime → store → ECS trace).

The coverage line travels beside the answer, never inside it:

* CAPTURE: the system agent's ``InvocationToolResultCapture`` hook records
  bounded coverage lines into ``invocation_state`` for every telemetry
  history read (independently guarded — a raising recorder never breaks the
  Wave-5 metadata or Wave-6 trace-step paths);
* WRITE: ``_write_investigation_trace`` carries them in the SAME W6-F2
  investigation-trace entry (``telemetry_coverage``, present only when
  non-empty — every existing entry stays byte-identical) keyed by the D3
  ``(session_id, query_ref)`` join; the store re-bounds them at write;
* ANSWER BYTE-EQUAL: ``_run_mode_a_query`` returns the answer unmodified —
  no coverage text is ever appended to what the orchestrator model reads;
* READ: the ECS cloud stream (report-only path) mints the SAME
  ``query_ref`` from the ``query_<id>`` tool's ``toolUseId`` and sends the
  lines as the ``tool_result`` frame's own ``telemetry_coverage`` field (the
  Reasoning pill shows them above the summary), never inside the summary,
  which the outcome classifiers read from its head; any other tool performs
  ZERO store reads; a raising store leaves the step exactly as today;
  enforce (integrity) mode is untouched.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import agents.investigation_trace as it
import agents.investigation_trace_store as its
import agents.provenance_store as provenance_store
import agents.system_agent as sa
import agents.telemetry_coverage as tc
from agents.chat_client import ThinkingFilter, _CloudStreamState
from agents.events import ToolResultEvent
from agents.job_store import JobStore
from agents.provenance import mint_query_ref
from agents.runtime_asgi import provenance_context_var, provenance_write_token_var
from agents.tool_result_capture import InvocationToolResultCapture
from tests.unit._shared_table import SharedTable

SID = "sess-cov"
START = "2026-01-02T06:00:00Z"
END = "2026-01-05T06:00:00Z"
LINE = ("Telemetry read: asset-a / pressure, 2026-01-02T06:00Z to "
        "2026-01-05T06:00Z, 0 points (no data)")


@pytest.fixture(autouse=True)
def _memory_store(monkeypatch):
    data = SharedTable()
    monkeypatch.setattr(its, "_job_store", JobStore(its._KIND, table=data))
    monkeypatch.setattr(provenance_store, "_job_store",
                        JobStore(provenance_store._KIND, table=data))
    monkeypatch.setattr(provenance_store, "_cap_store",
                        JobStore(provenance_store._CAP_KIND, table=SharedTable()))
    token = provenance_context_var.set(None)
    wtoken = provenance_write_token_var.set(None)
    yield
    provenance_context_var.reset(token)
    provenance_write_token_var.reset(wtoken)


def _issue_ctx(session_id: str, query_ref: str) -> None:
    arg = provenance_store.issue_provenance_arg(session_id, query_ref)
    provenance_context_var.set((session_id, query_ref))
    provenance_write_token_var.set(arg.get("write_token"))


def _history_result() -> dict:
    payload = [{"elementId": "asset-a:pressure", "result": {"values": []}}]
    return {"status": "success", "content": [{"text": json.dumps(payload)}]}


def _after_event(state: dict, name: str = "sitewise_get_asset_history"):
    return SimpleNamespace(
        selected_tool=None,
        tool_use={"toolUseId": "t-1", "name": name,
                  "input": {"element_id": "asset-a:pressure",
                            "start_time": START, "end_time": END}},
        invocation_state=state, result=_history_result(), exception=None,
        retry=False, agent=SimpleNamespace(messages=[]))


# ── capture hook ────────────────────────────────────────────────────────────

def test_capture_hook_records_coverage_for_history_reads():
    state: dict = {}
    InvocationToolResultCapture(system_id="hist")._on_after_tool_call(
        _after_event(state))
    assert tc.collect_coverage(state) == [LINE]


def test_raising_recorder_never_breaks_the_hook(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("coverage bug")
    monkeypatch.setattr(tc, "record_coverage", boom)
    state: dict = {}
    InvocationToolResultCapture(system_id="hist")._on_after_tool_call(
        _after_event(state))
    assert state[it.TRACE_STATE_KEY]  # the W6-F3 step still recorded


# ── trace entry + store ─────────────────────────────────────────────────────

def _entry(**overrides) -> dict:
    kwargs = dict(session_id=SID, query_ref="q-0123456789ab", system_id="hist",
                  question="q", kg_context_attached=False, steps=[],
                  steps_dropped=0, final_answer="a")
    kwargs.update(overrides)
    return it.build_trace_entry(**kwargs)


def test_entry_carries_coverage_only_when_present():
    assert "telemetry_coverage" not in _entry()
    assert "telemetry_coverage" not in _entry(telemetry_coverage=[])
    assert _entry(telemetry_coverage=[LINE])["telemetry_coverage"] == [LINE]


def test_store_round_trip_rebounds_coverage():
    ref = "q-0123456789ab"
    entry = _entry(telemetry_coverage=[LINE])
    entry["telemetry_coverage"] = [LINE, 7, "x" * 999] + [LINE] * 40
    assert its.put_trace(SID, ref, entry)
    lines = its.get_telemetry_coverage(SID, ref)
    assert lines[0] == LINE and lines[1] == "x" * tc.MAX_COVERAGE_LINE_CHARS
    assert len(lines) == tc.MAX_COVERAGE_LINES


def test_store_without_coverage_keeps_legacy_shape_and_reads_empty():
    ref = "q-0123456789ab"
    assert its.put_trace(SID, ref, _entry())
    assert "telemetry_coverage" not in its.get_trace(SID, ref)
    assert its.get_telemetry_coverage(SID, ref) == []
    assert its.get_telemetry_coverage(SID, "q-ffffffffffff") == []
    assert its.get_telemetry_coverage("bad id!", ref) == []


# ── system-agent writer (answer byte-equal) ─────────────────────────────────

def _run_query(answer: str, state: dict) -> str:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sa, "_invoke_mode_a_agent_with_state",
                   lambda _agent, _q: (answer, state))
        return asyncio.run(sa._run_mode_a_query(object(), "hist", "q?", ""))


def test_writer_stores_coverage_and_answer_is_byte_equal():
    state: dict = {}
    InvocationToolResultCapture(system_id="hist")._on_after_tool_call(
        _after_event(state))
    ref = mint_query_ref(SID, "orch-tool-1")
    _issue_ctx(SID, ref)
    answer = "The channel returned no samples in that window."
    assert _run_query(answer, state) == answer
    assert its.get_telemetry_coverage(SID, ref) == [LINE]


# ── ECS cloud stream: the trace summary (never the model's text) ────────────

class _SilentLog:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def debug(self, *a, **k): pass
    def error(self, *a, **k): pass


def _stream_result(tool_name: str, *, integrity: bool = False) -> ToolResultEvent:
    state = _CloudStreamState(
        ThinkingFilter(), integrity_mode=integrity, log=_SilentLog(),
        owner_id="u", session_id=SID, runtime_session_id="rt-x")
    state.events_for({"message": {"role": "assistant", "content": [
        {"toolUse": {"toolUseId": "orch-tool-1", "name": tool_name,
                     "input": {"question": "q?"}}}]}})
    events = state.events_for({"message": {"role": "user", "content": [
        {"toolResult": {"toolUseId": "orch-tool-1", "status": "success",
                        "content": [{"text": "Answer text."}]}}]}})
    state.close()
    return next(e for e in events if isinstance(e, ToolResultEvent))


def _store_coverage():
    ref = mint_query_ref(SID, "orch-tool-1")
    assert its.put_trace(SID, ref, _entry(query_ref=ref,
                                          telemetry_coverage=[LINE]))


def _frame(result: ToolResultEvent) -> dict:
    return json.loads(result.to_sse_format().split("data: ", 1)[1])


def test_query_tool_carries_its_coverage_lines_beside_the_summary():
    _store_coverage()
    result = _stream_result("hist-target___query_hist")
    assert result.summary == "Answer text."
    assert result.telemetry_coverage == [LINE]
    assert _frame(result)["telemetry_coverage"] == [LINE]


def test_unprefixed_query_tool_name_is_recognized():
    _store_coverage()
    assert _stream_result("query_hist").telemetry_coverage == [LINE]


def test_non_query_tool_performs_zero_store_reads(monkeypatch):
    calls = []
    monkeypatch.setattr(its, "get_trace_telemetry",
                        lambda *a: calls.append(a) or ([LINE], ""))
    result = _stream_result("explore_graph")
    assert (result.summary, result.telemetry_coverage) == ("Answer text.", None)
    assert calls == []


def test_no_entry_leaves_summary_unchanged():
    result = _stream_result("hist-target___query_hist")
    assert (result.summary, result.telemetry_coverage) == ("Answer text.", None)
    assert "telemetry_coverage" not in _frame(result)


def test_raising_store_leaves_summary_unchanged(monkeypatch):
    def boom(*_a):
        raise RuntimeError("store down")
    monkeypatch.setattr(its, "get_trace_telemetry", boom)
    result = _stream_result("hist-target___query_hist")
    assert (result.summary, result.telemetry_coverage) == ("Answer text.", None)


def test_integrity_mode_summary_is_untouched():
    _store_coverage()
    result = _stream_result("hist-target___query_hist", integrity=True)
    assert "Telemetry read:" not in result.summary
    assert result.telemetry_coverage is None
