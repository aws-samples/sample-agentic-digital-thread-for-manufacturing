# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A follow-up that cites earlier evidence cites only the labels the results
carry now.

Live walk, 2026-10-28, three new chats with the same three questions (a trend,
a second reading on the same machine, then whether the two correlate). The
third question made no system call in every chat and answered from the two
results the questions before it had returned. ``CitationOrdinalHook`` had
labelled those results ``[E1]`` and ``[E2]``. Two chats cited them that way
and showed the chips "earlier in this chat"; the third wrote ``[1][2]``,
which no Sources chip carries, so the answer had no Sources row, and it said
both series came from this turn's own queries.

What the model read beside the labelled results: its two earlier answers,
each citing its own result as ``[1]``; Step 5 of the prompt, "Each system
agent you call gets a number in order" (kept for AWS Quick, whose results
carry no stamp); and HANDLING SYSTEM ERRORS, which forbade any figure from a
system that did not return it this turn.

Two reviews on 2026-10-29 found that rewriting an earlier answer's
markers to the current labels cannot be done safely: grouped forms, and an
``[E<j>]`` an earlier answer wrote, which named that turn's labels and can
name a different result once the 8-result cap, a window cut or an "Ask
again" read shifts them; the binding is not stored. Pinned here:

* ``strip_earlier_answers`` removes every citation marker from the earlier
  answers in the model's working copy: any group holding an ``[E<j>]``
  member, and a number or group of numbers whose every number its turn gave
  out (counted from the turn's stored calls the way the hook numbered them
  live). The earlier results keep their current ``[E<k>]`` labels. Every
  other bracket stays as written, an index such as ``sensor[1]`` and a
  footnote line ``[1]: ...`` always; in a turn the window cut, a number
  counts only up to the calls still visible. Stored memory, what the reader
  saw and this turn's answer and stamps are unchanged;
* crossing the label cap, a window cut and an "Ask again" read that drops
  turns leave no stale marker in what the model reads;
* the result labels are the form the client links (``citation_markers``,
  pinned to the client by ``test_citation_marker_parity.py``);
* the gate's structured answer (``FinalAnswerDraft``) gets no number and no
  label; a fence closes only on its own character and length;
* the citation rule says a bare ``[N]`` exists only for a result stamped with
  it and that an ``[E<k>]`` result is never described as this turn's; HANDLING
  SYSTEM ERRORS names the ``[E<k>]`` exception and still forbids filling a
  failed call's gap; AWS Quick's instructions are unchanged.

A unit test cannot show that the model now always writes ``[E<k>]``; the live
walk does. Synthetic, domain-neutral payloads under two naming schemes.
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

import agents.orchestrator as orch
from agents import citation_ordinals as co
from agents import memory_turns
from agents.citation_markers import linked_markers
from agents.citation_ordinals import CitationOrdinalHook, citation_stamp
from agents.conversation_window import TurnKeepingSlidingWindow
from agents.history_turns import without_unfinished_turns
from agents.output_cap_recovery import RECOVERY_NOTICE
from agents.workflow_guidance import compose_quick_system_prompt
from tests.unit.test_orchestrator_output_cap_recovery import (
    _FakeMemoryPlane, _answer, _errors, _run, _structured_answer, _tool_call, _usage,
)

_MODES = ("agent", "orchestrator")
_SCHEMES = [
    {"tool": "plant_x___query_plant_x", "q": "question {n}", "a": "reading {n} rose"},
    {"tool": "vendor_ledger_7___lookup", "q": "Wie steht es um Los {n}?", "a": "Los {n} offen"},
]


