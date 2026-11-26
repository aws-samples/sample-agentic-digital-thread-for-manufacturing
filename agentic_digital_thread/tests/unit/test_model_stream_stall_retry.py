# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A Bedrock model stream that goes quiet BEFORE any content is retried once.

What a stall looks like on a deployed account: ``converse_stream`` returns
(HTTP 200, headers read), then no event bytes arrive for the client's 120 s
read timeout. botocore's own retries cover only the request; the timeout is
raised later, while strands iterates the event stream, as
``urllib3.exceptions.ReadTimeoutError``, and the whole answer failed with
"Query failed." At that point nothing has been streamed to the user and no
tool has run, so asking the model again is safe. Once any content chunk has
been handed on, a retry would duplicate or corrupt the message, so the error
must propagate exactly as before.

Two layers of test:

* ``TestGuardedStream`` drives the guard with a scripted inner stream.
* ``TestRealTransport`` points a REAL strands ``Agent`` + ``BedrockModel`` +
  botocore client at a loopback server that speaks the Bedrock event-stream
  wire format, so the exception type, the place it is raised and the event
  loop's handling are the production ones, not a fake's.
"""
from __future__ import annotations

import asyncio
import binascii
import http.server
import json
import logging
import struct
import threading

import pytest
import urllib3.exceptions

from agents import model_provider
from agents import model_stream_retry as msr

_MODEL_ID = "us.anthropic.claude-sonnet-5"


# --------------------------------------------------------------------------- #
# Scripted inner streams
# --------------------------------------------------------------------------- #
def _stall() -> urllib3.exceptions.ReadTimeoutError:
    return urllib3.exceptions.ReadTimeoutError(None, None, "Read timed out.")


_START = {"messageStart": {"role": "assistant"}}
_TEXT = {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "ok"}}}
_STOP_BLOCK = {"contentBlockStop": {"contentBlockIndex": 0}}
_STOP = {"messageStop": {"stopReason": "end_turn"}}
_CLEAN = [_START, _TEXT, _STOP_BLOCK, _STOP]


class _ScriptedModel:
    """Stands in for BedrockModel: each ``stream`` call plays the next script.

    A script is a list of events; an exception instance in it is raised at
    that point, the way BedrockModel.stream raises after its worker thread
    fails.
    """

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.calls: list[tuple] = []
        self.config = {"model_id": _MODEL_ID}

    async def stream(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        script = self.scripts[min(len(self.calls), len(self.scripts)) - 1]
        for item in script:
            if isinstance(item, BaseException):
                raise item
            yield item


def _drain(model, *args, **kwargs):
    async def run():
        return [event async for event in model.stream(*args, **kwargs)]
    return asyncio.run(run())


class TestGuardedStream:
    def test_clean_stream_passes_through_unchanged(self):
        model = msr.guard_stream_stalls(_ScriptedModel(_CLEAN))
        assert _drain(model, ["m"], None, "sys") == _CLEAN
        assert len(model.calls) == 1

    def test_stall_before_any_chunk_is_retried_once(self):
        model = msr.guard_stream_stalls(_ScriptedModel([_stall()], _CLEAN))
        assert _drain(model, ["m"]) == _CLEAN
        assert len(model.calls) == 2

    def test_stall_after_only_message_start_is_retried_and_start_not_doubled(self):
        """messageStart carries no content, so it is held until the first
        content chunk; a stall right after it still counts as 'nothing sent'
        and the caller sees exactly one messageStart."""
        model = msr.guard_stream_stalls(_ScriptedModel([_START, _stall()], _CLEAN))
        events = _drain(model, ["m"])
        assert events == _CLEAN
        assert sum("messageStart" in e for e in events) == 1
        assert len(model.calls) == 2

    def test_retry_resends_the_same_request(self):
        model = msr.guard_stream_stalls(_ScriptedModel([_stall()], _CLEAN))
        _drain(model, ["m1", "m2"], [{"spec": 1}], "sys", tool_choice={"auto": {}},
               system_prompt_content=[{"text": "sys"}], invocation_state={"k": 1})
        first, second = model.calls
        assert first == second

    def test_stall_after_content_is_not_retried(self):
        model = msr.guard_stream_stalls(_ScriptedModel([_START, _TEXT, _stall()], _CLEAN))
        seen = []

        async def run():
            async for event in model.stream(["m"]):
                seen.append(event)

        with pytest.raises(urllib3.exceptions.ReadTimeoutError):
            asyncio.run(run())
        assert seen == [_START, _TEXT]
        assert len(model.calls) == 1

    def test_second_stall_propagates(self):
        model = msr.guard_stream_stalls(_ScriptedModel([_stall()], [_START, _stall()]))
        with pytest.raises(urllib3.exceptions.ReadTimeoutError):
            _drain(model, ["m"])
        assert len(model.calls) == 2

    @pytest.mark.parametrize("error", [
        RuntimeError("boom"),
        TimeoutError("builtin timeout is not the transport stall"),
        urllib3.exceptions.ProtocolError("connection reset"),
    ])
    def test_other_errors_are_not_retried(self, error):
        model = msr.guard_stream_stalls(_ScriptedModel([error], _CLEAN))
        with pytest.raises(type(error)):
            _drain(model, ["m"])
        assert len(model.calls) == 1

    def test_botocore_request_phase_timeout_is_left_to_botocore(self):
        """A timeout while waiting for the response HEADERS is raised by
        botocore after its own retries; adding ours would multiply them."""
        import botocore.exceptions
        error = botocore.exceptions.ReadTimeoutError(endpoint_url="https://x")
        model = msr.guard_stream_stalls(_ScriptedModel([error], _CLEAN))
        with pytest.raises(botocore.exceptions.ReadTimeoutError):
            _drain(model, ["m"])
        assert len(model.calls) == 1

    def test_stream_ending_after_message_start_still_yields_it(self):
        model = msr.guard_stream_stalls(_ScriptedModel([_START]))
        assert _drain(model, ["m"]) == [_START]

    def test_retry_is_logged_with_model_and_wait(self, caplog):
        model = msr.guard_stream_stalls(_ScriptedModel([_stall()], _CLEAN))
        with caplog.at_level(logging.WARNING, logger=msr.logger.name):
            _drain(model, ["m"])
        lines = [r.getMessage() for r in caplog.records if r.name == msr.logger.name]
        assert len(lines) == 1
        assert msr.STALL_RETRY_LOG_MARKER in lines[0]
        assert _MODEL_ID in lines[0]

    def test_guard_is_idempotent(self):
        model = _ScriptedModel([_stall()], [_stall()], _CLEAN)
        msr.guard_stream_stalls(msr.guard_stream_stalls(model))
        with pytest.raises(urllib3.exceptions.ReadTimeoutError):
            _drain(model, ["m"])
        assert len(model.calls) == 2, "wrapping twice must not allow two retries"

    def test_object_without_stream_is_returned_untouched(self):
        class NoStream:
            pass
        obj = NoStream()
        assert msr.guard_stream_stalls(obj) is obj
        assert not hasattr(obj, "stream")

    def test_final_attempt_passes_message_start_straight_through(self):
        """No retry is left on the last attempt, so nothing is gained by
        holding messageStart, and holding it would delay strands' per-chunk
        cancel check. The caller sees it before the second stall's error."""
        model = msr.guard_stream_stalls(_ScriptedModel([_stall()], [_START, _stall()]))
        seen = []

        async def run():
            async for event in model.stream(["m"]):
                seen.append(event)

        with pytest.raises(urllib3.exceptions.ReadTimeoutError):
            asyncio.run(run())
        assert seen == [_START]


