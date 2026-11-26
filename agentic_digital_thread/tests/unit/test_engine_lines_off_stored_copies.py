# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Stored copies of an answer never keep an orchestrator-only engine line.

When the orchestrator copies an engine line meant for it alone (the
identifiers line, ``answer_ids``, or the names line, ``answer_names``) into
its answer, the live stream drops it (``public_answer``). The session manager
stores its own copy of the assistant message, and Recent sessions replays the
stored events (``memory_routes.get_events`` through
``memory_turns.display_blocks``). The stored assistant copy is written
without the line, a record stored before that is shown without it, and a
streamed line longer than the stream's hold bound is still left out.
"""
from __future__ import annotations

import json

from agents import answer_ids, answer_names
from agents.public_answer import InternalTelemetryFilter, redact_internal_telemetry
from tests.unit.test_memory_events_rehydrate import _event, _route

IDS = answer_ids.engine_line(["KLN-FAB-9999"])
NAMES = answer_names.engine_line(["Harbor Tile Works"])
ANSWER = f"Lot no 41 is the kiln lot that got Matte White.\n\n{IDS}\n\nTotal: 3 lots."
PUBLIC = "Lot no 41 is the kiln lot that got Matte White.\n\nTotal: 3 lots."


class _FakeManager:
    """A session manager with the three hooks ``_wrap_session_manager`` wraps."""

    def __init__(self):
        self.stored: list = []

    def retrieve_customer_context(self, event):
        return None

    def create_message(self, session_id, agent_id, session_message, **kwargs):
        self.stored.append(session_message)

    def list_messages(self, *args, **kwargs):
        return list(self.stored)


def _store(message: dict):
    from strands.types.session import SessionMessage
    from agents.orchestrator import _wrap_session_manager
    manager = _wrap_session_manager(_FakeManager())
    original = SessionMessage.from_message(message, 0)
    manager.create_message("s1", "default", original)
    return original, manager.stored[-1]


def _texts(session_message) -> list:
    return [b["text"] for b in session_message.message["content"] if isinstance(b, dict)
            and isinstance(b.get("text"), str)]


def test_the_stored_assistant_message_drops_the_line():
    original, stored = _store({"role": "assistant", "content": [{"text": ANSWER}]})
    assert _texts(stored) == [PUBLIC]
    assert _texts(original) == [ANSWER], "the live conversation was changed"


def test_the_stored_assistant_message_drops_the_names_line_too():
    _orig, stored = _store({"role": "assistant",
                            "content": [{"text": f"Two lots shipped.\n\n{NAMES}"}]})
    assert _texts(stored) == ["Two lots shipped."]


def test_a_stored_tool_result_keeps_the_line_for_the_orchestrator():
    """The tool result is what the orchestrator reads back on a later turn;
    its line stays. Replay shows only its summary, which drops the line."""
    result = {"toolResult": {"toolUseId": "t1", "status": "success",
                             "content": [{"text": f"Lot KLN-FAB-9999.\n\n{IDS}"}]}}
    _orig, stored = _store({"role": "user", "content": [result]})
    kept = stored.message["content"][0]["toolResult"]["content"][0]["text"]
    assert kept.endswith(IDS)


def test_an_assistant_message_without_the_line_is_stored_as_is():
    _orig, stored = _store({"role": "assistant", "content": [{"text": "Plain answer."}]})
    assert _texts(stored) == ["Plain answer."]


def _replayed(answer: str, tool_text: str = "") -> dict:
    events = [_event("e1", 0, "user", [{"text": "Which lot got Matte White?"}])]
    if tool_text:
        events += [
            _event("e2", 1, "assistant", [{"toolUse": {"toolUseId": "t1",
                                                       "name": "kiln-mes___query_kiln_mes",
                                                       "input": {"question": "q"}}}]),
            _event("e3", 2, "user", [{"toolResult": {"toolUseId": "t1", "status": "success",
                                                     "content": [{"text": tool_text}]}}]),
        ]
    events.append(_event("e9", 9, "assistant", [{"text": answer}]))
    body, _fake = _route(events)
    return body


def test_recent_sessions_replay_of_an_older_record_drops_the_line():
    """A record stored before the write-side strip existed still holds the
    line; the replay shows the answer without it, in content and in blocks."""
    body = _replayed(ANSWER, tool_text=f"Lot KLN-FAB-9999.\n\n{IDS}")
    text = json.dumps(body)
    assert answer_ids.MARKER not in text, text
    answer = body["messages"][-1]
    assert answer["content"] == PUBLIC
    assert [b["text"] for b in answer["blocks"] if b["type"] == "text"] == [PUBLIC]


def test_recent_sessions_replay_of_a_plain_answer_is_unchanged():
    body = _replayed("Plain answer.")
    answer = body["messages"][-1]
    assert answer["content"] == "Plain answer."
    assert answer["blocks"] == [{"type": "text", "text": "Plain answer."}]


def test_replay_of_a_plain_text_record_drops_the_line():
    """An event whose payload is plain text (no message envelope)."""
    events = [_event("e1", 0, "user", "Which lot?"), _event("e2", 1, "assistant", ANSWER)]
    body, _fake = _route(events)
    assert answer_ids.MARKER not in json.dumps(body)


# --------------------------------------------------------------------------- #
# The stream filter past its hold bound
# --------------------------------------------------------------------------- #

def _long_line() -> str:
    return answer_ids.MARKER + " the answer gives " + ", ".join(
        f'"KLN-{"9" * 40}-{k}"' for k in range(120)) + ". Do not state them as data."


def test_a_marker_line_past_the_hold_bound_is_left_out():
    line = _long_line()
    assert len(line) > 4000
    public, _ = redact_internal_telemetry(f"Answer.\n\n{line}\n\nAfter.")
    assert answer_ids.MARKER not in public and "KLN-9999" not in public
    assert public == "Answer.\n\nAfter."


def test_a_long_marker_line_streamed_in_chunks_is_left_out():
    text = f"Answer.\n\n{_long_line()}\nAfter."
    for size in (1, 7, 500, 5000):
        filter_ = InternalTelemetryFilter()
        out = "".join(filter_.feed(text[i:i + size]) for i in range(0, len(text), size))
        out += filter_.flush()
        assert answer_ids.MARKER not in out, size
        assert out == "Answer.\n\nAfter.", (size, out[:80])


def test_a_long_marker_line_at_the_end_of_the_answer_is_left_out():
    public, _ = redact_internal_telemetry(f"Answer.\n\n{_long_line()}")
    assert public == "Answer.\n\n"
    assert answer_ids.MARKER not in public


def test_a_long_ordinary_line_is_shown_as_written():
    line = "[engine] other " + "x" * 5000
    public, _ = redact_internal_telemetry(f"Answer.\n{line}\nAfter.")
    assert public == f"Answer.\n{line}\nAfter."


def test_the_counter_is_still_recorded_after_a_long_marker_line():
    public, counts = redact_internal_telemetry(
        f"Answer.\n{_long_line()}\nadditional_candidate_pairs_observed: 2\nEnd.")
    assert counts == [2]
    assert answer_ids.MARKER not in public
