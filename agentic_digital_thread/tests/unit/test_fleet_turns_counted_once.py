# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Each orchestrator turn counts once on the Agents tab.

A deployed AgentCore runtime writes every log line to its log group twice:
once as plain text in the per-session runtime stream
(``<date>/[runtime-logs]<uuid>``, the trace in ``[trace_id=<32 hex> ...]``)
and once as OpenTelemetry JSON in the shared ``otel-rt-logs`` stream (the
same text in ``body``, the trace in ``"traceId"``). A line can also be
exported more than once, and a window can hold one copy of some turns and the
other copy of others. Each turn runs in its own trace, so the turn's trace id
is the identity both copies carry.

The card's session count, hourly sparkline, average events per session and
tool-call count must count each turn exactly once in every one of those
shapes. A line with no trace id (a runtime that logged none) must never be
folded into another turn. A turn whose tool count cannot be read leaves the
tool count unmeasured, never a silent 0. The per-stream session count used by
the fallback and by system agents must not count the shared streams.

Offline: :class:`_FakeInsights` evaluates each query over fake log lines.
It applies only what the query text asks for: a query that keys lines by
both trace forms folds each turn's copies, a query that groups by stream kind
reads per kind, and anything else counts every matching line, so a query that
does not separate the copies gets the doubled answer a real log group gives.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import agents._insights as insights
import agents.agents_routes as ar

ORCH = "adt_orchestrator"
ORCH_ID = "adt_orchestrator-abc123"
SYSTEM = "adt_system_a"
SYSTEM_ID = "adt_system_a-def456"

OTEL = "otel-rt-logs"
SPANS = "spans"
RUNTIME = "runtime"
H0 = "2026-01-05 10:00:00.000"
H1 = "2026-01-05 11:00:00.000"
BOTH = (RUNTIME, OTEL)

#: One turn: its hour, session, event count and the tools it called.
TURNS = [
    {"hour": H0, "session": "s-0", "events": 100, "tools": ["explore_graph", "sys_a___query"]},
    {"hour": H0, "session": "s-1", "events": 200, "tools": ["explore_graph"]},
    {"hour": H1, "session": "s-2", "events": 300,
     "tools": ["explore_graph", "sys_a___query", "sys_b___query"]},
]
SESSIONS = len(TURNS)
HOURLY = [2, 1]
AVG_EVENTS = 200
TOOL_CALLS = 6


def _trace(i: int) -> str:
    return f"{i + 1:032x}"


def _runtime_stream(i: int) -> str:
    return f"2026/01/05/[runtime-logs]0000000{i}-aaaa-bbbb-cccc-dddddddddddd"


def _complete_text(turn: dict, line_tools: bool) -> str:
    tools = f" tool_calls={len(turn['tools'])}" if line_tools else ""
    return (f"=== ORCHESTRATOR COMPLETE === session={turn['session']} "
            f"total_events={turn['events']}{tools} skipped=0 yielded=0.0MB "
            "msg=0.0MB evt=0.0MB tool=0.0MB")


def _determinism_text(turn: dict) -> str:
    calls = [(n + 1, name, "G") for n, name in enumerate(turn["tools"])]
    return f"DETERMINISM: {len(calls)}/{len(calls)} grounded, 0 wasted -> 100% | {calls}"


def _plain(text: str, trace: str | None) -> str:
    ctx = f" [trace_id={trace} span_id=00000000000000aa]" if trace else ""
    return f"2026-01-05 10:00:00,000 INFO [__main__] [runtime_app.py:1]{ctx} - {text}"


def _otel(text: str, trace: str | None) -> str:
    record = {"resource": {"attributes": {"aws.log.stream.names": OTEL}},
              "severityText": "INFO", "body": text}
    if trace:
        record.update({"traceId": trace, "spanId": "00000000000000aa"})
    return json.dumps(record, separators=(",", ":"))