class _CancelAgent:
    def __init__(self, cancelled: bool):
        self._cancel_signal = threading.Event()
        if cancelled:
            self._cancel_signal.set()


class TestRetryPreconditions:
    def test_cancelled_turn_is_not_retried(self):
        model = msr.guard_stream_stalls(_ScriptedModel([_stall()], _CLEAN))
        with pytest.raises(urllib3.exceptions.ReadTimeoutError):
            _drain(model, ["m"], invocation_state={"agent": _CancelAgent(True)})
        assert len(model.calls) == 1

    def test_live_turn_is_retried(self):
        model = msr.guard_stream_stalls(_ScriptedModel([_stall()], _CLEAN))
        assert _drain(model, ["m"], invocation_state={"agent": _CancelAgent(False)}) == _CLEAN
        assert len(model.calls) == 2

    def test_no_window_means_retry(self):
        model = msr.guard_stream_stalls(_ScriptedModel([_stall()], _CLEAN))
        assert msr.retry_window_remaining() is None
        assert _drain(model, ["m"]) == _CLEAN

    def test_window_too_short_for_a_retry_is_not_retried(self, caplog):
        """The caller must still be listening, with room for the retried call
        and the rest of the query, when a retry is sent."""
        model = msr.guard_stream_stalls(_ScriptedModel([_stall()], _CLEAN))
        with caplog.at_level(logging.WARNING, logger=msr.logger.name):
            with msr.retry_window(msr.STALL_RETRY_MIN_REMAINING_S - 1):
                with pytest.raises(urllib3.exceptions.ReadTimeoutError):
                    _drain(model, ["m"])
        assert len(model.calls) == 1
        lines = [r.getMessage() for r in caplog.records if r.name == msr.logger.name]
        assert len(lines) == 1 and "not retrying" in lines[0]

    def test_window_with_room_is_retried(self):
        model = msr.guard_stream_stalls(_ScriptedModel([_stall()], _CLEAN))
        with msr.retry_window(msr.STALL_RETRY_MIN_REMAINING_S + 5):
            assert _drain(model, ["m"]) == _CLEAN
        assert len(model.calls) == 2

    def test_first_model_call_stall_in_a_system_query_is_retried(self):
        """The likeliest stall in a system query is its first model call:
        120 s gone, 180 s of the 300 s caller window left. That must retry."""
        from agents.system_agent import SYSTEM_QUERY_CALLER_WINDOW_S
        assert SYSTEM_QUERY_CALLER_WINDOW_S - 120 >= msr.STALL_RETRY_MIN_REMAINING_S
        # ...and the slowest whole system call measured (78.5 s) fits the rest.
        assert msr.STALL_RETRY_MIN_REMAINING_S >= 78.5

    def test_window_is_scoped_and_restored(self):
        assert msr.retry_window_remaining() is None
        with msr.retry_window(100):
            assert 99 < msr.retry_window_remaining() <= 100
            with msr.retry_window(10):
                assert msr.retry_window_remaining() <= 10
            assert msr.retry_window_remaining() > 99
        assert msr.retry_window_remaining() is None


