# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A tool's charts come back when a chat is reopened from Recent sessions.

Live, a tool chart reaches the page only as a stream event: ChartRedactionHook
takes the ``<chart-block .../>`` tags out of the model-visible tool result and
queues them, and the runtime yields them as answer text right after the event
that queued them. No message the model writes holds them. The session manager
stores the tool-result message before the hook edits it, so the stored copy
still holds the tags, and the reload rebuilds the charts from there:

* the hook and the reload read a tool result's tags with one reader
  (``orchestrator.chart_tags_in``), for the string, text-part and JSON shapes;
* a stored tag is replayed only when the turn's stream sent it to the
  browser: the app keeps the fingerprints of the chart tags each turn's
  stream sent (``turn_evidence_store``), and ``memory_turns.with_reload_charts``
  gives an ungated tool result the ``charts`` among those, in order and
  deduped across the turn by the key delivery dedups on. A turn with no
  record, a gated turn, and a turn whose charts the byte ceiling held back
  are marked so the chat says its charts are not all shown;
* the byte ceiling is spent newest turn first;
* the model's restored history (``_wrap_list_messages``) holds a neutral note
  in place of an earlier turn's tags, never the tags and never a claim that
  they are on screen.

The first test is a GUARD (it also passes on the code before this change): it
pins the hook order the reload relies on, through a real Strands Agent.
All payloads are synthetic. The file runs offline: every AWS API call is
refused before it leaves the process (at import, where the branding module's
SSM lookup falls back to its defaults, and in every test, which must make
none), and the route module's import-time memory client gets a fixed config,
so no memory lookup is attempted.
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

import botocore.client
import pytest


def _refusing(record: list):
    def _refuse(self, operation_name, kwargs):
        record.append(operation_name)
        raise RuntimeError(f"offline unit test: {operation_name} refused")
    return _refuse


_IMPORT_CALLS: list = []
_OFFLINE_MEMORY = SimpleNamespace(has_memory=True, memory_id="mem-offline",
                                  aws_region="us-east-1")
with patch.object(botocore.client.BaseClient, "_make_api_call", _refusing(_IMPORT_CALLS)):
    import agents.memory_client as mc
    with patch.object(mc, "get_config", return_value=_OFFLINE_MEMORY):
        import agents.memory_routes as routes
    from agents import memory_turns, orchestrator
    from agents import turn_evidence_store as evidence
    from agents.orchestrator import ChartRedactionHook
    from strands.session.session_repository import SessionRepository


@pytest.fixture(autouse=True)
def _no_aws_call():
    """Every test here runs without one AWS API call."""
    calls: list = []
    with patch.object(botocore.client.BaseClient, "_make_api_call", _refusing(calls)):
        yield
    assert calls == []


def test_importing_the_route_module_here_looks_up_no_memory():
    assert "ListMemories" not in _IMPORT_CALLS

TAG_A = '<chart-block type="line" title="unit-7: alpha" labels=\'["t1","t2"]\' datasets=\'[{"label":"alpha","data":[1,2]}]\' />'
TAG_B = '<chart-block type="line" title="unit-7: beta" labels=\'["t1","t2"]\' datasets=\'[{"label":"beta","data":[3,4]}]\' />'
TAG_C = '<chart-block type="line" title="unit-9: gamma" labels=\'["t1"]\' datasets=\'[{"label":"gamma","data":[5]}]\' />'
NOTICE = ('<chart-block type="line" kind="notice" title="No chart could be drawn for this read" '
          "labels='[]' datasets='[]' />")


def _env(mode="report-only", query_mode="agent"):
    return patch.dict("os.environ", {"RESPONSE_INTEGRITY_MODE": mode,
                                     "SYSTEM_QUERY_MODE": query_mode})


