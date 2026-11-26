# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Telling a dropped chat stream apart from our own failures.

On a test event (2026-10-15) four chat turns ended with "The connection
dropped before the answer arrived" and nothing in the app or runtime logs
said which side closed. These pins add one line on each side and fix the
category a mid-stream read timeout shows:

* B: the app logs ``ASK_CLIENT_DISCONNECT`` (elapsed time + the turn's
  support reference) when the browser's ``http.disconnect`` arrives on an
  /api/ask stream, once, and never on a turn that finished;
* C: the runtime logs ``ORCHESTRATOR_CLIENT_GONE`` (elapsed time + the tool
  call still running) when the consumer of its stream goes away early, and
  never on a normal end or an inner error;
* D: a botocore read/connect timeout is the ``timeout`` category, so the user
  reads "timed out" instead of the generic stream error;
* R2 (app side): ``AskRequest.retry`` rides to the runtime as the RESERVED
  ``retryUndeliveredTurn`` payload key only when it is exactly true.

Offline: stub chat clients, no AWS, no sockets.
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest

import agents.server as server
from agents import runtime_app
from agents.events import DoneEvent, MessageEvent
from agents.models import AskRequest
from tests.unit.test_ask_asgi_disconnect import (  # noqa: F401 - harness
    _AsgiRun,
    _BlockedInReadClient,
    _FakeRequest,
    _scope,
    _wait,
)


def _ask(client, *, session_id: str = "s-ref-1", retry=None):
    fields = {"question": "How many records failed?", "session_id": session_id}
    if retry is not None:
        fields["retry"] = retry
    body = AskRequest(**fields)
    with patch.object(server, "get_chat_client", lambda: client):
        return asyncio.run(server.ask(body, _FakeRequest()))


class _QuickClient:
    """Streams two frames and ends; records the kwargs it was called with."""

    def __init__(self):
        self.calls: list[dict] = []

    def stream(self, cancel=None, **kwargs):
        self.calls.append(kwargs)
        yield MessageEvent(content="Three records.")
        yield DoneEvent(text="Three records.")


async def _complete(response) -> list[dict]:
    """Drive *response* to the end with a client that never disconnects."""
    sent: list[dict] = []
    never = asyncio.Event()

    async def receive():
        await never.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await response(_scope(), receive, send)
    return sent


def _lines(caplog, tag: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(tag)]


# --------------------------------------------------------------------------- #
# B. The app records a browser disconnect
# --------------------------------------------------------------------------- #
def test_a_browser_disconnect_logs_one_line_with_elapsed_and_reference(caplog):
    caplog.set_level(logging.INFO, logger="agents.server")
    client = _BlockedInReadClient()
    response = _ask(client, session_id="s-ref-1")
    asyncio.run(_AsgiRun()(response))
    lines = _lines(caplog, "ASK_CLIENT_DISCONNECT")
    assert len(lines) == 1, lines
    assert "elapsed_s=" in lines[0]
    assert "reference=s-ref-1" in lines[0]


def test_the_spec_24_disconnect_is_logged_too(caplog):
    from starlette.requests import ClientDisconnect
    caplog.set_level(logging.INFO, logger="agents.server")
    client = _BlockedInReadClient()
    response = _ask(client)
    with pytest.raises(ClientDisconnect):
        asyncio.run(_AsgiRun(spec_version="2.4", send_raises=True)(response))
    assert len(_lines(caplog, "ASK_CLIENT_DISCONNECT")) == 1


def test_a_finished_turn_logs_no_disconnect(caplog):
    caplog.set_level(logging.INFO, logger="agents.server")
    sent = asyncio.run(_complete(_ask(_QuickClient())))
    assert any(m["type"] == "http.response.body" for m in sent)
    assert _lines(caplog, "ASK_CLIENT_DISCONNECT") == []


def test_the_reference_is_reduced_to_safe_characters():
    line = server._client_disconnect_line(0.0, "s-1\nFORGED line")
    assert "\n" not in line
    assert "FORGED line" not in line


# --------------------------------------------------------------------------- #
# C. The runtime records a consumer that went away early
# --------------------------------------------------------------------------- #
def _tool_start(tool_id: str, name: str) -> dict:
    return {"current_tool_use": {"toolUseId": tool_id, "name": name, "input": ""}}


def _tool_result(tool_id: str) -> dict:
    return {"message": {"role": "user", "content": [
        {"toolResult": {"toolUseId": tool_id, "status": "success",
                        "content": [{"text": "3 rows"}]}}]}}


def _close_after(events: list[dict], *, block: bool = True) -> list:
    """Consume len(events) events from the wrapper, then close it early."""
    async def inner():
        for event in events:
            yield event
        if block:
            await asyncio.sleep(30)
        yield {"data": "never reached"}

    async def run():
        agen = runtime_app._with_stream_heartbeat(inner(), interval=1000)
        got = []
        async for event in agen:
            got.append(event)
            if len(got) == len(events):
                break
        await agen.aclose()
        return got

    return asyncio.run(run())


def test_the_runtime_logs_the_consumer_leaving_with_the_tool_in_flight(caplog):
    caplog.set_level(logging.INFO, logger="agents.runtime_app")
    _close_after([_tool_start("t1", "plant1___query_records")])
    lines = _lines(caplog, "ORCHESTRATOR_CLIENT_GONE")
    assert len(lines) == 1, lines
    assert "elapsed_s=" in lines[0]
    assert "in_flight_tool=plant1___query_records" in lines[0]


def test_a_finished_tool_is_no_longer_in_flight(caplog):
    caplog.set_level(logging.INFO, logger="agents.runtime_app")
    _close_after([_tool_start("t1", "plant1___query_records"), _tool_result("t1")])
    [line] = _lines(caplog, "ORCHESTRATOR_CLIENT_GONE")
    assert "in_flight_tool=none" in line


