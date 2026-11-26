# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""GET /api/memory/events feeds the chat's Recent sessions reload.

Seen on a Workshop Studio test event: a reopened session showed
the answer ABOVE the question, no Reasoning fold, no Sources row, and the raw
---VALUE-MAPPING-PROPOSAL--- / ---FOLLOWUPS--- blocks. On the read path:

  * ``ListEvents`` returns the NEWEST event first (the SDK's own readers
    reverse it: ``AgentCoreMemoryConverter.events_to_messages`` and
    ``read_agent``'s ``max_results=1``), and ``get_events`` passed that order
    straight through. The timestamp it read (``createdAt``) is not a field of
    an event (``eventTimestamp`` is), so nothing could reorder it later.
  * One page of 50 events: a single investigation turn writes two events per
    tool call, so a long turn lost its question entirely.
  * Tool calls and results were flattened to ``Tool call: <name>`` /
    ``Tool result`` labels, so the chat had nothing to rebuild the trace or
    the numbered Sources from, although the stored turn holds both.

The route now returns the turn's ordered ``blocks`` (text, tool_use,
tool_result with the same one-line summary the live trace shows), oldest
event first, across pages. AWS is fully faked.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

# Make `agents...` importable when run as a script (the fixture writer below);
# a fixed memory id there keeps config from looking one up in AWS at import.
_ADT_ROOT = Path(__file__).resolve().parents[2]
if __name__ == "__main__":
    if str(_ADT_ROOT) not in sys.path:
        sys.path.insert(0, str(_ADT_ROOT))
    os.environ.setdefault("MEMORY_ID", "mem-fixture-writer")

import agents.memory_client as mc  # noqa: E402
import agents.memory_routes as routes  # noqa: E402
from agents.memory_client import MemoryClient  # noqa: E402

_T0 = datetime(2026, 10, 18, 15, 0, 0, tzinfo=timezone.utc)

_ROUTE_SEED = "[ROUTE SEED: graph-first routing context for the planner]\nconcepts: ..."
_COMPOSED = (
    _ROUTE_SEED
    + "\n\n---\n\n[Temporal context: now is 2026-10-18 15:00 UTC] "
    + "Why did Line 3 stop overnight?"
)
_ANSWER = (
    "Line 3 stopped on a jam [1].\n\n"
    "---VALUE-MAPPING-PROPOSAL---\nsource: x\n---END-PROPOSAL---\n\n"
    "---FOLLOWUPS---\nQ1: What caused the jam?\nA1: Schedule maintenance\n"
)


def _cfg():
    return types.SimpleNamespace(has_memory=True, memory_id="mem-1", aws_region="us-east-1")


def _event(eid, seconds, role, content):
    """One raw ListEvents entry as the Strands session manager writes it: the
    payload text is the JSON message envelope, ``eventTimestamp`` a datetime."""
    text = content if isinstance(content, str) else json.dumps(
        {"message": {"role": role, "content": content}, "message_id": 0})
    return {
        "eventId": eid,
        "eventTimestamp": _T0 + timedelta(seconds=seconds),
        "payload": [{"conversational": {"role": role.upper(), "content": {"text": text}}}],
    }


def _turn_events():
    """One stored investigation turn, OLDEST first."""
    return [
        _event("e1", 0, "user", [{"text": _COMPOSED}]),
        _event("e2", 1, "assistant", [
            {"text": "Checking the graph first."},
            {"toolUse": {"toolUseId": "t1", "name": "explore_graph",
                         "input": {"question": "line 3 stop"}}},
        ]),
        _event("e3", 2, "user", [
            {"toolResult": {"toolUseId": "t1", "status": "success",
                            "content": [{"text": "line 3 is fed by ERP"}]}},
        ]),
        _event("e4", 3, "assistant", [
            {"toolUse": {"toolUseId": "t2", "name": "erp___query_erp",
                         "input": {"sql": "select * from downtime"}}},
        ]),
        _event("e5", 4, "user", [
            {"toolResult": {"toolUseId": "t2", "status": "success",
                            "content": [{"text": json.dumps(
                                {"columns": ["id"], "rows": [[1], [2]]})}]}},
        ]),
        _event("e6", 5, "assistant", [{"text": _ANSWER}]),
    ]