@pytest.fixture(autouse=True)
def _report_only(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.delenv("ADT_WORKSHOP", raising=False)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _use(tool_id, name):
    return {"toolUse": {"toolUseId": tool_id, "name": name, "input": {}}}


def _result(tool_id, text="rows"):
    return {"toolResult": {"toolUseId": tool_id, "status": "success",
                           "content": [{"text": text}]}}


def _turn(question, calls, answer):
    """One answered turn: ``calls`` is a list of (toolUseId, tool name)."""
    msgs = [{"role": "user", "content": [{"text": question}]}]
    for tool_id, name in calls:
        msgs += [{"role": "assistant", "content": [_use(tool_id, name)]},
                 {"role": "user", "content": [_result(tool_id)]}]
    msgs.append({"role": "assistant", "content": [{"text": answer}]})
    return msgs


def _start(hook, messages, incoming="next question"):
    hook._on_invocation_start(SimpleNamespace(
        agent=SimpleNamespace(messages=messages),
        messages=[{"role": "user", "content": [{"text": incoming}]}]))


def _answers(messages):
    """Every assistant text in ``messages``, in order."""
    return [b["text"] for m in messages if m.get("role") == "assistant"
            for b in m["content"] if isinstance(b.get("text"), str)]


def _result_tail(messages, tool_id):
    for message in messages:
        for block in message["content"]:
            if block.get("toolResult", {}).get("toolUseId") == tool_id:
                return block["toolResult"]["content"][-1]["text"]
    raise AssertionError(tool_id)


#: Any citation-shaped marker, single or grouped (no code in these answers).
_MARKER = re.compile(r"\[(?:E?\d+)(?: *, *E?\d+)*\](?!\()")


def _no_marker_left(messages):
    return not any(_MARKER.search(text) for text in _answers(messages))


# --------------------------------------------------------------------------- #
# 1. The walk's shape: two earlier answers each cited their own [1]
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("scheme", _SCHEMES)
def test_a_no_call_follow_up_reads_earlier_answers_without_their_markers(scheme):
    tool = scheme["tool"]
    history = (_turn(scheme["q"].format(n=1), [("a1", tool)], scheme["a"].format(n=1) + " [1].")
               + _turn(scheme["q"].format(n=2), [("b1", tool)],
                       scheme["a"].format(n=2) + " [1]."))
    _start(CitationOrdinalHook(), history)
    # The results carry the labels the model cites from now.
    assert _result_tail(history, "a1") == co.earlier_stamp(1)
    assert _result_tail(history, "b1") == co.earlier_stamp(2)
    assert _answers(history) == [scheme["a"].format(n=1) + ".", scheme["a"].format(n=2) + "."]
    assert _no_marker_left(history)


def test_each_earlier_turn_is_counted_on_its_own():
    """The first turn gave out [1] and [2] (the graph call is never numbered),
    so its [3] was not a citation; the second turn's [1] was."""
    history = (_turn("q1", [("g1", "explore_graph"), ("a1", "sys-a___query_sys_a"),
                            ("c1", "sys-c___query_sys_c")], "first [1], second [2], other [3].")
               + _turn("q2", [("b1", "sys-b___query_sys_b")], "third [1]."))
    _start(CitationOrdinalHook(), history)
    assert _answers(history) == ["first, second, other [3].", "third."]
    assert co.earlier_sources(history) == [
        {"label": "E1", "tool": "sys-a___query_sys_a"},
        {"label": "E2", "tool": "sys-c___query_sys_c"},
        {"label": "E3", "tool": "sys-b___query_sys_b"}]


# --------------------------------------------------------------------------- #
# 2. Grouped and earlier citations never reach the model
# --------------------------------------------------------------------------- #
def test_grouped_and_earlier_citations_never_reach_the_model():
    """The reviewers' forms: [1, 2], [E1] and [E1, E2]. None of them reaches
    the model's working copy; the results are listed with their labels now."""
    history = (_turn("q1", [("a1", "sys-a___query_sys_a"), ("b1", "sys-b___query_sys_b")],
                     "Both rose [1, 2]; the first [1,2].")
               + _turn("q2", [], "Same as before [E1], both [E1, E2]."))
    _start(CitationOrdinalHook(), history)
    assert _answers(history) == ["Both rose; the first.", "Same as before, both."]
    assert _result_tail(history, "a1") == co.earlier_stamp(1)
    assert _result_tail(history, "b1") == co.earlier_stamp(2)
    assert co.earlier_sources(history) == [
        {"label": "E1", "tool": "sys-a___query_sys_a"},
        {"label": "E2", "tool": "sys-b___query_sys_b"}]


def test_a_group_holding_an_earlier_label_is_removed_whole():
    """An [E<j>] is only ever a citation, so a group holding one is too, even
    beside a number its turn never gave out."""
    history = _turn("q1", [], "Same [3, E1] as before.")
    _start(CitationOrdinalHook(), history)
    assert _answers(history) == ["Same as before."]


def test_crossing_the_label_cap_leaves_no_stale_marker():
    """An answer wrote [E1] when E1 was the first result. Eight newer results
    later that result has no label and E1 is another one; a kept [E1] would
    show the wrong source. The answer loses it, and every [1] whose result
    is past the cap goes too."""
    history = (_turn("q0", [("a0", "sys-a___query_sys_a")], "value 0 [1].")
               + _turn("q-same", [], "still value 0 [E1]."))
    for i in range(1, co.MAX_EARLIER_RESULTS + 1):
        history += _turn(f"q{i}", [(f"a{i}", "sys-b___query_sys_b")], f"value {i} [1].")
    _start(CitationOrdinalHook(), history)
    assert _result_tail(history, "a1") == co.earlier_stamp(1)
    assert _result_tail(history, "a0") == "rows"
    assert _answers(history)[:3] == ["value 0.", "still value 0.", "value 1."]
    assert _no_marker_left(history)


def test_a_window_cut_leaves_no_stale_marker():
    """The real turn-keeping window cuts the first turn away. The third
    answer's [E1] [E2] named the first and second results; now E1 is the
    second and E2 the fourth turn's. Nothing of the kind is left to copy."""
    history = (_turn("qa", [("a1", "sys-a___query_sys_a")], "A is 3 [1].")
               + _turn("qb", [("b1", "sys-b___query_sys_b")], "B is 4 [1].")
               + _turn("qc", [], "A then B [E1] [E2].")
               + _turn("qd", [("d1", "sys-d___query_sys_d")], "D is 5 [1]."))
    agent = SimpleNamespace(messages=history)
    TurnKeepingSlidingWindow(window_size=len(history) - 2).reduce_context(agent)
    assert agent.messages[0] == {"role": "user", "content": [{"text": "qb"}]}
    _start(CitationOrdinalHook(), agent.messages)
    assert co.earlier_sources(agent.messages) == [
        {"label": "E1", "tool": "sys-b___query_sys_b"},
        {"label": "E2", "tool": "sys-d___query_sys_d"}]
    assert _answers(agent.messages) == ["B is 4.", "A then B.", "D is 5."]


def test_an_ask_again_read_that_drops_turns_leaves_no_stale_marker():
    """"Ask again" reads the history without unfinished turns (the real read
    rule). An unfinished turn's result was E1 when the later answer cited
    [E1] [E2]; after the drop E1 is the next result. Nothing of the kind is
    left to copy."""
    unfinished = [{"role": "user", "content": [{"text": "qx"}]},
                  {"role": "assistant", "content": [_use("x1", "sys-x___query_sys_x")]},
                  {"role": "user", "content": [_result("x1")]}]
    stored = (unfinished
              + _turn("qa", [("a1", "sys-a___query_sys_a")], "A is 3 [1].")
              + _turn("qc", [], "X then A [E1] [E2].")
              + _turn("qr", [("r1", "sys-r___query_sys_r")], "R [1]."))
    read = without_unfinished_turns(stored, retry_question="qr")
    assert read == stored[len(unfinished):len(unfinished) + 6]
    _start(CitationOrdinalHook(), read)
    assert co.earlier_sources(read) == [{"label": "E1", "tool": "sys-a___query_sys_a"}]
    assert _answers(read) == ["A is 3.", "X then A."]


def test_a_turn_the_window_cut_loses_only_numbers_its_visible_calls_could_give_out():
    """A history cut mid-turn starts before any question: that turn's first
    calls are gone, so how many numbers it gave out is not known. Its labels
    are removed, and a number or group only when every member is at most
    the calls still visible (two here): a year, an interval past them and an
    index stay as written; code too."""
    history = [{"role": "assistant", "content": [_use("z1", "sys-a___query_sys_a")]},
               {"role": "user", "content": [_result("z1")]},
               {"role": "assistant", "content": [_use("z2", "sys-b___query_sys_b")]},
               {"role": "user", "content": [_result("z2")]},
               {"role": "assistant", "content": [{"text": (
                   "cut turn [2] and [E1], `x[3]` [7, 9], both [1, 2].\n"
                   "Baseline [2024], range [4, 7], sensor[12], sensor[1].")}]},
               *_turn("q1", [("a1", "sys-a___query_sys_a")], "kept [1].")]
    _start(CitationOrdinalHook(), history)
    assert _answers(history) == [
        "cut turn and, `x[3]` [7, 9], both.\n"
        "Baseline [2024], range [4, 7], sensor[12], sensor[1].", "kept."]


def test_a_cut_turn_with_no_visible_call_loses_only_its_labels():
    history = [{"role": "assistant", "content": [{"text": "cut [1] and [E2] [1, 2]."}]},
               *_turn("q1", [("a1", "sys-a___query_sys_a")], "kept [1].")]
    _start(CitationOrdinalHook(), history)
    assert _answers(history) == ["cut [1] and [1, 2].", "kept."]


# --------------------------------------------------------------------------- #
# 3. What is not a citation is left exactly as written
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    "see `rows[1]` and `rows[E1]` here",
    "```\nlist[1]\npair[1, 2] [E1]\n```",
    "~~~\n[1]\n~~~",
    "[1](https://example.invalid/doc) [E1](https://example.invalid/e)",
    "<chart-block type=\"line\" title=\"t\" labels='[\"a\"]' datasets='[{\"data\":[1]}]' />",
])
def test_code_links_and_chart_tags_are_left_as_written(text):
    assert co.without_citations(text, 2) == text
    assert co.without_memory_citations(text) == text


