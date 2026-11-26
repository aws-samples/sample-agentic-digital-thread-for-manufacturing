# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Server-delivered answers drop the model's between-tool narration.

Observed (workshop rehearsal, 2026-10-14, Quick path): answers relayed to
AWS Quick started with planning lines the model wrote before its next
tool call, glued onto the real answer: "Now I need to find all other
batches ... receive those batches.## Recall Picture", "Now I need the
run_number ... for all 5 runs.## Supplier-Lot Traceability".

Cause: the orchestrator streams its text in runs separated by tool calls.
The web chat already rebuilds the answer from those runs
(``AnswerSegments`` + ``selectFinalAnswer``: the longest run plus anything
after it, with charts and the FOLLOWUPS block reattached), but every
server-side fold (the async ask the Quick bridge polls, ``/api/ask/sync``
for the Quick Lambda proxy, scheduled prompts) joined every text token of
the turn. ``agents/answer_runs.py`` is the server copy of the chat's rule;
parity with ``lib/analysis-util.js`` is pinned through node.

No text scrubbing: runs are split only at tool calls.

Offline: the chat client is stubbed.
"""
from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 - fixed internal argv lists only
import types
from pathlib import Path

import pytest

import agents.server as server
from agents.events import DoneEvent, MessageEvent, MetadataEvent, ToolResultEvent, ToolUseEvent

_ROOT = Path(__file__).resolve().parents[2]
_UTIL_JS = _ROOT / "frontend" / "src" / "lib" / "analysis-util.js"
_NODE = shutil.which("node")
_needs_node = pytest.mark.skipif(_NODE is None, reason="node not available")

PLANNING = "Now query ERP shipment_items with these run_numbers as batch_ids."
ANSWER = ("## Customers who received parts from those batches\n\n"
          + "| Customer | Units |\n|---|---|\n| A | 12 |\n| B | 30 |\n" * 6
          + "\n**Total: 110 units** [3]\n")
TAG = '<chart-block type="line" title="Coolant" labels=\'["a","b"]\' datasets=\'[{"data":[1,2]}]\' />'
TAG_OTHER = '<chart-block type="line" title="Coolant" labels=\'["c","d"]\' datasets=\'[{"data":[3,4]}]\' />'


def _runs():
    import importlib
    return importlib.import_module("agents.answer_runs")


def _tool(name: str, tid: str) -> ToolUseEvent:
    return ToolUseEvent(tool_name=name, tool_use_id=tid)


def _chat_v1_stream(planning: str = PLANNING, answer: str = ANSWER) -> list:
    """The 2026-10-14 chat-v1 event order (route seed, four calls, the
    planning line, a fifth call, the answer)."""
    events = [_tool("explore_graph", "mode-a-route-seed")]
    for i, name in enumerate(("erp___query_erp", "mes-ind___query_mes_ind",
                              "erp___query_erp", "mes-ind___query_mes_ind")):
        events += [_tool(name, f"t{i}"), ToolResultEvent(tool_name=name, tool_use_id=f"t{i}")]
    events += [MessageEvent(planning), _tool("erp___query_erp", "t9"),
               ToolResultEvent(tool_name="erp___query_erp", tool_use_id="t9")]
    events += [MessageEvent(answer[:40]), MessageEvent(answer[40:]),
               MetadataEvent(data={"intelligence_summary": {}}), DoneEvent()]
    return events


# --------------------------------------------------------------------------- #
# The selection rule is the chat's rule (node parity)
# --------------------------------------------------------------------------- #
def _js_select(segments):
    script = (
        f"const m=require({json.dumps(str(_UTIL_JS))});"
        "const f=m.selectFinalAnswer||globalThis.selectFinalAnswer;"
        f"process.stdout.write(JSON.stringify(f({json.dumps(segments)})));"
    )
    out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, check=True)  # nosec B603 - fixed argv list, internal inputs
    return json.loads(out.stdout)


_PARITY_CASES = [
    [],
    ["   ", "\n"],
    ["only run"],
    [PLANNING, ANSWER],
    ["Let me check MES.", "Now ERP.", ANSWER],
    [ANSWER, "Two correlations saved."],
    ["short", ANSWER, "tail one", "tail two"],
    ["same", "same"],
    ["  padded answer run that wins  ", "x"],
]


@_needs_node
@pytest.mark.parametrize("segments", _PARITY_CASES)
def test_select_final_answer_matches_the_chat(segments):
    assert _runs().select_final_answer(segments) == _js_select(segments)


# --------------------------------------------------------------------------- #
# AnswerRuns mirrors the chat's AnswerSegments
# --------------------------------------------------------------------------- #
def test_planning_run_before_the_answer_is_dropped():
    runs = _runs().AnswerRuns()
    runs.push_text(PLANNING)
    runs.tool_boundary()
    runs.push_text(ANSWER)
    out = runs.finalize()
    assert PLANNING not in out
    assert out == ANSWER.strip()


def test_report_before_a_housekeeping_call_keeps_its_epilogue():
    runs = _runs().AnswerRuns()
    runs.push_text(ANSWER)
    runs.tool_boundary()
    runs.push_text("Correlation saved.")
    out = runs.finalize()
    assert out.startswith(ANSWER.strip()[:30]) and out.endswith("Correlation saved.")


def test_a_chart_from_a_dropped_run_is_put_back_above_the_answer():
    runs = _runs().AnswerRuns()
    runs.push_text(f"{TAG}\n\n{PLANNING}")
    runs.tool_boundary()
    runs.push_text(ANSWER)
    out = runs.finalize()
    assert out.startswith(TAG)
    assert PLANNING not in out and ANSWER.strip() in out


def test_charts_split_across_tokens_are_harvested_once_each():
    runs = _runs().AnswerRuns()
    runs.push_text(TAG[:20])
    runs.push_text(TAG[20:] + "\n")
    runs.tool_boundary()
    runs.push_text(f"{TAG}\n{TAG_OTHER}\n")
    runs.tool_boundary()
    runs.push_text(ANSWER)
    out = runs.finalize()
    assert out.count("<chart-block") == 2
    assert out.index(TAG) < out.index(TAG_OTHER) < out.index("## Customers")


def test_a_chart_already_in_the_answer_is_not_repeated():
    runs = _runs().AnswerRuns()
    runs.push_text("short")
    runs.tool_boundary()
    runs.push_text(ANSWER + "\n" + TAG)
    out = runs.finalize()
    assert out.count("<chart-block") == 1 and not out.startswith("<chart-block")


def test_followups_from_a_short_trailing_run_are_reattached():
    runs = _runs().AnswerRuns()
    runs.push_text(ANSWER)
    runs.tool_boundary()
    runs.push_text("Saved.")
    runs.tool_boundary()
    runs.push_text("---FOLLOWUPS---\nQ1: What next?")
    out = runs.finalize()
    assert out.count("---FOLLOWUPS---") == 1 and out.endswith("Q1: What next?")


def test_followups_from_a_dropped_leading_run_use_the_last_block():
    runs = _runs().AnswerRuns()
    runs.push_text("---FOLLOWUPS---\nQ1: stale")
    runs.tool_boundary()
    runs.push_text(ANSWER)
    out = runs.finalize()
    assert out == ANSWER.strip() + "\n\n---FOLLOWUPS---\nQ1: stale"


def test_whitespace_runs_and_an_empty_stream():
    runs = _runs().AnswerRuns()
    runs.push_text("  \n")
    runs.tool_boundary()
    assert runs.finalize() == ""
    runs = _runs().AnswerRuns()
    assert runs.finalize() == ""


def test_answer_text_folds_message_and_tool_use_events_only():
    text = _runs().answer_text(_chat_v1_stream())
    assert PLANNING not in text and text == ANSWER.strip()


# --------------------------------------------------------------------------- #
# Every server-side answer fold uses it
# --------------------------------------------------------------------------- #
def test_async_ask_fold_drops_the_planning_line():
    """The Quick bridge path (``/api/ask/async`` -> ``/api/ask/result``)."""
    text, category, action = server._collect_ask_stream(_chat_v1_stream())
    assert PLANNING not in text
    assert text == ANSWER.strip()
    assert category is None and action is None


def test_async_ask_record_relays_the_clean_answer():
    record, reasons = server._ask_job_record(*server._collect_ask_stream(_chat_v1_stream()))
    assert record["status"] == "done" and reasons == []
    assert PLANNING not in record["answer"]
    assert record["answer"].startswith("## Customers who received parts")


class _Client:
    """Chat client stand-in replaying the chat-v1 stream."""

    def __init__(self, *a, **k):
        pass

    def stream(self, **_kwargs):
        yield from _chat_v1_stream()


def test_sync_ask_cloud_drops_the_planning_line(monkeypatch):
    import agents.chat_client as chat_client
    monkeypatch.setattr(chat_client, "AgentCoreChatClient", _Client)
    answer = server._ask_sync_cloud("q", "sync-1", "u")["answer"]
    assert PLANNING not in answer and answer == ANSWER.strip()


def test_local_sync_answer_drops_the_planning_line(monkeypatch):
    import agents.chat_client as chat_client
    monkeypatch.setattr(chat_client, "LocalChatClient", _Client)
    answer = server._local_sync_integrity_answer("q", "sync-1", "u")
    assert PLANNING not in answer and answer == ANSWER.strip()


def test_cloud_scheduled_prompt_drops_the_planning_line(monkeypatch):
    import agents.chat_client as chat_client
    monkeypatch.setattr(server, "get_config", lambda: types.SimpleNamespace(has_runtime=True))
    monkeypatch.setattr(chat_client, "AgentCoreChatClient", _Client)
    answer = server._execute_prompt_admitted("daily briefing")
    assert PLANNING not in answer and answer == ANSWER.strip()


def test_a_single_run_answer_is_byte_identical_to_the_old_join():
    """No tool boundary inside the text: the fold changes nothing."""
    events = [_tool("explore_graph", "seed"), _tool("erp___query_erp", "a"),
              MessageEvent("widget "), MessageEvent("answer"), DoneEvent()]
    text, _, _ = server._collect_ask_stream(events)
    assert text == "widget answer"


# --------------------------------------------------------------------------- #
# The answer starts after the last tool call that reads data
# --------------------------------------------------------------------------- #
# A longer planning run before the last data read, then the real answer,
# which is shorter. Longest-run selection kept the planning run and glued the
# answer onto it.
LONG_PLANNING = (
    "The shipment lines came back for all five batches, so the next step is to "
    "price them: each line has a product and an order line, and the order line "
    "carries the unit price, so I will join the shipment lines to their order "
    "lines on the line key and add up quantity times price per customer. ") * 2
SHORT_ANSWER = "## Customers\n\n| Customer | Units |\n|---|---|\n| A | 110 |\n\nTotal: 110 units [2]"
WRITE_BACK_TOOLS = ["save_correlation", "save_value_mapping", "propose_metric_definition",
                    "digital-thread___save_correlation", "digital-thread___save_value_mapping"]
READ_TOOLS = ["erp___query_erp", "explore_graph", "digital-thread___explore_graph",
              "get_system_schema", "digital-thread___verify_relationship", "query_erp",
              "mes-ind___query_mes_ind", "unknown_tool"]


def test_the_planning_run_really_is_longer_than_the_answer():
    assert len(LONG_PLANNING) > len(SHORT_ANSWER)


def test_a_short_answer_after_a_longer_planning_run_is_the_answer():
    runs = _runs().AnswerRuns()
    runs.push_text(LONG_PLANNING)
    runs.tool_boundary("erp___query_erp")
    runs.push_text(SHORT_ANSWER)
    assert runs.finalize() == SHORT_ANSWER


def test_the_async_ask_fold_keeps_the_short_answer_after_long_planning():
    events = [_tool("explore_graph", "seed"), _tool("erp___query_erp", "a"),
              ToolResultEvent(tool_name="erp___query_erp", tool_use_id="a"),
              MessageEvent(LONG_PLANNING), _tool("erp___query_erp", "b"),
              ToolResultEvent(tool_name="erp___query_erp", tool_use_id="b"),
              MessageEvent(SHORT_ANSWER[:20]), MessageEvent(SHORT_ANSWER[20:]), DoneEvent()]
    text, _, _ = server._collect_ask_stream(events)
    assert text == SHORT_ANSWER
    assert _runs().answer_text(events) == SHORT_ANSWER


@pytest.mark.parametrize("tool", WRITE_BACK_TOOLS)
def test_a_write_back_call_after_the_report_keeps_the_report(tool):
    runs = _runs().AnswerRuns()
    runs.push_text(PLANNING)
    runs.tool_boundary("erp___query_erp")
    runs.push_text(ANSWER)
    runs.tool_boundary(tool)
    runs.push_text("Correlation saved.")
    out = runs.finalize()
    assert out == (ANSWER + "\n\nCorrelation saved.").strip()


def test_a_short_line_before_a_write_back_and_the_report_after_it():
    runs = _runs().AnswerRuns()
    runs.push_text(LONG_PLANNING)
    runs.tool_boundary("erp___query_erp")
    runs.push_text("Saving the pattern first.")
    runs.tool_boundary("digital-thread___save_correlation")
    runs.push_text(ANSWER)
    assert runs.finalize() == ANSWER.strip()


@pytest.mark.parametrize("tool", READ_TOOLS)
def test_every_other_call_ends_the_answer_so_far(tool):
    runs = _runs().AnswerRuns()
    runs.push_text(LONG_PLANNING)
    runs.tool_boundary(tool)
    runs.push_text(SHORT_ANSWER)
    assert runs.finalize() == SHORT_ANSWER


def test_no_text_after_the_last_read_falls_back_to_every_run():
    runs = _runs().AnswerRuns()
    runs.push_text(ANSWER)
    runs.tool_boundary("erp___query_erp")
    assert runs.finalize() == ANSWER.strip()


def test_a_boundary_without_a_tool_name_keeps_the_longest_run_rule():
    runs = _runs().AnswerRuns()
    runs.push_text(LONG_PLANNING)
    runs.tool_boundary()
    runs.push_text(SHORT_ANSWER)
    assert runs.finalize() == (LONG_PLANNING + "\n\n" + SHORT_ANSWER).strip()


def _js_call(fn: str, *args):
    script = (
        f"const m=require({json.dumps(str(_UTIL_JS))});"
        f"const f=m.{fn}||globalThis.{fn};"
        f"process.stdout.write(JSON.stringify(f(...{json.dumps(list(args))})));"
    )
    out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, check=True)  # nosec B603 - fixed argv list, internal inputs
    return json.loads(out.stdout)


@_needs_node
@pytest.mark.parametrize("tool", WRITE_BACK_TOOLS + READ_TOOLS + ["", "x___", "save_correlation_x"])
def test_the_write_back_classifier_matches_the_chat(tool):
    expected = tool in WRITE_BACK_TOOLS
    assert _runs().is_write_back_tool(tool) is expected
    assert _js_call("isWriteBackTool", tool) is expected


@_needs_node
@pytest.mark.parametrize("segments,start", [
    ([LONG_PLANNING, SHORT_ANSWER], 1),
    ([PLANNING, ANSWER, "Correlation saved."], 1),
    ([ANSWER, "x"], 2),
    ([ANSWER, "x"], 0),
    (["a", "b", "c"], 5),
    ([], 0),
])
def test_select_from_a_start_index_matches_the_chat(segments, start):
    assert (_runs().select_final_answer(segments, start)
            == _js_call("selectFinalAnswer", segments, start))
