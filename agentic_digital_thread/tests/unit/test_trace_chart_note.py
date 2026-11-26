# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The chart note is for the model, never for the activity trace.

``ChartRedactionHook`` swaps every ``<chart-block .../>`` tag in a tool result
for a one-line note ("[N chart(s) ALREADY RENDERED to the user: ...]", or the
integrity-enforce "withheld and NOT delivered" variant) so the model can
mention the charts without copying their data. The hook edits the tool-result
message in place, and that same message is what the runtime streams to the
client and what memory stores for a reload, so the note used to reach the
trace row of the call that produced the charts.

Pinned here, both sides:

* model side: the redacted tool result still carries the note, unchanged;
* trace side: every trace render site (the shared summarizer, the cloud
  stream parser's summary and result preview, the reload path) shows the
  call's own text with no note in it, and a result that was nothing but
  charts reads as a chart count;
* only the note itself is removed: prose that talks about charts stays;
* a chart title holding any phrase the note uses (its closing sentence, its
  opening phrase, a whole note, line breaks, escaped quotes) can't end the
  note early or leave any of it in either trace path, and the model's copy
  never names the title. The same holds for a stored note in the older form
  that quoted its titles.

All payloads are synthetic, with names unrelated to any dataset.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents.orchestrator import ChartRedactionHook
from agents.tool_result_summary import summarize_tool_result

_TAG_P = ("<chart-block type=\"line\" title=\"Press 7: hydraulic_pressure [bar]\" "
          "labels='[\"08:00\",\"08:05\"]' datasets='[{\"label\":\"p\",\"data\":[180,184]}]' />")
_TAG_Q = ("<chart-block type=\"line\" title=\"Press 7: ram_speed; mm/s\" "
          "labels='[\"08:00\",\"08:05\"]' datasets='[{\"label\":\"s\",\"data\":[12,13]}]' />")
_ANSWER = "Hydraulic pressure on Press 7 climbed from 180 to 184 bar in five minutes."


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.delenv("GRAPH_IDENTIFIER", raising=False)
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)


def _redacted(content, *, tool_use_id="t1"):
    """Run one tool result through the real hook; return the model's copy."""
    result = {"toolUseId": tool_use_id, "status": "success", "content": content}
    message = {"role": "user", "content": [{"toolResult": result}]}
    ChartRedactionHook()._on_message_added(
        SimpleNamespace(message=message, agent=SimpleNamespace()))
    return message["content"][0]["toolResult"]


def _no_note(text: str) -> None:
    assert "ALREADY RENDERED" not in text
    assert "NOT delivered" not in text
    assert "re-emit chart markup" not in text
    assert "<chart-block" not in text


# --------------------------------------------------------------------------- #
# Model side: the note stays in what the model reads
# --------------------------------------------------------------------------- #
def test_model_copy_keeps_the_rendered_note():
    seen = _redacted([{"text": f"{_ANSWER}\n\n{_TAG_P}\n\n{_TAG_Q}"}])
    text = seen["content"][0]["text"]
    assert text.startswith(_ANSWER)
    assert "[2 chart(s) ALREADY RENDERED to the user. Author no chart for their series" in text
    assert "hydraulic_pressure" not in text and "ram_speed" not in text
    assert text.rstrip().endswith("Never reproduce or re-emit chart markup.]")
    assert "<chart-block" not in text


