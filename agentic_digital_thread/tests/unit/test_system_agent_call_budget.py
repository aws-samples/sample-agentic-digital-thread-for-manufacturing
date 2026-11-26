# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Soft tool-call / wall-clock budget for system agents (the gateway-window guard).

``limits={"turns": 8}`` bounds loop ITERATIONS, but one turn can carry many
parallel tool calls and there is no wall bound — observed live: a 56-call
SiteWise sweep overran the ~75s gateway window into a silent-empty envelope the
orchestrator retried. The ``_ToolCallBudget`` hook cancels further tool calls
once either cap trips, forcing the model to return its best PARTIAL answer with
an explicit marker ('partial: stopped at the limit of N data lookups …') — real
text instead of a silent timeout. The wording is operator-facing prose; the
machine contract is the 'partial: ' PREFIX (pinned below) plus the
'partial: stopped at' stem the UI call classifier matches
(frontend/src/lib/arch-store.ts PARTIAL_MARKER).

Fully offline: the hook is exercised with a stubbed agent loop (fake events);
no Bedrock model, no AWS client (mirrors the test_sitewise_schema_seed idiom).
The test of an exact split started in a call runs the split on a clock that
starts when the split does (``_athena_split_clock``), with pieces that run
until the split stops them (``RunsUntilStopped``).
"""
import pytest

import agents.system_agent as sa


class _FakeModel:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs


class _FakeAgent:
    """Captures what create_system_agent built without running an LLM."""

    def __init__(self, *args, **kwargs):
        self.system_prompt = kwargs.get("system_prompt", "")
        self.tools = kwargs.get("tools") or []
        self.hooks = kwargs.get("hooks") or []


class _FakeToolCallEvent:
    """Writable stand-in for strands' BeforeToolCallEvent (which is attr-locked)."""

    def __init__(self, invocation_state):
        self.invocation_state = invocation_state
        self.cancel_tool = False


# --------------------------------------------------------------------------- #
# 1. Env-tunable budget knobs
# --------------------------------------------------------------------------- #
def test_budget_defaults(monkeypatch):
    monkeypatch.delenv("SYSTEM_AGENT_TOOL_CALL_BUDGET", raising=False)
    monkeypatch.delenv("SYSTEM_AGENT_WALL_BUDGET_SECONDS", raising=False)
    assert sa._tool_call_budget() == 40
    assert sa._wall_budget_seconds() == 60.0


def test_budget_env_overrides(monkeypatch):
    monkeypatch.setenv("SYSTEM_AGENT_TOOL_CALL_BUDGET", "12")
    monkeypatch.setenv("SYSTEM_AGENT_WALL_BUDGET_SECONDS", "30")
    assert sa._tool_call_budget() == 12
    assert sa._wall_budget_seconds() == 30.0


def test_budget_bad_env_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("SYSTEM_AGENT_TOOL_CALL_BUDGET", "banana")
    monkeypatch.setenv("SYSTEM_AGENT_WALL_BUDGET_SECONDS", "-5")
    assert sa._tool_call_budget() == 40
    assert sa._wall_budget_seconds() == 60.0
    monkeypatch.setenv("SYSTEM_AGENT_TOOL_CALL_BUDGET", "0")
    assert sa._tool_call_budget() == 1  # clamped to a sane minimum


# --------------------------------------------------------------------------- #
# 2. Budget trip — stubbed agent loop drives the partial-answer path
# --------------------------------------------------------------------------- #
def _start_invocation(budget):
    state = {}
    budget._on_invocation_start(type("Start", (), {"invocation_state": state})())
    return state


def test_call_budget_trips_with_explicit_partial_marker():
    budget = sa._ToolCallBudget(max_calls=3, max_wall_seconds=600.0)
    state = _start_invocation(budget)

    cancelled_at = None
    cancel_message = ""
    for i in range(1, 10):
        event = _FakeToolCallEvent(state)
        budget._on_before_tool_call(event)
        if event.cancel_tool:
            cancelled_at = i
            cancel_message = event.cancel_tool
            break

    assert cancelled_at == 4
    assert ("partial: stopped at the limit of 3 data lookups for this question "
            "— some results may be missing") in cancel_message
    assert "TOOL BUDGET EXHAUSTED" in cancel_message
    assert "fabricate" in cancel_message.lower()


