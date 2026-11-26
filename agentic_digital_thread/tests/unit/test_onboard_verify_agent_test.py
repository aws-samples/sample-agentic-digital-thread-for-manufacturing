# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The onboarding verify step's agent test calls the NEW system's own query
tool through the AgentCore gateway.

It used to stream a whole chat question ("What data is available in ...?")
through the orchestrator with a 30 s thread join. That had four problems:

* a whole chat answer often takes longer than 30 s (a cold runtime too), so
  the wizard usually showed "no answer within 30 s, so this check is
  inconclusive";
* the thread kept running after the wait and burned a chat turn;
* the orchestrator can answer from the knowledge graph without ever calling
  the new system's tool, so a passing answer proved nothing about it;
* it was skipped when only one system was onboarded.

Now the test is a job (``agents.onboard_verify_agent``): it checks the
system's runtime, then its gateway target and the target's tool in the
gateway's tools/list, then calls that tool with the smallest read that proves
it reads this system's data (a row count of one of its tables). The wizard
polls the job until it finishes or its deadline passes. Each failure names the
step that failed.

Offline: a fake control plane, a fake gateway session and a fake clock.
"""
from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import sys
import threading
import time
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.onboard as onboard  # noqa: E402
from agents import connection_types as ct  # noqa: E402

SID = "system-a"
TARGET = "system-a"
MODE_A_TOOL = "system-a___query_system_a"
MODE_B_TOOL = "system-a___run_query"
RUNTIME_NAME = "adt_system_a"


def _mod():
    import agents.onboard_verify_agent as mod
    return mod


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeClock:
    """Monotonic time that only moves when the code sleeps or a call takes time."""

    def __init__(self):
        self.now = 1000.0
        self.slept: list = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeControl:
    """``list_agent_runtimes`` / ``list_gateway_targets`` answering one scripted
    page per poll; the last page repeats."""

    def __init__(self, runtimes, targets):
        self._runtimes = runtimes
        self._targets = targets
        self.runtime_polls = 0
        self.target_polls = 0
        self.gateway_ids: list = []

    def list_agent_runtimes(self, **_kwargs):
        page = self._runtimes[min(self.runtime_polls, len(self._runtimes) - 1)]
        self.runtime_polls += 1
        return {"agentRuntimes": page}

    def list_gateway_targets(self, **kwargs):
        self.gateway_ids.append(kwargs.get("gatewayIdentifier"))
        page = self._targets[min(self.target_polls, len(self._targets) - 1)]
        self.target_polls += 1
        return {"items": page}


def _runtime(status="READY", name=RUNTIME_NAME):
    return {"agentRuntimeName": name, "agentRuntimeId": "rt-1", "status": status}


def _target(status="READY", name=TARGET):
    return {"name": name, "targetId": "tg-1", "status": status}


def _ok(text):
    return {"status": "success", "toolUseId": "t", "content": [{"text": text}]}


def _err(text):
    return {"status": "error", "toolUseId": "t", "content": [{"text": text}]}


MODE_A_SCHEMA = {"type": "object", "properties": {"question": {"type": "string"},
                                                   "kg_context": {"type": "string"}},
                 "required": ["question"]}
MODE_B_SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}},
                 "required": ["query"]}


class FakeGateway:
    """The gateway seen through one MCP session at a time.

    A Mode A (question) call writes one provenance sidecar entry the way a
    PostgreSQL system agent would (its inner agent ran a SELECT that read
    ``capture_table`` and returned ``capture_rows`` rows), so the agent test
    can read it back and prove the table was read. ``capture=False`` writes
    none (a tool that answered with a number but recorded no read)."""

    def __init__(self, tools=None, replies=None, clock=None, *,
                 capture=True, capture_table="table_x", capture_rows=42,
                 capture_schema="sch_a"):
        self.tools = tools if tools is not None else [
            {"name": MODE_A_TOOL, "input_schema": MODE_A_SCHEMA},
            {"name": "system-b___query_system_b", "input_schema": MODE_A_SCHEMA},
        ]
        self.replies = list(replies or [_ok("42")])
        self.clock = clock
        self.capture = capture
        self.capture_table = capture_table
        self.capture_rows = capture_rows
        self.capture_schema = capture_schema
        self.calls: list = []
        self.bearers: list = []
        self.provenance_args: list = []
        self.lists = 0
        self.open_sessions = 0
        self.opened = 0

    @contextlib.contextmanager
    def open(self, url, bearer, startup_timeout_s):
        assert url == "https://gateway.example.test/mcp"
        assert startup_timeout_s >= 1
        self.bearers.append(bearer)
        self.opened += 1
        self.open_sessions += 1
        try:
            yield self
        finally:
            self.open_sessions -= 1

    def list_tools(self):
        self.lists += 1
        return list(self.tools)

    def _maybe_capture(self, arguments):
        from agents.provenance import parse_provenance_context
        from agents import provenance_store
        context = parse_provenance_context(arguments.get("_adt_provenance"))
        self.provenance_args.append(context)
        if context is None or not self.capture or self.capture_table is None:
            return
        session_id, query_ref = context
        provenance_store.put_turn_queries(session_id, {query_ref: {
            "system_id": SID,
            "tool_name": f"query_{SID.replace('-', '_')}",
            "executed_query":
                f'SELECT COUNT(*) FROM "{self.capture_schema}"."{self.capture_table}"',
            "entity": {"table": self.capture_table, "columns": ["row_count"]},
            "row_count": self.capture_rows,
            "captured_at": "2026-01-01T00:00:00Z",
        }})

    def call_tool(self, name, arguments, timeout_s, tool_use_id=None):
        self.calls.append((name, dict(arguments), timeout_s))
        self._maybe_capture(arguments)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return reply(timeout_s) if callable(reply) else reply


class Cfg:
    is_local_mode = False
    has_gateway = True
    agentcore_gateway_url = "https://gateway.example.test/mcp"
    agentcore_gateway_id = "gw-1"
    aws_region = "us-east-1"


class LocalCfg(Cfg):
    is_local_mode = True
    has_gateway = False
    agentcore_gateway_url = None
    agentcore_gateway_id = None


@pytest.fixture
def env(monkeypatch):
    """A deployed app with one onboarded PostgreSQL system ``system-a``."""
    mod = _mod()
    from agents.job_store import JobStore
    state = {"entry": {"id": SID, "connection_type": ct.POSTGRESQL, "schema": "sch_a",
                       "status": "onboarded"},
             "tables": [{"name": "table_x"}, {"name": "table_y"}],
             "graph_calls": 0}
    monkeypatch.setattr(mod, "get_config", lambda: Cfg())
    monkeypatch.setattr(mod.catalog, "get",
                        lambda sid: state["entry"] if sid == state["entry"]["id"] else None)
    monkeypatch.setattr(mod.catalog, "get_onboarded", lambda: [state["entry"]])

    def _graph(query, params=None):
        state["graph_calls"] += 1
        assert params == {"sid": state["entry"]["id"]}
        return list(state["tables"])
    monkeypatch.setattr(mod, "neptune_query", _graph)
    monkeypatch.setattr(mod, "_claim_secret", lambda: None)
    monkeypatch.setattr(mod, "_JOBS", JobStore("verify-agent-test", table=None))
    # A fresh in-memory provenance store so a Mode A capture is isolated.
    from agents import provenance_store
    provenance_store._job_store = JobStore(kind="provenance-query", table=None)

    def _no_chat():
        raise AssertionError("the agent test must not send a chat turn")
    monkeypatch.setattr("agents.chat_client.get_chat_client", _no_chat, raising=False)
    return state


def _run(clock=None, control=None, gateway=None, token="user-token", deadline_s=None):
    mod = _mod()
    clock = clock or FakeClock()
    control = control or FakeControl([[_runtime()]], [[_target()]])
    gateway = gateway or FakeGateway(clock=clock)
    kwargs = {}
    if deadline_s is not None:
        kwargs["deadline_s"] = deadline_s
    result = mod.run_agent_test(SID, user_access_token=token, control=control,
                                open_session=gateway.open, clock=clock, sleep=clock.sleep,
                                **kwargs)
    return result, control, gateway, clock


@pytest.fixture
def real_clock(env):
    """For a test that runs the job on the real clock with a deadline of a
    second or less. The stages it times take a few milliseconds, so the
    deadline holds only while nothing else stops this process:

    * one run through the same stages on the fake clock first (an answer and
      a timeout), so each module the run imports lazily is already imported
      when the real clock starts;
    * the cyclic garbage collector is paused until the test ends. Late in a
      full unit lane the test process holds millions of objects, and one
      automatic full collection can take longer than the whole deadline,
      which ends the run before it reaches the stage under test."""
    _run()
    _run(gateway=FakeGateway(replies=[_err("Timed out while waiting for response")]))
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


# ── pass ──────────────────────────────────────────────────────────────────────

def test_calls_the_new_systems_own_query_tool_and_passes_on_a_row_count(env):
    result, control, gateway, _ = _run()
    assert result["outcome"] == "passed", result
    assert result["tool"] == MODE_A_TOOL
    assert result["table"] == "table_x"
    assert result["rows"] == 42
    assert result["query_kind"] == "question"
    assert result["step"] is None
    assert [c[0] for c in gateway.calls] == [MODE_A_TOOL]
    question = gateway.calls[0][1]["question"]
    assert "sch_a.table_x" in question and "only the number" in question
    assert gateway.bearers and set(gateway.bearers) == {"user-token"}
    assert control.gateway_ids and set(control.gateway_ids) == {"gw-1"}
    assert gateway.open_sessions == 0


def test_runs_when_this_is_the_only_onboarded_system(env):
    """The chat-based test was skipped with fewer than two systems."""
    mod = _mod()
    assert len(mod.catalog.get_onboarded()) == 1
    result, _, gateway, _ = _run()
    assert gateway.calls, "a single onboarded system is tested too"
    assert result["outcome"] == "passed"


def test_a_sql_tool_gets_a_count_query_and_its_rows_are_read(env):
    gateway = FakeGateway(
        tools=[{"name": MODE_B_TOOL, "input_schema": MODE_B_SCHEMA}],
        replies=[_ok(json.dumps({"columns": ["row_count"], "rows": [[7]], "row_count": 1}))],
        capture=False)
    result, _, gateway, _ = _run(gateway=gateway)
    assert result["outcome"] == "passed", result
    assert result["tool"] == MODE_B_TOOL and result["query_kind"] == "sql"
    assert result["rows"] == 7
    assert gateway.calls[0][1]["query"] == 'SELECT COUNT(*) AS row_count FROM "sch_a"."table_x"'


def test_the_count_read_from_an_answer_skips_digits_in_the_table_name():
    mod = _mod()
    names = ["sch_a", "table_x2"]
    assert mod.count_from_answer("1,234", names) == 1234
    assert mod.count_from_answer("table_x2 has 17 rows.", names) == 17
    assert mod.count_from_answer("There are 0 rows in sch_a.table_x2", names) == 0
    assert mod.count_from_answer("I could not find that table.", names) is None
    assert mod.count_from_answer("About 3.5 per day", names) is None
    assert mod.count_from_answer("", names) is None
    # Only the first paragraph is read: engine notes come after a blank line.
    assert mod.count_from_answer("No count here.\n\n[note] 100 of 100", names) is None


# ── each failure names its step ───────────────────────────────────────────────

def test_no_runtime_fails_at_the_runtime_step(env):
    result, _, gateway, _ = _run(control=FakeControl([[_runtime(name="adt_other")]],
                                                     [[_target()]]))
    assert (result["outcome"], result["step"]) == ("failed", "runtime"), result
    assert RUNTIME_NAME in result["reason"]
    assert gateway.calls == []


def test_a_failed_runtime_fails_at_the_runtime_step(env):
    result, _, _, _ = _run(control=FakeControl([[_runtime("CREATE_FAILED")]], [[_target()]]))
    assert (result["outcome"], result["step"]) == ("failed", "runtime"), result
    assert "CREATE_FAILED" in result["reason"]


def test_no_gateway_target_fails_at_the_target_step(env):
    result, _, gateway, _ = _run(control=FakeControl([[_runtime()]], [[_target(name="other")]]))
    assert (result["outcome"], result["step"]) == ("failed", "gateway_target"), result
    assert TARGET in result["reason"]
    assert gateway.calls == []


def test_an_unsynced_gateway_target_fails_at_the_target_step(env):
    result, _, _, _ = _run(control=FakeControl([[_runtime()]],
                                               [[_target("SYNCHRONIZE_UNSUCCESSFUL")]]))
    assert (result["outcome"], result["step"]) == ("failed", "gateway_target"), result
    assert "SYNCHRONIZE_UNSUCCESSFUL" in result["reason"]


def test_a_tools_list_without_the_systems_tool_fails_at_the_target_step(env):
    gateway = FakeGateway(tools=[{"name": "system-a___describe_schema",
                                  "input_schema": {"properties": {}}}])
    result, _, gateway, _ = _run(gateway=gateway)
    assert (result["outcome"], result["step"]) == ("failed", "gateway_target"), result
    assert "query_system_a" in result["reason"] and "describe_schema" in result["reason"]
    assert gateway.calls == []


def test_a_tool_error_fails_at_the_tool_step_without_the_raw_text(env):
    raw = "pgdriver.OperationalError: could not connect to host db.internal.example:5432"
    result, _, _, _ = _run(gateway=FakeGateway(replies=[_err(raw)]))
    assert (result["outcome"], result["step"]) == ("failed", "tool"), result
    assert "db.internal.example" not in json.dumps(result)
    assert "Reference:" in result["reason"]


def test_an_empty_result_fails_at_the_tool_step(env):
    result, _, _, _ = _run(gateway=FakeGateway(replies=[_ok("   ")]))
    assert (result["outcome"], result["step"]) == ("failed", "tool"), result
    assert "empty" in result["reason"]


def test_an_answer_without_a_count_fails_at_the_tool_step(env):
    result, _, _, _ = _run(gateway=FakeGateway(replies=[_ok("I can describe the table.")],
                                               capture=False))
    assert (result["outcome"], result["step"]) == ("failed", "tool"), result
    assert "row count" in result["reason"]
    # finding 5: the model's answer text is never echoed into the reason.
    assert "I can describe the table." not in result["reason"]
    assert "Reference:" in result["reason"]


def test_an_identity_policy_denial_fails_at_the_tool_step(env, monkeypatch):
    import base64
    from agents.runtime_asgi import identity_deny_texts
    mod = _mod()
    # A signed claim WAS attached (the app could reach the system as chat does),
    # so a group denial of the signed user is a real failure (finding 2).
    monkeypatch.setattr(mod, "_claim_secret", lambda: "claim-signing-key")
    payload = base64.urlsafe_b64encode(json.dumps(
        {"sub": "u", "cognito:groups": ["g"]}).encode()).decode().rstrip("=")
    token = f"h.{payload}.s"
    result, _, _, _ = _run(gateway=FakeGateway(replies=[_err(identity_deny_texts(SID)[2])],
                                               capture=False),
                           token=token)
    assert (result["outcome"], result["step"]) == ("failed", "tool"), result
    assert "identity policy" in result["reason"]


# ── cold start and the deadline ───────────────────────────────────────────────

def test_waits_for_a_cold_runtime_then_passes(env):
    clock = FakeClock()
    control = FakeControl([[_runtime("CREATING")], [_runtime("UPDATING")], [_runtime()]],
                          [[_target("SYNCHRONIZING")], [_target()]])
    cold = "MCP server 'https://runtime.example.test/invocations' is not reachable"
    gateway = FakeGateway(replies=[_err(cold), _err(cold), _ok("42")], clock=clock)
    result, control, gateway, clock = _run(clock=clock, control=control, gateway=gateway)
    assert result["outcome"] == "passed", result
    assert len(gateway.calls) == 3
    assert control.runtime_polls == 3 and control.target_polls == 2
    assert result["elapsed_s"] == round(sum(clock.slept), 1)
    assert sum(clock.slept) < _mod().AGENT_TEST_DEADLINE_S


def test_a_runtime_that_never_answers_stops_at_the_deadline(env):
    mod = _mod()
    clock = FakeClock()
    cold = "No endpoint or agent found"

    def _slow_cold(timeout_s):
        clock.advance(min(timeout_s, 20.0))
        return _err(cold)
    gateway = FakeGateway(replies=[_slow_cold], clock=clock)
    started = clock.now
    result, _, gateway, clock = _run(clock=clock, gateway=gateway)
    assert (result["outcome"], result["step"]) == ("failed", "runtime"), result
    assert f"{mod.AGENT_TEST_DEADLINE_S} s" in result["reason"]
    assert "runtime.example" not in result["reason"]
    assert clock.now - started <= mod.AGENT_TEST_DEADLINE_S
    assert len(gateway.calls) > 1, "a cold runtime is retried until the deadline"
    # No call was ever allowed to wait past the deadline.
    for _name, _args, timeout_s in gateway.calls:
        assert 0 < timeout_s <= mod.AGENT_TEST_DEADLINE_S


def test_a_tool_call_that_times_out_at_the_deadline_fails_at_the_tool_step(env):
    mod = _mod()
    clock = FakeClock()

    def _hang(timeout_s):
        clock.advance(timeout_s)
        return _err("Timed out while waiting for response to ClientRequest.")
    result, _, gateway, clock = _run(clock=clock, gateway=FakeGateway(replies=[_hang], clock=clock))
    assert (result["outcome"], result["step"]) == ("failed", "tool"), result
    assert f"within {mod.AGENT_TEST_DEADLINE_S} s" in result["reason"]
    assert len(gateway.calls) == 1


def test_a_runtime_still_creating_at_the_deadline_fails_at_the_runtime_step(env):
    mod = _mod()
    result, _, gateway, clock = _run(control=FakeControl([[_runtime("CREATING")]], [[_target()]]))
    assert (result["outcome"], result["step"]) == ("failed", "runtime"), result
    assert "CREATING" in result["reason"] and f"{mod.AGENT_TEST_DEADLINE_S} s" in result["reason"]
    assert sum(clock.slept) <= mod.AGENT_TEST_DEADLINE_S
    assert gateway.calls == []


def test_the_deadline_is_long_enough_for_a_cold_runtime():
    """The deadline covers the slowest cold start measured in this repo (an
    idle VPC runtime after an in-place update: ~130 s, scripts/set_query_mode.py)
    plus a short tool answer, and stays well under the 300 s Mode A caller
    window."""
    mod = _mod()
    from agents.system_agent import SYSTEM_QUERY_CALLER_WINDOW_S
    assert 130 + 30 <= mod.AGENT_TEST_DEADLINE_S < SYSTEM_QUERY_CALLER_WINDOW_S


def test_no_thread_or_session_outlives_the_deadline(env, real_clock):
    """Real clock: the gateway never answers; the job's thread is gone by the
    deadline (plus a small margin) and no session is left open."""
    mod = _mod()
    gate = threading.Event()

    class _Silent(FakeGateway):
        def call_tool(self, name, arguments, timeout_s, tool_use_id=None):
            self.calls.append((name, dict(arguments), timeout_s))
            gate.wait(timeout_s)  # like a read timeout
            return _err("Timed out while waiting for response")

    gateway = _Silent()
    control = FakeControl([[_runtime()]], [[_target()]])
    out = {}

    def _job():
        out["result"] = mod.run_agent_test(SID, user_access_token="user-token",
                                           control=control, open_session=gateway.open,
                                           deadline_s=1.0)
    started = time.monotonic()
    worker = threading.Thread(target=_job, daemon=True)
    worker.start()
    worker.join(timeout=5.0)
    try:
        assert not worker.is_alive(), "the agent test thread outlived its deadline"
        assert time.monotonic() - started < 3.0
        assert out["result"]["outcome"] == "failed"
        assert gateway.calls, "the silent tool was never called"
        assert gateway.open_sessions == 0
        # The call's own read timeout ends before the deadline, so no stage
        # thread is left behind once the result is in.
        assert not [t for t in threading.enumerate()
                    if t.name == "verify-agent-test-stage" and t.is_alive()]
    finally:
        gate.set()


# ── connection types ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("ctype", [ct.SITEWISE_DIRECT, ct.SITEWISE_I3X, ct.TULIP])
def test_read_only_api_systems_are_called_but_cannot_prove_a_read(env, ctype):
    """These are called (the count question still proves the gateway path),
    but their agents produce no SQL capture, so the read of the named table
    cannot be confirmed: the result is "no_read", not "passed"."""
    env["entry"] = {"id": SID, "connection_type": ctype, "schema": "sitewise"}
    env["tables"] = [{"name": "Entity B"}, {"name": "Entity A"}]
    # capture=False: a non-SQL agent writes no sidecar statement.
    result, _, gateway, _ = _run(gateway=FakeGateway(replies=[_ok("12")], capture=False))
    assert result["outcome"] == "no_read", result
    assert result["table"] == "Entity A"
    question = gateway.calls[0][1]["question"]
    assert '"Entity A"' in question and "only the number" in question
    # Expected for this type: flagged so the wizard doesn't show a warning.
    assert result["read_provable"] is False
    assert "expected" in result["reason"]
    assert "recorded no read" not in result["reason"]


def test_the_built_in_hub_is_called_but_cannot_prove_a_read(env):
    """Its runtime keeps only the hub's read tools; it produces no SQL
    capture, so the read cannot be confirmed (no_read, not passed)."""
    from agents.builtin_highbyte import BUILTIN_ID
    mod = _mod()
    env["entry"] = {"id": BUILTIN_ID, "connection_type": ct.MCP_SERVER, "schema": "mcp"}
    env["tables"] = [{"name": "Tool_A"}]
    clock = FakeClock()
    tool = f"{BUILTIN_ID}___query_{BUILTIN_ID}"
    gateway = FakeGateway(tools=[{"name": tool, "input_schema": MODE_A_SCHEMA}],
                          replies=[_ok("5")], capture=False)
    result = mod.run_agent_test(
        BUILTIN_ID, user_access_token="user-token",
        control=FakeControl([[_runtime(name=f"adt_{BUILTIN_ID}")]],
                            [[_target(name=BUILTIN_ID)]]),
        open_session=gateway.open, clock=clock, sleep=clock.sleep)
    assert result["outcome"] == "no_read", result
    assert gateway.calls[0][0] == tool


# A source whose agent records no SQL statement (``read_provable`` False: the
# read-only API types and the built-in hub) cannot show which table a read
# used, so an answer to the count question through the gateway is the
# expected ``no_read``. The answer's wording must not decide that: "one
# record" and "1" are the same answer. An error or empty marker fails, so
# does an answer with no count in it, and nothing on this path passes.

#: How the hub's agent answered on a real run, with the names made neutral.
_HUB_WORDS = ("The Tool_A tool returns a single record containing a hierarchical "
              "snapshot of the namespace with telemetry for every site. This is "
              "one record representing the current state of all sites.")
_REST_WORDS = "There are six records in Entity A."
_ERROR_ANSWERS = ("Error: the source answered with HTTP 503.",
                  "ValueError: no entity named Entity A")
#: The API types plus the hub (the only MCP source the agent test calls).
_UNPROVABLE_CALLED = (ct.MCP_SERVER, ct.SITEWISE_DIRECT, ct.SITEWISE_I3X, ct.REST_API, ct.TULIP)


def _run_source(env, ctype, answer, *, capture=False):
    """One agent test of a ``ctype`` source whose tool answered ``answer``.
    ``ct.MCP_SERVER`` is the built-in hub. ``capture`` writes the provenance
    capture of the first table, as a PostgreSQL agent does."""
    from agents.builtin_highbyte import BUILTIN_ID
    mod = _mod()
    sid = BUILTIN_ID if ctype == ct.MCP_SERVER else SID
    env["entry"] = {"id": sid, "connection_type": ctype, "schema": "src_a"}
    env["tables"] = [{"name": "Entity B"}, {"name": "Entity A"}]
    clock = FakeClock()
    tool = mod._tool_names(sid)[0]
    gateway = FakeGateway(tools=[{"name": tool, "input_schema": MODE_A_SCHEMA}],
                          replies=[_ok(answer)], capture=capture, capture_table="Entity A")
    result = mod.run_agent_test(
        sid, user_access_token="user-token",
        control=FakeControl([[_runtime(name=mod.runtime_name(sid))]],
                            [[_target(name=mod.target_name(sid))]]),
        open_session=gateway.open, clock=clock, sleep=clock.sleep)
    return result, gateway


def _outcome(result: dict) -> tuple:
    return (result["outcome"], result["step"], result["reason"], result["rows"],
            result["read_provable"], result["table"])


@pytest.mark.parametrize("ctype, words", [
    (ct.MCP_SERVER, _HUB_WORDS),
    (ct.SITEWISE_DIRECT, _REST_WORDS),
    (ct.SITEWISE_I3X, _REST_WORDS),
    (ct.REST_API, _REST_WORDS),
    (ct.TULIP, _REST_WORDS),
], ids=["hub", "sitewise-direct", "sitewise-i3x", "rest", "tulip"])
def test_a_count_in_words_gets_the_same_expected_no_read_as_a_number(env, ctype, words):
    """Before: a words answer failed with "returned no row count", because
    that check ran before the expected no_read; "1" got the no_read."""
    words_result, gateway = _run_source(env, ctype, words)
    assert gateway.calls, words_result
    assert words_result["outcome"] == "no_read", words_result
    assert words_result["step"] is None
    assert words_result["read_provable"] is False
    assert "That's expected here." in words_result["reason"]
    assert words_result["rows"] is None
    digit_result, _ = _run_source(env, ctype, "1")
    assert _outcome(words_result) == _outcome(digit_result)


@pytest.mark.parametrize("ctype", _UNPROVABLE_CALLED)
def test_a_number_from_a_source_that_cannot_prove_a_read_is_the_expected_no_read(env, ctype):
    result, _ = _run_source(env, ctype, "12")
    assert result["outcome"] == "no_read", result
    assert result["reason"] == (
        f"{_mod().bare_tool_name(result['tool'])} answered through the gateway. This "
        "type of system doesn't record which table a read used, so the test can't "
        "confirm it read Entity A. That's expected here.")
    assert result["rows"] is None and result["step"] is None


#: A log reference that happens to contain the error answer's digits: the
#: reference is random hex, so any run can draw one like it.
_REF_WITH_503 = "6a5035b33b99"


@pytest.mark.parametrize("answer", _ERROR_ANSWERS, ids=["error-colon", "exception-type"])
@pytest.mark.parametrize("ctype", (ct.POSTGRESQL,) + _UNPROVABLE_CALLED)
def test_an_error_answer_still_fails_for_every_kind_of_source(env, ctype, answer, monkeypatch):
    """The test fails, with a fixed message and a log reference, and the tool's
    own error text stays out of what the wizard shows. Only the text before the
    reference is checked for the error text, since the random reference can
    contain the same digits."""
    import agents.public_errors as public_errors
    monkeypatch.setattr(public_errors, "new_ref", lambda: _REF_WITH_503)
    result, _ = _run_source(env, ctype, answer)
    assert (result["outcome"], result["step"]) == ("failed", "tool"), result
    assert "returned no row count for Entity A" in result["reason"]
    shown, ref = result["reason"].rsplit("Reference:", 1)
    assert ref.strip() == _REF_WITH_503
    assert "503" not in shown and "ValueError" not in shown


@pytest.mark.parametrize("ctype", _UNPROVABLE_CALLED)
def test_an_answer_that_says_there_is_no_data_keeps_the_base_outcome(env, ctype):
    """An empty marker is no sign the source answered with data, so it never
    takes the words path: with no number it fails, as before; with a number
    it is the same expected no_read as before."""
    result, _ = _run_source(env, ctype, "No records found.")
    assert (result["outcome"], result["step"]) == ("failed", "tool"), result
    assert "returned no row count for Entity A" in result["reason"]
    result, _ = _run_source(env, ctype, "No records found (0).")
    assert result["outcome"] == "no_read", result
    assert "That's expected here." in result["reason"]


def test_a_source_that_captures_its_sql_still_needs_the_capture_or_a_number(env):
    """``read_provable`` True is unchanged: a words answer with no capture
    fails, the same answer with a capture of the table passes, and a number
    with no capture is the warning no_read."""
    result, _ = _run_source(env, ct.POSTGRESQL, _HUB_WORDS)
    assert (result["outcome"], result["step"]) == ("failed", "tool"), result
    assert result["read_provable"] is True
    result, _ = _run_source(env, ct.POSTGRESQL, _HUB_WORDS, capture=True)
    assert result["outcome"] == "passed", result
    assert result["rows"] is None
    result, _ = _run_source(env, ct.POSTGRESQL, "12")
    assert result["outcome"] == "no_read", result
    assert "recorded no read" in result["reason"]


@pytest.mark.parametrize("answer", [
    "12", "1", _HUB_WORDS, _REST_WORDS, "No records found.", "No records found (0).",
    *_ERROR_ANSWERS, "   ",
], ids=["number", "one", "hub-words", "rest-words", "empty", "empty-number",
        "error-colon", "exception-type", "blank"])
@pytest.mark.parametrize("ctype", ct.NON_SQL_CONNECTION_TYPES)
def test_a_source_that_cannot_prove_a_read_never_passes(env, ctype, answer):
    """Every connection type but PostgreSQL, every kind of answer, no SQL
    capture: never ``passed``. A type the test does not call is not_run."""
    result, gateway = _run_source(env, ctype, answer)
    assert result["outcome"] != "passed", result
    if gateway.calls:
        assert result["read_provable"] is False, result
        assert result["outcome"] in ("no_read", "failed"), result
    else:
        assert result["outcome"] == "not_run", result


#: Answers with no count in them: an unrecognised outage, a failure or a
#: nonanswer in prose, a number word with nothing counted after it, and a
#: negated count (an answer that says nothing came back, as "No records found."
#: does). None carries an error or empty marker.
_NO_COUNT_ANSWERS = ("Service unavailable", "I could not query the source.",
                     "I can describe the table.",
                     "Here is an overview of what this system holds.",
                     "One moment please.", "There are six.",
                     "I couldn't read a single record.", "Not one record came back.",
                     "We cannot return even a single record.")
_NO_COUNT_IDS = ["unavailable", "could-not-query", "describe", "overview", "one-moment",
                 "bare-word", "negated-single", "negated-one", "negated-verb-even"]


@pytest.mark.parametrize("answer", _NO_COUNT_ANSWERS, ids=_NO_COUNT_IDS)
@pytest.mark.parametrize("ctype", _UNPROVABLE_CALLED)
def test_an_answer_with_no_count_still_fails_for_a_source_that_cannot_prove_a_read(
        env, ctype, answer):
    """The expected no_read is for an answer to the count question. Prose
    that gives no count fails at the tool step, as it does on the base."""
    result, gateway = _run_source(env, ctype, answer)
    assert gateway.calls, result
    assert (result["outcome"], result["step"]) == ("failed", "tool"), result
    assert "returned no row count for Entity A" in result["reason"]
    assert answer not in result["reason"]


@pytest.mark.parametrize("ctype", _UNPROVABLE_CALLED)
def test_a_stray_digit_in_an_unrecognised_failure_keeps_the_base_outcome(env, ctype):
    """Pre-existing and unchanged: the base reads 503 as the answer's number,
    so this reply gets the expected no_read there too."""
    result, _ = _run_source(env, ctype, "HTTP 503 Service Unavailable")
    assert result["outcome"] == "no_read", result
    assert "That's expected here." in result["reason"]


_WORDS_COUNTS = (
    "one row", "Entity A has two rows.", "a single record", "There are three records.",
    "one entry", "It holds four entries.", "one item", "There are twenty-one items.",
    "Twenty one items", "There are six Entity A records.", "It holds six Entity A.",
    "zero rows", "This is one record representing the current state of all sites.",
    "No errors and one record was returned.", "Without errors one record was returned.",
    "I did not see errors and one record came back.")


@pytest.mark.parametrize("answer", _WORDS_COUNTS)
@pytest.mark.parametrize("ctype", _UNPROVABLE_CALLED)
def test_a_count_in_words_next_to_what_it_counts_is_the_expected_no_read(env, ctype, answer):
    result, _ = _run_source(env, ctype, answer)
    assert result["outcome"] == "no_read", result
    assert "That's expected here." in result["reason"]
    assert result["rows"] is None


@pytest.mark.parametrize("answer", [
    "Error: one record could not be read.", "ValueError: two rows failed to load",
    "No records found; one row was expected.", "No matching rows for one item.",
], ids=["error-one-record", "exception-two-rows", "empty-one-row", "empty-one-item"])
@pytest.mark.parametrize("ctype", _UNPROVABLE_CALLED)
def test_an_error_or_empty_marker_vetoes_a_count_in_words(env, ctype, answer):
    result, _ = _run_source(env, ctype, answer)
    assert (result["outcome"], result["step"]) == ("failed", "tool"), result
    assert "returned no row count for Entity A" in result["reason"]


@pytest.mark.parametrize("text, count", [
    ("one record", 1), ("a single record", 1), ("A single record.", 1), ("zero rows", 0),
    ("nineteen items", 19), ("twenty entries", 20), ("twenty-one items", 21),
    ("ninety nine rows", 99), ("six Station One records", 6), ("seven Station One", 7),
    ("The tool returns one record.\n\nThe engine note lists two rows.", 1),
    ("one moment please", None), ("There are six.", None), ("six, records", None),
    ("I couldn't read a single record.", None), ("Not one record came back.", None),
    ("Station One records are listed.", None), ("plant_two rows", None),
    ("one hundred records", None),
    ("Here is the overview.\n\nOne record.", None),
    # "no" and "without" negate the noun they come before, not a later count.
    ("No errors and one record was returned.", 1),
    ("Without errors one record was returned.", 1),
    ("There were no warnings and two rows came back.", 2),
    ("without a single record", None), ("no single record came back", None),
    # A negated verb negates a count right after its verb, past "a"/"even".
    ("We cannot return even a single record.", None),
    ("It did not find one record.", None), ("It never had a single row.", None),
    # A negation further back is not read, as with a digit: these get the
    # outcome "... and return 1 record" and "... or 1 record" get on the base.
    ("I could not read and return one record.", 1),
    ("It didn't find any data or one record.", 1),
    # A count that starts a new clause is not under an earlier negation.
    ("I did not see errors and one record came back.", 1),
    ("It never failed and one record came back.", 1),
    ("The result cannot be empty because one record came back.", 1),
    ("No errors and a single record came back.", 1),
], ids=lambda value: str(value)[:28])
def test_the_count_in_words_reader(text, count):
    """Number words from zero to ninety-nine, next to what they count. A
    number word inside the table or schema name never counts; nor does one
    in the engine notes after the first paragraph."""
    mod = _mod()
    assert mod._count_in_words(text, ["Station One", "plant_two"], "Station One") == count


def test_a_customer_mcp_server_is_not_called(env):
    env["entry"] = {"id": SID, "connection_type": ct.MCP_SERVER, "schema": "mcp"}
    result, control, gateway, _ = _run()
    assert result["outcome"] == "not_run", result
    assert "MCP server" in result["reason"] and "read" in result["reason"]
    assert gateway.calls == [] and control.runtime_polls == 0


def test_an_athena_system_is_not_counted(env):
    env["entry"] = {"id": SID, "connection_type": ct.ATHENA, "schema": "lake_db"}
    result, _, gateway, _ = _run()
    assert result["outcome"] == "not_run", result
    assert "scan" in result["reason"]
    assert gateway.calls == []


def test_a_structured_tool_on_an_api_system_is_not_called(env):
    env["entry"] = {"id": SID, "connection_type": ct.SITEWISE_DIRECT, "schema": "sitewise"}
    gateway = FakeGateway(tools=[{"name": MODE_B_TOOL, "input_schema": MODE_B_SCHEMA}])
    result, _, gateway, _ = _run(gateway=gateway)
    assert result["outcome"] == "not_run", result
    assert gateway.calls == []


def test_local_mode_says_the_test_was_not_run(env, monkeypatch):
    mod = _mod()
    monkeypatch.setattr(mod, "get_config", lambda: LocalCfg())
    result, control, gateway, _ = _run()
    assert result["outcome"] == "not_run", result
    assert "local mode" in result["reason"]
    assert control.runtime_polls == 0 and gateway.opened == 0


def test_a_system_with_no_tables_in_the_graph_is_not_run(env):
    env["tables"] = []
    result, _, gateway, _ = _run()
    assert result["outcome"] == "not_run", result
    assert gateway.calls == []


# ── identity ──────────────────────────────────────────────────────────────────

def test_the_call_carries_the_users_identity_claim_when_the_key_is_readable(env, monkeypatch):
    import base64
    from agents.auth import identity_claim
    mod = _mod()
    monkeypatch.setattr(mod, "_claim_secret", lambda: "claim-signing-key")
    payload = base64.urlsafe_b64encode(json.dumps(
        {"sub": "user-1", "cognito:groups": ["integrator"]}).encode()).decode().rstrip("=")
    token = f"h.{payload}.s"
    result, _, gateway, _ = _run(token=token)
    assert result["outcome"] == "passed", result
    claim = gateway.calls[0][1][identity_claim.RESERVED_CLAIM_ARG]
    claims = identity_claim.verify_claim(claim, "claim-signing-key")
    assert claims["sub"] == "user-1" and claims["groups"] == ["integrator"]
    assert gateway.bearers == [token, token]


def test_without_a_user_token_the_service_token_signs_in(env, monkeypatch):
    import agents.orchestrator as orch
    monkeypatch.setattr(orch, "_fetch_service_token", lambda config: "service-token")
    result, _, gateway, _ = _run(token=None)
    assert result["outcome"] == "passed", result
    assert set(gateway.bearers) == {"service-token"}


#: The real key reader (the ``env`` fixture stubs ``_claim_secret``).
_REAL_CLAIM_SECRET = _mod()._claim_secret
#: Set on the app task by the compute stack only when its role may read the
#: identity-claim key. Spelled literally so this test fails on code with no gate.
_KEY_READABLE_ENV = "IDENTITY_CLAIM_KEY_READABLE"


def _user_token(sub="user-1", groups=("integrator",)):
    import base64
    payload = base64.urlsafe_b64encode(json.dumps(
        {"sub": sub, "cognito:groups": list(groups)}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def _secrets_manager(monkeypatch, answer):
    """Patch boto3.client; returns the list of SecretIds read."""
    import boto3
    from agents.auth import delegated_identity
    # Start uncached; teardown restores the cache so a key never leaks out.
    monkeypatch.setattr(delegated_identity, "_identity_claim_secret", None)
    reads = []

    class _Sm:
        def get_secret_value(self, SecretId):
            reads.append(SecretId)
            if isinstance(answer, Exception):
                raise answer
            return {"SecretString": answer}

    monkeypatch.setattr(boto3, "client", lambda service, **_k: _Sm())
    return reads


def test_no_key_read_when_the_app_is_not_granted_the_key(env, monkeypatch):
    """Workshop deploy 2026-10-20: the app task role may read the identity-claim
    key only when the Quick gateway takes delegated user tokens, so every
    wizard agent test on any other deploy logged AccessDenied in CloudTrail.
    Without the grant flag the test reads no key and calls the tool with no
    claim, as a chat turn on that deploy does, and still passes."""
    from agents.auth import identity_claim
    mod = _mod()
    monkeypatch.setattr(mod, "_claim_secret", _REAL_CLAIM_SECRET)
    monkeypatch.delenv(_KEY_READABLE_ENV, raising=False)
    reads = _secrets_manager(monkeypatch, RuntimeError("AccessDeniedException"))
    result, _, gateway, _ = _run(token=_user_token())
    assert result["outcome"] == "passed", result
    assert reads == []
    assert identity_claim.RESERVED_CLAIM_ARG not in gateway.calls[0][1]


def test_the_granted_app_reads_the_key_and_signs_the_claim(env, monkeypatch):
    from agents.auth import identity_claim
    mod = _mod()
    monkeypatch.setattr(mod, "_claim_secret", _REAL_CLAIM_SECRET)
    monkeypatch.setenv(_KEY_READABLE_ENV, "1")
    reads = _secrets_manager(monkeypatch, "claim-signing-key")
    result, _, gateway, _ = _run(token=_user_token())
    assert result["outcome"] == "passed", result
    assert reads == [identity_claim.IDENTITY_CLAIM_SECRET_NAME]
    claims = identity_claim.verify_claim(
        gateway.calls[0][1][identity_claim.RESERVED_CLAIM_ARG], "claim-signing-key")
    assert claims["sub"] == "user-1"


# ── the real gateway session (strands MCPClient) ─────────────────────────────

def test_the_gateway_session_reads_every_tools_list_page():
    from types import SimpleNamespace
    from strands.types.collections import PaginatedList
    mod = _mod()

    def _tool(name):
        return SimpleNamespace(mcp_tool=SimpleNamespace(name=name, inputSchema=MODE_A_SCHEMA))

    class _Client:
        def __init__(self):
            self.tokens = []
            self.called = []

        def list_tools_sync(self, pagination_token=None):
            self.tokens.append(pagination_token)
            if pagination_token is None:
                return PaginatedList([_tool("a___query_a")], token="page-2")
            return PaginatedList([_tool(MODE_A_TOOL)], token=None)

        def call_tool_sync(self, tool_use_id, name, arguments, read_timeout_seconds=None):
            self.called.append((name, arguments, read_timeout_seconds.total_seconds()))
            return _ok("1")
    client = _Client()
    session = mod._GatewaySession(client)
    assert [t["name"] for t in session.list_tools()] == ["a___query_a", MODE_A_TOOL]
    assert client.tokens == [None, "page-2"]
    assert session.call_tool(MODE_A_TOOL, {"question": "q"}, 12.5)["status"] == "success"
    assert client.called == [(MODE_A_TOOL, {"question": "q"}, 12.5)]


def test_the_gateway_session_bounds_its_start_and_closes(monkeypatch):
    import agents.gateway_auth as gateway_auth
    mod = _mod()
    seen = {}

    class _FakeMCPClient:
        def __init__(self, transport, **kwargs):
            seen["kwargs"] = kwargs
            seen["entered"] = seen["exited"] = 0

        def __enter__(self):
            seen["entered"] += 1
            return self

        def __exit__(self, *exc):
            seen["exited"] += 1
            return False
    monkeypatch.setattr(gateway_auth, "MCPClient", _FakeMCPClient)
    with mod._open_gateway_session("https://gateway.example.test/mcp", "tok", 7) as session:
        assert isinstance(session, mod._GatewaySession)
    assert seen == {"kwargs": {"startup_timeout": 7}, "entered": 1, "exited": 1}


# ── the job routes ────────────────────────────────────────────────────────────

def _client(monkeypatch, session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.middleware.base import BaseHTTPMiddleware

    class _Who(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.is_integrator = True
            request.state.session = session
            return await call_next(request)
    app = FastAPI()
    app.include_router(onboard.router)
    app.add_middleware(_Who)
    return TestClient(app)


def _poll(client, job_id, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        body = client.get(f"/api/onboard/verify/agent-test/{job_id}").json()
        if body.get("status") != "running":
            return body
        time.sleep(0.02)
    raise AssertionError("the agent test job never finished")


def test_the_routes_start_a_job_and_poll_it_to_its_result(env, monkeypatch):
    mod = _mod()
    gateway = FakeGateway()
    control = FakeControl([[_runtime()]], [[_target()]])
    monkeypatch.setattr(mod, "_control_client", lambda region, **kw: control)
    monkeypatch.setattr(mod, "_open_gateway_session", gateway.open)
    client = _client(monkeypatch, {"access_token": "user-token"})
    start = client.post("/api/onboard/verify/agent-test", json={"system_id": SID})
    assert start.status_code == 200, start.text
    body = start.json()
    assert body["status"] == "running" and body["job_id"]
    assert body["deadline_s"] == mod.AGENT_TEST_DEADLINE_S
    done = _poll(client, body["job_id"])
    assert done["status"] == "done", done
    assert done["result"]["outcome"] == "passed"
    assert done["result"]["rows"] == 42
    assert set(gateway.bearers) == {"user-token"}
    # The user's token is never stored in the job record.
    assert "user-token" not in json.dumps(mod._JOBS.get(body["job_id"]))


def test_an_unknown_system_is_refused_and_an_unknown_job_is_named(env, monkeypatch):
    client = _client(monkeypatch, {})
    res = client.post("/api/onboard/verify/agent-test", json={"system_id": "nope"})
    assert res.status_code == 404, res.text
    lost = client.get("/api/onboard/verify/agent-test/0000").json()
    assert "Unknown job_id" in lost.get("error", ""), lost


# ── the verify route itself ───────────────────────────────────────────────────

def _verify(monkeypatch, onboarded=2):
    from agents.models import VerifyRequest
    entry = {"id": SID, "connection_type": ct.POSTGRESQL, "schema": "sch_a",
             "connection_string": ""}
    monkeypatch.setattr(onboard.catalog, "get", lambda sid: entry)
    monkeypatch.setattr(onboard.catalog, "get_onboarded", lambda: [entry] * onboarded)
    monkeypatch.setattr(onboard, "_verify_pg_reachability",
                        lambda conn_str, schema: (["table_x"], 5, None))
    asked = []

    def _chat():
        asked.append(True)
        raise AssertionError("verify must not send a chat turn")
    monkeypatch.setattr("agents.chat_client.get_chat_client", _chat, raising=False)
    result = asyncio.run(onboard.onboard_verify(VerifyRequest(system_id=SID, schema="sch_a")))
    return result, asked


def test_verify_sends_no_chat_turn(monkeypatch):
    result, asked = _verify(monkeypatch)
    assert asked == [], "verify streamed a chat question"
    assert "test_query_result" not in result, result
    assert result["tables_accessible"] == 1


def test_verify_names_the_system_so_the_chat_checkpoint_can_start_the_agent_test(monkeypatch):
    result, _ = _verify(monkeypatch, onboarded=1)
    assert result.get("system_id") == SID, result


# ── provenance capture gate, deadline bounds, job slot, no backend text ──────
#
# The provenance capture gate (finding 1): the gateway writes a sidecar entry
# keyed by the (session_id, query_ref) the test forwards in the reserved
# ``_adt_provenance`` argument, exactly as a PostgreSQL system agent does for a
# chat turn (FakeGateway does this by default). The test reads it back and
# passes only when the captured statement read the named table and returned a
# result. The ``env`` fixture gives each test a fresh in-memory store.


# F1 HIGH: a number in model text does not pass; a captured read of the table does.

def test_a_bare_number_with_no_capture_does_not_pass(env):
    result, _, gateway, _ = _run(gateway=FakeGateway(replies=[_ok("42")], capture=False))
    assert result["outcome"] != "passed", result
    assert result["outcome"] == "no_read", result
    assert "table_x" in result["reason"]
    assert result["step"] is None
    # PostgreSQL captures its SQL, so a missing capture is a real warning.
    assert result["read_provable"] is True
    assert "recorded no read" in result["reason"]


def test_a_capture_of_another_table_does_not_pass(env):
    gateway = FakeGateway(replies=[_ok("9")], capture_table="table_y", capture_rows=9)
    result, _, gateway, _ = _run(gateway=gateway)
    assert result["outcome"] == "no_read", result
    assert "table_x" in result["reason"]


def test_a_capture_of_the_named_table_passes_and_shows_the_agents_answer(env):
    gateway = FakeGateway(replies=[_ok("5")], capture_table="table_x", capture_rows=1)
    result, _, gateway, _ = _run(gateway=gateway)
    assert result["outcome"] == "passed", result
    assert result["rows"] == 5, result
    assert result["table"] == "table_x"
    assert gateway.provenance_args and gateway.provenance_args[0] is not None


def test_the_captures_result_size_is_never_shown_as_the_count(env):
    # The capture says the statement returned 5 rows; the agent answered 999.
    # The capture proves the read only, so the answer is what is shown.
    gateway = FakeGateway(replies=[_ok("999")], capture_table="table_x", capture_rows=5)
    result, _, gateway, _ = _run(gateway=gateway)
    assert result["outcome"] == "passed", result
    assert result["rows"] == 999, result
    assert "model_number" not in result, result


def test_mode_b_sql_still_passes_on_its_own_result_without_a_capture(env):
    gateway = FakeGateway(
        tools=[{"name": MODE_B_TOOL, "input_schema": MODE_B_SCHEMA}],
        replies=[_ok(json.dumps({"columns": ["row_count"], "rows": [[7]], "row_count": 1}))],
        capture=False)
    result, _, gateway, _ = _run(gateway=gateway)
    assert result["outcome"] == "passed", result
    assert result["rows"] == 7


# F2 MED: an identity deny when the app could not sign the claim is "not_run".

def test_an_identity_deny_without_a_signable_claim_is_not_run(env, monkeypatch):
    mod = _mod()
    from agents.runtime_asgi import identity_deny_texts
    monkeypatch.setattr(mod, "_claim_secret", lambda: None)  # key unreadable here
    result, _, _, _ = _run(gateway=FakeGateway(replies=[_err(identity_deny_texts(SID)[0])],
                                               capture=False),
                           token="user-token")
    assert result["outcome"] == "not_run", result
    assert result["step"] is None
    assert "group" in result["reason"] and "sign" in result["reason"]


def test_an_identity_deny_with_a_signed_claim_still_fails(env, monkeypatch):
    import base64
    mod = _mod()
    from agents.runtime_asgi import identity_deny_texts
    monkeypatch.setattr(mod, "_claim_secret", lambda: "claim-signing-key")
    payload = base64.urlsafe_b64encode(json.dumps(
        {"sub": "u", "cognito:groups": ["g"]}).encode()).decode().rstrip("=")
    token = f"h.{payload}.s"
    result, _, _, _ = _run(gateway=FakeGateway(replies=[_err(identity_deny_texts(SID)[2])],
                                               capture=False),
                           token=token)
    assert (result["outcome"], result["step"]) == ("failed", "tool"), result
    assert "identity policy" in result["reason"]


# F3 MED: a blocking graph read and a blocking control-plane list cannot
# outlast the deadline.

def test_a_blocking_graph_read_is_bounded_by_the_deadline(env, monkeypatch):
    mod = _mod()
    clock = FakeClock()

    def _slow_graph(query, params=None):
        clock.advance(1000.0)  # longer than any deadline
        return [{"name": "table_x"}]
    monkeypatch.setattr(mod, "neptune_query", _slow_graph)
    started = clock.now
    result, _, gateway, clock = _run(clock=clock, deadline_s=5.0)
    assert result["outcome"] in ("failed", "not_run"), result
    # The run returned without starting a tool call after the deadline passed.
    assert gateway.calls == [], "no tool call after the deadline already passed"


def test_the_control_client_is_given_timeouts_capped_to_the_time_left(env, monkeypatch):
    mod = _mod()
    seen = {}

    def _fake_control(region, *, connect_timeout_s=None, read_timeout_s=None):
        seen["connect"] = connect_timeout_s
        seen["read"] = read_timeout_s
        return FakeControl([[_runtime()]], [[_target()]])
    monkeypatch.setattr(mod, "_control_client", _fake_control)
    result = mod.run_agent_test(SID, user_access_token="user-token",
                                open_session=FakeGateway().open,
                                clock=FakeClock(), sleep=lambda s: None)
    assert seen.get("read") is not None and seen["read"] > 0, seen
    assert seen["read"] <= mod.AGENT_TEST_DEADLINE_S


# F4 MED: a stale running record becomes a terminal interrupted result; a
# duplicate start returns the live job.

def test_a_stale_running_record_reads_as_interrupted(env):
    mod = _mod()
    mod._JOBS["old"] = {"status": "running", "system_id": SID, "job_id": "old",
                        "started_at": time.time() - 9999,
                        "deadline_s": mod.AGENT_TEST_DEADLINE_S}
    body = mod.agent_test_status("old")
    assert body["status"] == "done", body
    assert body["result"]["outcome"] in ("failed", "not_run"), body
    assert body["result"].get("step") in (None, "runtime", "gateway_target", "tool")
    # A record started moments ago still reads as running.
    mod._JOBS["fresh"] = {"status": "running", "system_id": SID, "job_id": "fresh",
                          "started_at": time.time(),
                          "deadline_s": mod.AGENT_TEST_DEADLINE_S}
    assert mod.agent_test_status("fresh")["status"] == "running"


def test_a_duplicate_start_returns_the_live_job(env, monkeypatch):
    mod = _mod()
    control = FakeControl([[_runtime("CREATING")]], [[_target()]])  # never settles fast
    monkeypatch.setattr(mod, "_control_client", lambda *a, **k: control)
    monkeypatch.setattr(mod, "_open_gateway_session", FakeGateway().open)
    session = {"access_token": "user-token"}
    first = mod.start_agent_test({"system_id": SID}, session)
    second = mod.start_agent_test({"system_id": SID}, session)
    assert first["job_id"] == second["job_id"], (first, second)


# F5 MED: a failed natural-language answer never echoes backend text.

def test_an_answer_without_a_count_shows_no_backend_text(env):
    secret = "pgdriver.OperationalError host=db.internal.example"
    result, _, _, _ = _run(gateway=FakeGateway(replies=[_ok(secret)], capture=False))
    assert result["outcome"] in ("failed", "no_read"), result
    assert "db.internal.example" not in json.dumps(result), result
    assert "pgdriver" not in json.dumps(result), result


# F6 LOW: the onboarding agent prompt no longer says verify_system tests the
# agent; it checks the source and the graph registration, and says the panel
# runs the agent test separately.

def test_the_onboard_prompt_describes_verify_system_as_source_and_graph():
    from agents.onboard_agent import ONBOARD_AGENT_PROMPT
    line = next(ln for ln in ONBOARD_AGENT_PROMPT.splitlines()
                if ln.strip().startswith("12. verify_system"))
    assert "test that the agent can query the data" not in line
    low = line.lower()
    assert "source" in low and ("registration" in low or "graph" in low)
    assert "agent test" in low and "separate" in low


# The capture proves the read; it does not carry the table's count. A
# COUNT(*) result is one row, so the capture's row_count is 1 whatever the
# table holds. The sidecar here is built by the real capture helpers from the
# PostgreSQL result shape, not written by hand.

def _real_count_sidecar(table: str, count: int) -> dict:
    from agents.system_agent import _build_sidecar_entry
    from agents.tool_result_capture import (collect_sidecar_statements,
                                            record_sidecar_statement,
                                            safe_tool_result_metadata)
    result = {"columns": ["count"], "rows": [[count]], "row_count": 1}
    meta = safe_tool_result_metadata(json.dumps(result), tool_name="query",
                                     max_chars=4000)
    state: dict = {}
    record_sidecar_statement(state, executed_query=f'SELECT COUNT(*) FROM "sch_a"."{table}"',
                             physical_schema="sch_a", metadata=meta, result=result)
    return _build_sidecar_entry(collect_sidecar_statements(state), SID)


class _RealCaptureGateway(FakeGateway):
    def __init__(self, count, **kwargs):
        super().__init__(**kwargs)
        self.count = count

    def _maybe_capture(self, arguments):
        from agents.provenance import parse_provenance_context
        from agents import provenance_store
        context = parse_provenance_context(arguments.get("_adt_provenance"))
        self.provenance_args.append(context)
        session_id, query_ref = context
        provenance_store.put_turn_queries(
            session_id, {query_ref: _real_count_sidecar("table_x", self.count)})


def test_a_count_query_capture_proves_the_read_but_not_the_count(env):
    sidecar = _real_count_sidecar("table_x", 42)
    assert sidecar["row_count"] == 1          # the result's own row count
    result, _, _, _ = _run(gateway=_RealCaptureGateway(42, replies=[_ok("42")]))
    assert result["outcome"] == "passed", result
    # The number shown is the agent's answer, never the capture's result size.
    assert result["rows"] == 42, result
    assert result["query_kind"] == "question"


def test_a_read_with_no_number_in_the_answer_passes_without_a_count(env):
    result, _, _, _ = _run(gateway=_RealCaptureGateway(42, replies=[_ok("The table is read.")]))
    assert result["outcome"] == "passed", result
    assert result["rows"] is None, result


# Real clock: a stage that blocks past the deadline does not hold the job.

def _blocking_run(monkeypatch, *, graph=None, gateway=None, deadline_s=0.2):
    mod = _mod()
    if graph is not None:
        monkeypatch.setattr(mod, "neptune_query", graph)
    control = FakeControl([[_runtime()]], [[_target()]])
    gateway = gateway or FakeGateway()
    started = time.monotonic()
    result = mod.run_agent_test(SID, user_access_token="user-token", control=control,
                                open_session=gateway.open, deadline_s=deadline_s)
    return result, time.monotonic() - started


def test_a_blocking_graph_read_returns_by_the_deadline(env, monkeypatch, real_clock):
    release = threading.Event()

    def _graph(query, params=None):
        release.wait(5.0)
        return [{"name": "table_x"}]
    try:
        result, elapsed = _blocking_run(monkeypatch, graph=_graph)
    finally:
        release.set()
    assert elapsed < 1.5, elapsed
    assert result["outcome"] == "failed", result


def test_a_blocking_tools_list_returns_by_the_deadline(env, monkeypatch, real_clock):
    release = threading.Event()

    class _Blocking(FakeGateway):
        def list_tools(self):
            release.wait(5.0)
            return super().list_tools()
    try:
        result, elapsed = _blocking_run(monkeypatch, gateway=_Blocking())
    finally:
        release.set()
    assert elapsed < 1.5, elapsed
    assert (result["outcome"], result["step"]) == ("failed", "gateway_target"), result


def test_a_blocking_service_token_returns_by_the_deadline(env, monkeypatch, real_clock):
    mod = _mod()
    release = threading.Event()

    def _bearer(config, token):
        release.wait(5.0)
        return "service-token"
    monkeypatch.setattr(mod, "_gateway_bearer", _bearer)
    try:
        result, elapsed = _blocking_run(monkeypatch)
    finally:
        release.set()
    assert elapsed < 1.5, elapsed
    assert result["outcome"] == "failed", result


def test_a_blocking_session_close_returns_by_the_deadline(env, monkeypatch, real_clock):
    release = threading.Event()

    class _SlowClose(FakeGateway):
        @contextlib.contextmanager
        def open(self, url, bearer, startup_timeout_s):
            with super().open(url, bearer, startup_timeout_s) as session:
                yield session
            if self.calls:            # the call's session, not the tools/list one
                release.wait(5.0)
    try:
        result, elapsed = _blocking_run(monkeypatch, gateway=_SlowClose(), deadline_s=0.5)
    finally:
        release.set()
    assert elapsed < 2.0, elapsed
    assert result["outcome"] == "failed", result


# One live test per system, also when the holder finishes between a failed
# claim and the read of who holds the slot.

def test_a_vanished_holder_race_starts_only_one_test(env, monkeypatch):
    mod = _mod()
    started_threads: list = []

    class _Thread:
        def __init__(self, target=None, daemon=None, name=None, args=()):
            self.args = args

        def start(self):
            started_threads.append(self.args[0])
    monkeypatch.setattr(mod.threading, "Thread", _Thread)
    real_claim = mod._JOBS.claim_slot
    real_live = mod._live_job_for
    winner = {}
    calls = {"claim": 0}

    def _claim(key, state, *, stale_seconds, now=None):
        calls["claim"] += 1
        if calls["claim"] == 1:
            return False                  # a first holder is still running
        return real_claim(key, state, stale_seconds=stale_seconds, now=now)

    def _live(system_id):
        if not winner:
            # The first holder finished; a third caller claims the free slot.
            winner["job"] = "third"
            real_claim(mod._slot_key(system_id),
                       {"status": "running", "job_id": "third", "system_id": system_id,
                        "started_at": time.time(), "deadline_s": mod.AGENT_TEST_DEADLINE_S},
                       stale_seconds=mod._stale_seconds())
            return None
        return real_live(system_id)
    monkeypatch.setattr(mod._JOBS, "claim_slot", _claim)
    monkeypatch.setattr(mod, "_live_job_for", _live)
    out = mod.start_agent_test({"system_id": SID}, {"access_token": "user-token"})
    assert started_threads == [], started_threads
    assert out["job_id"] == "third", out
