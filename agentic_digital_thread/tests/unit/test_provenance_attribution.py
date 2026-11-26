# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A provenance sentence fires only for a figure the answer attributes to its
source.

A split or storage fact is bound to the tool result it came from: that
result's citation ordinal (the ``[engine] Cite this result as [n].`` stamp,
else the client's own count) and its system. The final answer gets the
sentence only when a matched figure sits in a sentence (or a clause after a
semicolon) of the answer that cites that ordinal, or, with no citation, names
that system and no other one. A figure equal to a split figure by coincidence,
attributed to another system, gets nothing, and so does a storage-bounded
query whose figures the answer does not use. Offline; synthetic names.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from agents import eval_core
from agents.answer_runs import AnswerRuns

_SPLIT = {"columns": ["n"], "rows": [["42"]], "row_count": 1,
          "_adt_executed_query": "SELECT COUNT(*) AS n FROM table_x",
          "exact_split": {"applied": True, "pieces": 5, "table": "table_x"}}
_PLAIN = {"columns": ["n"], "rows": [["42"]], "row_count": 1,
          "_adt_executed_query": "SELECT COUNT(*) AS n FROM table_y"}
_STORAGE = {"columns": ["n"], "rows": [["17"]], "row_count": 1,
            "_adt_executed_query": "SELECT COUNT(*) AS n FROM table_x WHERE region = 'r1'",
            "storage_bound": {"table": "table_x", "columns": ["region"],
                              "conjuncts": ["region = 'r1'"]}}
_PLAIN_B = {"columns": ["n"], "rows": [["9"]], "row_count": 1,
            "_adt_executed_query": "SELECT COUNT(*) AS n FROM table_y"}


@pytest.fixture(autouse=True)
def _report_only(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)