def test_once_tripped_every_further_tool_call_is_cancelled():
    budget = sa._ToolCallBudget(max_calls=1, max_wall_seconds=600.0)
    state = _start_invocation(budget)
    first = _FakeToolCallEvent(state)
    budget._on_before_tool_call(first)
    assert first.cancel_tool is False
    for _ in range(3):
        event = _FakeToolCallEvent(state)
        budget._on_before_tool_call(event)
        assert event.cancel_tool


def test_wall_budget_trips_with_time_marker():
    budget = sa._ToolCallBudget(max_calls=100, max_wall_seconds=60.0)
    state = _start_invocation(budget)
    state[sa._TOOL_BUDGET_STARTED_STATE_KEY] -= 120
    event = _FakeToolCallEvent(state)
    budget._on_before_tool_call(event)
    assert event.cancel_tool
    assert ("partial: stopped at the 60-second time limit for this question "
            "— some results may be missing") in event.cancel_tool


def test_both_markers_keep_the_pinned_partial_prefix_contract():
    """Wording is free prose; 'partial: ' as a PREFIX is the machine contract.

    The orchestrator/UI treat an answer STARTING with 'partial: ' as honestly
    incomplete, and the UI classifier additionally matches the 'stopped at'
    stem — so both stay pinned even as the prose is humanized.
    """
    for marker in (sa._budget_partial_marker(40), sa._wall_partial_marker(60.0)):
        assert marker.startswith("partial: ")
        assert "partial: stopped at" in marker
        # Humanized prose, not internal jargon.
        assert "budget" not in marker


def test_counters_reset_per_invocation():
    budget = sa._ToolCallBudget(max_calls=1, max_wall_seconds=60.0)
    first_state = _start_invocation(budget)
    budget._on_before_tool_call(_FakeToolCallEvent(first_state))
    tripped = _FakeToolCallEvent(first_state)
    budget._on_before_tool_call(tripped)
    assert tripped.cancel_tool
    fresh_state = _start_invocation(budget)
    fresh = _FakeToolCallEvent(fresh_state)
    budget._on_before_tool_call(fresh)
    assert fresh.cancel_tool is False


def test_overlapping_invocations_have_independent_call_and_time_budgets():
    now = {"value": 1000.0}
    budget = sa._ToolCallBudget(max_calls=2, max_wall_seconds=10.0)
    budget._time = type("Clock", (), {"monotonic": staticmethod(lambda: now["value"])})

    state_a = _start_invocation(budget)
    now["value"] = 1004.0
    state_b = _start_invocation(budget)

    budget._on_before_tool_call(_FakeToolCallEvent(state_a))
    budget._on_before_tool_call(_FakeToolCallEvent(state_a))
    b_first = _FakeToolCallEvent(state_b)
    budget._on_before_tool_call(b_first)
    assert b_first.cancel_tool is False

    a_third = _FakeToolCallEvent(state_a)
    budget._on_before_tool_call(a_third)
    assert "limit of 2 data lookups" in a_third.cancel_tool

    now["value"] = 1011.0
    a_time = _FakeToolCallEvent(state_a)
    budget._on_before_tool_call(a_time)
    assert a_time.cancel_tool
    b_second = _FakeToolCallEvent(state_b)
    budget._on_before_tool_call(b_second)
    assert b_second.cancel_tool is False


def test_register_hooks_binds_invocation_and_tool_events():
    seen = []

    class _FakeRegistry:
        def add_callback(self, event_type, callback):
            seen.append(event_type)

    sa._ToolCallBudget(40, 60.0).register_hooks(_FakeRegistry())
    from strands.hooks import BeforeInvocationEvent, BeforeToolCallEvent

    assert BeforeInvocationEvent in seen
    assert BeforeToolCallEvent in seen


