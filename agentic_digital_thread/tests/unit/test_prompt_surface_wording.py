# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the orchestrator is told about tool charts and identifier matching.

Charts. Code owns telemetry charts: a telemetry read draws a chart only when
the user's own question asks for one (``chart_intent``), and the chart reaches
the page by itself while the model sees a bracketed note (``chart_note``)
where it was. The standing chart rule and that note give exactly one rule for
each series:

* a series a tool chart on screen covers: the model authors no chart (no
  copy, no placeholder), at most "the chart above shows";
* a telemetry or sensor-history series no tool chart shows (none was drawn,
  or a note counts some as left out): the model authors no chart and says in
  plain words it is not charted, how many from the note's counts, never by
  name (the note carries no names by design);
* any other time-series or trend from the turn's results: the model charts it
  with the documented tag, and the hook leaves that chart alone.

The reader is never told about tool results, the note, how charts are shown or
a rule about charts.

Identifier matching. The route seed's origin note lets the model explain how
systems or identifiers were matched. It does so only when the user asks, or
to disclose a translation it actually applied; the standing recovery rule
that discloses a real substitution in a footnote stays, and the note keeps
its length so seeds at their budget carry the same payload.

All payloads are synthetic, with names unrelated to any dataset.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

import agents.orchestrator as orch
from agents import chart_intent, chart_note

_MODES = ("agent", "orchestrator")
_CHART_ABOVE = "the chart above shows"
#: Words that tell a reader how the page draws charts or that a rule exists.
_PLUMBING = ("client-side", "displayed client", "the redaction applies",
             "withheld from you intentionally", "WITHHELD from your view")
#: The one rule for a telemetry series no tool chart shows.
_TELEMETRY_RULE = "A telemetry or sensor-history series with no tool chart on screen"
_NOT_CHARTED = "Say in plain words it is not charted"
_COUNTS_NOT_NAMES = "and, from a note's counts, how many; never name them."
#: What a note with omission counts tells the model to do with them.
_COUNT_CLAUSE = ("Say in plain words how many were not charted, from these counts, never "
                 "by name, and chart none of them")
#: A trend question about one machine, shaped like a workshop question whose
#: user wording sets no chart intent ("trending" is not a chart word).
_TREND_QUESTION = ("Is vibration on the PX-200 trending up over the last hour, and which "
                   "current orders are at risk if it fails?")


def _normalized(text: str) -> str:
    """``text`` with each run of whitespace read as one space, so a pin does
    not depend on where a line wraps."""
    return " ".join(text.split())


def _chart_section(mode: str) -> str:
    prompt = orch._build_orchestrator_prompt(mode)
    return _normalized(prompt[prompt.index("CHART RENDERING"):])


def _units(mode: str) -> "list[str]":
    """The prompt as its instruction units: each bullet or unindented line,
    with its wrapped continuation lines, whitespace normalized."""
    prompt = orch._build_orchestrator_prompt(mode)
    return [_normalized(u) for u in re.split(r"\n(?=- |\S)", prompt) if u.strip()]


def _sentences(unit: str) -> "list[str]":
    return [s for s in re.split(r"(?<=[.;])\s+", unit) if s]


# --------------------------------------------------------------------------- #
# The standing chart rule: one rule per series
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", _MODES)
def test_chart_rule_authors_no_chart_for_a_series_a_tool_chart_covers(mode):
    section = _chart_section(mode)
    assert ("- A tool chart on screen covers it: author no chart-block (no copy, no "
            "placeholder); at most say \"the chart above shows\".") in section
    assert ("Never mention tool results, that note, how charts are shown or any rule about "
            "charts.") in section


@pytest.mark.parametrize("mode", _MODES)
def test_chart_rule_never_explains_how_the_page_draws_charts(mode):
    section = _chart_section(mode)
    for phrase in _PLUMBING:
        assert phrase not in section, phrase


@pytest.mark.parametrize("mode", _MODES)
def test_chart_rule_asks_for_a_chart_only_for_other_series(mode):
    section = _chart_section(mode)
    assert "When you have time-series or historical data, render it as a chart" not in section
    assert ("- Any other time-series or trend from this turn's results: render a chart with "
            "this EXACT tag:") in section


@pytest.mark.parametrize("mode", _MODES)
def test_chart_rule_still_has_the_tag_and_full_series_rule(mode):
    """Guard: the model still authors charts for other series, with the
    documented tag, every point and full UTC labels."""
    section = _chart_section(mode)
    assert "<chart-block type=\"line\" title=\"CHART TITLE\"" in section
    assert "do not downsample" in section
    assert "full UTC instant" in section
    assert section.count("CHART RENDERING") == 1