def test_markers_outside_code_on_the_same_line_are_still_removed():
    text = "value [1] and `idx[1]` then [2]"
    assert co.without_citations(text, 2) == "value and `idx[1]` then"


def test_the_result_labels_are_the_form_the_client_links():
    """Each stamp's [E<k>] is a marker the client links for label k
    (``citation_markers`` is the client's grammar, pinned by the shared
    fixture)."""
    for k in range(1, co.MAX_EARLIER_RESULTS + 1):
        stamp = co.earlier_stamp(k)
        [group] = linked_markers(stamp, (), (k,))
        assert stamp[group.start:group.end] == f"[E{k}]"


def test_brackets_the_turn_never_gave_out_are_left_as_written():
    """The reviewers' examples. The turn gave out [1] only: an index, a
    number, an interval, a group with a number never given out and a
    footnote the client never linked stay exactly as written."""
    text = ("Read sensor[2] before [12]. Range [4, 7] or [1, 3] holds [1].\n\n"
            "[3]: the method.")
    history = _turn("q1", [("a1", "sys-a___query_sys_a")], text)
    _start(CitationOrdinalHook(), history)
    assert _answers(history) == [text.replace(" holds [1].", " holds.")]


def test_an_index_or_a_footnote_line_is_never_a_citation():
    """The reviewer's examples after a two-call turn: an index, an index
    group, an index of a label and a footnote definition stay exactly as
    written, on the client (the shared fixture) and so here."""
    text = ("Read sensor[1] before sensor[2], a[1, 2] and x[E1], then [1].\n\n"
            "[1]: calibrated reading.\n  [1, 2]: both readings.")
    history = _turn("q1", [("a1", "sys-a___query_sys_a"), ("b1", "sys-b___query_sys_b")], text)
    _start(CitationOrdinalHook(), history)
    assert _answers(history) == [text.replace(", then [1].", ", then.")]