# --------------------------------------------------------------------------- #
# 3. Wiring — every system agent carries the budget hook (env-tuned)
# --------------------------------------------------------------------------- #
def _fake_agent(monkeypatch, **kwargs):
    monkeypatch.setattr(sa, "Agent", _FakeAgent)
    monkeypatch.setattr("strands.models.bedrock.BedrockModel", _FakeModel)
    defaults = dict(
        system_id="erp-x", system_name="ERP X", schema="erp_data",
        system_prompt="", tables_desc="t(a: int)",
        conn_params={"host": "h", "dbname": "d"}, connection_type="PostgreSQL",
    )
    defaults.update(kwargs)
    return sa.create_system_agent(**defaults)


def test_agent_constructed_with_budget_hook(monkeypatch):
    monkeypatch.setenv("SYSTEM_AGENT_TOOL_CALL_BUDGET", "17")
    monkeypatch.setenv("SYSTEM_AGENT_WALL_BUDGET_SECONDS", "45")
    agent = _fake_agent(monkeypatch)
    budgets = [h for h in agent.hooks if isinstance(h, sa._ToolCallBudget)]
    assert len(budgets) == 1
    assert budgets[0].max_calls == 17
    assert budgets[0].max_wall_seconds == 45.0


def test_sitewise_agent_also_gets_budget_hook(monkeypatch):
    import agents.sitewise_read as sitewise_read
    monkeypatch.setattr(
        sitewise_read, "list_asset_models_with_properties",
        lambda cfg, region, max_models=None: [])
    agent = _fake_agent(
        monkeypatch, connection_type="SiteWise (Direct)",
        conn_params=None, connection_config={}, tables_desc="")
    assert any(isinstance(h, sa._ToolCallBudget) for h in agent.hooks)


# --------------------------------------------------------------------------- #
# 4. Prompt carries the budget note + marker for every connection type
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ctype", ["PostgreSQL", "Athena", "SiteWise (Direct)", "MCP Server"])
def test_prompt_contains_budget_note_and_marker(monkeypatch, ctype):
    monkeypatch.delenv("SYSTEM_AGENT_TOOL_CALL_BUDGET", raising=False)
    prompt = sa._compose_system_prompt(
        system_id="x", system_name="X", schema="x_data",
        system_prompt="", tables_desc="", connection_type=ctype,
    )
    assert "BUDGET (required)" in prompt
    assert ("partial: stopped at the limit of 40 data lookups for this question "
            "— some results may be missing") in prompt
    # Standing directives are untouched.
    assert "SELF-RECOVERY" in prompt
    assert "EFFICIENCY" in prompt


def test_prompt_budget_note_tracks_env_budget(monkeypatch):
    monkeypatch.setenv("SYSTEM_AGENT_TOOL_CALL_BUDGET", "25")
    prompt = sa._compose_system_prompt(
        system_id="x", system_name="X", schema="x_data",
        system_prompt="", tables_desc="", connection_type="PostgreSQL",
    )
    assert ("partial: stopped at the limit of 25 data lookups for this question "
            "— some results may be missing") in prompt


# --------------------------------------------------------------------------- #
# 5. One Athena call fits the question budget; engine waiting is credited
# --------------------------------------------------------------------------- #
# Measured in an answer eval (3 of 5 runs of one question failed): the budget
# was checked only before a call, so ONE Athena call ran 146-189 s inside a
# 60 s question (an exact split's piece queued 110 s; a plain query sat 145 s
# in the engine and was retried), and the next call, the right query, was
# cancelled. Each Athena call now runs to the question's remaining budget at
# its start, and the time a query waited in the engine's queue, or the
# stopped attempt of a retried poll, is credited like a retried model stall.
_ATHENA_CFG = {"workgroup": "wg", "database": "db_a", "region": "us-east-1"}
_COUNT = "SELECT COUNT(*) AS n FROM table_x"


class _Ctx:
    """Stands in for strands' ToolContext (the tool reads invocation_state)."""

    def __init__(self, state):
        self.invocation_state = state