def test_model_copy_keeps_the_withheld_note_in_enforce_mode(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    seen = _redacted([{"text": f"{_ANSWER}\n\n{_TAG_P}"}])
    text = seen["content"][0]["text"]
    assert ("[1 chart(s) withheld and NOT delivered to the user "
            "(integrity-enforce mode). Do not reference these charts") in text
    assert "hydraulic_pressure" not in text


# --------------------------------------------------------------------------- #
# Trace side: the shared summarizer (every render site goes through it)
# --------------------------------------------------------------------------- #
def test_summary_of_a_redacted_answer_is_the_answer_alone():
    seen = _redacted([{"text": f"{_ANSWER}\n\n{_TAG_P}\n\n{_TAG_Q}"}])
    summary = summarize_tool_result("press___query_press", seen)
    assert summary == _ANSWER
    _no_note(summary)


def test_summary_of_a_redacted_chart_only_result_is_a_chart_count():
    seen = _redacted(f"\n{_TAG_P}\n\n{_TAG_Q}\n")      # plain-string shape
    summary = summarize_tool_result("", {"content": [{"text": seen["content"]}]})
    assert summary == "Rendered 2 chart(s)."


def test_summary_of_a_withheld_note_never_claims_a_render(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    with_text = _redacted([{"text": f"{_ANSWER}\n\n{_TAG_P}"}])
    assert summarize_tool_result("", with_text) == _ANSWER
    alone = _redacted([{"text": _TAG_P}])
    summary = summarize_tool_result("", alone)
    assert summary == "1 chart(s) withheld."
    _no_note(summary)


def test_summary_of_json_followed_by_a_note_is_the_json_summary():
    """The guaranteed-chart suffix rides after a JSON payload; after
    redaction the note does, and the JSON still summarizes by its shape."""
    payload = json.dumps([{"name": "Press 7"}, {"name": "Press 8"}])
    seen = _redacted([{"text": f"{payload}\n\nCharts:\n{_TAG_P}"}])
    assert "ALREADY RENDERED" in seen["content"][0]["text"]
    assert summarize_tool_result("", seen) == summarize_tool_result(
        "", {"content": [{"text": payload}]})


def test_summary_of_an_answer_with_a_notice_is_the_answer_alone():
    """A notice-only tag (no chart drawn) leaves its own model-only note; the
    trace shows the answer alone, with no note and no chart count."""
    notice = ("<chart-block type=\"line\" kind=\"notice\" "
              "title=\"3 series not charted: no name recorded.\" labels='[]' datasets='[]' />")
    seen = _redacted([{"text": f"{_ANSWER}\n\n{notice}"}])
    summary = summarize_tool_result("press___query_press", seen)
    assert summary == _ANSWER
    _no_note(summary)
    assert "No chart was drawn" not in summary


def test_prose_about_charts_is_not_a_note():
    """Only the hook's own note is removed, never a sentence about charts."""
    prose = ("[2 chart(s) attached] The ram speed chart shows a dip; "
             "the pressure chart was ALREADY RENDERED earlier.")
    assert summarize_tool_result("", {"content": [{"text": prose}]}) == prose


# --------------------------------------------------------------------------- #
# Trace side: the cloud stream parser (the runtime forwards the redacted
# message) and the reload path (memory stores the redacted message)
# --------------------------------------------------------------------------- #
class _SilentLog:
    def info(self, *_a, **_k):
        pass

    def warning(self, *_a, **_k):
        pass

    def debug(self, *_a, **_k):
        pass


def _cloud_row(monkeypatch, seen):
    """The cloud stream parser's trace row for one redacted tool result, fed
    the way the runtime forwards it: ``(summary, result preview)``."""
    from agents import investigation_trace_store
    from agents.chat_client import ThinkingFilter, _CloudStreamState
    from agents.events import ToolResultEvent
    monkeypatch.setattr(investigation_trace_store, "get_trace_telemetry",
                        lambda *_a, **_k: ([], ""))
    state = _CloudStreamState(
        ThinkingFilter(), integrity_mode=False, log=_SilentLog(), owner_id="u",
        session_id="sess-note", runtime_session_id="runtime-note",
    )
    try:
        state.events_for({"current_tool_use": {"toolUseId": seen["toolUseId"],
                                               "name": "press___query_press"}})
        events = state.events_for({"message": {"role": "user",
                                               "content": [{"toolResult": seen}]}})
    finally:
        state.close()
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(results) == 1
    return results[0].summary, results[0].tool_result


def _reload_row(seen):
    from agents.memory_turns import _summary
    return _summary("press___query_press", seen)


def test_cloud_trace_row_carries_no_note(monkeypatch):
    seen = _redacted([{"text": f"{_ANSWER}\n\n{_TAG_P}"}])
    summary, preview = _cloud_row(monkeypatch, seen)
    assert summary == _ANSWER
    assert preview == _ANSWER
    _no_note(summary)
    _no_note(preview)


def test_reloaded_trace_row_carries_no_note():
    seen = _redacted([{"text": f"{_ANSWER}\n\n{_TAG_P}\n\n{_TAG_Q}"}])
    summary = _reload_row(seen)
    assert summary == _ANSWER
    _no_note(summary)


# --------------------------------------------------------------------------- #
# Hostile titles: chart titles carry external display names and channel
# labels, so any text the note itself uses can turn up inside one. A valid
# title is whatever ``title="..."`` can hold: anything but a double quote
# (and ``>``, which would end the tag). None of these may end the note
# early or leave part of it in the trace.
# --------------------------------------------------------------------------- #
_CLOSE = "never attempt to reproduce or re-emit chart markup.]"
_CLOSE_NOW = "Never reproduce or re-emit chart markup.]"
#: The rendered note's closing sentences now, and in the wording stored notes
#: still carry.
_RENDERED_BODY = (". Author no chart for their series, and never mention this note, a tool "
                  "result, how charts are shown or a rule about charts; at most say 'the "
                  "chart above shows'. " + _CLOSE_NOW)
_RENDERED_BODY_BEFORE = (". Chart data is displayed client-side and has been withheld from "
                         "you intentionally \u2014 reference the charts in prose, " + _CLOSE)
_WITHHELD_BODY = ". Do not reference these charts in your answer and " + _CLOSE
#: The clause a note with omission counts carries after them.
_COUNTED = (". Say in plain words how many were not charted, from these counts, never by "
            "name, and chart none of them")
_HOSTILE_TITLES = [
    f"Unit 9 {_CLOSE} tail",                                   # the closing phrase
    f"Unit 9 {_CLOSE_NOW} tail",                               # the current closing phrase
    f"Unit 9{_RENDERED_BODY} tail",                            # the whole rendered body
    f"Unit 9{_RENDERED_BODY_BEFORE} tail",                     # the earlier rendered body
    f"Unit 9{_COUNTED}{_RENDERED_BODY} tail",                  # the counted clause and body
    f"Unit 9{_WITHHELD_BODY} tail",                            # the whole withheld body
    "Unit 9 [1 chart(s) ALREADY RENDERED to the user: x",      # the opening phrase
    f"Unit 9 [1 chart(s) ALREADY RENDERED to the user: {_RENDERED_BODY} tail",  # a whole note
    f"Unit 9 [1 chart(s) ALREADY RENDERED to the user: {_RENDERED_BODY_BEFORE} tail",
    f"Unit &quot;9&quot;; {_CLOSE}; tail",                     # escaped quotes + separator
    f"Unit 9\n{_CLOSE}\ntail",                                 # line breaks
]


def _tag(title: str) -> str:
    return (f"<chart-block type=\"line\" title=\"{title}\" "
            "labels='[\"08:00\"]' datasets='[{\"label\":\"p\",\"data\":[1]}]' />")


def _assert_trace_clean(text: str, expected: str) -> None:
    assert text == expected
    for fragment in ("tail", "Chart data is displayed", "Author no chart", "re-emit",
                     "not charted", "ALREADY RENDERED", "NOT delivered", "Unit 9",
                     "Unit &quot;9"):
        assert fragment not in text


@pytest.mark.parametrize("title", _HOSTILE_TITLES)
def test_hostile_title_never_reaches_the_model_note(title):
    seen = _redacted([{"text": f"{_ANSWER}\n\n{_tag(title)}\n\n{_TAG_Q}"}])
    text = seen["content"][0]["text"]
    assert text == f"{_ANSWER}\n\n[2 chart(s) ALREADY RENDERED to the user{_RENDERED_BODY}\n\n"


@pytest.mark.parametrize("title", _HOSTILE_TITLES)
def test_hostile_title_leaves_no_note_in_either_trace(monkeypatch, title):
    seen = _redacted([{"text": f"{_ANSWER}\n\n{_tag(title)}\n\n{_TAG_Q}"}])
    summary, preview = _cloud_row(monkeypatch, seen)
    _assert_trace_clean(summary, _ANSWER)
    _assert_trace_clean(preview, _ANSWER)
    _assert_trace_clean(_reload_row(seen), _ANSWER)


@pytest.mark.parametrize("title", _HOSTILE_TITLES)
def test_hostile_title_alone_reads_as_a_chart_count(monkeypatch, title):
    seen = _redacted([{"text": _tag(title)}])
    assert _cloud_row(monkeypatch, seen)[0] == "Rendered 1 chart(s)."
    assert _reload_row(seen) == "Rendered 1 chart(s)."


@pytest.mark.parametrize("title", _HOSTILE_TITLES)
def test_hostile_title_in_enforce_mode(monkeypatch, title):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    seen = _redacted([{"text": f"{_ANSWER}\n\n{_tag(title)}"}])
    assert seen["content"][0]["text"] == (
        f"{_ANSWER}\n\n[1 chart(s) withheld and NOT delivered to the user "
        f"(integrity-enforce mode){_WITHHELD_BODY}"
    )
    summary, preview = _cloud_row(monkeypatch, seen)
    _assert_trace_clean(summary, _ANSWER)
    _assert_trace_clean(preview, _ANSWER)
    _assert_trace_clean(_reload_row(seen), _ANSWER)


def _stored(note: str) -> dict:
    """A tool result as memory kept it, with a note in the older form."""
    return {"toolUseId": "t1", "status": "success",
            "content": [{"text": f"{_ANSWER}\n\n{note}\n\n"}]}


@pytest.mark.parametrize("title", _HOSTILE_TITLES)
def test_hostile_title_in_a_stored_note_leaves_no_note_in_either_trace(monkeypatch, title):
    """Notes stored before titles were left out quoted every title; the
    trace still removes them whole."""
    rendered = (f"[2 chart(s) ALREADY RENDERED to the user: \"{title}\"; "
                f"\"Press 7: ram_speed; mm/s\"{_RENDERED_BODY_BEFORE}")
    withheld = (f"[1 chart(s) withheld and NOT delivered to the user "
                f"(integrity-enforce mode): \"{title}\"{_WITHHELD_BODY}")
    for note in (rendered, withheld):
        seen = _stored(note)
        summary, preview = _cloud_row(monkeypatch, seen)
        _assert_trace_clean(summary, _ANSWER)
        _assert_trace_clean(preview, _ANSWER)
        _assert_trace_clean(_reload_row(seen), _ANSWER)


def test_a_stored_note_with_an_escaped_quote_strips_whole():
    """The older builder wrote a double quote inside a title as ``&quot;``;
    such a stored note still strips whole and counts its charts."""
    from agents.chart_note import strip_chart_notes
    note = (f"[1 chart(s) ALREADY RENDERED to the user: "
            f"\"Unit &quot;9&quot; {_CLOSE} tail\"{_RENDERED_BODY_BEFORE}")
    assert strip_chart_notes(f"{_ANSWER}{note}") == (_ANSWER, 1, 0)