def test_a_free_standing_group_of_the_turns_numbers_is_removed_as_the_client_linked_it():
    """The one case the grammar cannot tell apart: [1, 2] standing alone
    after a two-call turn. The client showed it as two chips, so the reader
    saw citations, and the model's copy follows the client."""
    history = _turn("q1", [("a1", "sys-a___query_sys_a"), ("b1", "sys-b___query_sys_b")],
                    "The normalized range is [1, 2].")
    _start(CitationOrdinalHook(), history)
    assert _answers(history) == ["The normalized range is."]


@pytest.mark.parametrize(("text", "written"), [
    ("value [1][3], done.", "value [3], done."),
    ("[1] Opens the line.", "Opens the line."),
    ("Ends here [1][2]", "Ends here"),
    ("x [1]y", "x y"),
])
def test_a_removed_marker_leaves_one_space_between_words(text, written):
    assert co.without_citations(text, 2) == written


@pytest.mark.parametrize("text", [
    "````\n```\nx[1]\n```\ny[1]\n````",
    "```\n~~~\nx[1]\n~~~\ny[1]\n```",
    "~~~\n```\nx[1]\n```\ny[1]\n~~~",
    "```\n``` not a close [1]\ny[1]\n```",
])
def test_code_up_to_its_own_closing_fence_is_left_as_written(text):
    """A fence closes only on a run of its own character, at least as long,
    with nothing else on the line; the marker after the block is a citation."""
    history = _turn("q1", [("a1", "sys-a___query_sys_a")], text + "\nAfter [1].")
    _start(CitationOrdinalHook(), history)
    assert _answers(history) == [text + "\nAfter."]


