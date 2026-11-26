# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The code-based AgentCore evaluator that checks the orchestrator's figures.

``agents/eval_figure_check.py`` is the Lambda body of the ``adt_figure_check``
evaluator (``stacks/_evaluations.py``). It reads one orchestrator turn (one
trace) from the AgentCore Evaluations payload and asks the app's own figure
reader (``agents/answer_figures``) whether each total or count the final
answer states is in what the turn's tools returned. Pinned here:

* a total the orchestrator added up wrong from figures an agent returned is
  FAIL, and the explanation names it;
* a figure copied from a tool result, and a correct sum of figures from two
  tool results, are PASS;
* an answer with no total or count is NO_FIGURES, with no ``value``, so it
  does not move the average;
* a figure a system agent's own engine line already named as in no result
  is FAIL even though that line writes the figure;
* ids and UUIDs in tool text never account for a figure (and without the id
  masking they would: the hazard is real);
* only the target trace's spans are read when the session holds several
  turns;
* malformed or truncated input returns the error shape, never a label;
* the handler round-trips the documented JSON contract.

Synthetic, dataset-neutral span documents of the shape AgentCore sends
(OTEL span records: ``invoke_agent`` with ``gen_ai.user.message`` and
``gen_ai.choice`` events, ``chat`` spans whose ``gen_ai.tool.message`` events
carry ``toolResult`` blocks, ``execute_tool`` spans, and ``mcp tools/call``
spans whose ``gen_ai.tool.call.result`` is the MCP result's repr). Offline.
"""
from __future__ import annotations

import json

import pytest

from agents import answer_figures
from agents import eval_figure_check as efc

TRACE = "0123456789abcdef0123456789abcdef"
OTHER_TRACE = "fedcba9876543210fedcba9876543210"

#: Six per-code figures one agent returned: they add up to 4,941.
_CODES = [("code_a", "1,200"), ("code_b", "900"), ("code_c", "800"),
          ("code_d", "741"), ("code_e", "700"), ("code_f", "600")]


# --------------------------------------------------------------------------- #
# Span builders (the shape the Evaluate API is fed).
# --------------------------------------------------------------------------- #
def _span(name, span_id, *, parent="root0000", trace=TRACE, start=1, attributes=None,
          events=None) -> dict:
    span = {"traceId": trace, "spanId": span_id, "parentSpanId": parent, "name": name,
            "kind": "INTERNAL", "startTimeUnixNano": start, "endTimeUnixNano": start + 1,
            "attributes": {"session.id": "session-test", **(attributes or {})},
            "status": {"code": "OK"}}
    if events is not None:
        span["events"] = events
    return span


def _event(name, **attributes) -> dict:
    return {"timeUnixNano": 1, "name": name, "attributes": attributes}


def _agent(answer, *, question="How many items were recorded?", span_id="agent001",
           trace=TRACE, start=10) -> dict:
    return _span("invoke_agent Strands Agents", span_id, trace=trace, start=start,
                 attributes={"gen_ai.operation.name": "invoke_agent"},
                 events=[_event("gen_ai.user.message",
                                content=json.dumps([{"text": question}])),
                         _event("gen_ai.choice", message=answer, finish_reason="end_turn")])


def _mcp_repr(texts) -> str:
    items = ", ".join(f"TextContent(type='text', text={t!r}, annotations=None, meta=None)"
                      for t in texts)
    return f"meta=None content=[{items}] structuredContent=None isError=False"


def _tool_call(tool_id, text, *, trace=TRACE, n=1, chat=True, mcp=True) -> list[dict]:
    """One system-agent call: its execute_tool span, the MCP client span under
    it, and the chat span whose tool message carries the result."""
    spans = [_span("execute_tool sys-a___query_sys_a", f"exec{n:04d}", trace=trace, start=20 + n,
                   attributes={"gen_ai.tool.call.id": tool_id,
                               "gen_ai.tool.name": "sys-a___query_sys_a"})]
    if mcp:
        spans.append(_span("mcp tools/call sys-a___query_sys_a", f"mcp0{n:04d}",
                           parent=f"exec{n:04d}", trace=trace, start=20 + n,
                           attributes={"gen_ai.tool.call.result": _mcp_repr([text])}))
    if chat:
        result = {"toolResult": {"status": "success", "toolUseId": tool_id, "content": [
            {"text": text}, {"text": f"[engine] Cite this result as [{n}]."}]}}
        spans.append(_span("chat", f"chat{n:04d}", trace=trace, start=40 + n, events=[
            _event("gen_ai.tool.message", content=json.dumps([result]))]))
    return spans


def _payload(spans, trace_ids=(TRACE,)) -> dict:
    return {"schemaVersion": "1.0", "evaluatorId": "adt_figure_check-AbCdE12345",
            "evaluatorName": "adt_figure_check", "evaluationLevel": "TRACE",
            "evaluationInput": {"sessionSpans": list(spans)},
            "evaluationReferenceInputs": [],
            "evaluationTarget": {"traceIds": list(trace_ids)}}


def _codes_text() -> str:
    return "Counts by reason code:\n" + "\n".join(f"- {c}: {v}" for c, v in _CODES)


def _answer_with_total(total: str) -> str:
    return _codes_text() + f"\n\nTotal: {total}\n"


def _check(spans, trace_ids=(TRACE,)) -> dict:
    return efc.evaluate(_payload(spans, trace_ids))


# --------------------------------------------------------------------------- #
# The verdicts.
# --------------------------------------------------------------------------- #
def test_a_wrong_sum_of_figures_an_agent_returned_fails_and_names_it():
    spans = [_agent(_answer_with_total("4,899")), *_tool_call("tool-1", _codes_text())]
    result = _check(spans)
    assert result["label"] == efc.FAIL
    assert result["value"] == 0.0
    assert "4,899" in result["explanation"]
    assert "not with the data" in result["explanation"]


def test_the_right_sum_of_the_same_listing_passes():
    spans = [_agent(_answer_with_total("4,941")), *_tool_call("tool-1", _codes_text())]
    result = _check(spans)
    assert (result["label"], result["value"]) == (efc.PASS, 1.0)
    assert "not with the data" in result["explanation"]


def test_a_figure_copied_from_a_tool_result_passes():
    spans = [_agent("Total records in the window: 4,960 [1]."),
             *_tool_call("tool-1", "The window holds 4,960 records in total.")]
    assert _check(spans)["label"] == efc.PASS


def test_a_correct_sum_of_figures_from_two_tool_results_passes():
    spans = [_agent("Total across both sites: 3,579 [1][2]."),
             *_tool_call("tool-1", "Site north recorded 1,234 items.", n=1),
             *_tool_call("tool-2", "Site south recorded 2,345 items.", n=2)]
    assert _check(spans)["label"] == efc.PASS


def test_a_sum_neither_result_supports_fails():
    spans = [_agent("Total across both sites: 3,600 [1][2]."),
             *_tool_call("tool-1", "Site north recorded 1,234 items.", n=1),
             *_tool_call("tool-2", "Site south recorded 2,345 items.", n=2)]
    result = _check(spans)
    assert result["label"] == efc.FAIL and "3,600" in result["explanation"]


def test_an_answer_with_no_total_or_count_is_no_figures_without_a_value():
    spans = [_agent("The line ran normally and no stop was recorded [1]."),
             *_tool_call("tool-1", "No stop was recorded.")]
    result = _check(spans)
    assert result["label"] == efc.NO_FIGURES
    assert "value" not in result
    assert result["explanation"]


def test_no_figures_needs_no_tool_result_at_all():
    """Nothing to compare: a truncated tool record cannot matter."""
    spans = [_agent("The line ran normally."),
             _span("execute_tool sys-a___query_sys_a", "exec0001",
                   attributes={"gen_ai.tool.call.id": "tool-1"})]
    assert _check(spans)["label"] == efc.NO_FIGURES


@pytest.mark.parametrize("tied", [False, True])
def test_a_figure_an_engine_line_named_as_in_no_result_fails(tied):
    """The agent's own figure check named 4,800; its line (and the requirement
    block under it) writes the figure, so only the flag can catch the relay."""
    found = [("4,800", [])]
    line = answer_figures.figure_line(
        found, [], tied={"4,800": [(1, answer_figures.Decimal(4960))]} if tied else None)
    block = answer_figures.requirement_block(["4,800"], False)
    text = f"Records in the window: 4,960.\n\n{line}\n\n{block}"
    assert "4,800" in text
    spans = [_agent("Total records: 4,800 [1]."), *_tool_call("tool-1", text)]
    result = _check(spans)
    assert result["label"] == efc.FAIL
    assert "4,800" in result["explanation"]
    assert "figure check" in result["explanation"]


def test_every_figure_an_engine_line_lists_is_read():
    found = [(f"{n:,}", []) for n in (1111, 2222, 3333, 4444, 5555, 6666, 7777, 8888)]
    line = answer_figures.figure_line(found, [])
    assert " and 2 more in this answer" in line
    flagged = efc._flagged_in(line)
    assert flagged == {answer_figures.Decimal(n) for n in (1111, 2222, 3333, 4444, 5555, 6666)}


def test_an_engine_line_that_only_counts_its_figures_names_none():
    line = answer_figures.FIGURE_PREFIX + answer_figures._loose_sentence([], 8)
    assert efc._flagged_in(line) == set()


def test_a_figure_named_in_model_written_prose_is_not_a_flag():
    """Only the engine prefix marks a flag (the system agent neutralizes a
    model-written copy of it before the engine lines are added)."""
    text = "Figures in no result: 4,960 in this answer is stated as a total."
    spans = [_agent("Total records: 4,960 [1]."), *_tool_call("tool-1", text)]
    assert _check(spans)["label"] == efc.PASS


_ID_TEXT = ("Run WO-4941 finished. Batch 9f1c2b3a-4941-4abc-8def-0123456789ab and batch "
            "12345678-4941-4123-8123-123456789012 were read; query id 7e641 and "
            "trace LAS8-4941 are attached. The run recorded 1,200 items.")


def test_ids_and_uuids_in_tool_text_cannot_account_for_a_figure():
    spans = [_agent("Total: 4,941 [1]."), *_tool_call("tool-1", _ID_TEXT)]
    result = _check(spans)
    assert result["label"] == efc.FAIL and "4,941" in result["explanation"]


def test_without_the_id_masking_the_ids_would_have_held_the_figure():
    """The hazard the masking closes: the app's reader takes 4941 out of
    ``WO-4941`` and a UUID's all-digit group."""
    state = {}
    answer_figures.record_result_values(state, {"content": [{"text": _ID_TEXT}]})
    values = answer_figures.result_values(state)
    assert answer_figures.unfound_figures("Total: 4,941", values, []) == []