class TestStallTimeBookkeeping:
    """A retried stall's wait is recorded on the invocation so the system
    agent's wall budget can leave it out."""

    def test_retried_stall_records_its_wait(self):
        state = {}
        model = msr.guard_stream_stalls(_ScriptedModel([_stall()], _CLEAN))
        _drain(model, ["m"], invocation_state=state)
        assert msr.stalled_seconds(state) >= 0.0
        assert msr.STALL_SECONDS_STATE_KEY in state

    def test_unretried_stall_records_nothing(self):
        state = {"agent": _CancelAgent(True)}
        model = msr.guard_stream_stalls(_ScriptedModel([_stall()], _CLEAN))
        with pytest.raises(urllib3.exceptions.ReadTimeoutError):
            _drain(model, ["m"], invocation_state=state)
        assert msr.STALL_SECONDS_STATE_KEY not in state

    def test_waits_accumulate_across_model_calls(self, monkeypatch):
        """Each attempt reads the clock at its start and again when it fails;
        the fake clock jumps 100 s per read, so each retried stall waited
        100 s, and two model calls with one stall each record 200 s."""
        ticks = iter(range(0, 10_000, 100))
        monkeypatch.setattr(msr.time, "monotonic", lambda: float(next(ticks)))
        state = {}
        for _ in range(2):
            model = msr.guard_stream_stalls(_ScriptedModel([_stall()], _CLEAN))
            _drain(model, ["m"], invocation_state=state)
        assert msr.stalled_seconds(state) == pytest.approx(200.0)

    @pytest.mark.parametrize("state", [None, {}, {"_adt_stream_stall_seconds": "x"},
                                       {"_adt_stream_stall_seconds": -5}])
    def test_stalled_seconds_is_never_negative_or_raises(self, state):
        assert msr.STALL_SECONDS_STATE_KEY == "_adt_stream_stall_seconds"
        assert msr.stalled_seconds(state) == 0.0


