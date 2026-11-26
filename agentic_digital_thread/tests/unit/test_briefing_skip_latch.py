# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Briefing admission releases exact ownership tokens on every stream exit."""
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import patch

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.prompts as prompts  # noqa: E402
import agents.server as server  # noqa: E402
from agents.events import DoneEvent, MessageEvent  # noqa: E402
from agents.models import AskRequest  # noqa: E402
from agents.query_admission import get_admission  # noqa: E402


def _drain(events):
    """A directly-owned user turn with queued events and an admission token."""
    admission = get_admission()
    token = admission.acquire("query")
    owner = server._StreamOwner(token)
    worker = server._StreamWorker(maxsize=max(1, len(events)))
    owner.attach(worker)
    for event in events:
        worker.queue.put(event)
    stats = {"token_count": 0, "tools_used": []}
    return server._drain_event_queue(
        worker, stats, time.monotonic(), owner), admission


def setup_function():
    get_admission().reset()


def teardown_function():
    get_admission().reset()


def test_generator_closed_mid_stream_releases_its_token():
    gen, admission = _drain([
        MessageEvent(content="hello"), MessageEvent(content="world")])
    assert "hello" in next(gen)
    assert admission.busy is True

    gen.close()

    assert admission.busy is False


def test_normal_completion_releases_its_token():
    gen, admission = _drain([MessageEvent(content="answer"), None])
    frames = list(gen)
    assert len(frames) == 1
    assert admission.busy is False


class _FakeRequestState:
    user = None
    session = None


class _FakeRequest:
    state = _FakeRequestState()


class _StubClient:
    def __init__(self):
        self.streams = 0

    def stream(self, **kwargs):
        self.streams += 1
        yield MessageEvent(content="ok")


async def _all_chunks(response) -> str:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
    return "".join(chunks)


def test_briefing_not_skipped_after_mid_stream_disconnect():
    gen, admission = _drain([MessageEvent(content="partial")])
    next(gen)
    gen.close()
    assert admission.busy is False

    stub = _StubClient()
    body = AskRequest(question="morning briefing",
                      session_id="briefing-2026-07-29")
    with patch.object(server, "get_chat_client", lambda: stub):
        response = asyncio.run(server.ask(body, _FakeRequest()))
        stream = asyncio.run(_all_chunks(response))

    assert "Briefing skipped" not in stream
    assert stub.streams == 1
    assert admission.busy is False


def test_busy_scheduler_result_is_not_persisted(monkeypatch):
    class _PromptStore:
        @staticmethod
        def get(template_id):
            return {"template_id": template_id, "text": "brief me"}

    class _CardStore:
        @staticmethod
        def get_by_prompt(_template_id):
            raise AssertionError("busy result must not reach card persistence")

    monkeypatch.setattr(prompts, "_prompt_store", _PromptStore())
    monkeypatch.setattr(prompts, "_card_store", _CardStore())
    monkeypatch.setattr(prompts, "_execute_fn",
                        lambda _text: prompts.SCHEDULER_BUSY_RESULT)

    asyncio.run(prompts._execute_scheduled_prompt("scheduled-1"))


def test_briefing_is_skipped_while_query_token_is_held():
    admission = get_admission()
    query = admission.acquire("query")
    try:
        body = AskRequest(question="morning briefing",
                          session_id="briefing-2026-07-29")
        with patch.object(server, "get_chat_client",
                          side_effect=AssertionError("must not reach client")):
            response = asyncio.run(server.ask(body, _FakeRequest()))
            stream = asyncio.run(_all_chunks(response))
        expected = DoneEvent(text="[Briefing skipped — query in progress]")
        assert stream == expected.to_sse_format()
        assert admission.owners() == ("query",)
    finally:
        admission.release(query)