def test_a_huge_exponent_id_cannot_hold_every_figure():
    text = "Query 7e641 timed out. Query 0e9-1 timed out. " + _codes_text()
    spans = [_agent(_answer_with_total("4,899")), *_tool_call("tool-1", text)]
    assert _check(spans)["label"] == efc.FAIL


def test_ids_in_a_json_tool_result_cannot_account_for_a_figure():
    body = {"run": "WO-4941", "batch": "9f1c2b3a-4941-4abc-8def-0123456789ab",
            "counts": {c: int(v.replace(",", "")) for c, v in _CODES}}
    spans = [_agent("Total: 4,941 [1]."), *_tool_call("tool-1", json.dumps(body))]
    assert _check(spans)["label"] == efc.FAIL


def test_a_citation_stamp_is_not_a_returned_value():
    """``[engine] Cite this result as [1].`` ends every cited result; read as
    a value it would hold any figure one off a returned one."""
    spans = [_agent("Total records: 4,961 [1]."), *_tool_call("tool-1", "4,960 rows.")]
    assert _check(spans)["label"] == efc.FAIL


def test_the_citation_stamp_pattern_is_the_orchestrators_own():
    from agents import citation_ordinals
    assert efc._STAMP.pattern == citation_ordinals._STAMP_RE.pattern
    assert efc._STAMP.fullmatch(citation_ordinals.citation_stamp(12))
    assert efc._STAMP.fullmatch(citation_ordinals.earlier_stamp(3))