# --------------------------------------------------------------------------- #
# 4. The gate's structured answer is never a source
# --------------------------------------------------------------------------- #
_GATE = "FinalAnswerDraft"


def test_the_structured_answer_tool_is_the_one_the_memory_reload_hides():
    assert co.STRUCTURED_ANSWER_TOOL is memory_turns.STRUCTURED_ANSWER_TOOL == _GATE


def test_the_structured_answer_tool_gets_no_number_live():
    """The client never sees the gate's call, so a cited call after it keeps
    the number the client gives it."""
    hook = CitationOrdinalHook()
    _start(hook, [])
    uses = {"role": "assistant", "content": [_use("a1", "sys-a___query_sys_a"),
                                             _use("g1", _GATE)]}
    hook._on_message_added(SimpleNamespace(message=uses, agent=SimpleNamespace()))
    later = {"role": "assistant", "content": [_use("b1", "sys-b___query_sys_b")]}
    hook._on_message_added(SimpleNamespace(message=later, agent=SimpleNamespace()))
    results = {"role": "user", "content": [_result("a1"), _result("g1", "validated"),
                                           _result("b1")]}
    hook._on_message_added(SimpleNamespace(message=results, agent=SimpleNamespace()))
    assert [_result_tail([results], t) for t in ("a1", "g1", "b1")] == [
        citation_stamp(1), "validated", citation_stamp(2)]


def test_the_structured_answer_tool_gets_no_earlier_label():
    history = _turn("q1", [("a1", "sys-a___query_sys_a"), ("g1", _GATE),
                           ("b1", "sys-b___query_sys_b")], "a [1] b [2] c [3]")
    _start(CitationOrdinalHook(), history)
    assert co.earlier_sources(history) == [
        {"label": "E1", "tool": "sys-a___query_sys_a"},
        {"label": "E2", "tool": "sys-b___query_sys_b"}]
    assert _result_tail(history, "g1") == "rows"
    # The turn gave out two numbers, so [3] was never a citation.
    assert _answers(history) == ["a b c [3]"]


def test_the_numbers_rebuilt_from_stored_calls_are_the_ones_stamped_live():
    """The stamps are not stored (memory keeps a result before the hook
    stamps it), so an earlier turn's numbers are rebuilt from its stored
    calls. Same predicate, same order, and the same turn boundaries: the
    output-limit continuation keeps counting, a graph call and the gate's
    call get no number, parallel calls count in message order."""
    hook = CitationOrdinalHook()
    turn = [{"role": "user", "content": [{"text": "q1"}]}]
    _start(hook, [])
    steps = [[_use("g0", "explore_graph"), _use("a1", "sys-a___query_sys_a"),
              _use("b1", "sys-b___query_sys_b")],
             [_use("c1", "sys-c___query_sys_c")]]
    for step, uses in enumerate(steps):
        if step:
            turn.append({"role": "user", "content": [{"text": RECOVERY_NOTICE}]})
            _start(hook, list(turn), incoming=RECOVERY_NOTICE)
        message = {"role": "assistant", "content": uses}
        hook._on_message_added(SimpleNamespace(message=message, agent=SimpleNamespace()))
        turn.append(message)
    final = {"role": "assistant", "content": [_use("f1", _GATE)]}
    hook._on_message_added(SimpleNamespace(message=final, agent=SimpleNamespace()))
    turn.append(final)
    assert hook._ordinals == {"a1": 1, "b1": 2, "c1": 3}
    rebuilt = co._turns(turn)[1]
    assert co._turn_ordinals(rebuilt) == (hook._ordinals, 3)


# --------------------------------------------------------------------------- #
# 5. Live conversation only, once per question, this turn untouched
# --------------------------------------------------------------------------- #
def test_an_agent_kept_across_questions_strips_from_the_answers_own_text():
    """The local singleton keeps its history between questions. Each removal
    starts from what the answer wrote (put back first), so the stored-in-agent
    answers stay the reader's between questions."""
    history = []
    for i in range(3):
        history += _turn(f"q{i}", [(f"a{i}", "sys-a___query_sys_a")], f"value {i} [1].")
    hook = CitationOrdinalHook()
    _start(hook, history)
    assert _answers(history) == ["value 0.", "value 1.", "value 2."]
    history += _turn("q-next", [("a-next", "sys-a___query_sys_a")], "value next [1].")
    _start(hook, history)
    assert _answers(history) == ["value 0.", "value 1.", "value 2.", "value next."]
    assert [original for _, original, _ in hook._stripped] == [
        "value 0 [1].", "value 1 [1].", "value 2 [1].", "value next [1]."]