def test_a_trend_question_sets_no_chart_intent_so_no_tool_chart_is_drawn(monkeypatch):
    """Guard: the case the telemetry rule owns is real. The user's trend
    question sets no chart intent, and with the bit false the telemetry
    history path returns before it builds a chart."""
    import agents.system_agent as sa
    from agents import runtime_asgi
    assert chart_intent.wants_chart(_TREND_QUESTION) is False
    assert chart_intent.turn_block(_TREND_QUESTION) is False
    built = []
    monkeypatch.setattr(sa, "_sitewise_chart_channels",
                        lambda *a, **k: built.append(a) or [])
    token = runtime_asgi.chart_intent_var.set(chart_intent.turn_intent())
    try:
        assert sa._sitewise_history_charts({"series": {}}, [], "sys-x") == ""
    finally:
        runtime_asgi.chart_intent_var.reset(token)
        chart_intent.reset_turn_intent()
    assert built == []


@pytest.mark.parametrize("mode", _MODES)
def test_a_telemetry_series_with_no_tool_chart_has_exactly_one_rule(mode):
    """For the trend question above the prompt gives one instruction: author
    no chart for the telemetry series, and say it is not charted."""
    owning = [u for u in _units(mode) if _TELEMETRY_RULE in u]
    assert len(owning) == 1
    rule = owning[0]
    assert rule.startswith(f"- {_TELEMETRY_RULE}")
    assert "author no chart-block." in rule
    assert _NOT_CHARTED in rule


@pytest.mark.parametrize("mode", _MODES)
def test_no_other_instruction_charts_telemetry_or_promises_a_chart_for_it(mode):
    """No pair of instructions contradicts the telemetry rule: the only
    instruction to author a chart is the bullet for the series the two rules
    before it leave, and no other unit tells the model to chart telemetry,
    to fetch a chart some other way, or to stay silent about a missing one."""
    units = _units(mode)
    authoring = [u for u in units if "EXACT tag" in u]
    assert len(authoring) == 1
    assert authoring[0].startswith("- Any other time-series or trend")
    telemetry_charting = [s for u in units for s in _sentences(u)
                          if "telemetry" in s.lower()
                          and ("chart-block" in s or "EXACT tag" in s or "chart tags" in s)]
    assert telemetry_charting == [f"- {_TELEMETRY_RULE} (none drawn or delivered, or a note "
                                  "counts it as left out): author no chart-block."]
    prompt = _normalized(orch._build_orchestrator_prompt(mode))
    for phrase in ("render it as a chart", "ask the owning telemetry system for a chart",
                   "do NOT build chart tags yourself", "HAND-BUILT CHARTS",
                   "say nothing of charts"):
        assert phrase not in prompt, phrase


@pytest.mark.parametrize("mode", _MODES)
def test_the_telemetry_rule_counts_left_out_series_and_never_names_them(mode):
    rule = next(u for u in _units(mode) if _TELEMETRY_RULE in u)
    assert f"{_NOT_CHARTED} {_COUNTS_NOT_NAMES}" in rule


# --------------------------------------------------------------------------- #
# The notes the hook leaves where a tool's charts were
# --------------------------------------------------------------------------- #
def test_rendered_note_says_what_the_prompt_says():
    note = chart_note.rendered_note(2)
    assert note.startswith("[2 chart(s) ALREADY RENDERED to the user. ")
    assert "Author no chart for their series" in note
    assert ("never mention this note, a tool result, how charts are shown or a rule "
            "about charts") in note
    assert _CHART_ABOVE in note
    for phrase in _PLUMBING:
        assert phrase not in note, phrase
    assert note.endswith("chart markup.]")


def test_a_note_with_no_omission_asks_for_no_count():
    """Guard: a note that left nothing out does not ask the model to report
    what was not charted."""
    assert "not charted" not in chart_note.rendered_note(3)


@pytest.mark.parametrize("counts, clause", [
    ((4, 0, 0), "; 4 more chart(s) were not drawn (chart limit)"),
    ((0, 2, 0), "; 2 series were not charted (no name recorded)"),
    ((0, 0, 5), "; 5 more asset(s) were not charted (asset limit)"),
])
def test_a_note_with_omission_counts_asks_for_the_count_and_no_chart(counts, clause):
    note = chart_note.rendered_note(2, *counts)
    assert f"{clause}. {_COUNT_CLAUSE}. Author no chart" in note
    assert chart_note.strip_chart_notes(f"x\n{note}") == ("x\n", 2, 0)