def test_a_correct_total_of_many_prose_figures_needs_its_listing():
    """The reader's own rule: from plain text, a figure is held by a value, a
    pair, or the figures the answer lists next to it, never a sum of three or
    more values it does not list. So the right total of six agent figures,
    stated alone, is unaccounted (named in the docs as a limit)."""
    spans = [_agent("Total: 4,941 [1]."), *_tool_call("tool-1", _codes_text())]
    assert _check(spans)["label"] == efc.FAIL


def test_units_dates_and_plain_figures_still_read_after_masking():
    text = "Mean cycle 12h over 3.5k parts on 2026-07-20T00:00:00Z; 1,234 parts passed."
    masked = efc._without_ids(text)
    assert "12h" in masked and "3.5k" in masked and "2026-07-20T00" in masked
    assert "1,234" in masked


def test_only_the_target_trace_is_read_when_the_session_has_several_turns():
    earlier = [_agent("Total: 4,941 [1].", span_id="agent002", trace=OTHER_TRACE),
               *_tool_call("tool-9", "Total records: 4,941.", trace=OTHER_TRACE, n=9)]
    target = [_agent("Total: 4,941 [1]."), *_tool_call("tool-1", "Site north: 1,234.")]
    assert _check(earlier + target)["label"] == efc.FAIL
    assert _check(earlier + target, trace_ids=(OTHER_TRACE,))["label"] == efc.PASS


def test_the_target_traces_own_answer_is_the_one_checked():
    earlier = [_agent("Total: 7,777.", span_id="agent002", trace=OTHER_TRACE)]
    target = [_agent("Total records: 1,234 [1]."), *_tool_call("tool-1", "1,234 records.")]
    assert _check(earlier + target)["label"] == efc.PASS


def test_the_mcp_span_alone_carries_a_result():
    spans = [_agent("Total records: 4,960 [1]."),
             *_tool_call("tool-1", "4,960 records.", chat=False)]
    assert _check(spans)["label"] == efc.PASS


def test_a_continued_answer_is_checked_whole():
    """An output-cap continuation is a second top-level agent span."""
    spans = [_agent(_codes_text()), _agent("Total: 4,899", span_id="agent009", start=90),
             *_tool_call("tool-1", _codes_text())]
    result = _check(spans)
    assert result["label"] == efc.FAIL and "4,899" in result["explanation"]


def test_the_explanation_names_at_most_three_figures():
    answer = "\n".join(f"Total {k}: {v}" for k, v in
                       (("a", "1,111"), ("b", "2,903"), ("c", "4,417"), ("d", "5,555"),
                        ("e", "6,021")))
    spans = [_agent(answer), *_tool_call("tool-1", "Nothing like those.")]
    result = _check(spans)
    assert result["label"] == efc.FAIL
    assert "1,111, 2,903, 4,417 and 2 more" in result["explanation"]
    assert "5,555" not in result["explanation"]


def test_a_total_with_no_tool_result_to_compare_is_unchecked():
    result = _check([_agent("Total records: 4,960.")])
    assert result["label"] == efc.UNCHECKED and "value" not in result


def test_a_tool_result_too_long_to_read_whole_is_unchecked():
    long_text = "1 " * (efc._MAX_RESULT_CHARS // 2 + 10)
    spans = [_agent("Total records: 4,960 [1]."), *_tool_call("tool-1", long_text)]
    result = _check(spans)
    assert result["label"] == efc.UNCHECKED and "value" not in result


def test_a_figure_from_the_turns_own_prompt_is_accounted_for():
    """The prompt carries the app's preamble (metric-layer results, the route
    seed): a figure the orchestrator was handed there is not one it made up."""
    spans = [_agent("Total defects: 812.", question="Metric layer: defects = 812 (computed)."),
             *_tool_call("tool-1", "No rows matched.")]
    assert _check(spans)["label"] == efc.PASS


# --------------------------------------------------------------------------- #
# Malformed and truncated input: the error shape, never a label.
# --------------------------------------------------------------------------- #
def _good() -> dict:
    return _payload([_agent("Total records: 4,960 [1]."), *_tool_call("tool-1", "4,960 rows.")])


def _without(path):
    def edit(event):
        node = event
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]
    return edit


def _setting(path, value):
    def edit(event):
        node = event
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    return edit


def _spans_edit(change):
    def edit(event):
        event["evaluationInput"]["sessionSpans"] = change(event["evaluationInput"]["sessionSpans"])
    return edit


def _no_result_for_a_call(spans):
    return [s for s in spans if not s["name"].startswith(("chat", "mcp"))]


