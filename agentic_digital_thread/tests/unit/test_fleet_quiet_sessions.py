# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The orchestrator's session count on a quiet fleet.

The app pings the orchestrator runtime every 180 s to keep it warm. The ping
is answered without a model turn and logs no ``ORCHESTRATOR COMPLETE`` line,
but AgentCore still counts it in the native ``Invocations`` metric, and each
ping opens its own log stream. The Agents tab defines an orchestrator session
by the ``ORCHESTRATOR COMPLETE`` count, so when that query succeeds its answer
is the session count even when it is 0: the native invocations, the hourly
sum and the log-stream count must not replace it. When the query fails, the
earlier fallbacks still apply. System agents keep their stream-based counts.

Offline: a fake Logs Insights client answers each query by its text.
"""
from __future__ import annotations

import threading
from unittest.mock import Mock

import pytest

import agents._insights as insights
import agents.agents_routes as ar

ORCH = "adt_orchestrator"
ORCH_ID = "adt_orchestrator-abc123"
SYSTEM = "adt_system_a"
SYSTEM_ID = "adt_system_a-def456"

#: Native AgentCore figures for a quiet day: every one is a warmup ping.
NATIVE = {"invocations": 480, "hourly": [20, 20, 20], "latency_ms": 250.0}
#: Distinct log streams in the same window, one per ping.
PING_STREAMS = 480

COMPLETE = "complete"
COMPLETE_HOURLY = "complete_hourly"
TOOLS = "tools"
STREAMS = "streams"
STREAMS_HOURLY = "streams_hourly"


def _row(**fields) -> list[dict]:
    return [{"field": k, "value": str(v)} for k, v in fields.items()]


def _kind(query: str) -> str:
    hourly = "bin(1h)" in query
    if "ORCHESTRATOR COMPLETE" in query:
        return COMPLETE_HOURLY if hourly else COMPLETE
    if "DETERMINISM" in query:
        return TOOLS
    if "count_distinct(@logStream)" in query:
        return STREAMS_HOURLY if hourly else STREAMS
    raise AssertionError(f"unexpected Insights query: {query!r}")


class _FakeLogs:
    """Answers start_query/get_query_results per query kind.

    An answer is ``("Complete", rows)``, ``("Failed", [])`` or an exception
    that start_query raises. Every kind started is recorded.
    """

    def __init__(self, answers: dict):
        self._answers = answers
        self._results: dict[str, tuple[str, list]] = {}
        self._lock = threading.Lock()
        self.started: list[str] = []

    def start_query(self, *, logGroupName, startTime, endTime, queryString, limit):
        kind = _kind(queryString)
        answer = self._answers.get(kind, ("Complete", []))
        with self._lock:
            self.started.append(kind)
            qid = f"q{len(self.started)}"
            if isinstance(answer, Exception):
                raise answer
            self._results[qid] = answer
        return {"queryId": qid}

    def get_query_results(self, *, queryId):
        status, rows = self._results[queryId]
        return {"status": status, "results": rows}


class _FakeSession:
    def __init__(self, logs: _FakeLogs):
        self.logs = logs

    def client(self, name, **kwargs):
        assert name == "logs", f"unexpected client requested: {name}"
        return self.logs


@pytest.fixture(autouse=True)
def _no_poll_wait(monkeypatch):
    monkeypatch.setattr(ar.time, "sleep", lambda _s: None)
    monkeypatch.setattr(insights, "DEFAULT_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(ar, "_get_eval_scores", lambda *a, **k: {})


def _quiet_answers(**overrides) -> dict:
    """A quiet window: no COMPLETE lines, no tool calls, one stream per ping."""
    answers = {
        COMPLETE: ("Complete", []),
        COMPLETE_HOURLY: ("Complete", []),
        TOOLS: ("Complete", []),
        STREAMS: ("Complete", [_row(sessions=PING_STREAMS)]),
        STREAMS_HOURLY: ("Complete", [_row(hour="h0", sessions=PING_STREAMS)]),
    }
    answers.update(overrides)
    return answers


def _orchestrator_usage(logs: _FakeLogs, native: dict | None = None) -> dict:
    fc = {"session": _FakeSession(logs), "hours": 24.0,
          "orch_pool": {"input": 0, "output": 0, "cost": 0.0, "latency_ms": None},
          "total_tool_calls": 0, "orch_model_id": "model-x"}
    rt = {"agentRuntimeName": ORCH, "agentRuntimeId": ORCH_ID}
    return ar._fleet_orchestrator_usage(fc, rt, dict(NATIVE if native is None else native))


_ZERO_COMPLETE = {
    "no rows": ("Complete", []),
    "a zero row": ("Complete", [_row(sessions=0, avg_events=0)]),
}


@pytest.mark.parametrize("zero", list(_ZERO_COMPLETE), ids=list(_ZERO_COMPLETE))
def test_a_quiet_fleet_shows_zero_sessions_not_its_warmup_pings(zero):
    logs = _FakeLogs(_quiet_answers(**{COMPLETE: _ZERO_COMPLETE[zero]}))

    usage = _orchestrator_usage(logs)

    assert usage["invocations"] == 0
    assert usage["hourly"] == []
    assert usage["avg_events"] is None


@pytest.mark.parametrize("zero", list(_ZERO_COMPLETE), ids=list(_ZERO_COMPLETE))
def test_a_successful_zero_count_runs_no_stream_count_fallback(zero):
    logs = _FakeLogs(_quiet_answers(**{COMPLETE: _ZERO_COMPLETE[zero]}))

    result = ar._get_metrics(_FakeSession(logs), ORCH_ID, ORCH, 24.0)

    assert result["invocations_24h"] == 0
    assert result["hourly_sessions"] == []
    assert STREAMS not in logs.started


def test_a_turn_row_with_no_sessions_still_replaces_the_native_ping_series():
    # A turn whose tool line is in the window but whose COMPLETE line is not:
    # the query succeeded with no sessions, so 0 and no sparkline stand.
    logs = _FakeLogs(_quiet_answers(**{COMPLETE: ("Complete", [
        _row(hour="0", sessions=0, events=0, tool_calls=0, tooled=0)])}))

    usage = _orchestrator_usage(logs)

    assert usage["invocations"] == 0
    assert usage["hourly"] == []


def test_a_measured_session_count_still_replaces_native_invocations():
    logs = _FakeLogs(_quiet_answers(**{
        COMPLETE: ("Complete", [_row(hour="h0", sessions=1, events=12, tool_calls=0, tooled=1),
                                _row(hour="h1", sessions=2, events=24, tool_calls=0, tooled=2)]),
    }))

    usage = _orchestrator_usage(logs)

    assert usage["invocations"] == 3
    assert usage["hourly"] == [1, 2]
    assert usage["avg_events"] == 12
    assert STREAMS not in logs.started


_FAILURES = {
    "start raises": RuntimeError("logs unavailable"),
    "query ends Failed": ("Failed", []),
    "query ends Cancelled": ("Cancelled", []),
}


@pytest.mark.parametrize("failure", list(_FAILURES), ids=list(_FAILURES))
def test_a_failed_session_query_keeps_the_stream_count_fallback(failure):
    logs = _FakeLogs(_quiet_answers(**{COMPLETE: _FAILURES[failure],
                                       COMPLETE_HOURLY: _FAILURES[failure],
                                       STREAMS: ("Complete", [_row(sessions=7)])}))

    usage = _orchestrator_usage(logs)

    assert STREAMS in logs.started
    assert usage["invocations"] == 7
    assert usage["hourly"] == NATIVE["hourly"]


def test_a_failed_query_with_no_fallback_data_keeps_native_figures():
    failed = ("Failed", [])
    logs = _FakeLogs({COMPLETE: failed, COMPLETE_HOURLY: failed, TOOLS: failed,
                      STREAMS: failed})

    usage = _orchestrator_usage(logs)

    assert usage["invocations"] == NATIVE["invocations"]
    assert usage["hourly"] == NATIVE["hourly"]


def test_a_system_agent_still_counts_its_log_streams():
    logs = _FakeLogs(_quiet_answers(**{
        STREAMS_HOURLY: ("Complete", [_row(hour="h0", sessions=2),
                                      _row(hour="h1", sessions=3)]),
    }))

    result = ar._get_metrics(_FakeSession(logs), SYSTEM_ID, SYSTEM, 24.0)

    assert result["invocations_24h"] == 5
    assert result["hourly_sessions"] == [2, 3]


def test_a_system_agent_falls_back_to_the_stream_count():
    logs = _FakeLogs(_quiet_answers(**{STREAMS_HOURLY: ("Complete", []),
                                       STREAMS: ("Complete", [_row(sessions=6)])}))

    result = ar._get_metrics(_FakeSession(logs), SYSTEM_ID, SYSTEM, 24.0)

    assert result["invocations_24h"] == 6
    assert result["hourly_sessions"] == []


def test_a_system_agent_row_keeps_native_invocations(monkeypatch):
    monkeypatch.setattr(ar, "_get_metrics",
                        Mock(side_effect=AssertionError("system rows read native metrics")))
    fc = {"tools": {}, "agent_pool": {"latency_ms": None}, "agent_model_id": "model-y"}
    rt = {"agentRuntimeName": SYSTEM, "agentRuntimeId": SYSTEM_ID}

    usage = ar._fleet_system_usage(fc, rt, dict(NATIVE), is_tools_runtime=False)

    assert usage["invocations"] == NATIVE["invocations"]
    assert usage["hourly"] == NATIVE["hourly"]