# --------------------------------------------------------------------------- #
# GUARD: the stored copy keeps the tags, the live copy gets the note
# --------------------------------------------------------------------------- #
class _JsonRepository(SessionRepository):
    """An in-memory SessionRepository that serializes each message when it is
    written, as the AgentCore Memory repository does (``create_message`` turns
    the message into its event payload before it returns)."""

    def __init__(self):
        self.sessions, self.agents, self.messages = {}, {}, {}

    def create_session(self, session, **_kw):
        self.sessions[session.session_id] = json.dumps(session.to_dict())
        return session

    def read_session(self, session_id, **_kw):
        from strands.types.session import Session
        raw = self.sessions.get(session_id)
        return Session.from_dict(json.loads(raw)) if raw else None

    def create_agent(self, session_id, session_agent, **_kw):
        self.agents[(session_id, session_agent.agent_id)] = json.dumps(session_agent.to_dict())

    def read_agent(self, session_id, agent_id, **_kw):
        from strands.types.session import SessionAgent
        raw = self.agents.get((session_id, agent_id))
        return SessionAgent.from_dict(json.loads(raw)) if raw else None

    def update_agent(self, session_id, session_agent, **kw):
        self.create_agent(session_id, session_agent, **kw)

    def create_message(self, session_id, agent_id, session_message, **_kw):
        self.messages.setdefault((session_id, agent_id), []).append(
            json.dumps(session_message.to_dict()))

    def read_message(self, session_id, agent_id, message_id, **_kw):
        return None

    def update_message(self, session_id, agent_id, session_message, **_kw):
        return None

    def list_messages(self, session_id, agent_id, limit=None, offset=0, **_kw):
        from strands.types.session import SessionMessage
        rows = self.messages.get((session_id, agent_id), [])[offset:]
        return [SessionMessage.from_dict(json.loads(r)) for r in rows]

    def stored(self, session_id, agent_id="default"):
        return [json.loads(r)["message"] for r in self.messages.get((session_id, agent_id), [])]


def _scripted_model(scripts):
    from strands.models.model import Model

    class _ScriptedModel(Model):
        def update_config(self, **_config):
            return None

        def get_config(self):
            return {}

        def structured_output(self, *_a, **_k):
            raise NotImplementedError

        async def stream(self, messages, tool_specs=None, system_prompt=None, **_kw):
            for event in scripts.pop(0):
                yield event

    return _ScriptedModel()


def _call_script(tool_id):
    return [{"messageStart": {"role": "assistant"}},
            {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tool_id,
                                                          "name": "read_unit_series"}}}},
            {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}}},
            {"contentBlockStop": {}},
            {"messageStop": {"stopReason": "tool_use"}}]