class _QueuedAthena:
    """A boto3 Athena double whose query is in ``state`` (SUCCEEDED by default)
    after ``took`` seconds on the test clock per poll, reporting ``queue_ms``
    of queue time (None: no statistic). Once stopped, a read is instant: the
    first ``running_after_stop`` reads still say RUNNING with no statistics
    (cancellation is asynchronous), then CANCELLED with ``terminal`` ones."""

    def __init__(self, clock, *, took, queue_ms, state="SUCCEEDED", terminal=None,
                 running_after_stop=0):
        self.clock, self.took, self.queue_ms, self.state = clock, took, queue_ms, state
        self.terminal = {"DataScannedInBytes": 10, **(terminal or {})}
        self.running_after_stop = running_after_stop
        self.reads_after_stop = 0
        self.started: list[str] = []
        self.stopped: list[str] = []

    def start_query_execution(self, QueryString, **_kwargs):
        self.started.append(QueryString)
        return {"QueryExecutionId": f"q{len(self.started)}"}

    def get_query_execution(self, QueryExecutionId):
        if QueryExecutionId in self.stopped:
            self.reads_after_stop += 1
            if self.reads_after_stop <= self.running_after_stop:
                return {"QueryExecution": {"Status": {"State": "RUNNING"},
                                           "Statistics": {"DataScannedInBytes": 0}}}
            return {"QueryExecution": {"Status": {"State": "CANCELLED"},
                                       "Statistics": dict(self.terminal)}}
        self.clock["t"] += self.took
        statistics = {"DataScannedInBytes": 10}
        if self.queue_ms is not None:
            statistics["QueryQueueTimeInMillis"] = self.queue_ms
        return {"QueryExecution": {"Status": {"State": self.state}, "Statistics": statistics}}

    def stop_query_execution(self, QueryExecutionId=None, **_kwargs):
        self.stopped.append(QueryExecutionId)

    def get_query_results(self, QueryExecutionId, **_kwargs):
        return {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": [{"Name": "n", "Type": "bigint"}]},
                              "Rows": [{"Data": [{"VarCharValue": "n"}]},
                                       {"Data": [{"VarCharValue": "3"}]}]}}


def _clocked(monkeypatch, *, took, queue_ms, state="SUCCEEDED", terminal=None,
             running_after_stop=0):
    """A 60 s budget, the Athena tool, and one test clock for both (the
    connector's poll included)."""
    import types

    import agents.athena_call_time as act
    import agents.athena_tools as at

    clock = {"t": 1000.0}
    monkeypatch.setattr(act, "now", lambda: clock["t"])
    monkeypatch.setattr(at, "time", types.SimpleNamespace(monotonic=lambda: clock["t"],
                                                          sleep=lambda _s: None))
    fake = _QueuedAthena(clock, took=took, queue_ms=queue_ms, state=state, terminal=terminal,
                         running_after_stop=running_after_stop)
    # Pauses (the deferred reads of a stopped query) move the test clock.
    monkeypatch.setattr(act, "pause", lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
                        raising=False)
    monkeypatch.setattr(at, "boto3_client_for", lambda *_a, **_k: fake)
    budget = sa._ToolCallBudget(max_calls=40, max_wall_seconds=60.0)
    budget._time = type("Clock", (), {"monotonic": staticmethod(lambda: clock["t"])})
    state = _start_invocation(budget)
    guarded = sa._make_scoped_athena_run_query(_ATHENA_CFG, {"table_x": []})
    return budget, state, guarded, fake


def _call(budget, state, guarded) -> dict:
    import json
    event = _FakeToolCallEvent(state)
    budget._on_before_tool_call(event)
    assert event.cancel_tool is False
    return json.loads(guarded._tool_func(sql=_COUNT, tool_context=_Ctx(state)))