class TestToolBudgetLeavesOutStallTime:
    def _event(self, state):
        return type("Event", (), {"invocation_state": state, "cancel_tool": None})()

    def _budget_at(self, monkeypatch, elapsed, stalled):
        from agents import system_agent as sa
        budget = sa._ToolCallBudget(40, 60.0)
        monkeypatch.setattr(budget, "_time", type("T", (), {
            "monotonic": staticmethod(lambda: 1000.0 + elapsed)}))
        state = {sa._TOOL_BUDGET_STARTED_STATE_KEY: 1000.0}
        if stalled:
            state[msr.STALL_SECONDS_STATE_KEY] = stalled
        event = self._event(state)
        budget._on_before_tool_call(event)
        return event.cancel_tool

    def test_stall_time_is_not_charged(self, monkeypatch):
        assert self._budget_at(monkeypatch, elapsed=130.0, stalled=120.0) is None

    def test_real_work_over_budget_still_stops(self, monkeypatch):
        cancel = self._budget_at(monkeypatch, elapsed=190.0, stalled=120.0)
        assert cancel and "TOOL BUDGET EXHAUSTED" in cancel and "70s elapsed" in cancel

    def test_no_stall_behaves_as_before(self, monkeypatch):
        assert self._budget_at(monkeypatch, elapsed=59.0, stalled=0) is None
        assert "TOOL BUDGET EXHAUSTED" in self._budget_at(monkeypatch, elapsed=61.0, stalled=0)

    def test_a_new_invocation_drops_the_previous_stall_credit(self):
        """The blank-answer recovery path re-invokes the agent with the same
        invocation_state; its clock restarts, so an earlier stall must not
        still be subtracted from it."""
        from agents import system_agent as sa
        state = {msr.STALL_SECONDS_STATE_KEY: 120.0}
        sa._ToolCallBudget(40, 60.0)._on_invocation_start(self._event(state))
        assert msr.stalled_seconds(state) == 0.0


class TestSystemAgentCallWindow:
    """A Mode-A system agent answers one MCP tool call. Its caller (the
    orchestrator, through the gateway) stops listening after the MCP client's
    read timeout, so the retry must fit inside that call."""

    def test_window_matches_the_orchestrators_mcp_read_timeout(self):
        import inspect

        from mcp.client.streamable_http import streamablehttp_client

        from agents import gateway_auth, system_agent
        default = inspect.signature(streamablehttp_client).parameters["sse_read_timeout"].default
        assert system_agent.SYSTEM_QUERY_CALLER_WINDOW_S <= float(default)
        # The orchestrator's gateway client must not override that timeout,
        # or the window above would be measured against the wrong number.
        assert "sse_read_timeout" not in inspect.getsource(gateway_auth)

    def test_mode_a_query_runs_the_agent_inside_the_window(self, monkeypatch):
        from agents import system_agent as sa
        seen = {}

        def fake_invoke(agent, question):
            seen["remaining"] = msr.retry_window_remaining()
            return "answer", {}

        monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state", fake_invoke)
        monkeypatch.setattr(sa, "_relay_inner_chart_tags", lambda agent, answer: answer)
        out = asyncio.run(sa._run_mode_a_query(object(), "sys-a", "q"))
        assert out == "answer"
        assert seen["remaining"] is not None
        assert sa.SYSTEM_QUERY_CALLER_WINDOW_S - 5 < seen["remaining"] <= sa.SYSTEM_QUERY_CALLER_WINDOW_S
        assert msr.retry_window_remaining() is None