def _cut_tool_message(spans):
    for span in spans:
        if span["name"] == "chat":
            content = span["events"][0]["attributes"]["content"]
            span["events"][0]["attributes"]["content"] = content[: len(content) // 2]
    return spans


def _cut_mcp_only(spans):
    spans = [s for s in spans if s["name"] != "chat"]
    for span in spans:
        if span["name"].startswith("mcp"):
            value = span["attributes"]["gen_ai.tool.call.result"]
            span["attributes"]["gen_ai.tool.call.result"] = value[: len(value) // 2]
    return spans


_MALFORMED = {
    "schema version missing": _without(["schemaVersion"]),
    "schema version unknown": _setting(["schemaVersion"], "9.9"),
    "session level": _setting(["evaluationLevel"], "SESSION"),
    "no evaluation input": _without(["evaluationInput"]),
    "spans not a list": _setting(["evaluationInput", "sessionSpans"], {"a": 1}),
    "no target": _without(["evaluationTarget"]),
    "no trace ids": _setting(["evaluationTarget", "traceIds"], []),
    "two trace ids": _setting(["evaluationTarget", "traceIds"], [TRACE, OTHER_TRACE]),
    "trace id not a string": _setting(["evaluationTarget", "traceIds"], [7]),
    "target trace absent": _setting(["evaluationTarget", "traceIds"], [OTHER_TRACE]),
    "span not an object": _spans_edit(lambda spans: spans + ["junk"]),
    "events not a list": _spans_edit(lambda spans: [{**s, "events": "x"} if s["name"] == "chat"
                                                    else s for s in spans]),
    "no agent span": _spans_edit(lambda spans: [s for s in spans
                                                if not s["name"].startswith("invoke_agent")]),
    "no final answer": _spans_edit(lambda spans: [
        {**s, "events": s["events"][:1]} if s["name"].startswith("invoke_agent") else s
        for s in spans]),
    "a call with no result": _spans_edit(_no_result_for_a_call),
    "tool message cut short": _spans_edit(_cut_tool_message),
    "mcp result cut short": _spans_edit(_cut_mcp_only),
}


@pytest.mark.parametrize("case", sorted(_MALFORMED))
def test_malformed_or_truncated_input_returns_the_error_shape(case):
    event = _good()
    _MALFORMED[case](event)
    result = efc.evaluate(event)
    assert set(result) == {"errorCode", "errorMessage"}, (case, result)
    assert isinstance(result["errorCode"], str) and result["errorCode"]
    assert isinstance(result["errorMessage"], str) and result["errorMessage"]


@pytest.mark.parametrize("event", [None, [], "text", 3])
def test_a_payload_that_is_not_an_object_returns_the_error_shape(event):
    assert set(efc.handler(event, None)) == {"errorCode", "errorMessage"}


def test_an_unexpected_failure_returns_the_error_shape_without_its_text(monkeypatch):
    def boom(_event):
        raise RuntimeError("internal detail that must not leave")
    monkeypatch.setattr(efc, "evaluate", boom)
    result = efc.handler(_good(), None)
    assert set(result) == {"errorCode", "errorMessage"}
    assert "internal detail" not in result["errorMessage"]


# --------------------------------------------------------------------------- #
# The documented contract.
# --------------------------------------------------------------------------- #
def test_the_handler_round_trips_the_documented_json():
    """The doc's input shape (spanIds and an evaluatorId of its form included)
    through JSON and back; the answer is one of the documented success
    fields only, with a numeric value in [0, 1] when present."""
    event = _good()
    event["evaluationTarget"]["spanIds"] = ["span123"]
    event["evaluatorId"] = "my-evaluator-abc1234567"
    result = json.loads(json.dumps(efc.handler(json.loads(json.dumps(event)), None)))
    assert set(result) <= {"label", "value", "explanation"}
    assert result["label"] in efc.LABELS
    assert isinstance(result["value"], float) and 0.0 <= result["value"] <= 1.0
    assert isinstance(result["explanation"], str)


def test_every_label_is_documented_and_valued_as_promised():
    assert efc.LABELS == (efc.PASS, efc.FAIL, efc.NO_FIGURES, efc.UNCHECKED)
    cases = {
        efc.PASS: [_agent("Total records: 4,960 [1]."), *_tool_call("tool-1", "4,960 rows.")],
        efc.FAIL: [_agent("Total records: 4,961 [1]."), *_tool_call("tool-1", "4,960 rows.")],
        efc.NO_FIGURES: [_agent("Nothing to count.")],
        efc.UNCHECKED: [_agent("Total records: 4,960.")],
    }
    values = {label: _check(spans).get("value") for label, spans in cases.items()}
    assert values == {efc.PASS: 1.0, efc.FAIL: 0.0, efc.NO_FIGURES: None, efc.UNCHECKED: None}


def test_the_evaluator_name_matches_the_cfn_pattern():
    import re
    assert re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,47}", efc.EVALUATOR_NAME)


# --------------------------------------------------------------------------- #
# Counts stated without a total word (round 2).
#
# Measured on real answers: the orchestrator writes counts as "N <count
# noun>" and "<label naming a count noun>: N", mostly with no total, sum or
# count word, so the app's reader (answer_figures.stated_figures) read almost
# none of them and a wrong figure in those shapes passed. The ONE reader now
# lives in answer_figures (build brief G), shared by the evaluator and the
# engine's in-app check, with the reader's own count nouns, number spelling and
# skip filters. Each shape below is the measured one, with neutral content.
# --------------------------------------------------------------------------- #
_COUNT_SHAPES = {
    "figure then count noun, words between": (
        "The source shows **{n} ZN7 item records in state CLOSED** [1].", "4,941"),
    "label naming records, then a filter": (
        "- Records matching part name **WidgetAlpha**: **{n}** [1]", "318"),
    "label naming a count noun": ("- **CLOSED orders: {n}** (verified total)", "8,182"),
    "figure then count noun in a sentence": (
        "The north cell completed exactly **{n}** item records between 2031-03-04 and "
        "2031-03-11.", "2,604,551"),
    "label naming a count noun at a place": (
        "- **Distinct item records at SITE9: {n}** [1]", "381,906"),
    "label naming a count noun, then a dated qualifier": (
        "SITE9 closed orders — 2031-03-04 (working day): {n}", "1,207,743"),
}


def _plus_42(spelling: str) -> str:
    return f"{int(spelling.replace(',', '')) + 42:,}"


@pytest.mark.parametrize("shape", sorted(_COUNT_SHAPES))
def test_a_count_stated_without_a_total_word_is_read(shape):
    template, figure = _COUNT_SHAPES[shape]
    answer = template.format(n=figure)
    assert figure in efc.read_figures(answer)
    # The count-shape reader now lives in answer_figures and is shared, so the
    # engine's in-app reader reads these shapes too (build brief G).
    assert figure in answer_figures.stated_figures(answer)


@pytest.mark.parametrize("shape", sorted(_COUNT_SHAPES))
def test_a_wrong_count_in_each_shape_fails_and_names_it(shape):
    template, figure = _COUNT_SHAPES[shape]
    wrong = _plus_42(figure)
    spans = [_agent(template.format(n=wrong)),
             *_tool_call("tool-1", f"The query returned {figure} matching rows.")]
    result = _check(spans)
    assert result["label"] == efc.FAIL, result
    assert wrong in result["explanation"]


@pytest.mark.parametrize("shape", sorted(_COUNT_SHAPES))
def test_a_correct_count_in_each_shape_passes(shape):
    template, figure = _COUNT_SHAPES[shape]
    spans = [_agent(template.format(n=figure)),
             *_tool_call("tool-1", f"The query returned {figure} matching rows.")]
    assert _check(spans)["label"] == efc.PASS


def test_one_changed_count_among_correct_ones_fails_naming_only_it():
    lines = [template.format(n=figure) for template, figure in _COUNT_SHAPES.values()]
    tool = " ".join(f"Result {k}: {figure} rows." for k, (_t, figure) in
                    enumerate(_COUNT_SHAPES.values(), 1))
    assert _check([_agent("\n".join(lines)), *_tool_call("tool-1", tool)])["label"] == efc.PASS
    changed = "\n".join(lines).replace("8,182", "8,224")
    result = _check([_agent(changed), *_tool_call("tool-1", tool)])
    assert result["label"] == efc.FAIL
    assert "8,224" in result["explanation"]
    assert all(f not in result["explanation"] for _t, f in _COUNT_SHAPES.values())


_NOT_A_STATED_COUNT = {
    "percentage": "12% of records failed review.",
    "percentage in words": "12 percent of records failed review.",
    "date": "The 2026-07-14 records were kept.",
    "average": "An average 54 records arrived each day.",
    "rate": "The line moved 54 records per hour.",
    "rate with a slash": "The line moved 54 records/hour.",
    "identifier before the figure": "Batch ID 4941 records were reviewed.",
    "identifier label": "- Order ID: 4941",
    "identifier label naming a number": "- Order number: 4941",
    "approximation": "About 600 records were reviewed.",
    "approximation sign": "~600 records were reviewed.",
    "rough figure": "Roughly 600 records were reviewed.",
    "comparison": "More than 600 records were reviewed.",
    "decimal": "Each batch held 3.5 records.",
    "decimal label": "- Mean records: 3.5",
    "signed": "That is +42 records versus the week before.",
    "negative": "That is -42 records versus the week before.",
    "time": "The cell ran 12 hours of records.",
    "another plural counted": "3 sites reported item records.",
    "net change label": "- Net records: 42",
    "clock time after a label": "Item records in the window 2031-03-04 00:00 UTC [1].",
}


@pytest.mark.parametrize("case", sorted(_NOT_A_STATED_COUNT))
def test_the_skip_filters_hold_for_the_count_shapes(case):
    answer = _NOT_A_STATED_COUNT[case]
    assert efc.read_figures(answer) == []
    spans = [_agent(answer), *_tool_call("tool-1", "Nothing like that.")]
    assert _check(spans)["label"] == efc.NO_FIGURES


def test_a_figure_both_readers_read_is_read_once():
    answer = "Total records: 4,960\n- Item records: 4,960"
    assert efc.read_figures(answer) == ["4,960"]


def test_a_count_shape_figure_held_by_its_listing_passes():
    """1,507 is no value and no pair of them; it is the sum the answer lists."""
    answer = "- Item records: 1,507\n  - zone_n: 1,000\n  - zone_s: 500\n  - zone_w: 7"
    spans = [_agent(answer), *_tool_call("tool-1", "zone_n 1,000; zone_s 500; zone_w 7.")]
    assert _check(spans)["label"] == efc.PASS
    unlisted = "- Item records: 1,507"
    assert _check([_agent(unlisted), *_tool_call("tool-1", "zone_n 1,000; zone_s 500; "
                                                           "zone_w 7.")])["label"] == efc.FAIL


def _cut_table() -> str:
    return json.dumps({"columns": ["zone", "n"], "rows": [["north", 1200], ["south", 800]],
                       "row_count": 2, "truncated": True, "partial_reason": "row_limit",
                       "_adt_executed_query": "SELECT zone, COUNT(*) AS n FROM t GROUP BY zone"})


def test_past_a_cut_a_count_below_what_was_returned_fails():
    spans = [_agent("The source shows 1,500 item records [1]."),
             *_tool_call("tool-1", _cut_table())]
    result = _check(spans)
    assert result["label"] == efc.FAIL and "1,500" in result["explanation"]


def test_past_a_cut_a_count_the_cut_result_could_reach_is_unchecked():
    spans = [_agent("The source shows 2,500 item records [1]."),
             *_tool_call("tool-1", _cut_table())]
    assert _check(spans)["label"] == efc.UNCHECKED


# --------------------------------------------------------------------------- #
# A pair holds a figure only from counts the results state (round 3).
#
# System-agent results are prose, so every number in them (a percentage, a
# line or station number, minutes, a small count) was a pair operand, and a
# figure off by any such number passed: on real turns a wrong total passed as
# the right one plus a "42%" in the same result. Percentages are blanked from
# tool results, and a figure
# held ONLY by a sum or difference of two returned values passes only when
# both are counts a result states (count cells of a structured result, the
# figures the evaluator reads from a result's prose, an engine total).
# Direct support (any number in a result, the prompt, a listed sum) is kept.
# --------------------------------------------------------------------------- #
def test_a_figure_off_by_a_percentage_a_result_writes_fails():
    tool = ("- CLOSED orders: 8,140 (verified total)\n"
            "- ZONE_A: 3,310 (42% of all closings)\n- ZONE_B: 1,702")
    result = _check([_agent("- **CLOSED orders: 8,182** (verified total)"),
                     *_tool_call("tool-1", tool)])
    assert result["label"] == efc.FAIL and "8,182" in result["explanation"]
    right = _check([_agent("- **CLOSED orders: 8,140** (verified total)"),
                    *_tool_call("tool-1", tool)])
    assert right["label"] == efc.PASS


@pytest.mark.parametrize("written", ["42% of orders closed late",
                                     "42 % of orders closed late",
                                     "42 percent of orders closed late",
                                     "42 per cent of orders closed late",
                                     "42 pct of orders closed late",
                                     "between 40-42% of orders closed late",
                                     "orders closed late rose (42%)"])
def test_a_percentage_in_a_result_never_holds_a_figure(written):
    spans = [_agent("Late orders: 42 [1]."), *_tool_call("tool-1", f"Of 600 orders, {written}.")]
    result = _check(spans)
    assert result["label"] == efc.FAIL and "42" in result["explanation"]


def test_a_percentage_in_a_json_result_string_never_holds_a_figure():
    body = {"late_orders_note": "42% of orders closed late", "orders": 600}
    spans = [_agent("Late orders: 42 [1]."), *_tool_call("tool-1", json.dumps(body))]
    assert _check(spans)["label"] == efc.FAIL


def test_blanking_a_percentage_keeps_the_numbers_around_it():
    masked = efc._result_text("3,310 orders (42% of all) and 3.5% late; 1,200 orders open.")
    assert "42" not in masked and "3.5" not in masked
    assert "3,310 orders" in masked and "1,200 orders" in masked


def test_a_correct_difference_of_two_stated_counts_passes():
    spans = [_agent("Open orders: 1,200 [1]."),
             *_tool_call("tool-1", "Total orders: 5,000. Closed orders: 3,800.")]
    assert _check(spans)["label"] == efc.PASS


def test_a_correct_sum_of_counts_stated_in_two_results_passes():
    spans = [_agent("Total closed orders: 3,579 [1][2]."),
             *_tool_call("tool-1", "- North closed orders: 1,234", n=1),
             *_tool_call("tool-2", "The south site closed 2,345 orders.", n=2)]
    assert _check(spans)["label"] == efc.PASS


@pytest.mark.parametrize("tool, total", [
    ("Line 7 produced 1,200 item records.", "1,207"),
    ("Station 12 logged 1,200 item records.", "1,212"),
    ("The 1,200 item records took 15 minutes to load.", "1,215"),
    ("Over 3 hours the cell wrote 1,200 item records.", "1,203"),
    ("Shift 2 closed 1,200 orders.", "1,198"),
])
def test_a_sum_with_a_line_station_or_minute_operand_fails(tool, total):
    spans = [_agent(f"Total: {total} [1]."), *_tool_call("tool-1", tool)]
    result = _check(spans)
    assert result["label"] == efc.FAIL and total in result["explanation"]


def test_a_count_written_without_its_own_count_noun_is_not_a_pair_operand():
    """The cost of the count-only pair rule: in "1,200 and 800 records" only
    800 is read as a count, so even a correct sum of the two fails unless
    the answer lists them beside it. Pinned so a change to it is deliberate."""
    tool = "Zone north 1,200 and zone south 800 records."
    assert _check([_agent("Total records: 2,000 [1]."), *_tool_call("tool-1", tool)])[
        "label"] == efc.FAIL
    listed = "- zone north: 1,200\n- zone south: 800\n\nTotal records: 2,000"
    assert _check([_agent(listed), *_tool_call("tool-1", tool)])["label"] == efc.PASS


def _table(columns, rows, sql=None) -> str:
    body = {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}
    if sql:
        body["_adt_executed_query"] = sql
    return json.dumps(body)


_SQL = "SELECT zone, COUNT(*) AS n FROM item_log GROUP BY zone"


def test_count_cells_of_structured_results_are_pair_operands():
    north = _table(["zone", "n"], [["north", 1200], ["south", 800]], _SQL)
    east = _table(["zone", "n"], [["east", 300], ["west", 200]], _SQL)
    spans = [_agent("Records in north and east: 1,500 [1][2]."),
             *_tool_call("tool-1", north, n=1), *_tool_call("tool-2", east, n=2)]
    assert _check(spans)["label"] == efc.PASS
    both = [_agent("Total records: 2,500 [1][2]."),
            *_tool_call("tool-1", north, n=1), *_tool_call("tool-2", east, n=2)]
    assert _check(both)["label"] == efc.PASS


def test_a_key_cell_of_a_structured_result_is_not_a_pair_operand():
    sql = "SELECT line_no, COUNT(*) AS n FROM item_log GROUP BY line_no"
    spans = [_agent("Total records: 2,007 [1]."),
             *_tool_call("tool-1", _table(["line_no", "n"], [[7, 1200], [9, 800]], sql))]
    result = _check(spans)
    assert result["label"] == efc.FAIL and "2,007" in result["explanation"]


def test_without_a_statement_a_count_named_column_is_a_pair_operand():
    a = _table(["line_no", "record_count"], [[7, 1200], [9, 800]])
    b = _table(["line_no", "record_count"], [[4, 300], [5, 200]])
    assert _check([_agent("Records on lines 7 and 4: 1,500 [1][2]."),
                   *_tool_call("tool-1", a, n=1), *_tool_call("tool-2", b, n=2)])[
        "label"] == efc.PASS
    assert _check([_agent("Records on lines 7 and 4: 1,207 [1][2]."),
                   *_tool_call("tool-1", a, n=1), *_tool_call("tool-2", b, n=2)])[
        "label"] == efc.FAIL


def test_a_count_named_json_field_is_a_pair_operand_and_another_is_not():
    a = json.dumps({"station": 12, "record_count": 1200})
    b = json.dumps({"station": 4, "record_count": 300})
    assert _check([_agent("Total records: 1,500 [1][2]."),
                   *_tool_call("tool-1", a, n=1), *_tool_call("tool-2", b, n=2)])[
        "label"] == efc.PASS
    assert _check([_agent("Total records: 1,212 [1][2]."),
                   *_tool_call("tool-1", a, n=1), *_tool_call("tool-2", b, n=2)])[
        "label"] == efc.FAIL


def _engine_totals_line(counts, day: str) -> str:
    from agents.response_integrity_query import bounded_query_result
    sql = (f"SELECT k, COUNT(*) AS n FROM item_log WHERE loaded_at >= TIMESTAMP '{day} 00:00:00' "
           "GROUP BY k")
    body = {**bounded_query_result(["k", "n"], [[f"k_{i}", c] for i, c in enumerate(counts)],
                                   False), "_adt_executed_query": sql}
    line = answer_figures.totals_line([{"query_text": sql,
                                        "totals": answer_figures.totals_record(body)}])
    assert line.startswith(answer_figures.TOTALS_PREFIX)
    return line


def test_engine_totals_are_pair_operands():
    one = _engine_totals_line([1000, 150, 7], "2031-03-04")
    two = _engine_totals_line([2000, 229], "2031-03-05")
    assert "COUNT(*) 1,157" in one and "COUNT(*) 2,229" in two
    spans = [_agent("Total records across both days: 3,386 [1][2]."),
             *_tool_call("tool-1", "The query ran.\n\n" + one, n=1),
             *_tool_call("tool-2", "The query ran.\n\n" + two, n=2)]
    assert _check(spans)["label"] == efc.PASS
    assert efc._engine_totals(one) == {answer_figures.Decimal(1157)}


def test_engine_totals_in_a_figure_line_are_pair_operands():
    from agents.response_integrity_query import bounded_query_result
    sql = "SELECT k, COUNT(*) AS n FROM item_log GROUP BY k"
    body = {**bounded_query_result(["k", "n"], [["a", 1000], ["b", 157]], False),
            "_adt_executed_query": sql}
    statements = [{"query_text": sql, "totals": answer_figures.totals_record(body)}]
    line = answer_figures.figure_line([("4,800", [])], statements)
    assert "Engine totals" in line
    assert efc._engine_totals(line) == {answer_figures.Decimal(1157)}


def test_a_figure_an_engine_line_flagged_is_not_a_pair_operand():
    """A system agent wrote 4,800 and its own figure check named it as in no
    result; 4,800 + 160 must not account for the orchestrator's 4,960."""
    line = answer_figures.figure_line([("4,800", [])], [])
    tool = f"Total records: 4,800\nOther records: 160\n\n{line}"
    result = _check([_agent("Total records in the window: 4,960 [1]."),
                     *_tool_call("tool-1", tool)])
    assert result["label"] == efc.FAIL and "4,960" in result["explanation"]


def test_a_result_cut_short_gives_no_pair_operand_past_the_cut():
    """Past a cut the reader checks against the complete results only; a cut
    result's counts are not pair operands there either."""
    cut = json.dumps({"columns": ["zone", "n"], "rows": [["north", 1200], ["south", 800]],
                      "row_count": 2, "truncated": True, "partial_reason": "row_limit",
                      "_adt_executed_query": _SQL})
    whole = "The east site closed 300 item records."
    spans = [_agent("The source shows 1,500 item records [1][2]."),
             *_tool_call("tool-1", cut, n=1), *_tool_call("tool-2", whole, n=2)]
    result = _check(spans)
    assert result["label"] == efc.FAIL and "1,500" in result["explanation"]


# --------------------------------------------------------------------------- #
# Count columns under a multi-word header, and rate labels (round 3).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("header", ["Record Count", "Production record count", "Item records"])
def test_a_table_under_a_multi_word_count_header_is_read(header):
    answer = f"| Facility | {header} |\n|---|---|\n| SITE1 | 1,234 |\n| SITE2 | 5,678 |"
    assert {"1,234", "5,678"} <= set(efc.read_figures(answer))
    tool = "SITE1 holds 1,234 rows. SITE2 holds 5,678 rows."
    assert _check([_agent(answer), *_tool_call("tool-1", tool)])["label"] == efc.PASS
    wrong = answer.replace("5,678", "5,720")
    result = _check([_agent(wrong), *_tool_call("tool-1", tool)])
    assert result["label"] == efc.FAIL and "5,720" in result["explanation"]


@pytest.mark.parametrize("header", ["Records per hour", "Record ID", "% of records", "Facility",
                                    "Avg records", "Records/hour"])
def test_a_table_under_another_header_is_not_read(header):
    answer = f"| Line | {header} |\n|---|---|\n| L1 | 1,234 |\n| L2 | 5,678 |"
    assert efc.read_figures(answer) == []


@pytest.mark.parametrize("label", ["- Records per hour: 54", "- Records/hour: 54",
                                   "- Records (per hour): 54"])
def test_a_rate_label_is_not_read(label):
    assert efc.read_figures(label) == []


# --------------------------------------------------------------------------- #
# Round 4 (review E-r1).
#
# E3: a tool result whose call the trace does not record means the input was
# cut short, never evidence: an MCP result with no present parent
# ``execute_tool`` span, or a tool message whose ``toolUseId`` no call has.
# --------------------------------------------------------------------------- #
def _incomplete(spans) -> bool:
    result = _check(spans)
    return result == {"errorCode": efc.INCOMPLETE, "errorMessage": result.get("errorMessage")} \
        and bool(result["errorMessage"])


def test_an_mcp_result_whose_parent_call_is_missing_is_incomplete():
    """The reviewer's case: the answer copies the orphan result word for word."""
    spans = [_agent("Total records: 123."),
             *[s for s in _tool_call("tool-1", "Total records: 123.", chat=False)
               if not s["name"].startswith("execute_tool")]]
    assert _incomplete(spans)


def test_a_tool_message_whose_call_is_not_recorded_is_incomplete():
    spans = [_agent("Total records: 123."),
             *[s for s in _tool_call("tool-1", "Total records: 123.", mcp=False)
               if s["name"] == "chat"]]
    assert _incomplete(spans)


def test_an_unrecorded_result_beside_a_complete_call_is_incomplete():
    extra = [s for s in _tool_call("tool-9", "Total records: 123.", n=9, mcp=False)
             if s["name"] == "chat"]
    spans = [_agent("Total records: 123."), *_tool_call("tool-1", "Other rows: 7."), *extra]
    assert _incomplete(spans)


def test_a_call_without_its_result_is_incomplete():
    spans = [_agent("Total records: 123."),
             *[s for s in _tool_call("tool-1", "Total records: 123.")
               if s["name"].startswith("execute_tool")]]
    assert _incomplete(spans)


def test_a_result_whose_call_is_recorded_only_as_a_tool_use_block_is_read():
    choice = _event("gen_ai.choice", message=json.dumps(
        [{"toolUse": {"toolUseId": "tool-1", "name": "sys-a___query_sys_a", "input": {}}}]))
    call = _span("chat", "chat0000", start=15, events=[choice])
    result = [s for s in _tool_call("tool-1", "Total records: 123.", mcp=False)
              if s["name"] == "chat"]
    assert _check([_agent("Total records: 123."), call, *result])["label"] == efc.PASS


@pytest.mark.parametrize("dropped, incomplete", [
    (("execute_tool",), True), (("mcp",), False), (("chat",), False),
    (("mcp", "chat"), True), (("execute_tool", "mcp"), True), (("execute_tool", "chat"), True)])
def test_whole_spans_cut_from_a_trace(dropped, incomplete):
    """AgentCore may cut ``sessionSpans`` short: without a call's span its
    result has no call, and without both result spans the call has none."""
    spans = [_agent("Total across both sites: 3,579 [1][2]."),
             *_tool_call("tool-1", "Site north recorded 1,234 item records.", n=1),
             *[s for s in _tool_call("tool-2", "Site south recorded 2,345 item records.", n=2)
               if not s["name"].startswith(dropped)]]
    if incomplete:
        assert _incomplete(spans)
    else:
        assert _check(spans)["label"] == efc.PASS


# --------------------------------------------------------------------------- #
# E4: a list's length never holds a figure; a count the result states does.
# --------------------------------------------------------------------------- #
def _objects(n: int) -> list[dict]:
    return [{"name": f"item {chr(97 + i % 26)}", "state": "open"} for i in range(n)]


@pytest.mark.parametrize("body, total", [
    ({"items": _objects(100)}, "99"),
    ({"pages": [_objects(10) for _ in range(10)]}, "50"),
    ({"columns": ["name", "state"], "rows": [[o["name"], o["state"]] for o in _objects(100)]},
     "99"),
])
def test_a_figure_below_a_lists_length_is_not_accounted_for(body, total):
    spans = [_agent(f"Total records: {total} [1]."), *_tool_call("tool-1", json.dumps(body))]
    result = _check(spans)
    assert result["label"] == efc.FAIL and f"unaccounted for: {total}." in result["explanation"]


def test_a_count_the_structured_result_states_holds_the_figure():
    body = {"items": _objects(100), "record_count": 100}
    spans = [_agent("Total records: 100 [1]."), *_tool_call("tool-1", json.dumps(body))]
    assert _check(spans)["label"] == efc.PASS


# --------------------------------------------------------------------------- #
# E5: a count of one ("1 record") is a stated figure and a pair operand.
# --------------------------------------------------------------------------- #
def test_a_count_of_one_is_read():
    assert efc.read_figures("Exactly 1 record matched.") == ["1"]
    assert efc.read_figures("- **1 open order** was found [1].") == ["1"]


def test_a_wrong_count_of_one_fails():
    spans = [_agent("Exactly 1 record matched [1]."), *_tool_call("tool-1", "No records matched.")]
    result = _check(spans)
    assert result["label"] == efc.FAIL and "unaccounted for: 1." in result["explanation"]


def test_a_right_count_of_one_passes():
    spans = [_agent("Exactly 1 record matched [1]."), *_tool_call("tool-1", "1 record matched.")]
    assert _check(spans)["label"] == efc.PASS


def test_a_count_of_one_in_a_result_is_a_pair_operand():
    tool = "1 record was rejected. Accepted records: 1,233."
    assert _check([_agent("Total records: 1,234 [1]."), *_tool_call("tool-1", tool)])[
        "label"] == efc.PASS
    assert _check([_agent("Total records: 1,235 [1]."), *_tool_call("tool-1", tool)])[
        "label"] == efc.FAIL


@pytest.mark.parametrize("text", ["The scan found 75 defect codes.",
                                  "A 1,000 record limit applies.",
                                  "About 1 record a day arrives.", "Exactly 1.5 records matched."])
def test_a_singular_count_noun_is_read_only_after_one(text):
    assert efc.read_figures(text) == []