class _FakeEvents:
    """bedrock-agentcore stand-in: serves ``events`` NEWEST FIRST, as the real
    ListEvents does, in pages of the requested size chained by nextToken.
    The service applies ``maxResults`` BEFORE ``filter.eventMetadata``: a page
    is the next ``maxResults`` raw events with the non-matching ones dropped,
    so it can hold fewer than ``maxResults`` events (even none) while
    ``nextToken`` says raw events remain. This was seen live on 2026-11-25."""

    def __init__(self, events):
        self._newest_first = list(reversed(events))
        self.calls = []

    @staticmethod
    def _matches(event, expressions):
        metadata = event.get("metadata") or {}
        for expr in expressions:
            key, op = expr["left"]["metadataKey"], expr["operator"]
            if op == "EXISTS" and key not in metadata:
                return False
            if op == "NOT_EXISTS" and key in metadata:
                return False
            if op == "EQUALS_TO" and metadata.get(key) != expr["right"]["metadataValue"]:
                return False
        return True

    def list_events(self, **kwargs):
        self.calls.append(kwargs)
        expressions = (kwargs.get("filter") or {}).get("eventMetadata") or []
        start = int(kwargs.get("nextToken") or 0)
        size = kwargs["maxResults"]
        raw_page = self._newest_first[start:start + size]
        page = [e for e in raw_page if self._matches(e, expressions)]
        resp = {"events": page}
        if start + size < len(self._newest_first):
            resp["nextToken"] = str(start + size)
        return resp


def _client_for(fake):
    cfg = _cfg()
    with patch.object(mc, "get_config", return_value=cfg):
        client = MemoryClient()
    client._client = lambda: fake
    return client, cfg


def _endpoint():
    """The handler the app registers for ``GET /api/memory/events``."""
    (route,) = [r for r in routes.router.routes if r.path == "/api/memory/events"]
    return route.endpoint


def _route(events, *, integrity="report-only", query_mode="agent"):
    fake = _FakeEvents(events)
    client, cfg = _client_for(fake)

    class _Req:
        state = types.SimpleNamespace(user=types.SimpleNamespace(user_id="u1"))

    env = {"RESPONSE_INTEGRITY_MODE": integrity, "SYSTEM_QUERY_MODE": query_mode}
    with patch.object(mc, "get_config", return_value=cfg), \
            patch.object(routes, "_client", client), \
            patch.dict("os.environ", env):
        return asyncio.run(_endpoint()(_Req(), session_id="s1")), fake


def test_the_events_route_handler_only_offloads():
    # The route's whole body is the sync ``get_events``; the async handler
    # only hands it to the offload lane (agents/offload.py).
    import ast
    import inspect
    import textwrap
    node = ast.parse(textwrap.dedent(inspect.getsource(_endpoint()))).body[0]
    assert isinstance(node, ast.AsyncFunctionDef)
    assert len(node.body) == 2  # docstring + return await offload(...)
    call = node.body[1].value.value
    assert call.func.id == "offload" and call.args[0].id == "get_events"


def test_the_events_route_reads_memory_off_the_event_loop():
    # AgentCore ListEvents, the route-context reads and the kept-evidence read
    # are blocking boto3 calls: the route must not make them on the loop.
    import threading
    seen = {}

    class _Recording(_FakeEvents):
        def list_events(self, **kwargs):
            seen["read"] = threading.get_ident()
            return super().list_events(**kwargs)

    client, cfg = _client_for(_Recording(_turn_events()))

    class _Req:
        state = types.SimpleNamespace(user=types.SimpleNamespace(user_id="u1"))

    async def main():
        seen["loop"] = threading.get_ident()
        return await _endpoint()(_Req(), session_id="s1")

    with patch.object(mc, "get_config", return_value=cfg), \
            patch.object(routes, "_client", client), \
            patch.dict("os.environ", {"RESPONSE_INTEGRITY_MODE": "report-only"}):
        resp = asyncio.run(main())
    assert resp["totalCount"] > 0
    assert seen["read"] != seen["loop"]