def _answer_script(text):
    return [{"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"text": text}}},
            {"contentBlockStop": {}},
            {"messageStop": {"stopReason": "end_turn"}}]


def _charting_agent(repo, session_id, scripts):
    from strands import Agent, tool
    from strands.session.repository_session_manager import RepositorySessionManager

    @tool
    def read_unit_series() -> str:
        """Read the unit's two series."""
        return f"Two series over the window.\n{TAG_A}\n{TAG_B}"

    return Agent(model=_scripted_model(scripts), tools=[read_unit_series],
                 hooks=[ChartRedactionHook()], callback_handler=None,
                 session_manager=RepositorySessionManager(session_id=session_id,
                                                          session_repository=repo))


def _tool_result_text(message):
    for block in message.get("content", []):
        if "toolResult" in block:
            return block["toolResult"]["content"][0]["text"]
    return None


def test_guard_the_stored_tool_result_keeps_the_tags_the_live_one_has_the_note():
    """GUARD (passes before this change too). Strands runs the session
    manager's MessageAddedEvent callback before the agent's own hooks, so the
    stored copy is written before ChartRedactionHook edits the live one in
    place. The reload reads the tags from that stored copy; a Strands bump
    that reorders the hooks fails here."""
    repo = _JsonRepository()
    with _env():
        agent = _charting_agent(repo, "s-guard", [_call_script("c1"), _answer_script(
            "The chart above shows both series.")])
        agent("Show both series for unit 7")

    stored = [t for t in map(_tool_result_text, repo.stored("s-guard")) if t]
    live = [t for t in map(_tool_result_text, agent.messages) if t]
    assert stored == [f"Two series over the window.\n{TAG_A}\n{TAG_B}"]
    assert len(live) == 1 and "<chart-block" not in live[0]
    assert "ALREADY RENDERED" in live[0]
    assert agent._pending_chart_blocks == [TAG_A, TAG_B]


# --------------------------------------------------------------------------- #
# The model's restored history holds a neutral note, never the tags
# --------------------------------------------------------------------------- #
def _says_neither_rendered_nor_withheld(text):
    from agents.chart_note import strip_chart_notes
    assert "ALREADY RENDERED" not in text and "NOT delivered" not in text
    stripped, rendered, withheld = strip_chart_notes(text)
    assert "chart(s)" not in stripped and (rendered, withheld) == (0, 0)


def test_restored_history_holds_the_note_not_the_tags():
    """The next turn's Agent restores the session through the session
    manager's list_messages; the restored tool result holds a note in place
    of the earlier turn's chart payloads. The note is the neutral one, not
    the live 'ALREADY RENDERED' note this test first expected: the stored
    message does not say whether those charts reached the page, so the
    restored note claims neither."""
    repo = _JsonRepository()
    with _env():
        first = _charting_agent(repo, "s-restore", [_call_script("c1"), _answer_script("Done.")])
        first("Show both series for unit 7")
        orchestrator._wrap_list_messages(repo)
        second = _charting_agent(repo, "s-restore", [_answer_script("Again.")])

    restored = [t for t in map(_tool_result_text, second.messages) if t]
    assert len(restored) == 1
    assert "<chart-block" not in restored[0]
    assert "2 chart(s)" in restored[0]
    _says_neither_rendered_nor_withheld(restored[0])
    assert "Two series over the window." in restored[0]
    # The stored events are never changed: the reload still reads the tags.
    assert TAG_A in repo.stored("s-restore")[2]["content"][0]["toolResult"]["content"][0]["text"]


def test_the_read_wrapper_redacts_each_shape_on_a_copy_and_leaves_other_messages_as_they_are():
    """Expects the neutral note where it first expected the live rendered
    note (see the test above for why)."""
    from strands.types.session import SessionMessage
    shapes = [
        {"toolUseId": "a", "status": "success", "content": f"rows\n{TAG_A}"},
        {"toolUseId": "b", "status": "success", "content": [{"text": f"rows\n{TAG_B}"}]},
        {"toolUseId": "c", "status": "success", "content": [{"json": {"chart": f"x {TAG_C}"}}]},
    ]
    plain = SessionMessage.from_message({"role": "assistant", "content": [{"text": "hi"}]}, 0)
    stored = [plain] + [SessionMessage.from_message(
        {"role": "user", "content": [{"toolResult": copy.deepcopy(r)}]}, i + 1)
        for i, r in enumerate(shapes)]
    before = copy.deepcopy([m.message for m in stored])
    manager = SimpleNamespace(list_messages=lambda *a, **k: list(stored))
    orchestrator._wrap_list_messages(manager)
    with _env():
        read = manager.list_messages("s", "default")
    assert read[0] is plain
    assert "<chart-block" not in json.dumps([m.message for m in read])
    note = read[1].message["content"][0]["toolResult"]["content"]
    assert "1 chart(s)" in note
    _says_neither_rendered_nor_withheld(note)
    assert [m.message for m in stored] == before


@pytest.mark.parametrize("mode", ["report-only", "enforce"])
def test_a_gated_turns_restored_history_never_says_its_charts_are_on_screen(mode):
    """A Mode A turn the gate ran for showed no chart, and reloads with none.
    Its tool result, restored into a later turn's history after the gate was
    turned off, must not tell the model those charts were rendered (nor, with
    the gate on, that they were withheld this turn); notice tags keep their
    own note, which says no chart was drawn."""
    from strands.types.session import SessionMessage
    result = SessionMessage.from_message({"role": "user", "content": [{"toolResult": {
        "toolUseId": "t-mode-a", "status": "success",
        "content": [{"text": f"{TAG_A}\n{NOTICE}"}]}}]}, 0)
    manager = SimpleNamespace(list_messages=lambda *_a, **_k: [result])
    orchestrator._wrap_list_messages(manager)
    with _env(mode, "agent"):
        (restored,) = manager.list_messages("s", "a")
        note = restored.message["content"][0]["toolResult"]["content"][0]["text"]
        assert "1 chart(s)" in note and "No chart was drawn" in note
        _says_neither_rendered_nor_withheld(note)
        events = _turn("q", ("t-mode-a", TAG_A))
        events[-1:-1] = [_call("g1", memory_turns.STRUCTURED_ANSWER_TOOL), _result("g1", "ok")]
        shown, notes = _reopen(events)
    assert all(c is None for _, c in _charts(shown))
    # Not marked: the gated stream never mentioned charts (this test first
    # expected the mark, which disclosed what the gated stream withheld).
    assert notes == set()


# --------------------------------------------------------------------------- #
# One reader for the hook and the reload
# --------------------------------------------------------------------------- #
_SHAPES = {
    "string": {"toolUseId": "s", "content": f"head\n{TAG_A}\nmid\n{TAG_B}"},
    "text parts": {"toolUseId": "p", "content": [{"text": f"head\n{TAG_A}"},
                                                  {"text": "no chart"},
                                                  {"text": f"{TAG_B}\n{NOTICE}"}]},
    "json part": {"toolUseId": "j", "content": [{"json": {
        "rows": [["1"]], "charts": [f"ready:\n{TAG_A}", {"more": TAG_C}],
        "partial": "<chart-block type=\"line\" title=\"cut"}}]},
}


def test_the_hook_and_the_reload_read_the_same_tags_for_every_shape():
    for name, result in _SHAPES.items():
        hooked = copy.deepcopy(result)
        agent = SimpleNamespace()
        ChartRedactionHook()._on_message_added(SimpleNamespace(
            message={"role": "user", "content": [{"toolResult": hooked}]}, agent=agent))
        read_only = copy.deepcopy(result)
        assert orchestrator.chart_tags_in(read_only) == agent._pending_chart_blocks, name
        # The reader never edits what it reads.
        assert read_only == result, name


def test_the_reader_finds_nothing_in_a_result_without_complete_tags():
    assert orchestrator.chart_tags_in({"content": "plain rows"}) == []
    assert orchestrator.chart_tags_in({"content": '<chart-block type="line" title="cut'}) == []
    assert orchestrator.chart_tags_in({"content": [{"image": {}}]}) == []
    assert orchestrator.chart_tags_in("not a result") == []


# --------------------------------------------------------------------------- #
# The reload: each ungated tool result's delivered charts
# --------------------------------------------------------------------------- #
def _stored(role, content):
    """One event as ``MemoryClient.get_events`` gives it."""
    raw = json.dumps({"message": {"role": role, "content": content}, "message_id": 0})
    text = content if isinstance(content, str) else ""
    return SimpleNamespace(role=role, content=text or "x", blocks=memory_turns.message_blocks(raw))


def _question(text):
    return SimpleNamespace(role="user", content=text,
                           blocks=memory_turns.message_blocks(json.dumps(
                               {"message": {"role": "user", "content": [{"text": text}]}})))


def _call(tool_id, name="unit-log___query_unit_log"):
    return _stored("assistant", [{"toolUse": {"toolUseId": tool_id, "name": name, "input": {}}}])


def _result(tool_id, content):
    return _stored("user", [{"toolResult": {"toolUseId": tool_id, "status": "success",
                                            "content": content}}])


def _turn(question, *results, answer="The charts above show the series."):
    events = [_question(question)]
    for tool_id, content in results:
        events += [_call(tool_id), _result(tool_id, content)]
    return events + [_stored("assistant", [{"text": answer}])]


def _charts(shown):
    """Per tool_result block shown, in order: (tool_id, charts or None)."""
    return [(b["tool_id"], b.get("charts")) for view in shown for b in view.blocks
            if b.get("type") == "tool_result"]


def _turn_tags(events, question_index):
    """Every chart tag the stored results of one turn hold, in order."""
    tags, current = [], None
    for i, e in enumerate(events):
        if memory_turns._is_question(e):
            current = i
        elif current == question_index:
            tags += [t for b in e.blocks or [] if b.get("type") == "tool_result"
                     for t in orchestrator.chart_tags_in(b.get("result"))]
    return tags


def _reopen(events, delivered="all", *, enforced=False, ceiling=None):
    """The reload's two passes, as the events route runs them:
    ``display_blocks`` then ``with_reload_charts`` with a delivery record per
    question. ``delivered="all"``: each turn's stream sent every chart tag its
    stored results hold; else ``{question index: tags sent}`` (a missing
    question has no record). Returns (views, questions marked)."""
    shown = memory_turns.display_blocks(events, enforced=enforced)
    turns = memory_turns.question_turns(events, shown, enforced=enforced)
    records = {}
    for qi in turns:
        sent = _turn_tags(events, qi) if delivered == "all" else delivered.get(qi)
        if sent is not None:
            records[qi] = {"fingerprints": [orchestrator.chart_fingerprint(t) for t in sent],
                           "cut": False}
    kwargs = {} if ceiling is None else {"ceiling": ceiling}
    return memory_turns.with_reload_charts(shown, events, turns, records, **kwargs)


def test_display_blocks_alone_sends_no_charts():
    """Charts are attached by ``with_reload_charts`` from the turn's delivery
    record, never by ``display_blocks`` alone, so nothing that only reads the
    stored events can replay a chart the page was not sent."""
    events = _turn("Show unit 7", ("t1", f"Two series.\n{TAG_A}\n{TAG_B}"))
    for mode in ("report-only", "enforce"):
        with _env(mode, "orchestrator"):
            shown = memory_turns.display_blocks(events)
        assert _charts(shown) == [("t1", None)]


def test_an_ungated_turn_brings_back_each_results_charts_in_order():
    """Runs both reload passes with every stored tag recorded as delivered
    (``_reopen``): ``display_blocks`` no longer attaches charts itself."""
    events = _turn("Show unit 7", ("t1", f"Two series.\n{TAG_A}\n{TAG_B}"),
                   ("t2", [{"text": "no chart here"}]),
                   ("t3", [{"text": f"{TAG_C}\n{NOTICE}"}]))
    with _env():
        shown, notes = _reopen(events)
    assert _charts(shown) == [("t1", [TAG_A, TAG_B]), ("t2", None), ("t3", [TAG_C, NOTICE])]
    assert notes == set()
    # The summary still never carries a tag.
    assert "<chart-block" not in json.dumps([b.get("summary") for v in shown for b in v.blocks])


def test_a_result_without_charts_keeps_its_shape():
    """GUARD (passes before this change too): no ``charts`` key without a chart."""
    with _env():
        shown = memory_turns.display_blocks(_turn("q", ("t1", "rows only")))
    (block,) = [b for v in shown for b in v.blocks if b.get("type") == "tool_result"]
    assert set(block) == {"type", "tool_id", "tool", "status", "summary"}


def test_a_gated_turn_brings_back_no_charts():
    """A turn that stored the gate's answer call, and every turn while the
    Mode A gate is on, shows only what the gated stream showed: no charts,
    even with a delivery record, and no mark, since the gated stream never
    mentioned charts and a mark would say its hidden results held some. The
    ungated turn beside it brings its chart back. Runs both reload passes
    (``_reopen``): ``display_blocks`` no longer attaches charts. This test
    first expected the gated turn to be marked; that disclosed what the gated
    stream withheld."""
    gated = _turn("q", ("t1", f"rows\n{TAG_A}"))
    gated[-1:-1] = [_call("t9", memory_turns.STRUCTURED_ANSWER_TOOL), _result("t9", "ok")]
    with _env():
        mixed, notes = _reopen(gated + _turn("q2", ("t2", f"rows\n{TAG_B}")))
    sent = [c for _, c in _charts(mixed)]
    assert sent[-1] == [TAG_B]
    assert all(c is None for c in sent[:-1]) and len(sent) > 1
    assert notes == set()
    with _env("enforce"):
        gate_on, notes = _reopen(_turn("q", ("t1", f"rows\n{TAG_A}")), enforced=True)
    assert "<chart-block" not in json.dumps([v.blocks for v in gate_on])
    assert all(c is None for _, c in _charts(gate_on))
    assert notes == set()


def test_a_turn_whose_stored_tags_are_only_notices_is_never_marked():
    """Reproduction (fails on f85aba80): a notice-only tag says no chart was
    drawn, so a turn with no delivery record whose stored results hold only
    notices was marked 'Charts drawn for this answer are not all shown',
    which is false. Only a rendered chart the reload cannot show marks a
    turn."""
    with _env():
        shown, notes = _reopen(_turn("q", ("t1", f"rows\n{NOTICE}")), {})
    assert _charts(shown) == [("t1", None)]
    assert notes == set()
    with _env():
        shown, notes = _reopen(_turn("q", ("t1", f"rows\n{NOTICE}\n{TAG_A}")), {})
    assert notes == {0}


def test_a_turn_whose_record_says_no_chart_was_sent_is_not_marked():
    """Reproduction (fails on f85aba80): a Mode B turn under enforce keeps a
    record that its stream sent no chart (the drain drops them live). The
    reload shows none either, so the answer reads as it did live; it was
    marked only because nothing was replayed."""
    with _env("enforce", "agent"):
        shown, notes = _reopen(_turn("q", ("t1", f"rows\n{TAG_A}")), {0: []})
    assert _charts(shown) == [("t1", None)]
    assert notes == set()


def test_charts_are_deduped_across_the_turn_and_shown_again_in_a_later_turn():
    """Runs both reload passes (``_reopen``): ``display_blocks`` no longer
    attaches charts itself."""
    events = (_turn("q1", ("t1", f"{TAG_A}\n{TAG_A}"), ("t2", f"{TAG_A}\n{TAG_B}"),
                    ("t3", [{"text": TAG_B}]))
              + _turn("q2", ("t4", TAG_A)))
    with _env():
        shown, notes = _reopen(events)
    assert _charts(shown) == [("t1", [TAG_A]), ("t2", [TAG_B]), ("t3", None), ("t4", [TAG_A])]
    assert notes == set()


def test_a_turn_replays_only_the_charts_its_stream_sent():
    """A turn stopped after the first of two charts reached the page reloads
    that one only. What it shows is what the live page showed, so it is not
    marked."""
    events = _turn("q", ("t1", f"Two series.\n{TAG_A}\n{TAG_B}"))
    with _env():
        shown, notes = _reopen(events, {0: [TAG_A]})
    assert _charts(shown) == [("t1", [TAG_A])]
    assert notes == set()


def test_a_turn_with_no_delivery_record_replays_no_charts_and_is_marked():
    """Older chats, a turn the store did not keep, an integrity-enforce turn
    (its stream sent no chart, so nothing was recorded), a stopped turn and a
    hook failure all look the same here: no record. None of their stored tags
    is replayed, whatever the integrity mode is now, and each turn whose
    stored results held a chart is marked. A turn without stored charts is
    never marked."""
    events = (_turn("q1", ("t1", f"rows\n{TAG_A}")) + _turn("q2", ("t2", "rows only"))
              + _turn("q3", ("t3", TAG_B)))
    for mode, query_mode in (("report-only", "orchestrator"), ("enforce", "orchestrator"),
                             ("report-only", "agent")):
        with _env(mode, query_mode):
            shown, notes = _reopen(events, {8: [TAG_B]})
        assert _charts(shown) == [("t1", None), ("t2", None), ("t3", [TAG_B])]
        assert notes == {0}


def _big_tag(n, size):
    return (f'<chart-block type="line" title="unit-{n}" labels=\'["t"]\' '
            f'datasets=\'[{{"label":"s","data":[{"1," * (size // 2)}1]}}]\' />')


def _wire(tags):
    """The bytes these chart strings add to the response body."""
    return sum(len(json.dumps(t, ensure_ascii=False).encode("utf-8")) + 1 for t in tags)


def test_the_byte_ceiling_is_spent_newest_turn_first():
    """The newest turns keep their charts; an older turn whose charts no
    longer fit sends none and is marked, so its answer says why. This test
    first expected the oldest charts to be kept (first come, first sent),
    which left every later answer silently without its charts."""
    ceiling = memory_turns.MAX_RELOAD_CHART_BYTES
    tag_bytes = 100_000
    count = ceiling // tag_bytes + 3
    events = []
    for n in range(count):
        events += _turn(f"q{n}", (f"t{n}", _big_tag(n, tag_bytes)))
    with _env():
        shown, notes = _reopen(events)
    sent = [c for _, c in _charts(shown)]
    kept = [c for c in sent if c]
    assert _wire([t for c in kept for t in c]) <= ceiling
    assert 0 < len(kept) < count
    # The newest charts are the ones kept; every older turn is marked.
    assert sent[count - len(kept):] == kept
    assert all(c is None for c in sent[:count - len(kept)])
    questions = [i for i, e in enumerate(events) if memory_turns._is_question(e)]
    assert notes == set(questions[:count - len(kept)])


def test_one_large_early_result_never_suppresses_later_charts():
    """Two turns, each with one 600 KB chart: the newest turn's chart is
    sent; the earlier one does not fit beside it and its turn is marked."""
    pad = "x" * 600_000
    first = f'<chart-block type="line" title="first" pad="{pad}" />'
    second = f'<chart-block type="line" title="second" pad="{pad}" />'
    events = (_turn("first", ("t1", first), answer="The chart above answers first.")
              + _turn("second", ("t2", second), answer="The chart above answers second."))
    with _env():
        shown, notes = _reopen(events)
    assert _charts(shown) == [("t1", None), ("t2", [second])]
    assert notes == {0}


def test_a_turn_partly_held_back_by_the_ceiling_keeps_what_fits_and_is_marked():
    small, big = TAG_A, _big_tag(1, 2000)
    events = _turn("q", ("t1", f"{big}\n{small}"))
    with _env():
        shown, notes = _reopen(events, ceiling=_wire([small]))
    assert _charts(shown) == [("t1", [small])]
    assert notes == {0}


# --------------------------------------------------------------------------- #
# Through GET /api/memory/events, with the store the live stream writes
# --------------------------------------------------------------------------- #
def _raw(i, role, content):
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    text = json.dumps({"message": {"role": role, "content": content}, "message_id": 0})
    return {"eventId": f"e{i}", "eventTimestamp": t0 + timedelta(seconds=i),
            "payload": [{"conversational": {"role": role.upper(), "content": {"text": text}}}]}


def _stored_chart_turn(start, question, tool_id, result_text, *, gated=False):
    events = [_raw(start, "user", [{"text": question}]),
              _raw(start + 1, "assistant", [{"toolUse": {
                  "toolUseId": tool_id, "input": {}, "name": "unit-log___query_unit_log"}}]),
              _raw(start + 2, "user", [{"toolResult": {
                  "toolUseId": tool_id, "status": "success",
                  "content": [{"text": result_text}]}}])]
    if gated:
        events += [_raw(start + 3, "assistant", [{"toolUse": {
                       "toolUseId": f"{tool_id}g", "input": {},
                       "name": memory_turns.STRUCTURED_ANSWER_TOOL}}]),
                   _raw(start + 4, "user", [{"toolResult": {
                       "toolUseId": f"{tool_id}g", "status": "success",
                       "content": [{"text": "ok"}]}}])]
    return events + [_raw(start + 5, "assistant", [{"text": "The chart above shows it."}])]


def _route(stored, session_id, *, mode="report-only", query_mode="agent"):
    import asyncio

    class _Fake:
        def list_events(self, **_kw):
            return {"events": list(reversed(stored))}

    with patch.object(mc, "get_config", return_value=_OFFLINE_MEMORY):
        client = mc.MemoryClient()
    client._client = lambda: _Fake()
    (route,) = [r for r in routes.router.routes if r.path == "/api/memory/events"]
    request = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(user_id="u1")))
    with patch.object(mc, "get_config", return_value=_OFFLINE_MEMORY), \
            patch.object(routes, "_client", client), _env(mode, query_mode):
        return asyncio.run(route.endpoint(request, session_id=session_id))


