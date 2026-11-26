# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the memory read path (``agents.memory_client``), AWS fully
mocked — the bedrock-agentcore boto3 client is patched, so no AWS is touched.

Covers the de-silenced read path (a memory FAILURE must raise
``MemoryUnavailableError``, never masquerade as 'no data'), plus the parsing
helpers that turn the raw AgentCore payloads into display-ready records.

Runnable two ways:
  * pytest:      pytest agentic_digital_thread/tests/test_memory_client.py
  * standalone:  python agentic_digital_thread/tests/test_memory_client.py
"""
import sys
import types
import json
from pathlib import Path
from unittest.mock import patch

import pytest

# Make `agents...` importable (agentic_digital_thread/ is one level up from tests/).
_ADT_ROOT = Path(__file__).resolve().parents[1]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents import memory_client as mc  # noqa: E402
from agents.memory_client import MemoryClient, MemoryUnavailableError  # noqa: E402


def _fake_config(*, has_memory: bool = True, memory_id: str = "mem-123",
                 region: str = "us-east-1"):
    """A stand-in for AppConfig with just the attributes memory_client reads."""
    return types.SimpleNamespace(
        has_memory=has_memory, memory_id=memory_id, aws_region=region,
    )


class _FakeAgentCore:
    """Records calls and returns canned responses (or raises)."""
    def __init__(self, *, events=None, records=None, sessions=None, raises=False):
        self._events = events or []
        self._records = records or []
        self._sessions = sessions or []
        self._raises = raises
        self.list_events_kwargs = None
        self.list_memory_records_kwargs = None
        self.list_sessions_kwargs = None

    def list_events(self, **kwargs):
        self.list_events_kwargs = kwargs
        if self._raises:
            raise RuntimeError("AccessDeniedException: not authorized")
        return {"events": self._events}

    def list_memory_records(self, **kwargs):
        self.list_memory_records_kwargs = kwargs
        if self._raises:
            raise RuntimeError("AccessDeniedException: not authorized")
        return {"memoryRecordSummaries": self._records}

    def list_sessions(self, **kwargs):
        self.list_sessions_kwargs = kwargs
        if self._raises:
            raise RuntimeError("AccessDeniedException: not authorized")
        # Mirror the real API's filter={"eventFilter": "HAS_EVENTS"} contract:
        # a session with no events for this fake (self._events is empty)
        # is excluded server-side, exactly as production code now expects.
        if kwargs.get("filter") == {"eventFilter": "HAS_EVENTS"} and not self._events:
            return {"sessionSummaries": []}
        return {"sessionSummaries": self._sessions}


def _make_client(cfg, fake):
    """Build a MemoryClient with config + the bedrock-agentcore client patched."""
    with patch.object(mc, "get_config", return_value=cfg):
        client = MemoryClient()
    # _client() builds the boto3 client; pin it to our fake.
    client._client = lambda: fake
    return client


# ---------------------------------------------------------------------------
# (a) has_memory False -> get_events/get_semantic return [] (NOT an error)
# ---------------------------------------------------------------------------
def test_get_events_returns_empty_when_memory_unconfigured():
    cfg = _fake_config(has_memory=False)
    fake = _FakeAgentCore()
    client = _make_client(cfg, fake)
    with patch.object(mc, "get_config", return_value=cfg):
        assert client.get_events(session_id="s1", user_id="u1") == []
    # The control plane is never even called.
    assert fake.list_events_kwargs is None


def test_get_semantic_returns_empty_when_memory_unconfigured():
    cfg = _fake_config(has_memory=False)
    fake = _FakeAgentCore()
    client = _make_client(cfg, fake)
    with patch.object(mc, "get_config", return_value=cfg):
        assert client.get_semantic(session_id="s1", user_id="u1") == []
    assert fake.list_memory_records_kwargs is None


# ---------------------------------------------------------------------------
# (b) client raising -> get_events/get_semantic RAISE MemoryUnavailableError
# ---------------------------------------------------------------------------
def test_get_events_raises_on_client_error():
    cfg = _fake_config(has_memory=True)
    fake = _FakeAgentCore(raises=True)
    client = _make_client(cfg, fake)
    with patch.object(mc, "get_config", return_value=cfg):
        with pytest.raises(MemoryUnavailableError):
            client.get_events(session_id="s1", user_id="u1")


def test_get_semantic_raises_on_client_error():
    cfg = _fake_config(has_memory=True)
    fake = _FakeAgentCore(raises=True)
    client = _make_client(cfg, fake)
    with patch.object(mc, "get_config", return_value=cfg):
        with pytest.raises(MemoryUnavailableError):
            client.get_semantic(session_id="s1", user_id="u1")


# ---------------------------------------------------------------------------
# (c) _extract_event_text unwraps {"message": {...}} envelopes; passes plain
#     text through unchanged.
# ---------------------------------------------------------------------------
def test_extract_event_text_unwraps_message_envelope():
    raw = (
        '{"message": {"role": "assistant", '
        '"content": [{"text": "Hello "}, {"text": "world"}]}}'
    )
    role, text = mc._extract_event_text(raw)
    assert role == "assistant"
    assert text == "Hello world"


def test_extract_event_text_passes_plain_text_through():
    role, text = mc._extract_event_text("just a plain turn")
    assert role is None
    assert text == "just a plain turn"


def test_extract_event_text_json_without_text_keeps_original():
    raw = '{"message": {"role": "user", "content": []}}'
    role, text = mc._extract_event_text(raw)
    # No extractable text -> original string returned, role None.
    assert role is None
    assert text == raw


# ---------------------------------------------------------------------------
# (d) get_events parses payload-as-list correctly and lowercases role.
# ---------------------------------------------------------------------------
def test_get_events_parses_payload_list_and_lowercases_role():
    events = [
        {
            "eventId": "e1",
            "createdAt": "2026-06-18T00:00:00Z",
            "payload": [
                {"conversational": {"role": "USER", "content": {"text": "hi there"}}}
            ],
        },
        {
            "eventId": "e2",
            "createdAt": "2026-06-18T00:00:01Z",
            "payload": [
                {"conversational": {"role": "ASSISTANT", "content": {"text": "hello"}}}
            ],
        },
    ]
    cfg = _fake_config(has_memory=True)
    fake = _FakeAgentCore(events=events)
    client = _make_client(cfg, fake)
    with patch.object(mc, "get_config", return_value=cfg):
        result = client.get_events(session_id="s1", user_id="u1")
    assert [e.role for e in result] == ["user", "assistant"]
    assert [e.content for e in result] == ["hi there", "hello"]
    assert [e.event_id for e in result] == ["e1", "e2"]
    # Correct identifiers forwarded to the API.
    assert fake.list_events_kwargs["memoryId"] == "mem-123"
    assert fake.list_events_kwargs["actorId"] == "u1"
    assert fake.list_events_kwargs["sessionId"] == "s1"


# ---------------------------------------------------------------------------
# (e) Parsers: summaries (global + topics, nested subtopic/content/timestamp,
#     no-markup fallback), preferences (structured + categories), facts.
# ---------------------------------------------------------------------------
def test_parse_summary_global_and_topics():
    raw = (
        "<global_summary>User asked about Line 3 OEE and yield.</global_summary>"
        "<delta_detailed_summary>"
        '<topic name="Production">Line 3 ran at 92% OEE last shift.</topic>'
        '<topic name="Yield">Milwaukee yield held at 94%.</topic>'
        "</delta_detailed_summary>"
    )
    out = mc._parse_summary_record(raw)
    assert out["overview"] == "User asked about Line 3 OEE and yield."
    assert [t["name"] for t in out["topics"]] == ["Production", "Yield"]
    assert out["topics"][0]["body"] == "Line 3 ran at 92% OEE last shift."
    assert out["topics"][1]["body"] == "Milwaukee yield held at 94%."
    # No raw tags leak into any rendered field.
    assert "<" not in out["overview"]
    assert all("<" not in t["body"] and "<" not in t["name"] for t in out["topics"])


def test_parse_summary_nested_subtopic_content_timestamp():
    raw = (
        '<topic name="Maintenance">'
        '<subtopic name="Open Work Orders">'
        "<content>Two work orders are open.</content>"
        "<item>Replace bearing on pump 4</item>"
        "<item>Inspect conveyor belt</item>"
        "<timestamp>2026-07-08T10:00:00Z</timestamp>"
        "<WO_number>WO-4821</WO_number>"
        "</subtopic>"
        "</topic>"
    )
    out = mc._parse_summary_record(raw)
    assert len(out["topics"]) == 1
    body = out["topics"][0]["body"]
    # Subtopic name preserved as a sub-heading.
    assert "Open Work Orders:" in body
    # Content flattened, items bulleted, leaf tag labeled, tags all stripped.
    assert "Two work orders are open." in body
    assert "• Replace bearing on pump 4" in body
    assert "• Inspect conveyor belt" in body
    assert "WO number: WO-4821" in body
    assert "2026-07-08T10:00:00Z" in body
    assert "<" not in body and ">" not in body


def test_parse_summary_no_markup_fallback():
    raw = "Just a plain summary with no topics or global markup."
    out = mc._parse_summary_record(raw)
    assert out["overview"] == ""
    assert len(out["topics"]) == 1
    assert out["topics"][0]["name"] == ""
    assert out["topics"][0]["body"] == "Just a plain summary with no topics or global markup."


def test_parse_summary_malformed_never_raises():
    # Unclosed topic tag -> no clean match; falls back to stripped text.
    raw = '<topic name="Broken">dangling body without a close'
    out = mc._parse_summary_record(raw)
    assert out["topics"]  # something renders
    assert all("<" not in t["body"] for t in out["topics"])


def test_parse_summary_spanish_renders_cleanly():
    # The built-in strategy summarizes in the source language; old records may
    # be Spanish. The parser must render whatever language cleanly.
    raw = (
        "<global_summary>El usuario preguntó sobre la producción.</global_summary>"
        '<topic name="Producción">La línea 3 funcionó al 92% de OEE.</topic>'
    )
    out = mc._parse_summary_record(raw)
    assert out["overview"] == "El usuario preguntó sobre la producción."
    assert out["topics"][0]["name"] == "Producción"
    assert out["topics"][0]["body"] == "La línea 3 funcionó al 92% de OEE."


def test_parse_preference_structured_with_categories():
    raw = (
        '{"context": "The user explicitly stated a preference.", '
        '"preference": "Prefers metric units for measurements", '
        '"categories": ["work", "units"]}'
    )
    out = mc._parse_preference_record(raw)
    assert out["preference"] == "Prefers metric units for measurements"
    assert out["context"] == "The user explicitly stated a preference."
    assert out["categories"] == ["work", "units"]


def test_parse_preference_without_context_or_categories():
    out = mc._parse_preference_record('{"preference": "Likes concise answers"}')
    assert out["preference"] == "Likes concise answers"
    assert out["context"] == ""
    assert out["categories"] == []


def test_parse_preference_non_json_fallback():
    out = mc._parse_preference_record("just a plain preference line")
    assert out["preference"] == "just a plain preference line"
    assert out["categories"] == []


def test_parse_fact_plain_text_unchanged():
    assert mc._parse_fact_text("just a fact") == "just a fact"


def test_parse_fact_strips_stray_tags():
    assert mc._parse_fact_text("<topic>Yield</topic> held at 94%.") == "Yield held at 94%."


def test_parse_fact_unwraps_text_envelope():
    assert mc._parse_fact_text('{"text": "Milwaukee runs 3 shifts."}') == "Milwaukee runs 3 shifts."


# ---------------------------------------------------------------------------
# (f) get_semantic builds the EXACT namespaces (leading + trailing slash).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "memory_type, expected",
    [
        ("facts", "/facts/u1/"),
        ("summaries", "/summaries/u1/s1/"),
        ("preferences", "/preferences/u1/"),
    ],
)
def test_get_semantic_builds_exact_namespace(memory_type, expected):
    cfg = _fake_config(has_memory=True)
    fake = _FakeAgentCore(records=[])
    client = _make_client(cfg, fake)
    with patch.object(mc, "get_config", return_value=cfg):
        client.get_semantic(session_id="s1", user_id="u1", memory_type=memory_type)
    assert fake.list_memory_records_kwargs["namespace"] == expected
    assert fake.list_memory_records_kwargs["memoryId"] == "mem-123"


def test_get_semantic_humanizes_records():
    records = [
        {
            "memoryRecordId": "r1",
            "createdAt": "2026-06-18T00:00:00Z",
            "content": {"text": '{"preference": "Prefers dark mode", "categories": ["ui"]}'},
        },
    ]
    cfg = _fake_config(has_memory=True)
    fake = _FakeAgentCore(records=records)
    client = _make_client(cfg, fake)
    with patch.object(mc, "get_config", return_value=cfg):
        facts = client.get_semantic(session_id="s1", user_id="u1", memory_type="preferences")
    assert len(facts) == 1
    assert facts[0].content == "Prefers dark mode"
    assert facts[0].fact_id == "r1"
    # Preferences expose structured categories so the UI can chip them.
    assert facts[0].structured == {
        "preference": "Prefers dark mode", "context": "", "categories": ["ui"],
    }


def test_get_semantic_summaries_returns_structured():
    records = [
        {
            "memoryRecordId": "sum1",
            "createdAt": "2026-06-18T00:00:00Z",
            "content": {"text": (
                "<global_summary>Discussed OEE.</global_summary>"
                '<topic name="Production">Line 3 at 92%.</topic>'
            )},
        },
    ]
    cfg = _fake_config(has_memory=True)
    fake = _FakeAgentCore(records=records)
    client = _make_client(cfg, fake)
    with patch.object(mc, "get_config", return_value=cfg):
        facts = client.get_semantic(session_id="s1", user_id="u1", memory_type="summaries")
    assert facts[0].structured["overview"] == "Discussed OEE."
    assert facts[0].structured["topics"][0]["name"] == "Production"
    # content is a plain-text fallback (the overview) for non-structured clients.
    assert facts[0].content == "Discussed OEE."
    # No raw tags in any surfaced field.
    assert "<" not in facts[0].content


def test_get_semantic_facts_are_clean_strings():
    records = [
        {
            "memoryRecordId": "f1",
            "createdAt": "2026-06-18T00:00:00Z",
            "content": {"text": "<topic>Yield</topic> held at 94%."},
        },
    ]
    cfg = _fake_config(has_memory=True)
    fake = _FakeAgentCore(records=records)
    client = _make_client(cfg, fake)
    with patch.object(mc, "get_config", return_value=cfg):
        facts = client.get_semantic(session_id="s1", user_id="u1", memory_type="facts")
    assert facts[0].content == "Yield held at 94%."
    assert facts[0].structured is None


# ---------------------------------------------------------------------------
# (g) tool turns render as concise labels, not raw envelope JSON.
# ---------------------------------------------------------------------------
def test_extract_event_text_labels_tool_use():
    raw = (
        '{"message": {"role": "assistant", '
        '"content": [{"toolUse": {"name": "query_erp", "input": {}}}]}}'
    )
    role, text = mc._extract_event_text(raw)
    assert role == "assistant"
    assert text == "Tool call: query_erp"


def test_extract_event_text_labels_tool_result():
    raw = (
        '{"message": {"role": "user", '
        '"content": [{"toolResult": {"toolUseId": "t1", '
        '"content": [{"text": "big raw payload"}]}}]}}'
    )
    role, text = mc._extract_event_text(raw)
    assert role == "user"
    assert text == "Tool result"


def test_extract_event_text_mixed_text_and_tool():
    raw = (
        '{"message": {"role": "assistant", '
        '"content": [{"text": "Let me check."}, {"toolUse": {"name": "query_x"}}]}}'
    )
    role, text = mc._extract_event_text(raw)
    assert role == "assistant"
    # Prose kept verbatim, tool label on its own line.
    assert text == "Let me check.\nTool call: query_x"


def test_extract_event_text_tool_use_without_name():
    raw = '{"message": {"role": "assistant", "content": [{"toolUse": {"input": {}}}]}}'
    role, text = mc._extract_event_text(raw)
    assert text == "Tool call"


def test_get_events_labels_tool_turns_end_to_end():
    envelope = json.dumps({
        "message": {"role": "assistant", "content": [{"toolUse": {"name": "query_cmms"}}]}
    })
    events = [
        {
            "eventId": "e1",
            "createdAt": "2026-06-18T00:00:00Z",
            "payload": [{"conversational": {"role": "ASSISTANT", "content": {"text": envelope}}}],
        },
    ]
    cfg = _fake_config(has_memory=True)
    fake = _FakeAgentCore(events=events)
    client = _make_client(cfg, fake)
    with patch.object(mc, "get_config", return_value=cfg):
        result = client.get_events(session_id="s1", user_id="u1")
    assert result[0].role == "assistant"
    assert result[0].content == "Tool call: query_cmms"
    # Never leaks the raw envelope JSON to the UI.
    assert "toolUse" not in result[0].content


# ---------------------------------------------------------------------------
# (h) list_sessions: native AgentCore enumeration, newest-first, best-effort
#     first-user-question labels, id fallback, and error/unconfigured contracts.
# ---------------------------------------------------------------------------
def _label_events(question, ts="2026-06-01T00:00:00Z"):
    return [
        {
            "eventId": "ev",
            "createdAt": ts,
            "payload": [{"conversational": {"role": "USER", "content": {"text": question}}}],
        },
    ]


def test_list_sessions_returns_empty_when_memory_unconfigured():
    cfg = _fake_config(has_memory=False)
    fake = _FakeAgentCore()
    client = _make_client(cfg, fake)
    with patch.object(mc, "get_config", return_value=cfg):
        assert client.list_sessions(user_id="u1") == []
    assert fake.list_sessions_kwargs is None


def test_list_sessions_raises_on_client_error():
    cfg = _fake_config(has_memory=True)
    fake = _FakeAgentCore(raises=True)
    client = _make_client(cfg, fake)
    with patch.object(mc, "get_config", return_value=cfg):
        with pytest.raises(MemoryUnavailableError):
            client.list_sessions(user_id="u1")


def test_list_sessions_sorted_newest_first_with_labels():
    sessions = [
        {"sessionId": "s-old", "createdAt": "2026-06-01T00:00:00Z", "actorId": "u1"},
        {"sessionId": "s-new", "createdAt": "2026-06-09T00:00:00Z", "actorId": "u1"},
    ]
    fake = _FakeAgentCore(events=_label_events("What is the line OEE?"), sessions=sessions)
    cfg = _fake_config(has_memory=True)
    client = _make_client(cfg, fake)
    with patch.object(mc, "get_config", return_value=cfg):
        result = client.list_sessions(user_id="u1")
    assert [s.session_id for s in result] == ["s-new", "s-old"]
    assert all(s.label == "What is the line OEE?" for s in result)
    # Correct identifiers forwarded.
    assert fake.list_sessions_kwargs["memoryId"] == "mem-123"
    assert fake.list_sessions_kwargs["actorId"] == "u1"


def test_list_sessions_label_truncates_long_question():
    long_q = "A" * 200
    sessions = [{"sessionId": "s1", "createdAt": "2026-06-01T00:00:00Z", "actorId": "u1"}]
    fake = _FakeAgentCore(events=_label_events(long_q), sessions=sessions)
    cfg = _fake_config(has_memory=True)
    client = _make_client(cfg, fake)
    with patch.object(mc, "get_config", return_value=cfg):
        result = client.list_sessions(user_id="u1")
    assert result[0].label.endswith("…")
    assert len(result[0].label) == 81  # 80 chars + ellipsis


def test_list_sessions_drops_zero_event_sessions():
    # No events -> the session's events aged out (7-day TTL); the server-side
    # HAS_EVENTS filter excludes it before it ever becomes a candidate, so it
    # is NOT offered in the picker.
    sessions = [{"sessionId": "s-unlabeled", "createdAt": "2026-06-01T00:00:00Z", "actorId": "u1"}]
    fake = _FakeAgentCore(events=[], sessions=sessions)
    cfg = _fake_config(has_memory=True)
    client = _make_client(cfg, fake)
    with patch.object(mc, "get_config", return_value=cfg):
        result = client.list_sessions(user_id="u1")
    assert result == []


def test_list_sessions_label_ignores_tool_turns():
    # A session whose only user-side turn is a tool result should not be
    # labeled with the tool label — it falls back to a readable date (never
    # the raw session id, which is meaningless to a human).
    envelope = json.dumps({
        "message": {"role": "user", "content": [{"toolResult": {"toolUseId": "t1"}}]}
    })
    events = [
        {
            "eventId": "e1",
            "createdAt": "2026-06-01T00:00:00Z",
            "payload": [{"conversational": {"role": "USER", "content": {"text": envelope}}}],
        },
    ]
    sessions = [{"sessionId": "s1", "createdAt": "2026-06-01T00:00:00Z", "actorId": "u1"}]
    fake = _FakeAgentCore(events=events, sessions=sessions)
    cfg = _fake_config(has_memory=True)
    client = _make_client(cfg, fake)
    with patch.object(mc, "get_config", return_value=cfg):
        result = client.list_sessions(user_id="u1")
    assert result[0].label == "Chat from Jun 01, 2026 00:00"
    assert "Tool result" not in result[0].label
    # The session id still travels in the payload.
    assert result[0].session_id == "s1"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