def test_notice_note_says_the_readings_are_not_charted():
    note = chart_note.notice_note([chart_note.unnamed_series_notice(3)])
    assert note.startswith('[No chart was drawn for these readings: '
                           '"3 series not charted: no name recorded."')
    assert "say in plain words they are not charted" in note
    assert "build no chart" in note
    assert "refer to one" not in note
    assert chart_note.strip_chart_notes(f"x\n{note}") == ("x", 0, 0)


@pytest.mark.parametrize("old", [
    ("[3 chart(s) ALREADY RENDERED to the user. Chart data is displayed client-side and has "
     "been withheld from you intentionally \u2014 reference the charts in prose, never "
     "attempt to reproduce or re-emit chart markup.]"),
    ("[3 chart(s) ALREADY RENDERED to the user; 1 more chart(s) were not drawn (chart "
     "limit). Author no chart for their series, and never mention this note, a tool "
     "result, how charts are shown or a rule about charts; at most say 'the chart above "
     "shows'. Never reproduce or re-emit chart markup.]"),
])
def test_rendered_notes_in_earlier_wording_still_strip(old):
    """Guard: memory holds notes written before this wording; the trace still
    removes them whole and counts their charts."""
    answer = "Bay 4 pressure rose by 3 bar."
    assert chart_note.strip_chart_notes(f"{answer}\n\n{old}") == (f"{answer}\n\n", 3, 0)


def test_a_notice_note_in_the_earlier_wording_still_strips():
    old = ('[No chart was drawn for these readings: "2 series not charted: no name '
           'recorded.". Do not describe a chart or refer to one for these readings; give '
           'the readings in prose and never attempt to reproduce or re-emit chart markup.]')
    assert chart_note.strip_chart_notes(f"Flat.\n\n{old}") == ("Flat.", 0, 0)


def test_hook_leaves_a_chart_the_model_wrote_alone():
    """Guard: charts the model authors in its own answer are never redacted."""
    tag = ("<chart-block type=\"line\" title=\"Bay 4: count per hour\" "
           "labels='[\"2026-01-02T03:00Z\"]' datasets='[{\"label\":\"n\",\"data\":[4]}]' />")
    message = {"role": "assistant", "content": [{"text": f"Counts per hour.\n{tag}"}]}
    agent = SimpleNamespace()
    orch.ChartRedactionHook()._on_message_added(SimpleNamespace(message=message, agent=agent))
    assert message["content"][0]["text"] == f"Counts per hour.\n{tag}"
    assert not getattr(agent, "_pending_chart_blocks", [])


# --------------------------------------------------------------------------- #
# How identifiers were matched
# --------------------------------------------------------------------------- #
def _headers():
    return (orch._ROUTE_SEED_HEADER, orch._ROUTE_SEED_PARTIAL_HEADER,
            *orch._ROUTE_SEED_VALUE_MAPPING_HEADERS.values())


def test_origin_note_explains_matching_only_when_asked_or_to_disclose_a_translation():
    for header in _headers():
        assert "Say how systems or identifiers were matched only if asked" in header
        assert "or to disclose a translation you applied" in header
        assert "If the user asks how systems were chosen" not in header


#: The origin note's length before the matching sentence was tightened. Seeds
#: at the route-seed budget carry the same payload only while it holds.
_ORIGIN_NOTE_LENGTH = 455


def test_origin_note_keeps_its_length():
    """Guard: the header's length decides how much payload a seed at its
    budget keeps, so rewording the note must not change it."""
    assert len(orch._route_seed_origin_note()) == _ORIGIN_NOTE_LENGTH


def test_origin_note_still_names_the_source_and_the_plain_words():
    """Guard: when asked, the answer still says it came from the knowledge
    graph lookup, and the internal label stays banned."""
    for header in _headers():
        assert "came from that knowledge graph lookup" in header
        assert 'Never say "route seed" to the user' in header


def test_a_real_substitution_is_still_disclosed_in_a_footnote():
    """Guard: the standing recovery rule the origin note defers to."""
    prompt = orch._build_orchestrator_prompt("agent")
    assert "DISCLOSE the substitution in the final answer" in prompt
    assert "NEVER substitute silently" in prompt
    assert "visually subordinate footnote AFTER the" in prompt