def _turn_lines(i: int, turn: dict, copies, *, keyed=True, determinism=True,
                line_tools=False) -> list[dict]:
    """``copies`` lists where the turn's lines went: ``RUNTIME`` and/or
    ``OTEL``, once per export (a name twice is a duplicate export)."""
    trace = _trace(i) if keyed else None
    texts = ([_determinism_text(turn)] if determinism else []) + [_complete_text(turn, line_tools)]
    lines = []
    for n, copy in enumerate(copies):
        for k, text in enumerate(texts):
            stream = _runtime_stream(i) if copy == RUNTIME else OTEL
            body = _plain(text, trace) if copy == RUNTIME else _otel(text, trace)
            lines.append({"stream": stream, "hour": turn["hour"], "message": body,
                          "ms": i * 1000 + k * 10 + n})
    return lines


def _lines(coverage, **kwargs) -> list[dict]:
    """The log group's lines for :data:`TURNS`; ``coverage[i]`` is turn i's
    copies. A shared ``spans`` line is added every hour."""
    lines = []
    for i, (turn, copies) in enumerate(zip(TURNS, coverage)):
        lines += _turn_lines(i, turn, copies, **kwargs)
    for hour in (H0, H1):
        lines.append({"stream": SPANS, "hour": hour, "message": '{"name":"chat"}', "ms": 0})
    return lines


SHAPES = {
    "both copies": [BOTH, BOTH, BOTH],
    "runtime streams only": [(RUNTIME,), (RUNTIME,), (RUNTIME,)],
    "otel-rt-logs only": [(OTEL,), (OTEL,), (OTEL,)],
    # Older turns only in their runtime streams, the newer one only in otel-rt-logs.
    "complementary across hours": [(RUNTIME,), (RUNTIME,), (OTEL,)],
    # The copy changes within one hour.
    "complementary within an hour": [(RUNTIME,), (OTEL,), BOTH],
    # One copy exported twice.
    "a duplicate export": [BOTH, (RUNTIME, OTEL, OTEL), BOTH],
}

_TRACE = re.compile(r'trace_id=([0-9a-f]{32})|"traceId":"([0-9a-f]{32})"')
_SESSION = re.compile(r"session=([^ \"]+) total_events=(\d+)")
_LINE_TOOLS = re.compile(r"total_events=\d+ tool_calls=(\d+)")
_DET_TOOLS = re.compile(r"DETERMINISM: \d+/(\d+) grounded")


class _FakeInsights:
    """Evaluates the Agents tab's Insights queries over fake log lines.

    ``failing`` names markers whose query ends Failed.
    """

    def __init__(self, lines: list[dict], failing: tuple[str, ...] = ()):
        self._lines = lines
        self._failing = failing
        self._results: dict[str, tuple[str, list]] = {}
        self._lock = threading.Lock()
        self.queries: list[str] = []

    def start_query(self, *, logGroupName, startTime, endTime, queryString, limit):
        with self._lock:
            self.queries.append(queryString)
            qid = f"q{len(self.queries)}"
            failed = any(marker in queryString for marker in self._failing)
            self._results[qid] = ("Failed", []) if failed else ("Complete", self._answer(queryString))
        return {"queryId": qid}

    def get_query_results(self, *, queryId):
        status, rows = self._results[queryId]
        return {"status": status, "results": rows}

    def _answer(self, query: str) -> list:
        excluded = set(re.findall(r"@logStream\s*!=\s*'([^']+)'", query))
        lines = [ln for ln in self._lines if ln["stream"] not in excluded]
        if "count_distinct(@logStream)" in query:
            return _streams(lines, hourly="bin(1h)" in query)
        if "trace_id=" in query and '"traceId"' in query:
            return _turn_hours(lines)
        if "as otel_stream" in query:
            return _by_kind(lines, query)
        if "ORCHESTRATOR COMPLETE" in query or "DETERMINISM" in query or "Tool #" in query:
            return _naive(lines, query)
        raise AssertionError(f"unexpected Insights query: {query!r}")


def _turn_key(ln: dict) -> str:
    m = _TRACE.search(ln["message"])
    if m:
        return m.group(1) or m.group(2)
    s = _SESSION.search(ln["message"])
    return f"line:{ln['stream']}:{ln['ms']}:{s.group(1) if s else '-'}"


