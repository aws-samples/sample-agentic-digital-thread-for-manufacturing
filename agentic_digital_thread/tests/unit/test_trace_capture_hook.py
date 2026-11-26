# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Trace-capture hook extension (Wave 6, Task B1 — W6-F3 pins).

Pins the ``InvocationToolResultCapture`` extension:

* a simulated Before→After pair yields ONE step in
  ``invocation_state[TRACE_STATE_KEY]`` (name, serialized input, bounded
  output, success status, non-null ``duration_ms``, ``started_at``);
* exception events record ``status: "exception"`` with empty output;
* ``retry=True`` records NOTHING; the final attempt records exactly one;
* the ``registered_tools`` filter is honored on BOTH callbacks;
* a missing Before stamp records an honest ``duration_ms: null``;
* the Wave-5 metadata path is byte-identical — a raising ``record_step``
  (monkeypatched) never breaks ``_on_after_tool_call``;
* the hooks-list indices stay pinned ([0] budget / [1] capture — the
  existing test_response_integrity_iteration7 pins keep running unchanged in their own module).
"""
from __future__ import annotations

from types import SimpleNamespace

import agents.investigation_trace as it
import agents.tool_result_capture as trc
from agents.tool_result_capture import (
    TOOL_RESULTS_STATE_KEY,
    InvocationToolResultCapture,
)


def _capture(**kwargs) -> InvocationToolResultCapture:
    return InvocationToolResultCapture(system_id="erp", **kwargs)


def _agent_with_messages(tool_use_id: str, reasoning: str = "Thinking."):
    return SimpleNamespace(messages=[
        {"role": "assistant", "content": [
            {"text": reasoning},
            {"toolUse": {"toolUseId": tool_use_id, "name": "execute_sql",
                         "input": {}}},
        ]},
    ])


def _before_event(state: dict, tool_use_id: str = "t-1",
                  name: str = "execute_sql"):
    return SimpleNamespace(
        selected_tool=None,
        tool_use={"toolUseId": tool_use_id, "name": name,
                  "input": {"sql": "SELECT 1"}},
        invocation_state=state,
        agent=_agent_with_messages(tool_use_id),
    )


def _after_event(state: dict, tool_use_id: str = "t-1",
                 name: str = "execute_sql", *, exception=None, retry=False,
                 result=None):
    if result is None and exception is None:
        result = {"status": "success",
                  "content": [{"json": {"rows": [["a"]], "row_count": 1}}]}
    return SimpleNamespace(
        selected_tool=None,
        tool_use={"toolUseId": tool_use_id, "name": name,
                  "input": {"sql": "SELECT 1"}},
        invocation_state=state,
        result=result,
        exception=exception,
        retry=retry,
        agent=_agent_with_messages(tool_use_id),
    )


# --------------------------------------------------------------------------- #
# Before→After pair — one full step
# --------------------------------------------------------------------------- #

def test_before_after_pair_records_one_full_step():
    capture = _capture()
    state: dict = {}
    capture._on_before_tool_call(_before_event(state))
    capture._on_after_tool_call(_after_event(state))
    steps, dropped = it.collect_trace_steps(state)
    assert dropped == 0 and len(steps) == 1
    step = steps[0]
    assert step["tool_name"] == "execute_sql"
    assert '"sql":"SELECT 1"' in step["tool_input"]
    assert step["status"] == "success"
    assert step["duration_ms"] is not None and step["duration_ms"] >= 0
    assert step["started_at"].endswith("Z")
    assert step["reasoning"] == "Thinking."
    assert step["output_bytes"] > 0


def test_missing_before_records_null_duration():
    capture = _capture()
    state: dict = {}
    capture._on_after_tool_call(_after_event(state))
    (step,), _ = it.collect_trace_steps(state)
    assert step["duration_ms"] is None


def test_exception_event_records_exception_step():
    capture = _capture()
    state: dict = {}
    capture._on_before_tool_call(_before_event(state))
    capture._on_after_tool_call(
        _after_event(state, exception=RuntimeError("boom")))
    (step,), _ = it.collect_trace_steps(state)
    assert step["status"] == "exception"
    assert step["tool_output"] == "" and step["output_bytes"] == 0
    # the Wave-5 metadata path also captured the exception (unchanged)
    assert state[TOOL_RESULTS_STATE_KEY][0]["safe_error_category"] == "tool_exception"


# --------------------------------------------------------------------------- #
# Retry semantics — final attempt only
# --------------------------------------------------------------------------- #

def test_retry_true_records_no_step_final_attempt_records_one():
    capture = _capture()
    state: dict = {}
    capture._on_before_tool_call(_before_event(state))
    capture._on_after_tool_call(_after_event(state, retry=True))
    assert it.collect_trace_steps(state)[0] == []
    capture._on_after_tool_call(_after_event(state, retry=False))
    steps, _ = it.collect_trace_steps(state)
    assert len(steps) == 1


# --------------------------------------------------------------------------- #
# registered_tools filter — both callbacks
# --------------------------------------------------------------------------- #

def test_unregistered_tool_records_nothing_on_either_callback():
    capture = _capture(registered_tools={"other_tool"})
    state: dict = {}
    capture._on_before_tool_call(_before_event(state))
    capture._on_after_tool_call(_after_event(state))
    assert it.collect_trace_steps(state)[0] == []
    assert state.get(it._TRACE_PENDING_KEY) in (None, {})
    assert state.get(TOOL_RESULTS_STATE_KEY) in (None, [])


def test_registered_tool_passes_the_filter():
    capture = _capture(registered_tools={"execute_sql"})
    state: dict = {}
    capture._on_before_tool_call(_before_event(state))
    capture._on_after_tool_call(_after_event(state))
    assert len(it.collect_trace_steps(state)[0]) == 1


# --------------------------------------------------------------------------- #
# Fail-soft: a trace bug never breaks the Wave-5 metadata path
# --------------------------------------------------------------------------- #

def test_raising_record_step_never_breaks_metadata_capture(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("trace bug")

    monkeypatch.setattr(it, "record_step", _boom)
    capture = _capture()
    state: dict = {}
    capture._on_after_tool_call(_after_event(state))
    # Wave-5 metadata still captured despite the trace failure
    captured = state[TOOL_RESULTS_STATE_KEY]
    assert len(captured) == 1
    assert captured[0]["app_status"] == "success"


def test_raising_record_step_start_never_breaks_before(monkeypatch):
    monkeypatch.setattr(it, "record_step_start",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    capture = _capture()
    capture._on_before_tool_call(_before_event({}))  # must not raise


def test_trace_failure_logs_type_name_only(monkeypatch, caplog):
    import logging

    class _WeirdTraceError(RuntimeError):
        pass

    monkeypatch.setattr(it, "record_step",
                        lambda *a, **k: (_ for _ in ()).throw(
                            _WeirdTraceError("SELECT secret FROM t")))
    capture = _capture()
    with caplog.at_level(logging.DEBUG, logger="agents.tool_result_capture"):
        capture._on_after_tool_call(_after_event({}))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "_WeirdTraceError" in text
    assert "SELECT secret" not in text


# --------------------------------------------------------------------------- #
# Wave-5 path unchanged + hook registration shape
# --------------------------------------------------------------------------- #

def test_register_hooks_adds_before_tool_call_callback():
    registered: list = []

    class _Registry:
        def add_callback(self, event_type, callback):
            registered.append((event_type.__name__, callback.__name__))

    _capture().register_hooks(_Registry())
    names = [name for name, _ in registered]
    assert names == ["BeforeInvocationEvent", "BeforeToolCallEvent",
                     "AfterToolCallEvent"]


def test_sidecar_statement_retention_unchanged():
    """The Wave-5 sidecar row is still recorded exactly as before (the trace
    step rides ALONGSIDE it, never replaces it)."""
    capture = _capture()
    state: dict = {}
    result = {"status": "success", "content": [
        {"json": {"rows": [["a"]], "row_count": 1,
                  "_adt_executed_query": "SELECT part_no FROM orders"}}]}
    capture._on_after_tool_call(_after_event(state, result=result))
    rows = trc.collect_sidecar_statements(state)
    assert len(rows) == 1
    assert rows[0]["query_text"] == "SELECT part_no FROM orders"
    # and the trace step captured the SAME call independently
    assert len(it.collect_trace_steps(state)[0]) == 1