def test_the_integrity_path_result_event_also_ends_the_call(caplog):
    caplog.set_level(logging.INFO, logger="agents.runtime_app")
    _close_after([_tool_start("t1", "plant1___query_records"),
                  {"tool_result_metadata": {"toolUseId": "t1",
                                            "name": "plant1___query_records",
                                            "metadata": {}}}])
    [line] = _lines(caplog, "ORCHESTRATOR_CLIENT_GONE")
    assert "in_flight_tool=none" in line


def test_a_cancelled_consumer_is_logged(caplog):
    caplog.set_level(logging.INFO, logger="agents.runtime_app")

    async def inner():
        yield _tool_start("t1", "plant1___query_records")
        await asyncio.sleep(30)
        yield {"data": "never"}

    async def run():
        agen = runtime_app._with_stream_heartbeat(inner(), interval=1000)
        first = await agen.__anext__()
        task = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return first

    asyncio.run(run())
    assert len(_lines(caplog, "ORCHESTRATOR_CLIENT_GONE")) == 1


def test_a_normal_end_logs_nothing(caplog):
    caplog.set_level(logging.INFO, logger="agents.runtime_app")

    async def inner():
        yield _tool_start("t1", "plant1___query_records")
        yield _tool_result("t1")
        yield {"data": "Three records."}

    async def run():
        return [e async for e in runtime_app._with_stream_heartbeat(inner(), interval=1000)]

    assert len(asyncio.run(run())) == 3
    assert _lines(caplog, "ORCHESTRATOR_CLIENT_GONE") == []


def test_an_inner_error_is_not_a_client_leaving(caplog):
    caplog.set_level(logging.INFO, logger="agents.runtime_app")

    async def inner():
        yield {"data": "partial"}
        raise RuntimeError("inner failed")

    async def run():
        return [e async for e in runtime_app._with_stream_heartbeat(inner(), interval=1000)]

    with pytest.raises(RuntimeError):
        asyncio.run(run())
    assert _lines(caplog, "ORCHESTRATOR_CLIENT_GONE") == []


# --------------------------------------------------------------------------- #
# D. A read timeout reads as a timeout
# --------------------------------------------------------------------------- #
def test_a_mid_stream_read_timeout_is_the_timeout_category():
    from botocore.exceptions import ReadTimeoutError
    from agents.chat_client import _public_cloud_category
    exc = ReadTimeoutError(endpoint_url="https://runtime.invalid/invocations")
    assert _public_cloud_category(exc, default="stream_error") == "timeout"


def test_a_connect_timeout_is_the_timeout_category():
    from botocore.exceptions import ConnectTimeoutError
    from agents.chat_client import _public_cloud_category
    exc = ConnectTimeoutError(endpoint_url="https://runtime.invalid/invocations")
    assert _public_cloud_category(exc, default="runtime_unavailable") == "timeout"


def test_an_incomplete_read_stays_a_stream_error():
    from botocore.exceptions import ResponseStreamingError
    from agents.chat_client import _public_cloud_category
    exc = ResponseStreamingError(error="IncompleteRead(0 bytes read)")
    assert _public_cloud_category(exc, default="stream_error") == "stream_error"


def test_the_timeout_text_is_what_the_browser_shows():
    from agents.events import public_error_message
    assert "timed out" in public_error_message("timeout")


def test_a_mid_stream_read_timeout_streams_the_timeout_frame():
    from botocore.exceptions import ReadTimeoutError
    from agents import chat_client

    class _State:
        log = logging.getLogger("agentcore_client")
        runtime_session_id = "session-1"
        session_id = "s-ref-1"
        phase = "read"
        started = None
        line_count = total_bytes = 0

        def iter_stream(self, _body):
            yield MessageEvent(content="partial")
            raise ReadTimeoutError(endpoint_url="https://runtime.invalid/invocations")

        def close(self):
            return None

    with patch.object(chat_client, "_log_stream_failure", lambda *a, **k: None):
        events = list(chat_client._stream_cloud_response({"response": object()}, _State()))
    [error] = [e for e in events if getattr(e, "category", None)]
    assert error.category == "timeout"
    assert "timed out" in error.message


# --------------------------------------------------------------------------- #
# R2 (app side). "Ask again" reaches the runtime as a reserved key
# --------------------------------------------------------------------------- #
def test_ask_again_sends_the_reserved_retry_key_to_the_runtime():
    client = _QuickClient()
    asyncio.run(_complete(_ask(client, retry=True)))
    assert _wait(lambda: client.calls)
    assert client.calls[0].get("extra_payload") == {"retryUndeliveredTurn": True}


def test_the_app_and_the_runtime_share_one_key_spelling():
    from agents.history_turns import RETRY_PAYLOAD_KEY
    assert RETRY_PAYLOAD_KEY == "retryUndeliveredTurn"


@pytest.mark.parametrize("retry", [None, False])
def test_an_ordinary_send_carries_no_retry_key(retry):
    client = _QuickClient()
    asyncio.run(_complete(_ask(client, retry=retry)))
    assert _wait(lambda: client.calls)
    assert "extra_payload" not in client.calls[0]


def test_the_retry_key_survives_the_runtime_payload_merge():
    # A pin on the existing additive seam (passes before and after the fix).
    from agents.chat_client import _compose_runtime_payload
    base = {"prompt": "p", "sessionId": "s"}
    assert _compose_runtime_payload(base, {"retryUndeliveredTurn": True}) == {
        **base, "retryUndeliveredTurn": True}