def test_get_events_returns_the_oldest_event_first():
    fake = _FakeEvents(_turn_events())
    client, cfg = _client_for(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        events = client.get_events(session_id="s1", user_id="u1")
    assert [e.event_id for e in events] == ["e1", "e2", "e3", "e4", "e5", "e6"]
    assert events[0].role == "user"
    # The real event time travels to the UI (it was always '').
    assert events[0].timestamp.startswith("2026-10-18T15:00:00")


def test_get_events_reads_past_one_page_so_a_long_turn_keeps_its_question():
    # One user question followed by 120 tool events: more than one page.
    events = [_event("q", 0, "user", [{"text": "Why did Line 3 stop?"}])]
    for i in range(1, 121):
        events.append(_event(f"a{i}", i, "assistant", [
            {"toolUse": {"toolUseId": f"t{i}", "name": "erp___query_erp", "input": {}}}]))
    events.append(_event("ans", 200, "assistant", [{"text": "Done."}]))
    fake = _FakeEvents(events)
    client, cfg = _client_for(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        got = client.get_events(session_id="s1", user_id="u1")
    assert got[0].event_id == "q"
    assert got[-1].event_id == "ans"
    assert len(got) == 122
    assert all(c["maxResults"] <= 100 for c in fake.calls)  # the API ceiling
    assert len(fake.calls) == 2


def test_get_events_page_reads_are_bounded():
    events = [_event(f"a{i}", i, "assistant", [{"text": "x"}]) for i in range(1000)]
    fake = _FakeEvents(events)
    client, cfg = _client_for(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        got = client.get_events(session_id="s1", user_id="u1")
    assert len(got) <= 400
    assert len(fake.calls) <= 4


def test_events_route_returns_the_turn_blocks_in_order():
    resp, _ = _route(_turn_events())
    msgs = resp["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user", "assistant"]
    # The question the user typed, never the seed, and no seed in any block.
    assert msgs[0]["content"] == "Why did Line 3 stop overnight?"
    assert msgs[0]["blocks"] == []
    assert "ROUTE SEED" not in json.dumps(resp)
    assert msgs[1]["blocks"] == [
        {"type": "text", "text": "Checking the graph first."},
        {"type": "tool_use", "tool_id": "t1", "tool": "explore_graph",
         "input": {"question": "line 3 stop"}},
    ]
    result = msgs[4]["blocks"][0]
    # The result carries the tool NAME resolved from its call, and the
    # one-line summary the live trace shows, never the raw rows.
    assert result["type"] == "tool_result"
    assert result["tool_id"] == "t2"
    assert result["tool"] == "erp___query_erp"
    assert result["status"] == "success"
    from agents.tool_result_summary import summarize_tool_result
    stored = _turn_events()[4]
    stored_result = json.loads(
        stored["payload"][0]["conversational"]["content"]["text"])["message"]["content"][0]["toolResult"]
    assert result["summary"] == summarize_tool_result("erp___query_erp", stored_result)
    assert result["summary"]
    assert set(result) == {"type", "tool_id", "tool", "status", "summary"}
    assert msgs[5]["blocks"] == [{"type": "text", "text": _ANSWER}]
    # The display label stays for older clients.
    assert msgs[1]["content"].startswith("Checking the graph first.")


def test_reasoning_only_envelope_yields_no_text_block():
    reasoning = [{"reasoningContent": {"reasoningText": {"text": "thinking", "signature": "s"}}}]
    resp, _ = _route([
        _event("e1", 0, "user", [{"text": "q"}]),
        _event("e2", 1, "assistant", reasoning),
    ])
    assert resp["messages"][1]["blocks"] == []


def test_plain_text_assistant_turn_is_one_text_block():
    resp, _ = _route([
        _event("e1", 0, "user", "q"),
        _event("e2", 1, "assistant", "a plain answer"),
    ])
    assert resp["messages"][1]["blocks"] == [{"type": "text", "text": "a plain answer"}]


# ---------------------------------------------------------------------------
# The response-integrity gate. When the live chat runs it (Mode A, that is
# SYSTEM_QUERY_MODE != orchestrator, AND RESPONSE_INTEGRITY_MODE=enforce), the
# stream shows each call's tool name and a metadata-only result summary, never
# the model's narration, the call inputs or the result rows, and the answer is
# the gate's own rendering. The reload must show the same, never more.
# Markers below stand for content the gated stream never shows.
# ---------------------------------------------------------------------------
_HIDDEN = ("HIDDEN_NARRATION", "HIDDEN_INPUT", "HIDDEN_ROW", "HIDDEN_CLAIM")

_GATED_QUESTION = "How many orders shipped late last week?"
_LEGACY_QUESTION = "Which orders are still open?"
_LEGACY_ANSWER = (
    "Two orders are still open [1].\n\n"
    "---FOLLOWUPS---\nQ1: Which order is oldest?\nA1: Notify the order owner\n"
)


def _gated_turn_events(start=0, prefix="g"):
    """One turn stored while the gate ran: the model's narration, its call
    inputs, the raw rows and its structured draft are all in storage."""
    return [
        _event(f"{prefix}1", start, "user", [{"text": _GATED_QUESTION}]),
        _event(f"{prefix}2", start + 1, "assistant", [
            {"text": "HIDDEN_NARRATION checking the graph first"},
            {"toolUse": {"toolUseId": "tooluse_g1", "name": "explore_graph",
                         "input": {"question": "HIDDEN_INPUT late orders"}}},
        ]),
        _event(f"{prefix}3", start + 2, "user", [
            {"toolResult": {"toolUseId": "tooluse_g1", "status": "success",
                            "content": [{"text": "HIDDEN_ROW order links"}]}},
        ]),
        _event(f"{prefix}4", start + 3, "assistant", [
            {"toolUse": {"toolUseId": "tooluse_g2", "name": "erp___query_erp",
                         "input": {"sql": "HIDDEN_INPUT select count(*)"}}},
        ]),
        _event(f"{prefix}5", start + 4, "user", [
            {"toolResult": {"toolUseId": "tooluse_g2", "status": "success",
                            "content": [{"text": json.dumps(
                                {"columns": ["late"], "rows": [["HIDDEN_ROW 7"]]})}]}},
        ]),
        _event(f"{prefix}6", start + 5, "assistant", [
            {"toolUse": {"toolUseId": "tooluse_g3", "name": "FinalAnswerDraft",
                         "input": {"claims": [{"text": "HIDDEN_CLAIM seven"}]}}},
        ]),
        _event(f"{prefix}7", start + 6, "user", [
            {"toolResult": {"toolUseId": "tooluse_g3", "status": "success",
                            "content": [{"text": "HIDDEN_CLAIM accepted"}]}},
        ]),
        _event(f"{prefix}8", start + 7, "assistant", [{"text": "HIDDEN_NARRATION done"}]),
    ]


def _legacy_turn_events(start=0, prefix="l"):
    """One turn stored on the legacy answer path: narration, inputs and the
    model's answer were all shown live."""
    return [
        _event(f"{prefix}1", start, "user", [{"text": _LEGACY_QUESTION}]),
        _event(f"{prefix}2", start + 1, "assistant", [
            {"text": "Checking the order history first."},
            {"toolUse": {"toolUseId": "tooluse_l1", "name": "explore_graph",
                         "input": {"question": "open orders"}}},
        ]),
        _event(f"{prefix}3", start + 2, "user", [
            {"toolResult": {"toolUseId": "tooluse_l1", "status": "success",
                            "content": [{"text": "orders are kept in ERP"}]}},
        ]),
        _event(f"{prefix}4", start + 3, "assistant", [
            {"toolUse": {"toolUseId": "tooluse_l2", "name": "erp___query_erp",
                         "input": {"sql": "select id from orders where open"}}},
        ]),
        _event(f"{prefix}5", start + 4, "user", [
            {"toolResult": {"toolUseId": "tooluse_l2", "status": "success",
                            "content": [{"text": json.dumps(
                                {"columns": ["id"], "rows": [[1], [2]]})}]}},
        ]),
        _event(f"{prefix}6", start + 5, "assistant", [{"text": _LEGACY_ANSWER}]),
    ]


def _stored_result(events, tool_id):
    for e in events:
        text = e["payload"][0]["conversational"]["content"]["text"]
        for block in json.loads(text)["message"]["content"]:
            result = block.get("toolResult")
            if isinstance(result, dict) and result.get("toolUseId") == tool_id:
                return result
    raise AssertionError(tool_id)


def _live_public_summary(tool, result):
    """The gated live stream's tool_result summary: the runtime's public
    metadata event, serialized as the chat client does."""
    from agents.runtime_integrity import public_tool_result_event
    from agents.tool_result_capture import safe_tool_result_metadata
    event = public_tool_result_event(
        tool_use_id="x", tool_name=tool,
        metadata=safe_tool_result_metadata(result, tool_name=tool, max_chars=1024))
    return json.dumps(event["tool_result_metadata"]["metadata"], separators=(",", ":"))


def _blocks(resp):
    return [b for m in resp["messages"] for b in m.get("blocks") or []]


def test_gated_reload_shows_what_the_gated_stream_showed_and_nothing_more():
    events = _gated_turn_events()
    resp, _ = _route(events, integrity="enforce", query_mode="agent")
    # Never any hidden content: not in blocks, not in the display labels.
    dumped = json.dumps(resp)
    for marker in _HIDDEN:
        assert marker not in dumped
    msgs = resp["messages"]
    assert msgs[0]["content"] == _GATED_QUESTION
    # The same calls the stream announced: tool name and id, no input, and
    # never the gate's own structured-answer call.
    assert [b for b in _blocks(resp) if b["type"] == "tool_use"] == [
        {"type": "tool_use", "tool_id": "tooluse_g1", "tool": "explore_graph"},
        {"type": "tool_use", "tool_id": "tooluse_g2", "tool": "erp___query_erp"},
    ]
    results = [b for b in _blocks(resp) if b["type"] == "tool_result"]
    assert [(b["tool_id"], b["tool"]) for b in results] == [
        ("tooluse_g1", "explore_graph"), ("tooluse_g2", "erp___query_erp")]
    for b in results:
        assert b["summary"] == _live_public_summary(b["tool"], _stored_result(events, b["tool_id"]))
    assert not [b for b in _blocks(resp) if b["type"] == "text"]


def test_orchestrator_mode_with_enforce_set_reloads_the_legacy_blocks():
    # Mode B never runs the gate live (narration and inputs stream as usual),
    # so its reload keeps them, with the same blocks as the default mode.
    resp, _ = _route(_legacy_turn_events(), integrity="enforce", query_mode="orchestrator")
    plain, _ = _route(_legacy_turn_events(), integrity="report-only", query_mode="agent")
    assert resp == plain
    blocks = _blocks(resp)
    assert {"type": "text", "text": "Checking the order history first."} in blocks
    assert {"type": "tool_use", "tool_id": "tooluse_l2", "tool": "erp___query_erp",
            "input": {"sql": "select id from orders where open"}} in blocks
    assert {"type": "text", "text": _LEGACY_ANSWER} in blocks


def test_a_turn_stored_under_the_gate_stays_gated_after_the_gate_is_off():
    # The gate's structured-answer call in storage marks the turn: switching
    # the deployment back to report-only must not expose what it hid.
    events = _gated_turn_events(0) + _legacy_turn_events(100)
    resp, _ = _route(events, integrity="report-only", query_mode="agent")
    dumped = json.dumps(resp)
    for marker in _HIDDEN:
        assert marker not in dumped
    # The later legacy turn is untouched.
    assert {"type": "text", "text": "Checking the order history first."} in _blocks(resp)


def test_reload_and_live_stream_use_one_gate_decision():
    from agents.chat_client import _cloud_mode_a, _local_integrity_mode
    for query_mode in ("agent", "orchestrator"):
        for integrity in ("report-only", "annotate", "enforce"):
            env = {"RESPONSE_INTEGRITY_MODE": integrity, "SYSTEM_QUERY_MODE": query_mode}
            with patch.dict("os.environ", env):
                live = _cloud_mode_a()
                assert _local_integrity_mode() == live
            resp, _ = _route(_legacy_turn_events(), integrity=integrity, query_mode=query_mode)
            gated = not [b for b in _blocks(resp) if b["type"] == "text"]
            assert gated == live, (query_mode, integrity)


def test_structured_answer_tool_name_matches_the_strands_tool():
    from strands.tools.structured_output.structured_output_tool import StructuredOutputTool

    from agents.memory_turns import STRUCTURED_ANSWER_TOOL
    from agents.response_integrity_contracts import FinalAnswerDraft
    assert STRUCTURED_ANSWER_TOOL == StructuredOutputTool(FinalAnswerDraft).tool_name


# ---------------------------------------------------------------------------
# Shared fixture: the route's output for the gated, the Mode B and the
# switched-off cases, read by the chat's reload test
# (frontend/src/test/session-reload-integrity.test.tsx) so the two halves
# test one payload. Regenerate after a deliberate route change with
#   python tests/unit/test_memory_events_rehydrate.py --write-fixture
# ---------------------------------------------------------------------------
_FIXTURE = _ADT_ROOT / "frontend" / "src" / "test" / "fixtures" / "session-reload-events.json"
_FIXTURE_ABOUT = ("GET /api/memory/events output for three stored sessions; written by "
                  "tests/unit/test_memory_events_rehydrate.py, read by "
                  "src/test/session-reload-integrity.test.tsx.")


def _fixture_payloads():
    cases = {
        "mode_a_enforce": (_gated_turn_events(), "enforce", "agent"),
        "orchestrator_enforce": (_legacy_turn_events(), "enforce", "orchestrator"),
        "gate_turned_off": (_gated_turn_events(0) + _legacy_turn_events(100), "report-only", "agent"),
    }
    out = {}
    for name, (events, integrity, query_mode) in cases.items():
        resp, _ = _route(events, integrity=integrity, query_mode=query_mode)
        out[name] = {"RESPONSE_INTEGRITY_MODE": integrity, "SYSTEM_QUERY_MODE": query_mode,
                     "payload": resp}
    return {"about": _FIXTURE_ABOUT, "scenarios": out}


def test_shared_fixture_is_the_route_output():
    assert json.loads(_FIXTURE.read_text()) == _fixture_payloads()


if __name__ == "__main__" and "--write-fixture" in sys.argv:
    _FIXTURE.write_text(json.dumps(_fixture_payloads(), indent=2) + "\n")


def _state_event(eid, seconds, state_type, blob):
    """A session/agent STATE event as bedrock-agentcore >= 1.3 writes it: in
    the user's own session stream, a blob payload, tagged with the reserved
    ``stateType`` metadata key (the SDK's own readers filter on it)."""
    return {
        "eventId": eid,
        "eventTimestamp": _T0 + timedelta(seconds=seconds),
        "payload": [{"blob": json.dumps(blob)}],
        "metadata": {"stateType": {"stringValue": state_type}},
    }


def _turn_events_with_sdk_state():
    """The same turn with the SDK's session and agent state events between
    the messages, as one turn stores them under bedrock-agentcore 1.18."""
    turn = _turn_events()
    session = _state_event("s-session", -1, "SESSION",
                           {"session_id": "s1", "session_type": "AGENT"})
    agent = {"agent_id": "default", "state": {}, "conversation_manager_state": {}}
    return [session, turn[0], _state_event("s-agent-1", 0.5, "AGENT", agent),
            *turn[1:], _state_event("s-agent-2", 6, "AGENT", agent)]


def test_sdk_state_events_never_reach_the_reload_or_the_label():
    # They are SDK bookkeeping, not turns: read as turns they parse as blank
    # events with no role, which the reload and the label have no use for.
    fake = _FakeEvents(_turn_events_with_sdk_state())
    client, cfg = _client_for(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        events = client.get_events(session_id="s1", user_id="u1")
        label = client._label_for(fake, "s1", "u1")
        plain_label = client._label_for(_FakeEvents(_turn_events()), "s1", "u1")
    assert [e.event_id for e in events] == ["e1", "e2", "e3", "e4", "e5", "e6"]
    assert label == plain_label
    with_state, _ = _route(_turn_events_with_sdk_state())
    without, _ = _route(_turn_events())
    assert with_state == without


def _chat_with_state(turns: int):
    """``turns`` question/answer pairs with an AGENT state event after every
    message and a SESSION state event first, as 1.18.1 stores a chat.
    Returns (all raw events, the conversation events only), OLDEST first."""
    agent = {"agent_id": "default", "state": {}, "conversation_manager_state": {}}
    raw = [_state_event("s-session", -1, "SESSION", {"session_id": "s1"})]
    conversation = []
    for k in range(turns):
        q = _event(f"q{k}", 4 * k, "user", [{"text": f"Question {k + 1}?"}])
        a = _event(f"a{k}", 4 * k + 2, "assistant", [{"text": f"Answer {k + 1}."}])
        conversation += [q, a]
        raw += [q, _state_event(f"sq{k}", 4 * k + 1, "AGENT", agent),
                a, _state_event(f"sa{k}", 4 * k + 3, "AGENT", agent)]
    return raw, conversation


def test_state_events_do_not_use_up_the_label_scan():
    # 4 turns: 8 conversation events and 9 state events, 17 raw. The newest
    # raw page the old single-call scan read held only the last turns, so
    # the label must come from paging back to the first question.
    raw, conversation = _chat_with_state(4)
    assert len(raw) > len(conversation)
    client, cfg = _client_for(_FakeEvents(raw))
    with patch.object(mc, "get_config", return_value=cfg):
        label = client._label_for(_FakeEvents(raw), "s1", "u1")
        plain = client._label_for(_FakeEvents(conversation), "s1", "u1")
    assert label == plain
    assert "Question 1" in label


def test_the_label_is_the_first_question_of_a_chat_with_many_state_events():
    # 4 turns with a burst of agent state events between them, newest first
    # in the store: a 10-event raw page holds no first question.
    agent = {"agent_id": "default", "state": {}, "conversation_manager_state": {}}
    raw = [_state_event("s-session", -1, "SESSION", {"session_id": "s1"})]
    for k in range(4):
        raw.append(_event(f"q{k}", 40 * k, "user", [{"text": f"Question {k + 1}?"}]))
        raw += [_state_event(f"st{k}-{j}", 40 * k + 1 + j, "AGENT", agent) for j in range(12)]
        raw.append(_event(f"a{k}", 40 * k + 20, "assistant", [{"text": f"Answer {k + 1}."}]))
    client, cfg = _client_for(_FakeEvents(raw))
    with patch.object(mc, "get_config", return_value=cfg):
        label = client._label_for(_FakeEvents(raw), "s1", "u1")
    assert label is not None and "Question 1" in label
    assert "Question 4" not in label


def test_the_label_finds_a_first_question_several_pages_back():
    # 90 turns, 361 raw events: the first question is in the fourth raw page
    # (pages of 100, newest first).
    raw, _ = _chat_with_state(90)
    assert len(raw) > 3 * mc._EVENTS_PAGE_SIZE
    fake = _FakeEvents(raw)
    client, cfg = _client_for(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        label = client._label_for(fake, "s1", "u1")
    assert label is not None and label.startswith("Question 1?")
    assert len(fake.calls) > 1
    assert all(c["maxResults"] <= mc._EVENTS_PAGE_SIZE for c in fake.calls)


def test_a_label_read_failure_is_fail_open():
    class _Broken:
        def list_events(self, **kwargs):
            raise RuntimeError("throttled")

    client, cfg = _client_for(_Broken())
    with patch.object(mc, "get_config", return_value=cfg):
        assert client._label_for(_Broken(), "s1", "u1") is None


def test_state_events_do_not_use_up_the_reload_pages():
    # 90 turns: 180 conversation events and 181 state events, 361 raw. The
    # page bound counts RAW events (the service applies maxResults before the
    # stateType filter), so within it the reload returns every conversation
    # event and does not report the session as cut.
    raw, conversation = _chat_with_state(90)
    assert len(conversation) < len(raw) <= mc._EVENTS_MAX_TOTAL
    fake = _FakeEvents(raw)
    client, cfg = _client_for(fake)
    with patch.object(mc, "get_config", return_value=cfg):
        events, cut = client.read_session_events("s1", "u1")
    assert [e.event_id for e in events] == [e["eventId"] for e in conversation]
    assert cut is False