def _deliver(session_id, question, tool_ids, *texts):
    """What the app keeps after a turn whose stream sent ``texts`` as answer
    text (``server._keep_turn_evidence``): the fingerprints of the complete
    chart tags in them, under the turn's identity, with no provenance frame."""
    sent = evidence.DeliveredCharts()
    for text in texts:
        sent.feed(text)
    turn = evidence.turn_identity(question, tool_ids)
    assert evidence.keep("u1", session_id, None, turn=turn, gated=False, charts=sent)


def _route_charts(resp):
    blocks = [b for m in resp["messages"] for b in m["blocks"] if b["type"] == "tool_result"]
    marked = [m["content"] for m in resp["messages"] if m.get("charts_not_shown")]
    return [b.get("charts") for b in blocks], marked


def test_a_mode_b_turn_reloads_charts_only_if_its_stream_sent_them():
    """The same stored Mode B turn, reopened under either integrity mode:
    with no delivery record (its stream ran under enforce and sent no chart)
    it replays none and is marked; once the app has recorded that its stream
    sent the chart, it replays it and is not marked."""
    import uuid
    session_id = f"s-{uuid.uuid4().hex[:12]}"
    stored = _stored_chart_turn(0, "Show unit 7", "t1", f"Two series.\n{TAG_A}")
    for mode in ("report-only", "enforce"):
        charts, _marked = _route_charts(_route(stored, session_id, mode=mode,
                                               query_mode="orchestrator"))
        assert charts == [None], mode
    for mode in ("report-only", "enforce"):
        assert _route_charts(_route(stored, session_id, mode=mode, query_mode="orchestrator")) \
            == ([None], ["Show unit 7"])
    _deliver(session_id, "Show unit 7", ["t1"], f"\n\n{TAG_A}\n\n")
    for mode in ("enforce", "report-only"):
        assert _route_charts(_route(stored, session_id, mode=mode, query_mode="orchestrator")) \
            == ([[TAG_A]], [])