def test_an_output_cap_continuation_strips_nothing_and_stamps_this_turn():
    history = _turn("q1", [("a1", "sys-a___query_sys_a")], "first [1].")
    hook = CitationOrdinalHook()
    _start(hook, history)
    history.append({"role": "user", "content": [{"text": "q2"}]})
    use = {"role": "assistant", "content": [_use("b1", "sys-b___query_sys_b")]}
    hook._on_message_added(SimpleNamespace(message=use, agent=SimpleNamespace()))
    current = {"role": "user", "content": [_result("b1", "new rows")]}
    hook._on_message_added(SimpleNamespace(message=current, agent=SimpleNamespace()))
    partial = {"role": "assistant", "content": [{"text": "partial [1]"}]}
    history += [use, current, partial]
    _start(hook, history, incoming=RECOVERY_NOTICE)
    assert _answers(history)[0] == "first."
    assert partial["content"][0]["text"] == "partial [1]"
    assert current["content"][0]["toolResult"]["content"][-1] == {"text": citation_stamp(1)}


def test_a_bad_history_never_raises():
    hook = CitationOrdinalHook()
    hook._on_invocation_start(SimpleNamespace(
        agent=SimpleNamespace(messages=[None, 3, {"role": "assistant", "content": [None, 5]},
                                        {"role": "user", "content": "x"}]),
        messages=[]))
    assert co.strip_earlier_answers(None) == []
    assert co.without_citations("[" + "9" * 5000 + "]", 2) == "[" + "9" * 5000 + "]"
    assert co.without_memory_citations("[" + "9" * 5000 + "]") == "[" + "9" * 5000 + "]"
    assert co.without_memory_citations(None) is None


def _summary(events):
    return next(e["intelligence_summary"] for e in events
                if isinstance(e, dict) and "intelligence_summary" in e)


def test_the_real_runtime_follow_up_reads_the_earlier_answer_without_markers(monkeypatch):
    """Real runtime and the real AgentCore Memory session manager over an
    in-memory plane. The first answer grouped its two citations."""
    plane = _FakeMemoryPlane()
    first, _ = _run(monkeypatch, [_tool_call("t-1", "which records failed"),
                                  _tool_call("t-2", "and which were retried"),
                                  _answer("Three failed [1, 2].")],
                    session_id="s-rel", plane=plane)
    assert _errors(first) == []
    monkeypatch.undo()
    second, model = _run(monkeypatch, [_answer("Still three [E1] [E2].")],
                         session_id="s-rel", plane=plane, prompt="and how many now?")
    assert _errors(second) == []
    seen = model.calls[0]
    earlier_answer = [b["text"] for m in seen[:-1] if m["role"] == "assistant"
                      for b in m["content"] if "text" in b]
    assert earlier_answer == ["Three failed."]
    assert _summary(second)["earlier_sources"] == [{"label": "E1", "tool": "ask_system"},
                                                   {"label": "E2", "tool": "ask_system"}]
    assert _view_result(seen, "t-2")["content"][-1] == {"text": co.earlier_stamp(2)}
    # Stored memory keeps the answer the reader saw.
    stored = json.dumps(plane.stored_messages())
    assert "Three failed [1, 2]." in stored and "Three failed." not in stored