def _turn_hours(lines: list[dict]) -> list:
    """One row per hour of folded turns: sessions, events, tool_calls, tooled.
    A turn with no COMPLETE line lands in an hour ``0`` row with no sessions."""
    turns = defaultdict(lambda: {"done": False, "events": 0, "line": None, "det": None, "hour": "0"})
    for ln in lines:
        msg, t = ln["message"], turns[_turn_key(ln)]
        if "ORCHESTRATOR COMPLETE" in msg:
            t.update(done=True, hour=ln["hour"], events=int(_SESSION.search(msg).group(2)))
            if m := _LINE_TOOLS.search(msg):
                t["line"] = int(m.group(1))
        elif m := _DET_TOOLS.search(msg):
            t["det"] = int(m.group(1))
    hours = defaultdict(lambda: {"sessions": 0, "events": 0, "tool_calls": 0, "tooled": 0})
    for t in turns.values():
        h = hours[t["hour"] if t["done"] else "0"]
        tools = t["line"] if t["line"] is not None else t["det"]
        if t["done"]:
            h["sessions"] += 1
            h["events"] += t["events"]
            h["tool_calls"] += tools or 0
            h["tooled"] += tools is not None
    return [_row(hour=k, **v) for k, v in sorted(hours.items())]


def _by_kind(lines: list[dict], query: str) -> list:
    """A query grouped by stream kind: per-kind counts, as Insights gives."""
    hourly = "bin(1h)" in query
    marker = "ORCHESTRATOR COMPLETE" if "ORCHESTRATOR COMPLETE" in query else "DETERMINISM: "
    groups = defaultdict(list)
    for ln in lines:
        if marker in ln["message"]:
            key = {"otel_stream": int(ln["stream"] == OTEL), "span_stream": 0}
            if hourly:
                key["hour"] = ln["hour"]
            groups[tuple(sorted(key.items()))].append(ln["message"])
    rows = []
    for key, msgs in sorted(groups.items()):
        fields = dict(key)
        if marker == "DETERMINISM: ":
            fields.update(tool_calls=sum(int(_DET_TOOLS.search(m).group(1)) for m in msgs),
                          turns=len(msgs))
        else:
            fields["sessions"] = len(msgs)
            if not hourly:
                fields["avg_events"] = sum(int(_SESSION.search(m).group(2)) for m in msgs) / len(msgs)
        rows.append(_row(**fields))
    return sorted(rows, key=lambda r: _parse(r).get("hour", ""))


def _naive(lines: list[dict], query: str) -> list:
    marker = re.search(r"like /([^/]+)/", query).group(1)
    return [_row(sessions=sum(marker in ln["message"] for ln in lines))]


def _streams(lines: list[dict], hourly: bool) -> list:
    if not hourly:
        return [_row(sessions=len({ln["stream"] for ln in lines}))]
    by_hour = defaultdict(set)
    for ln in lines:
        by_hour[ln["hour"]].add(ln["stream"])
    return [_row(hour=h, sessions=len(s)) for h, s in sorted(by_hour.items())]


def _row(**fields) -> list[dict]:
    return [{"field": k, "value": str(v)} for k, v in fields.items()]


def _parse(row: list[dict]) -> dict:
    return {f["field"]: f["value"] for f in row}


class _FakeSession:
    def __init__(self, logs: _FakeInsights):
        self.logs = logs

    def client(self, name, **kwargs):
        assert name == "logs", f"unexpected client requested: {name}"
        return self.logs