def test_the_events_route_sends_the_charts_beside_the_trace_lines():
    """Through ``GET /api/memory/events``: the route's later passes over a
    system call's block keep its ``charts``. The turn now needs a delivery
    record (``_deliver``) to replay them, and the route runs offline."""
    import uuid
    session_id = f"s-{uuid.uuid4().hex[:12]}"
    stored = _stored_chart_turn(0, "Show unit 7", "t1", f"Two series.\n{TAG_A}")
    _deliver(session_id, "Show unit 7", ["t1"], f"\n\n{TAG_A}", "\n\nThe chart above.")
    resp = _route(stored, session_id)
    (block,) = [b for m in resp["messages"] for b in m["blocks"] if b["type"] == "tool_result"]
    assert block["charts"] == [TAG_A]
    assert "<chart-block" not in block["summary"]
    assert not any(m.get("charts_not_shown") for m in resp["messages"])


def test_the_route_leaves_a_gated_turn_unmarked_and_spends_the_ceiling_newest_first():
    """Through the events route: the gated turn replays no chart and is not
    marked (its stream never mentioned charts); of two large charts the newer
    is sent and the older turn is marked. This test first expected the gated
    turn to be marked, which disclosed what its gated stream withheld."""
    import uuid
    session_id = f"s-{uuid.uuid4().hex[:12]}"
    pad = "x" * 600_000
    first = f'<chart-block type="line" title="first" pad="{pad}" />'
    second = f'<chart-block type="line" title="second" pad="{pad}" />'
    stored = (_stored_chart_turn(0, "gated", "t0", TAG_A, gated=True)
              + _stored_chart_turn(10, "first", "t1", first)
              + _stored_chart_turn(20, "second", "t2", second))
    _deliver(session_id, "first", ["t1"], first)
    _deliver(session_id, "second", ["t2"], second)
    charts, marked = _route_charts(_route(stored, session_id))
    assert [c for c in charts if c is not None] == [[second]]
    assert marked == ["first"]