def test_queue_time_of_a_call_is_credited_so_the_next_call_runs(monkeypatch):
    budget, state, guarded, fake = _clocked(monkeypatch, took=113.0, queue_ms=110_000)
    assert _call(budget, state, guarded)["rows"] == [["3"]]
    assert sa._engine_wait_seconds(state) == pytest.approx(110.0)
    # 113 s on the clock, 110 of them in the engine's queue: 3 s of work.
    nxt = _FakeToolCallEvent(state)
    budget._on_before_tool_call(nxt)
    assert nxt.cancel_tool is False


@pytest.mark.parametrize("queue_ms", [None, -5_000, "110000", True])
def test_a_missing_or_bad_queue_statistic_credits_nothing(monkeypatch, queue_ms):
    budget, state, guarded, _fake = _clocked(monkeypatch, took=113.0, queue_ms=queue_ms)
    _call(budget, state, guarded)
    assert sa._engine_wait_seconds(state) == 0.0
    nxt = _FakeToolCallEvent(state)
    budget._on_before_tool_call(nxt)
    # The pinned marker is unchanged.
    assert ("partial: stopped at the 60-second time limit for this question "
            "— some results may be missing") in nxt.cancel_tool


def test_the_credit_never_runs_past_the_callers_window(monkeypatch):
    from agents.model_stream_retry import retry_window

    budget, state, guarded, _fake = _clocked(monkeypatch, took=113.0, queue_ms=110_000)
    with retry_window(10.0):
        _call(budget, state, guarded)
    # The caller stops listening ~10 s after the call returned (t=1113):
    # the question may end by 1123, 63 s past its own 60 s, not 110.
    assert sa._engine_wait_seconds(state) == pytest.approx(63.0, abs=0.5)


def test_the_credit_resets_with_the_invocation(monkeypatch):
    budget, state, guarded, _fake = _clocked(monkeypatch, took=113.0, queue_ms=110_000)
    _call(budget, state, guarded)
    budget._on_invocation_start(type("Start", (), {"invocation_state": state})())
    assert sa._engine_wait_seconds(state) == 0.0
    assert sa._ENGINE_WAIT_SPANS_STATE_KEY not in state


def test_calls_waiting_at_the_same_time_are_credited_once():
    state = {}
    first, second = sa._engine_wait_spans(state), sa._engine_wait_spans(state)
    assert first is second
    first.add(100.0, 150.0)     # a query of one call queued from 100 s to 150 s
    second.add(120.0, 160.0)    # a parallel call's query queued from 120 s to 160 s
    second.add(200.0, 205.0)
    sa._credit_engine_wait(state, first)
    assert sa._engine_wait_seconds(state) == pytest.approx(65.0)


def test_a_call_stopped_at_its_deadline_while_queued_credits_the_queued_span(monkeypatch):
    from agents.model_stream_retry import retry_window

    # Athena reports no queue time for a query still QUEUED. Polled at
    # 1010 ... 1060 (10 s a poll), QUEUED each time, stopped at the question's
    # deadline (1060) with no retry: queued 50 s by our own polls.
    budget, state, guarded, fake = _clocked(monkeypatch, took=10.0, queue_ms=None, state="QUEUED")
    payload = _call(budget, state, guarded)
    assert payload["error_type"] == "query_timeout" and "retried_once" not in payload
    assert len(fake.started) == 1 and fake.stopped == ["q1"]
    assert sa._engine_wait_seconds(state) == pytest.approx(50.0)
    nxt = _FakeToolCallEvent(state)
    budget._on_before_tool_call(nxt)
    assert nxt.cancel_tool is False
    # Stopped while RUNNING: it left the queue, so nothing is credited.
    budget, state, guarded, fake = _clocked(monkeypatch, took=10.0, queue_ms=None, state="RUNNING")
    _call(budget, state, guarded)
    assert sa._engine_wait_seconds(state) == 0.0
    # The same caller-window bound: the caller stops listening ~20 s after
    # the stop, so 20 s of the 50 are credited.
    budget, state, guarded, fake = _clocked(monkeypatch, took=10.0, queue_ms=None, state="QUEUED")
    with retry_window(20.0):
        _call(budget, state, guarded)
    assert sa._engine_wait_seconds(state) == pytest.approx(20.0, abs=0.5)


