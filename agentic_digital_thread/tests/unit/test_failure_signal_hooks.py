# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0059 Task 2B pins: the M2 failure-signal HOOKS + the sweep trigger.

Three seams, each best-effort and turn-safe by contract:

- ``orchestrator._observe_identifier_miss`` — PASSIVE terminology-miss
  observation on its own thin gateway-client wrapper
  (``_wrap_gateway_client_with_miss_observation``, composed under the retry
  wrapper in ``_build_gateway_client``; the ADR-0048 recovery loop is a
  PROMPT contract and stays untouched — the result reaches the model
  byte-identical); the accumulator write rides a daemon thread (zero
  chat-path latency);
- ``runtime_app._record_empty_join_signal`` — the post-turn empty-join
  classification at the ``record_rule_usage`` finally-block site, driven by
  the PURE ``query_intelligence.classify_empty_join``;
- ``schema_drift.run_scheduled_drift_check`` — the question sweep
  (``failure_signals.sweep_and_file_questions``) rides drift's OWN cadence
  tick (co-located; no second scheduler job — the scheduler-seam pins in
  ``test_schema_drift_job.py`` stay byte-identical).

The load-bearing pin: a RAISING (or unimportable) ``failure_signals`` module
never breaks a turn or a scheduler tick. Offline / pure import — no AWS, no
network. Placeholder systems only.
"""
from __future__ import annotations

import asyncio
import inspect
import sys

import pytest

import agents.failure_signals as failure_signals
import agents.orchestrator as orch
import agents.runtime_app as runtime_app
import agents.schema_drift as schema_drift


class _InlineThread:
    """Deterministic stand-in: ``start()`` runs the target synchronously."""

    def __init__(self, target=None, args=(), daemon=None, name=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


@pytest.fixture
def recorded(monkeypatch):
    """Spy on ``record_failure_signal`` + inline the observation thread."""
    calls: list[tuple] = []
    monkeypatch.setattr(failure_signals, "record_failure_signal",
                        lambda *a, **k: calls.append(a) or True)
    monkeypatch.setattr(orch.threading, "Thread", _InlineThread)
    return calls


_MISS_TEXT = (
    "I could not match that identifier.\n"
    "```\nIDENTIFIER-MISS:\ntoken: WO-1001\nlooked_in: orders.order_id\n"
    "my_format: 4400001234, 4400005678\n```\n"
)


def _miss_result():
    return {"status": "success", "content": [{"text": _MISS_TEXT}]}


# ---------------------------------------------------------------------------
# Orchestrator hook — passive terminology-miss observation
# ---------------------------------------------------------------------------

def test_marker_result_records_terminology_miss(recorded):
    orch._observe_identifier_miss(_miss_result(), ("tid-1", "sys-a___query"), {})
    assert recorded == [
        ("terminology_miss", "sys-a", {"term": "wo-1001"}, "tid-1")]


def test_marker_result_via_kwargs_records(recorded):
    orch._observe_identifier_miss(
        _miss_result(), (), {"tool_use_id": "tid-2", "name": "sys-b___query"})
    assert recorded == [
        ("terminology_miss", "sys-b", {"term": "wo-1001"}, "tid-2")]


def test_non_marker_result_records_nothing(recorded):
    orch._observe_identifier_miss(
        {"status": "success", "content": [{"text": "Found 3 rows."}]},
        ("tid-1", "sys-a___query"), {})
    assert recorded == []


def test_non_gateway_tool_is_skipped(recorded):
    orch._observe_identifier_miss(_miss_result(), ("tid-1", "explore_graph"), {})
    assert recorded == []


def test_malformed_result_never_raises(recorded):
    orch._observe_identifier_miss(None, (), {})
    orch._observe_identifier_miss("not-a-dict", ("tid", "sys-a___q"), {})
    orch._observe_identifier_miss({"content": "not-a-list"}, ("tid", "sys-a___q"), {})
    assert recorded == []


def test_raising_accumulator_never_breaks_the_observation(monkeypatch):
    monkeypatch.setattr(orch.threading, "Thread", _InlineThread)
    monkeypatch.setattr(failure_signals, "record_failure_signal",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    orch._observe_identifier_miss(_miss_result(), ("tid-1", "sys-a___query"), {})


def test_unimportable_failure_signals_module_never_breaks(monkeypatch):
    monkeypatch.setitem(sys.modules, "agents.failure_signals", None)
    orch._observe_identifier_miss(_miss_result(), ("tid-1", "sys-a___query"), {})


def test_wrapped_client_returns_marker_result_unchanged_and_observes(recorded):
    """ADR-0048 loop passivity: the wrapped client hands the marker result to
    the model BYTE-IDENTICAL (the recovery loop's input is untouched) while
    the observation fires exactly once. The observation wrapper composes
    UNDER the retry wrapper (`_build_gateway_client` order — pinned there)."""
    marker = _miss_result()

    class _FakeClient:
        async def call_tool_async(self, *a, **k):
            return marker

    wrapped = orch._wrap_gateway_client_with_retry(
        orch._wrap_gateway_client_with_miss_observation(_FakeClient()))
    result = asyncio.run(wrapped.call_tool_async("tid-9", "sys-a___query"))
    assert result is marker  # unchanged object — nothing redacted or rewritten
    assert recorded == [
        ("terminology_miss", "sys-a", {"term": "wo-1001"}, "tid-9")]


def test_build_gateway_client_applies_observation_before_retry():
    """Wiring pin: the production client stack includes the observation
    wrapper, applied before (wrapped by) the retry wrapper."""
    src = inspect.getsource(orch._build_gateway_client)
    assert ("_wrap_gateway_client_with_miss_observation(gateway_client)"
            in src)
    assert src.index("_wrap_gateway_client_with_miss_observation") < \
        src.index("_wrap_gateway_client_with_retry(")


def test_write_rides_a_daemon_thread_not_the_event_loop():
    """Zero chat-path latency: the accumulator write is spawned
    fire-and-forget (daemon thread), never awaited inline."""
    src = inspect.getsource(orch._observe_identifier_miss)
    assert "threading.Thread" in src and "daemon=True" in src


# ---------------------------------------------------------------------------
# Runtime post-turn hook — empty-join classification
# ---------------------------------------------------------------------------

_CALLS = [{"name": "sys-a___query", "toolUseId": "t1"},
          {"name": "sys-b___query", "toolUseId": "t2"}]


@pytest.fixture
def join_recorded(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(failure_signals, "record_failure_signal",
                        lambda *a, **k: calls.append(a) or True)
    return calls


def test_one_failed_leg_records_empty_join(join_recorded):
    results = {"t1": "Found 3 rows: ...", "t2": "Query returned no rows."}
    assert runtime_app._record_empty_join_signal(_CALLS, results, "q", "sess-1")
    assert join_recorded == [("empty_join", "sys-b",
                              {"failed_system": "sys-b",
                               "healthy_systems": ["sys-a"]}, "t1")]


def test_single_system_turn_records_nothing(join_recorded):
    results = {"t1": "Query returned no rows."}
    assert not runtime_app._record_empty_join_signal(
        [_CALLS[0]], results, "q", "sess-1")
    assert join_recorded == []


def test_all_legs_failed_is_systemic_not_a_join_gap(join_recorded):
    results = {"t1": "no results found", "t2": "Query returned no rows."}
    assert not runtime_app._record_empty_join_signal(_CALLS, results, "q", "s")
    assert join_recorded == []


def test_intra_turn_recovered_retry_is_a_healthy_leg(join_recorded):
    """A system whose failed call was retried and RECOVERED within the turn
    is healthy — only the persistently empty leg classifies."""
    calls = _CALLS + [{"name": "sys-a___query", "toolUseId": "t3"}]
    results = {"t1": "no results found", "t3": "Found 5 rows.",
               "t2": "Query returned no rows."}
    assert runtime_app._record_empty_join_signal(calls, results, "q", "s")
    assert join_recorded[0][1] == "sys-b"


def test_synthetic_and_briefing_turns_never_write(join_recorded):
    results = {"t1": "Found 3 rows.", "t2": "no results found"}
    assert not runtime_app._record_empty_join_signal(
        _CALLS, results, "q", "tryit-abc")
    assert not runtime_app._record_empty_join_signal(
        _CALLS, results, "daily briefing please", "sess-1")
    assert join_recorded == []


def test_raising_accumulator_never_breaks_the_turn(monkeypatch):
    monkeypatch.setattr(failure_signals, "record_failure_signal",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    results = {"t1": "Found 3 rows.", "t2": "no results found"}
    assert runtime_app._record_empty_join_signal(_CALLS, results, "q", "s") is False


def test_finally_block_wires_the_empty_join_hook():
    """The hook rides the post-turn learning seam that BOTH the legacy
    ``invoke`` finally block and the integrity mirror call at the
    ``record_rule_usage`` site (the ``test_steward_gate`` source-pin idiom;
    the legacy finally block itself is gate-frozen at 50 lines)."""
    src = inspect.getsource(runtime_app._post_turn_learning_checks)
    assert ("_record_empty_join_signal(s.tool_calls_log, s.tool_results, "
            "s.prompt, s.session_id)") in src
    finally_src = inspect.getsource(runtime_app._run_orchestrator_base)
    assert "_post_turn_learning_checks(s)" in finally_src


# ---------------------------------------------------------------------------
# Sweep trigger — rides the drift cadence tick (no second scheduler job)
# ---------------------------------------------------------------------------

def test_drift_tick_fires_the_question_sweep(monkeypatch):
    fired: list[int] = []
    monkeypatch.setattr(schema_drift, "onboarded_system_ids", lambda: [])
    monkeypatch.setattr(failure_signals, "sweep_and_file_questions",
                        lambda: fired.append(1) or {"filed": [], "skipped": 0,
                                                    "superseded": [], "error": ""})
    assert schema_drift.run_scheduled_drift_check() == []
    assert fired == [1]  # exactly once per tick


def test_raising_sweep_never_kills_the_drift_tick(monkeypatch):
    monkeypatch.setattr(schema_drift, "onboarded_system_ids", lambda: ["erp"])
    monkeypatch.setattr(schema_drift, "run_drift_check",
                        lambda sid: {"system_id": sid, "status": "ok",
                                     "drifts_found": 0, "proposals_filed": 0,
                                     "kinds": {}, "resolved_swept": 0})
    monkeypatch.setattr(failure_signals, "sweep_and_file_questions",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    results = schema_drift.run_scheduled_drift_check()
    assert [r["system_id"] for r in results] == ["erp"]  # drift results intact


def test_unimportable_failure_signals_never_kills_the_drift_tick(monkeypatch):
    monkeypatch.setattr(schema_drift, "onboarded_system_ids", lambda: [])
    monkeypatch.setitem(sys.modules, "agents.failure_signals", None)
    assert schema_drift.run_scheduled_drift_check() == []