# --------------------------------------------------------------------------- #
# What the app records of a turn's stream
# --------------------------------------------------------------------------- #
def test_the_record_holds_fingerprints_of_the_tags_the_browser_harvests():
    """The chat harvests a complete tag from the text of the current run
    (``answer-segments.ts``): a tag split across token events counts, one
    split across a tool call does not, and a repeat counts once. The record
    holds the same tags as short digests of the delivery dedup key, never
    tag text."""
    sent = evidence.DeliveredCharts()
    half = len(TAG_A) // 2
    for text in ("intro <chart-bl", "ock" + TAG_A[len("<chart-block"):half], TAG_A[half:],
                 f" and {TAG_A} again\n"):
        sent.feed(text)
    sent.feed(TAG_B[:30])
    sent.boundary()
    sent.feed(TAG_B[30:])
    sent.feed(f"\n\n{TAG_C}\n\n{NOTICE}\n\n")
    assert sent.fingerprints == [orchestrator.chart_fingerprint(t) for t in (TAG_A, TAG_C, NOTICE)]
    assert all(len(fp) == 16 and int(fp, 16) >= 0 for fp in sent.fingerprints)
    assert sent.cut is False
    assert orchestrator.chart_fingerprint(TAG_A) != orchestrator.chart_fingerprint(TAG_B)