def test_a_call_stopped_while_athena_held_it_in_service_credits_that_wait(monkeypatch):
    # Polled RUNNING with no wait statistic in flight until the question's
    # deadline (1055; the poll that crosses it is at 1060). After the stop
    # Athena reports the time it held the query (service processing, here
    # 144,796 ms of a stopped query that scanned nothing); at most the 60 s
    # polled is credited, so the next call, the right query, still runs.
    budget, state, guarded, fake = _clocked(
        monkeypatch, took=10.0, queue_ms=None, state="RUNNING",
        terminal={"QueryQueueTimeInMillis": 266, "QueryPlanningTimeInMillis": 3_344,
                  "EngineExecutionTimeInMillis": 5_219, "ServiceProcessingTimeInMillis": 144_796})
    state[sa._TOOL_BUDGET_STARTED_STATE_KEY] -= 5.0     # the question began 5 s before the call
    payload = _call(budget, state, guarded)
    assert payload["error_type"] == "query_timeout" and fake.stopped == ["q1"]
    assert sa._engine_wait_seconds(state) == pytest.approx(60.0)
    nxt = _FakeToolCallEvent(state)
    budget._on_before_tool_call(nxt)
    assert nxt.cancel_tool is False


_IN_SERVICE = {"QueryQueueTimeInMillis": 266, "QueryPlanningTimeInMillis": 3_344,
               "EngineExecutionTimeInMillis": 5_219, "ServiceProcessingTimeInMillis": 144_796}


def test_a_stop_reported_late_is_credited_before_the_next_budget_check(monkeypatch):
    # The same stall, but Athena still says RUNNING on both reads right after
    # the stop and reports the service time only later. The call credits
    # nothing; the next budget check reads the stopped query again first,
    # credits the 60 s polled, and does not cancel the next call.
    budget, state, guarded, fake = _clocked(monkeypatch, took=10.0, queue_ms=None, state="RUNNING",
                                            terminal=_IN_SERVICE, running_after_stop=2)
    state[sa._TOOL_BUDGET_STARTED_STATE_KEY] -= 5.0
    payload = _call(budget, state, guarded)
    assert payload["error_type"] == "query_timeout" and fake.reads_after_stop == 2
    assert sa._engine_wait_seconds(state) == 0.0
    nxt = _FakeToolCallEvent(state)
    budget._on_before_tool_call(nxt)
    assert nxt.cancel_tool is False
    assert fake.reads_after_stop == 3
    assert sa._engine_wait_seconds(state) == pytest.approx(60.0)
    # Settled once: a later check reads nothing more.
    budget._on_before_tool_call(_FakeToolCallEvent(state))
    assert fake.reads_after_stop == 3


def test_the_late_stop_is_credited_on_the_real_clock_before_the_next_check():
    import time

    import agents.athena_call_time as act

    # Real clock and a real (slightly slow) read: the stopped query polled for
    # 60 s answers CANCELLED with the live service time; the check credits it
    # and does not cancel, well inside the bound.
    budget = sa._ToolCallBudget(max_calls=40, max_wall_seconds=60.0)
    state = _start_invocation(budget)
    state[sa._TOOL_BUDGET_STARTED_STATE_KEY] -= 65.0
    with act.recording(sa._engine_wait_spans(state)):
        watch = act.PollWatch()
        watch._began -= 60.0
        watch.stopped()

        def read():
            time.sleep(0.05)
            return True, {"QueryQueueTimeInMillis": 266, "ServiceProcessingTimeInMillis": 144_796}

        watch.defer(read)
    event = _FakeToolCallEvent(state)
    started = time.monotonic()
    budget._on_before_tool_call(event)
    assert event.cancel_tool is False
    assert sa._engine_wait_seconds(state) == pytest.approx(60.0, abs=0.5)
    assert time.monotonic() - started < 1.0