@pytest.fixture(autouse=True)
def _no_poll_wait(monkeypatch):
    monkeypatch.setattr(ar.time, "sleep", lambda _s: None)
    monkeypatch.setattr(insights, "DEFAULT_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(ar, "_get_eval_scores", lambda *a, **k: {})


def _orch_metrics(lines: list[dict], failing: tuple[str, ...] = ()) -> dict:
    return ar._get_metrics(_FakeSession(_FakeInsights(lines, failing)), ORCH_ID, ORCH, 24.0)


@pytest.mark.parametrize("shape", list(SHAPES), ids=list(SHAPES))
def test_each_turn_is_one_session(shape):
    result = _orch_metrics(_lines(SHAPES[shape]))

    assert result["invocations_24h"] == SESSIONS
    assert result["sessions_measured"] is True


@pytest.mark.parametrize("shape", list(SHAPES), ids=list(SHAPES))
def test_each_turn_is_counted_once_in_its_hour(shape):
    result = _orch_metrics(_lines(SHAPES[shape]))

    assert result["hourly_sessions"] == HOURLY
    assert result["hourly_measured"] is True


@pytest.mark.parametrize("shape", list(SHAPES), ids=list(SHAPES))
def test_average_events_per_session_is_the_turns_average(shape):
    result = _orch_metrics(_lines(SHAPES[shape]))

    assert result["avg_events_per_session"] == AVG_EVENTS


@pytest.mark.parametrize("shape", list(SHAPES), ids=list(SHAPES))
def test_tool_calls_are_every_tool_each_turn_called(shape):
    result = _orch_metrics(_lines(SHAPES[shape]))

    assert result["tool_calls_24h"] == TOOL_CALLS
    assert result["tool_calls_measured"] is True


def test_turns_of_one_session_each_count():
    # Every turn of a multi-turn session runs in its own trace.
    lines = []
    for i, turn in enumerate(TURNS):
        lines += _turn_lines(i, dict(turn, session="s-shared"), BOTH)

    result = _orch_metrics(lines)

    assert result["invocations_24h"] == SESSIONS
    assert result["hourly_sessions"] == HOURLY


def test_lines_without_a_trace_id_are_never_folded_together():
    # A runtime that logged no trace id, in runtime streams only: every turn
    # still counts, even two in one session and one hour.
    lines = []
    for i, turn in enumerate(TURNS):
        lines += _turn_lines(i, dict(turn, session="s-shared"), (RUNTIME,), keyed=False)

    result = _orch_metrics(lines)

    assert result["invocations_24h"] == SESSIONS
    assert result["hourly_sessions"] == HOURLY


def test_a_turn_that_ran_tools_but_logged_no_tool_count_leaves_tools_unmeasured():
    # The last turn's DETERMINISM line was never written (its stream failed
    # after its tools ran) and its COMPLETE line predates the tool field.
    lines = (_turn_lines(0, TURNS[0], BOTH) + _turn_lines(1, TURNS[1], BOTH)
             + _turn_lines(2, TURNS[2], BOTH, determinism=False))

    result = _orch_metrics(lines)

    assert result["invocations_24h"] == SESSIONS
    assert result["tool_calls_measured"] is False


def test_the_complete_line_tool_count_needs_no_determinism_line():
    result = _orch_metrics(_lines(SHAPES["complementary across hours"],
                                  determinism=False, line_tools=True))

    assert result["tool_calls_24h"] == TOOL_CALLS
    assert result["tool_calls_measured"] is True


def test_a_tool_line_whose_turn_did_not_complete_in_the_window_is_not_counted():
    # The turn's COMPLETE line falls after the window; only its DETERMINISM
    # line is in it.
    lines = [ln for ln in _lines(SHAPES["both copies"])
             if not (_trace(2) in ln["message"] and "ORCHESTRATOR COMPLETE" in ln["message"])]

    result = _orch_metrics(lines)

    assert result["invocations_24h"] == SESSIONS - 1
    assert result["hourly_sessions"] == [2]
    assert result["tool_calls_24h"] == 3


def test_a_quiet_window_is_still_zero_with_no_stream_fallback():
    logs = _FakeInsights(_lines([(), (), ()]))

    result = ar._get_metrics(_FakeSession(logs), ORCH_ID, ORCH, 24.0)

    assert result["invocations_24h"] == 0
    assert result["hourly_sessions"] == []
    assert result["tool_calls_24h"] == 0
    assert not any("count_distinct" in q for q in logs.queries)


def _usage(lines: list[dict], failing: tuple[str, ...] = (), gateway_calls: int = 1,
           native: dict | None = None) -> dict:
    fc = {"session": _FakeSession(_FakeInsights(lines, failing)), "hours": 24.0,
          "orch_pool": {"input": 0, "output": 0, "cost": 0.0, "latency_ms": None},
          "total_tool_calls": gateway_calls, "orch_model_id": "model-x"}
    rt = {"agentRuntimeName": ORCH, "agentRuntimeId": ORCH_ID}
    return ar._fleet_orchestrator_usage(fc, rt, native or {"invocations": 99, "hourly": [99]})


def test_the_orchestrator_card_shows_its_turns_tool_calls():
    # The gateway metric sees only the calls that went through the gateway;
    # the card counts every tool the turns called.
    usage = _usage(_lines(SHAPES["both copies"]), gateway_calls=2)

    assert usage["tool_calls"] == TOOL_CALLS
    assert usage["invocations"] == SESSIONS
    assert usage["hourly"] == HOURLY


def test_the_card_falls_back_to_gateway_calls_when_a_turns_tools_are_unread():
    lines = (_turn_lines(0, TURNS[0], BOTH) + _turn_lines(1, TURNS[1], BOTH)
             + _turn_lines(2, TURNS[2], BOTH, determinism=False))

    usage = _usage(lines, gateway_calls=2)

    assert usage["tool_calls"] == 2
    assert usage["invocations"] == SESSIONS


def test_the_card_falls_back_to_gateway_calls_when_the_query_fails():
    usage = _usage(_lines(SHAPES["both copies"]), failing=("ORCHESTRATOR COMPLETE",),
                   gateway_calls=2)

    assert usage["tool_calls"] == 2


def test_the_card_does_not_prefer_the_per_line_session_metric():
    # The optional SessionCount metric filter adds one point per COMPLETE
    # line, so it counts every copy; the card keeps the folded Insights count.
    usage = _usage(_lines(SHAPES["both copies"]),
                   native={"invocations": 99, "hourly": [99], "sessions": 6, "avg_events": 7})

    assert usage["invocations"] == SESSIONS
    assert usage["avg_events"] == AVG_EVENTS


def test_the_stream_fallback_does_not_count_the_shared_streams():
    # The session query fails, so the count falls back to per-session streams:
    # one runtime stream per turn, never the shared otel-rt-logs or spans.
    result = _orch_metrics(_lines(SHAPES["both copies"]), failing=("ORCHESTRATOR COMPLETE",))

    assert result["sessions_measured"] is False
    assert result["invocations_24h"] == SESSIONS


def test_a_system_agents_stream_counts_leave_out_the_shared_streams():
    logs = _FakeInsights(_lines(SHAPES["both copies"]))

    result = ar._get_metrics(_FakeSession(logs), SYSTEM_ID, SYSTEM, 24.0)

    assert result["hourly_sessions"] == HOURLY
    assert result["invocations_24h"] == SESSIONS


def test_the_complete_line_carries_the_turns_tool_count(caplog):
    import agents.runtime_app as runtime_app
    s = SimpleNamespace(
        session_id="s-9", event_count=12, skipped_count=0, total_yielded_bytes=0,
        bytes_by_category={"message": 0, "event": 0, "current_tool_use": 0},
        tool_calls_log=[{"name": "a", "toolUseId": "1"}, {"name": "b", "toolUseId": "2"}])

    with caplog.at_level(logging.INFO, logger=runtime_app.logger.name):
        runtime_app._log_turn_complete(s)

    line = next(r.getMessage() for r in caplog.records if "ORCHESTRATOR COMPLETE" in r.getMessage())
    assert re.search(r"session=s-9 total_events=12 tool_calls=2 ", line)


def test_the_metric_filter_session_count_is_never_read_for_the_card(monkeypatch):
    calls = Mock(side_effect=lambda *a, **k: {"invocations_24h": 4, "sessions_measured": True,
                                               "hourly_sessions": [4], "hourly_measured": True})
    monkeypatch.setattr(ar, "_get_metrics", calls)
    fc = {"session": None, "hours": 24.0, "total_tool_calls": 0, "orch_model_id": "m",
          "orch_pool": {"input": 0, "output": 0, "cost": 0.0, "latency_ms": None}}
    rt = {"agentRuntimeName": ORCH, "agentRuntimeId": ORCH_ID}

    usage = ar._fleet_orchestrator_usage(fc, rt, {"invocations": 9, "hourly": [9], "sessions": 8})

    assert calls.called
    assert usage["invocations"] == 4