# --------------------------------------------------------------------------- #
# The seam: every Converse model build_model hands out is guarded
# --------------------------------------------------------------------------- #
class TestBuildModelInstallsTheGuard:
    def test_converse_model_is_guarded_and_keeps_its_type_and_config(self):
        m = model_provider.build_model(_MODEL_ID, max_tokens=8192)
        assert type(m).__name__ == "BedrockModel"
        assert m.config.get("max_tokens") == 8192
        assert msr.is_guarded(m)

    def test_mantle_model_is_not_wrapped(self, monkeypatch):
        sentinel = type("Provider", (), {})()
        monkeypatch.setattr(model_provider, "_mantle_model",
                            lambda bare, region, mt=None: sentinel)
        assert model_provider.build_model("openai.gpt-5.6-luna", max_tokens=8192) is sentinel
        assert not msr.is_guarded(sentinel)

    def test_system_agent_model_is_guarded(self, monkeypatch):
        monkeypatch.setenv("SYSTEM_AGENT_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
        from agents.system_agent import _system_agent_model
        assert msr.is_guarded(_system_agent_model())

    def test_orchestrator_model_is_guarded(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_MODEL", _MODEL_ID)
        from agents import orchestrator
        monkeypatch.setattr(orchestrator, "_build_orchestrator_prompt", lambda: "p")
        monkeypatch.setattr(orchestrator, "_metamodel_prompt_suffix", lambda: "")
        kwargs = orchestrator._base_agent_kwargs(tools=[], conversation_manager=None)
        assert msr.is_guarded(kwargs["model"])


# --------------------------------------------------------------------------- #
# Real transport: strands + botocore + urllib3 against a loopback event stream
# --------------------------------------------------------------------------- #
def _header(name: str, value: str) -> bytes:
    n, v = name.encode(), value.encode()
    return struct.pack(">B", len(n)) + n + struct.pack(">BH", 7, len(v)) + v


def _frame(event_type: str, payload: dict) -> bytes:
    """One AWS event-stream message (prelude, CRCs, string headers, JSON)."""
    headers = (_header(":event-type", event_type)
               + _header(":content-type", "application/json")
               + _header(":message-type", "event"))
    body = json.dumps(payload).encode()
    prelude = struct.pack(">II", 12 + len(headers) + len(body) + 4, len(headers))
    message = prelude + struct.pack(">I", binascii.crc32(prelude)) + headers + body
    return message + struct.pack(">I", binascii.crc32(message))


_WIRE_START = _frame("messageStart", {"role": "assistant"})
_WIRE_TEXT = _frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "hello"}})
_WIRE_ANSWER = [
    _WIRE_START, _WIRE_TEXT,
    _frame("contentBlockStop", {"contentBlockIndex": 0}),
    _frame("messageStop", {"stopReason": "end_turn"}),
    _frame("metadata", {"usage": {"inputTokens": 3, "outputTokens": 1, "totalTokens": 4},
                        "metrics": {"latencyMs": 5}}),
]


def test_wire_frames_decode_with_botocore():
    """Guards the fixture itself: botocore must parse what the server sends."""
    from botocore.eventstream import EventStreamBuffer
    buf = EventStreamBuffer()
    for frame in _WIRE_ANSWER:
        buf.add_data(frame)
    types = [m.headers[":event-type"] for m in buf]
    assert types == ["messageStart", "contentBlockDelta", "contentBlockStop",
                     "messageStop", "metadata"]


class _FakeBedrock:
    """Loopback ConverseStream endpoint. Each request plays the next scenario:
    a list of frames, optionally ending in ``"STALL"`` (send headers + those
    frames, then hold the connection open with no more bytes)."""

    def __init__(self, *scenarios):
        self.scenarios = list(scenarios)
        self.paths: list[str] = []
        self.release = threading.Event()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                owner.paths.append(self.path)
                plan = owner.scenarios[min(len(owner.paths), len(owner.scenarios)) - 1]
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.amazon.eventstream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for frame in plan:
                    if frame == "STALL":
                        self.wfile.flush()
                        self.close_connection = True
                        owner.release.wait(15)
                        return
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(frame), frame))
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


def _real_agent(endpoint: str, *, tools=None, hooks=None):
    """A production-built model (build_model) whose client points at the
    loopback server with a 1 s read timeout instead of 120 s."""
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    from strands import Agent

    model = model_provider.build_model(_MODEL_ID, max_tokens=256)
    model.client = boto3.Session(region_name="us-east-1").client(
        "bedrock-runtime", endpoint_url=endpoint,
        config=Config(signature_version=UNSIGNED, read_timeout=1, connect_timeout=2,
                      retries={"max_attempts": 1, "mode": "standard"}))
    return Agent(model=model, callback_handler=None, tools=tools or [], hooks=hooks or [])