def test_the_record_keeps_a_bounded_number_of_fingerprints():
    sent = evidence.DeliveredCharts()
    for n in range(evidence.MAX_TURN_CHARTS + 5):
        sent.feed(_big_tag(n, 10))
    assert len(sent.fingerprints) == evidence.MAX_TURN_CHARTS
    assert sent.cut is True


def test_a_cut_record_marks_a_turn_with_an_unrecorded_chart():
    events = _turn("q", ("t1", f"{TAG_A}\n{TAG_B}"))
    with _env():
        shown = memory_turns.display_blocks(events)
        turns = memory_turns.question_turns(events, shown)
        record = {0: {"fingerprints": [orchestrator.chart_fingerprint(TAG_A)], "cut": True}}
        shown, notes = memory_turns.with_reload_charts(shown, events, turns, record)
    assert _charts(shown) == [("t1", [TAG_A])]
    assert notes == {0}


def test_chart_records_never_evict_a_provenance_frame_earlier():
    """The session item's provenance frames keep exactly the bounds they had
    (``MAX_FRAMES``, ``MAX_SESSION_BYTES`` over the frames alone): chart
    records sit in their own list, at most ``MAX_CHART_TURNS`` entries of at
    most ``MAX_TURN_CHARTS`` fingerprints each, and hold no tag text."""
    import uuid
    with_charts, without = (f"s-{uuid.uuid4().hex[:12]}" for _ in range(2))
    for n in range(evidence.MAX_FRAMES + 5):
        turn = evidence.turn_identity(f"q{n}", [f"tooluse_{n}"])
        frame = {"records": [{"citation_num": 1, "tool_id": f"tooluse_{n}",
                              "source_system": "s", "verification": "partial",
                              "scope": {"filters": ["y" * 3000]}}], "truncated": False}
        sent = evidence.DeliveredCharts()
        for m in range(evidence.MAX_TURN_CHARTS + 2):
            sent.feed(_big_tag(m + 100 * n, 10))
        assert evidence.keep("u1", with_charts, frame, turn=turn, gated=False, charts=sent)
        assert evidence.keep("u1", without, frame, turn=turn, gated=False)
    a = evidence._jobs().get(evidence.item_key("u1", with_charts))
    b = evidence._jobs().get(evidence.item_key("u1", without))
    assert a["frames"] == b["frames"]
    assert "charts" not in b
    assert len(a["charts"]) == evidence.MAX_CHART_TURNS
    assert all(len(r["charts"]) == evidence.MAX_TURN_CHARTS and r["cut"] for r in a["charts"])
    assert "<chart-block" not in json.dumps(a)
    size = len(json.dumps(a["charts"], separators=(",", ":")).encode("utf-8"))
    assert size <= evidence.MAX_CHART_RECORD_BYTES


def test_a_turn_with_neither_a_frame_nor_a_delivery_record_keeps_nothing():
    """Nothing is kept without a frame or a delivery record; a delivery
    record whose stream sent no chart is kept as an empty list, so the
    reload can tell it from a missing one. This test first expected a turn
    with calls but no chart to keep nothing, which left the reload unable to
    tell 'the page showed no chart' from 'no record' (a false notice)."""
    import uuid
    session_id = f"s-{uuid.uuid4().hex[:12]}"
    turn = evidence.turn_identity("q", ["t1"])
    assert evidence.keep("u1", session_id, None, turn=turn, gated=False, charts=None) is False
    assert evidence._jobs().get(evidence.item_key("u1", session_id)) is None
    sent = evidence.DeliveredCharts()
    sent.feed("no chart in this answer")
    assert evidence.keep("u1", session_id, None, turn=turn, gated=False, charts=sent) is True
    item = evidence._jobs().get(evidence.item_key("u1", session_id))
    assert [r["charts"] for r in item["charts"]] == [[]] and item["frames"] == []