def test_a_hung_stop_never_costs_the_next_check_the_late_stops_credit(monkeypatch):
    import threading
    import time

    import agents.athena_call_time as act
    from agents.model_stream_retry import retry_window

    # Two kept stops: the first's read hangs, the second answers CANCELLED at
    # once with the live service time. The check reads both side by side
    # (the 5 s reconcile cap scaled to 0.3 s), credits the second's 60 s
    # polled, and does not cancel; the hung stop, whose own read used its
    # whole cap, is dropped. The caller's window is long enough for the
    # credit to fit inside it.
    monkeypatch.setattr(act, "RECONCILE_MAX_SECONDS", 0.3)
    budget = sa._ToolCallBudget(max_calls=40, max_wall_seconds=60.0)
    state = _start_invocation(budget)
    state[sa._TOOL_BUDGET_STARTED_STATE_KEY] -= 65.0
    release = threading.Event()
    spans = sa._engine_wait_spans(state)
    with act.recording(spans):
        for read in (lambda: (release.wait(10), (False, {}))[1],
                     lambda: (True, {"QueryQueueTimeInMillis": 266,
                                     "ServiceProcessingTimeInMillis": 144_796})):
            watch = act.PollWatch()
            watch._began -= 60.0
            watch.stopped()
            watch.defer(read)
    event = _FakeToolCallEvent(state)
    started = time.monotonic()
    with retry_window(120.0):
        budget._on_before_tool_call(event)
    took = time.monotonic() - started
    release.set()
    assert event.cancel_tool is False
    assert sa._engine_wait_seconds(state) == pytest.approx(60.0, abs=0.5)
    assert spans.pending_count() == 0
    assert took < 0.6, took


def test_a_stop_that_never_settles_credits_nothing_and_the_wait_is_bounded(monkeypatch):
    import agents.athena_call_time as act

    budget, state, guarded, fake = _clocked(monkeypatch, took=10.0, queue_ms=None, state="RUNNING",
                                            terminal=_IN_SERVICE, running_after_stop=10 ** 6)
    state[sa._TOOL_BUDGET_STARTED_STATE_KEY] -= 5.0
    _call(budget, state, guarded)
    began = fake.clock["t"]
    nxt = _FakeToolCallEvent(state)
    budget._on_before_tool_call(nxt)
    assert fake.clock["t"] - began <= act.RECONCILE_MAX_SECONDS
    assert sa._engine_wait_seconds(state) == 0.0
    assert ("partial: stopped at the 60-second time limit for this question "
            "— some results may be missing") in nxt.cancel_tool


def test_the_deferred_reads_stay_inside_the_callers_window(monkeypatch):
    from agents.model_stream_retry import retry_window

    budget, state, guarded, fake = _clocked(monkeypatch, took=10.0, queue_ms=None, state="RUNNING",
                                            terminal=_IN_SERVICE, running_after_stop=10 ** 6)
    state[sa._TOOL_BUDGET_STARTED_STATE_KEY] -= 5.0
    with retry_window(1.0):
        _call(budget, state, guarded)
        began = fake.clock["t"]
        budget._on_before_tool_call(_FakeToolCallEvent(state))
    assert fake.clock["t"] - began <= 1.0


def test_no_deferred_read_outside_a_budgeted_invocation(monkeypatch):
    import json

    _budget, _state, guarded, fake = _clocked(monkeypatch, took=10.0, queue_ms=None,
                                              state="RUNNING", terminal=_IN_SERVICE,
                                              running_after_stop=10 ** 6)
    payload = json.loads(guarded._tool_func(sql=_COUNT))      # no invocation: no budget hook
    assert payload["error_type"] == "query_timeout" and payload.get("retried_once") is True
    assert fake.reads_after_stop == 4                          # two right after each stop
    sa._reconcile_engine_wait({})                              # no recorded stops: nothing read
    sa._reconcile_engine_wait(None)
    assert fake.reads_after_stop == 4