def _narrated_structured_answer(text, tool_id):
    """The enforce path's answer with a line of narration before the call."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": text}}},
        {"contentBlockStop": {}},
        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tool_id,
                                                     "name": _GATE}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
        _usage(50),
    ]


def _view_result(messages, tool_id):
    return next(b["toolResult"] for m in messages for b in m["content"]
                if b.get("toolResult", {}).get("toolUseId") == tool_id)


def test_an_enforce_mode_follow_up_through_memory_labels_only_the_data_call(monkeypatch):
    """Two enforce-mode turns through the real runtime and the real AgentCore
    Memory session manager. Memory stores the gate's own call and result;
    only the data call is an earlier source, and the earlier turn's text the
    model reads carries no marker."""
    plane = _FakeMemoryPlane()
    first, _ = _run(monkeypatch, [_tool_call("t-1", "which records failed"),
                                  _narrated_structured_answer("Three failed [1].", "t-final")],
                    mode="enforce", session_id="s-gate", plane=plane)
    assert _errors(first) == []
    stored = plane.stored_messages()
    assert any(b.get("toolUse", {}).get("name") == _GATE for m in stored for b in m["content"])
    monkeypatch.undo()
    second, model = _run(monkeypatch, [_structured_answer("t-final-2")], mode="enforce",
                         session_id="s-gate", plane=plane, prompt="and how many now?")
    assert _errors(second) == []
    assert _summary(second)["earlier_sources"] == [{"label": "E1", "tool": "ask_system"}]
    seen = model.calls[0]
    assert _view_result(seen, "t-1")["content"][-1] == {"text": co.earlier_stamp(1)}
    gate_texts = json.dumps(_view_result(seen, "t-final")["content"])
    assert "[engine]" not in gate_texts
    earlier = [b["text"] for m in seen[:-1] if m["role"] == "assistant"
               for b in m["content"] if "text" in b]
    assert earlier == ["Three failed."]
    assert "Three failed [1]." in json.dumps(plane.stored_messages())


# --------------------------------------------------------------------------- #
# 6. The prompt: one numbering, and an earlier result is never this turn's
# --------------------------------------------------------------------------- #
def _flat(text):
    return " ".join(text.split())


@pytest.mark.parametrize("mode", _MODES)
def test_the_citation_rule_has_no_bare_number_for_earlier_evidence(mode):
    flat = _flat(orch._build_orchestrator_prompt(mode))
    assert ("These stamps replace counting the systems you call (Step 5): a bare [N] exists "
            "only for a result stamped with it, so a figure from a result stamped [E<k>] is "
            "cited as [E<k>], also when it is the answer's only source. Such a result is not "
            "this turn's: never say it was returned or queried this turn.") in flat, mode


@pytest.mark.parametrize("mode", _MODES)
def test_the_citation_rule_asks_for_one_marker_per_citation(mode):
    """A group links only when every member is a citation, while separate
    markers each link on their own, so the rule asks for one per citation.
    It no longer says the Sources list links no grouped form (it does now).
    A marker right after a letter or digit reads as an index and never
    links, so it asks for the space before it."""
    flat = _flat(orch._build_orchestrator_prompt(mode))
    assert ("Write each citation in its own brackets after a space, as [N] [M] or [E<k>] "
            "[E<j>], never grouped as [N, M].") in flat, mode
    assert "links no grouped form" not in flat, mode


@pytest.mark.parametrize("mode", _MODES)
def test_handling_system_errors_names_the_earlier_result_exception(mode):
    flat = _flat(orch._build_orchestrator_prompt(mode))
    start = flat.index("HANDLING SYSTEM ERRORS")
    end = flat.index("TRUNCATED / PARTIAL TOOL RESULTS", start)
    section = flat[start:end]
    assert ("Never present a number, ranking, table, or list attributed to a system that did "
            "not return it this turn, and never describe an earlier result as returned or "
            "queried this turn. The one exception is a result a system returned for an "
            "earlier question in this chat (stamped [E<k>]), cited as CITATION NUMBERS says; "
            "it never fills the gap a call that failed this turn left.") in section, mode
    # The failure contract itself is unchanged.
    assert "if the tool did not return it on THIS turn, you do NOT have it" in section, mode
    assert "provide ONLY the values that OTHER systems actually returned on THIS turn" in section


@pytest.mark.parametrize("mode", _MODES)
def test_quick_instructions_carry_none_of_the_new_text(mode):
    quick = _flat(compose_quick_system_prompt(mode))
    for fragment in ("These stamps replace counting", "stamped [E<k>]",
                     "never describe an earlier result", "never grouped as [N, M]"):
        assert fragment not in quick, (mode, fragment)
    # Quick still numbers its own results (its results carry no stamp).
    assert "Each system agent you call gets a number in order." in quick, mode


def test_the_new_rule_text_is_dataset_neutral_and_dash_free():
    text = orch._CITATION_STAMP_RULE
    assert "\u2014" not in text and "\u2013" not in text
    # The only number in it is the step it points at.
    assert re.findall(r"\d+", text) == ["5"]
    assert "(Step 5)" in text