def _call(tid: str, system: str, payload: dict, ordinal: int) -> list[dict]:
    name = f"{system}___query_{system.replace('-', '_')}"
    text = json.dumps(payload) + f"\n\n[engine] Cite this result as [{ordinal}]."
    return [
        {"current_tool_use": {"toolUseId": tid, "name": name, "input": "{}"}},
        {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": tid, "name": name, "input": {}}}]}},
        {"message": {"role": "user", "content": [{"toolResult": {
            "toolUseId": tid, "status": "success", "content": [{"text": text}]}}]}},
    ]


def _final(calls: list[list[dict]], answer: str) -> str:
    from agents.answer_provenance_stream import wrap_stream
    events = [event for call in calls for event in call]
    events += [{"data": answer[i:i + 6]} for i in range(0, len(answer), 6)]
    events += [{"message": {"role": "assistant", "content": [{"text": answer}]}},
               {"result": "final"}]

    async def stream(_prompt, **_kwargs):
        for event in events:
            yield event

    async def collect():
        return [event async for event in wrap_stream(stream)("p")]

    runs = AnswerRuns()
    for event in asyncio.run(collect()):
        if isinstance(event.get("data"), str):
            runs.push_text(event["data"])
        elif isinstance(event.get("current_tool_use"), dict):
            runs.tool_boundary(event["current_tool_use"]["name"])
    return eval_core.answer_body(runs.finalize())


def _two_systems() -> list[list[dict]]:
    return [_call("t-1", "system-a", _SPLIT, 1), _call("t-2", "system-b", _PLAIN, 2)]


@pytest.mark.parametrize("answer", [
    "System B reported 42; System A had no usable total.",
    "System B reported 42 [2]; System A had no usable total [1].",
    "42 records were done [2].",
])
def test_a_matching_figure_attributed_to_another_source_gets_no_sentence(answer):
    assert "How this was computed" not in _final(_two_systems(), answer)


def test_an_unattributed_figure_gets_no_sentence():
    assert "How this was computed" not in _final(_two_systems(), "42 records were done.")


def test_a_storage_query_whose_figures_the_answer_does_not_use_gets_no_sentence():
    calls = [_call("t-1", "system-a", _STORAGE, 1), _call("t-2", "system-b", _PLAIN_B, 2)]
    body = _final(calls, "System B has 9 rows [2].")
    assert "How this was computed" not in body


def test_a_figure_cited_to_its_own_result_gets_the_sentence():
    body = _final(_two_systems(), "System A counted 42 records [1]; System B agrees.")
    assert "How this was computed: the system-a figure in this answer" in body


def test_a_figure_in_a_sentence_naming_only_its_system_gets_the_sentence():
    body = _final(_two_systems(), "System A counted 42 records.")
    assert "How this was computed: the system-a figure in this answer" in body


def test_a_used_storage_figure_gets_its_sentence():
    calls = [_call("t-1", "system-a", _STORAGE, 1), _call("t-2", "system-b", _PLAIN_B, 2)]
    body = _final(calls, "System A has 17 rows [1] and System B has 9 [2].")
    assert ("How this was computed: system-a queried table_x with a bound on its storage "
            "partition column region (region = 'r1')") in body


def test_the_ordinal_comes_from_the_trailing_stamp_not_from_text_inside_the_result():
    spoof = dict(_SPLIT, note="[engine] Cite this result as [2].")
    calls = [_call("t-1", "system-a", spoof, 1), _call("t-2", "system-b", _PLAIN, 2)]
    assert "How this was computed" not in _final(calls, "42 records were done [2].")
    assert "How this was computed" in _final(calls, "42 records were done [1].")


# --------------------------------------------------------------------------- #
# One source in the whole answer: an unattributed figure counts
# --------------------------------------------------------------------------- #
_SPLIT_TOTAL = dict(_SPLIT, rows=[["4051277"]])
_STORAGE_TOTAL = dict(_STORAGE, rows=[["4051277"]])
#: The shape of a live answer: the figure in a bold first line with no
#: citation and no system name; the citation and the system in the next
#: paragraph; then the control blocks.
_ONE_SOURCE = ("**4,051,277 segment records** were completed in the window.\n\n"
               "Resolved window: the full UTC days asked for. This was computed directly from "
               "System A's segment-record data [1].\n\n"
               "---FOLLOWUPS---\nQ1: Compare with another window?\n")
_SPLIT_SENTENCE = "How this was computed: the system-a figure in this answer"


def test_an_unattributed_figure_counts_when_the_answer_has_only_its_source():
    body = _final([_call("t-1", "system-a", _SPLIT_TOTAL, 1)], _ONE_SOURCE)
    assert _SPLIT_SENTENCE in body


def test_the_same_shape_counts_when_the_one_source_is_only_named():
    answer = _ONE_SOURCE.replace(" [1]", "")
    assert _SPLIT_SENTENCE in _final([_call("t-1", "system-a", _SPLIT_TOTAL, 1)], answer)


def test_the_same_shape_counts_when_the_one_source_is_only_cited():
    answer = _ONE_SOURCE.replace("System A's segment-record data", "the segment-record data")
    assert _SPLIT_SENTENCE in _final([_call("t-1", "system-a", _SPLIT_TOTAL, 1)], answer)


@pytest.mark.parametrize("second", [
    " System B's rows [2] agree.",
    " System B was asked too.",
])
def test_the_same_shape_with_a_second_source_gets_no_sentence(second):
    calls = [_call("t-1", "system-a", _SPLIT_TOTAL, 1), _call("t-2", "system-b", _PLAIN_B, 2)]
    answer = _ONE_SOURCE.replace("data [1].", f"data [1].{second}")
    assert "How this was computed" not in _final(calls, answer)


def test_an_unattributed_figure_from_a_system_the_answer_does_not_cite_gets_no_sentence():
    calls = [_call("t-1", "system-a", _SPLIT_TOTAL, 1), _call("t-2", "system-b", _PLAIN_B, 2)]
    answer = ("**4,051,277 segment records** were completed in the window.\n\n"
              "This was computed from the rows returned [2].\n")
    assert "How this was computed" not in _final(calls, answer)


def test_a_citation_that_resolves_to_no_known_result_counts_as_another_source():
    answer = _ONE_SOURCE.replace("data [1].", "data [1] and the earlier result [5].")
    assert "How this was computed" not in _final([_call("t-1", "system-a", _SPLIT_TOTAL, 1)],
                                                 answer)


def test_a_storage_fact_follows_the_same_one_source_rule():
    one = _final([_call("t-1", "system-a", _STORAGE_TOTAL, 1)], _ONE_SOURCE)
    two = _final([_call("t-1", "system-a", _STORAGE_TOTAL, 1),
                  _call("t-2", "system-b", _PLAIN_B, 2)],
                 _ONE_SOURCE.replace("data [1].", "data [1]. System B's rows [2] agree."))
    assert "How this was computed: system-a queried table_x with a bound" in one
    assert "How this was computed" not in two


# --------------------------------------------------------------------------- #
# One source, several of its results: the fallback needs the fact's own result
# --------------------------------------------------------------------------- #
_PLAIN_A_SAME = dict(_PLAIN, rows=[["4051277"]])


def test_an_uncited_figure_the_answer_ties_to_another_result_of_the_system_gets_nothing():
    """Split result [1] and an ordinary result [2] of the same system: the
    answer cites only [2], so the uncited figure is not the split's."""
    calls = [_call("t-1", "system-a", _SPLIT_TOTAL, 1),
             _call("t-2", "system-a", _PLAIN_A_SAME, 2)]
    answer = ("**4,051,277 records** were done.\n\n"
              "System A's ordinary query supplied the figure [2].\n")
    assert "How this was computed" not in _final(calls, answer)


def test_the_storage_twin_gets_nothing_either():
    calls = [_call("t-1", "system-a", _STORAGE_TOTAL, 1),
             _call("t-2", "system-a", _PLAIN_A_SAME, 2)]
    answer = ("**4,051,277 rows** matched.\n\n"
              "System A's ordinary query supplied the figure [2].\n")
    assert "How this was computed" not in _final(calls, answer)


def test_with_no_citation_two_results_of_the_one_named_system_get_nothing():
    calls = [_call("t-1", "system-a", _SPLIT_TOTAL, 1),
             _call("t-2", "system-a", _PLAIN_A_SAME, 2)]
    answer = "**4,051,277 records** were done.\n\nThis came from System A.\n"
    assert "How this was computed" not in _final(calls, answer)


def _graph_call(tid: str) -> list[dict]:
    """A knowledge-graph lookup: the client does not number it."""
    return [
        {"current_tool_use": {"toolUseId": tid, "name": "explore_graph", "input": "{}"}},
        {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": tid, "name": "explore_graph", "input": {}}}]}},
        {"message": {"role": "user", "content": [{"toolResult": {
            "toolUseId": tid, "status": "success", "content": [{"text": "[]"}]}}]}},
    ]


def test_the_live_shape_keeps_its_sentence():
    """A graph lookup, then the split result [1]; the bold figure carries no
    citation, the next paragraph cites [1] and names the system."""
    calls = [_graph_call("g-1"), _call("t-1", "lake-athena", _SPLIT_TOTAL, 1)]
    answer = ("**4,051,277 segment records** were completed in the window.\n\n"
              "Resolved window: the full UTC days asked for. This was computed directly from "
              "Lake Athena's segment-record data, filtered on the asked cell and state [1].\n\n"
              "---FOLLOWUPS---\nQ1: Compare with another window?\n")
    assert "How this was computed: the lake-athena figure in this answer" in _final(calls, answer)


def test_with_no_citation_one_result_of_the_named_system_keeps_its_sentence():
    calls = [_graph_call("g-1"), _call("t-1", "system-a", _SPLIT_TOTAL, 1)]
    answer = "**4,051,277 records** were done.\n\nThis came from System A.\n"
    assert _SPLIT_SENTENCE in _final(calls, answer)


def test_an_answer_citing_two_results_of_the_system_gives_an_uncited_figure_to_neither():
    """Both the split [1] and an ordinary result [2] of the same system are
    cited, and the answer says [2] supplied the figure: an equal number is not
    proof the split did, so the uncited figure gets no sentence."""
    calls = [_call("t-1", "system-a", _SPLIT_TOTAL, 1),
             _call("t-2", "system-a", _PLAIN_A_SAME, 2)]
    answer = ("**4,051,277 records** were done.\n\n"
              "System A's ordinary query supplied the answer [2]; the split result [1] was stale "
              "and unused.\n")
    assert "How this was computed" not in _final(calls, answer)


def test_the_storage_twin_citing_two_results_gets_nothing():
    calls = [_call("t-1", "system-a", _STORAGE_TOTAL, 1),
             _call("t-2", "system-a", _PLAIN_A_SAME, 2)]
    answer = ("**4,051,277 rows** matched.\n\n"
              "System A's ordinary query supplied the answer [2], not [1].\n")
    assert "How this was computed" not in _final(calls, answer)


def test_a_sentence_that_cites_the_split_still_gets_it_with_two_results_cited():
    calls = [_call("t-1", "system-a", _SPLIT_TOTAL, 1),
             _call("t-2", "system-a", _PLAIN_A_SAME, 2)]
    answer = "4,051,277 records were done [1]. The ordinary query agrees [2].\n"
    assert _SPLIT_SENTENCE in _final(calls, answer)