def _lake_tool(monkeypatch, lake, cfg, *, left: float):
    import agents.athena_tools as at
    from . import test_athena_exact_split as base

    monkeypatch.setattr(at, "boto3_client_for",
                        lambda service, *_a, **_k: lake.glue if service == "glue" else lake)
    budget = sa._ToolCallBudget(max_calls=40, max_wall_seconds=60.0)
    state = _start_invocation(budget)
    state[sa._TOOL_BUDGET_STARTED_STATE_KEY] -= 60.0 - left
    guarded = sa._make_scoped_athena_run_query({**base._CFG, **cfg}, {}, "lake-a")
    return guarded, state


def test_a_split_started_in_the_call_stops_at_the_question_deadline(monkeypatch):
    import json

    from agents import athena_exact_split as sp

    from . import test_athena_exact_split as base
    from ._athena_split_clock import split_clock
    from ._athena_split_fakes import FakeLake, RunsUntilStopped

    # With no reserve kept back, the split's deadline is the question's: its
    # own cap (6 s) is longer than the 2 s the question has left.
    monkeypatch.setenv("SYSTEM_AGENT_SPLIT_RESERVE_SECONDS", "0")
    lake = FakeLake(columns=base._COLUMNS, rows=base._rows(), part_col="created",
                    cutoff=base._CUTOFF)
    # A piece keeps running until it is stopped, so it cannot end on its own
    # poll window at the same deadline: only the split's deadline stops it.
    athena = RunsUntilStopped(lake)
    left = 2.0
    guarded, state = _lake_tool(monkeypatch, athena, {"exact_split_max_seconds": 6}, left=left)
    # The split starts with the question's ``left`` seconds however long the
    # plain query before it took, and then runs in real time.
    clock = split_clock(monkeypatch, state, left)
    started = clock.now()
    payload = json.loads(guarded._tool_func(sql=base._P1, tool_context=_Ctx(state)))
    ended = clock.now()
    assert payload["error_type"] == "scan_budget_exceeded", payload
    split = payload["exact_split"]
    assert split["reason"] == "wall_time", payload
    # A piece was running at the deadline, and the split stopped it there.
    pieces = [qid for qid, sql in lake._queries.items() if "FOR VERSION AS OF" in sql]
    assert pieces and set(pieces) <= set(lake.stopped), (pieces, lake.stopped)
    assert sp._PIECES_LATE_DETAIL in split["detail"], split
    # It ran to the question's deadline and ended within the overrun after it.
    assert left - 1e-6 <= ended - started, ended - started
    assert ended <= sa._question_deadline(state) + sp.DEADLINE_OVERRUN_SECONDS, \
        ended - sa._question_deadline(state)


def test_the_poll_window_of_a_call_is_capped_and_no_retry_is_started(monkeypatch):
    import json
    import time

    import agents.athena_tools as at
    from . import test_athena_exact_split as base
    from ._athena_split_fakes import FakeLake

    monkeypatch.setattr(at, "_DEFAULT_POLL_INTERVAL", 0.01)
    lake = FakeLake(columns=base._COLUMNS, rows=base._rows(), part_col="created", cutoff=10 ** 12)
    lake.stall_first_plain = 2
    guarded, state = _lake_tool(monkeypatch, lake, {"poll_timeout_seconds": 5}, left=1.5)
    started = time.monotonic()
    payload = json.loads(guarded._tool_func(sql=base._P1, tool_context=_Ctx(state)))
    assert payload["error_type"] == "query_timeout", payload
    assert "retried_once" not in payload
    assert len(lake.data_queries()) == 1
    took = time.monotonic() - started
    assert took < 3.0
    # The query was stopped while still QUEUED and was not retried: the span
    # the polls saw it queued (most of the 1.5 s window) is credited.
    assert 1.0 < sa._engine_wait_seconds(state) <= took


def test_without_a_budget_the_call_runs_as_before(monkeypatch):
    import json

    budget, state, guarded, fake = _clocked(monkeypatch, took=1.0, queue_ms=500)
    payload = json.loads(guarded._tool_func(sql=_COUNT))
    assert payload["rows"] == [["3"]] and len(fake.started) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