@pytest.mark.allow_hosts(["127.0.0.1"])
class TestRealTransport:
    def test_stall_after_headers_is_retried_and_the_answer_arrives(self):
        with _FakeBedrock(["STALL"], _WIRE_ANSWER) as fake:
            result = _real_agent(fake.url)("hi")
        assert "hello" in str(result)
        assert len(fake.paths) == 2
        assert all(p.endswith("/converse-stream") for p in fake.paths)

    def test_stall_after_message_start_is_retried(self):
        with _FakeBedrock([_WIRE_START, "STALL"], _WIRE_ANSWER) as fake:
            result = _real_agent(fake.url)("hi")
        assert "hello" in str(result)
        assert len(fake.paths) == 2

    def test_stall_after_text_is_not_retried(self):
        with _FakeBedrock([_WIRE_START, _WIRE_TEXT, "STALL"], _WIRE_ANSWER) as fake:
            with pytest.raises(Exception) as info:
                _real_agent(fake.url)("hi")
        assert _caused_by_read_timeout(info.value)
        assert len(fake.paths) == 1

    def test_two_stalls_fail_the_turn_as_before(self):
        with _FakeBedrock(["STALL"], ["STALL"], _WIRE_ANSWER) as fake:
            with pytest.raises(Exception) as info:
                _real_agent(fake.url)("hi")
        assert _caused_by_read_timeout(info.value)
        assert len(fake.paths) == 2

    def test_cancel_during_the_stall_stops_the_retry(self):
        """Agent.cancel() mid-stall (the local-mode cancel seam) must not let
        the guard send a second request."""
        with _FakeBedrock(["STALL"], _WIRE_ANSWER) as fake:
            agent = _real_agent(fake.url)
            timer = threading.Timer(0.3, agent.cancel)
            timer.start()
            try:
                with pytest.raises(Exception) as info:
                    agent("hi")
            finally:
                timer.cancel()
        assert _caused_by_read_timeout(info.value)
        assert len(fake.paths) == 1

    def test_mode_a_query_window_crosses_strands_threads(self, monkeypatch):
        """The window set by _run_mode_a_query must reach the guard through
        asyncio.to_thread and strands' own run_async worker thread. Scaled
        down: 1 s read timeout, 1 s minimum remaining."""
        from agents import system_agent as sa
        monkeypatch.setattr(msr, "STALL_RETRY_MIN_REMAINING_S", 1.0)
        monkeypatch.setattr(sa, "_relay_inner_chart_tags", lambda agent, answer: answer)
        monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state",
                            lambda agent, question: (str(agent(question)), {}))

        # Too short: after a 1 s stall about 0.8 s are left, under the 1 s minimum.
        monkeypatch.setattr(sa, "SYSTEM_QUERY_CALLER_WINDOW_S", 1.8)
        with _FakeBedrock(["STALL"], _WIRE_ANSWER) as fake:
            with pytest.raises(Exception) as info:
                asyncio.run(sa._run_mode_a_query(_real_agent(fake.url), "sys-a", "hi"))
        assert _caused_by_read_timeout(info.value)
        assert len(fake.paths) == 1

        # Room enough: the same stall is retried and the answer arrives.
        monkeypatch.setattr(sa, "SYSTEM_QUERY_CALLER_WINDOW_S", 6.0)
        with _FakeBedrock(["STALL"], _WIRE_ANSWER) as fake:
            out = asyncio.run(sa._run_mode_a_query(_real_agent(fake.url), "sys-a", "hi"))
        assert "hello" in out
        assert len(fake.paths) == 2

    def test_retried_answer_still_gets_its_tool_time(self):
        """A system agent's tool budget is a wall clock from the start of the
        question (60 s by default). A recovered stall must not have eaten it:
        the retry asks for a data tool, the tool must RUN, and the closing
        answer must reach the caller. Scaled: 1 s read timeout, 0.8 s budget,
        so the 1 s stall alone is over budget."""
        from strands import tool

        from agents import system_agent as sa

        ran = []

        @tool
        def lookup(q: str) -> str:
            """Look up q."""
            ran.append(q)
            return "42 rows"

        tool_use = [
            _frame("messageStart", {"role": "assistant"}),
            _frame("contentBlockStart", {"contentBlockIndex": 0, "start": {
                "toolUse": {"toolUseId": "t1", "name": "lookup"}}}),
            _frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {
                "toolUse": {"input": json.dumps({"q": "x"})}}}),
            _frame("contentBlockStop", {"contentBlockIndex": 0}),
            _frame("messageStop", {"stopReason": "tool_use"}),
            _frame("metadata", {"usage": {"inputTokens": 3, "outputTokens": 1,
                                          "totalTokens": 4}, "metrics": {"latencyMs": 5}}),
        ]
        with _FakeBedrock(["STALL"], tool_use, _WIRE_ANSWER) as fake:
            agent = _real_agent(fake.url, tools=[lookup],
                                hooks=[sa._ToolCallBudget(40, 0.8)])
            result = agent("hi")
        assert ran == ["x"], "the data tool was cancelled by the wall budget"
        assert "hello" in str(result)
        assert len(fake.paths) == 3


def _caused_by_read_timeout(exc: BaseException) -> bool:
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, urllib3.exceptions.ReadTimeoutError):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False
