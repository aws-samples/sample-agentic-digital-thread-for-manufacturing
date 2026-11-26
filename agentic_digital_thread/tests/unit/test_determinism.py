# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Mechanical determinism scoring (``agents/determinism.py``).

Evidence-chain semantics, ported from the upstream reference with two
DELIBERATE deviations (pinned below in the source-tripwire section):

* KG membership goes through ``query_intelligence.is_graph_query`` plus the
  schema tool's two spellings — never the ref's hardcoded tool-name tuple
  (those tool names do not exist in this repo);
* failure detection goes through ``query_intelligence.is_failed_result``
  (envelope markers) — never the ref's bare-word "error" marker, which this
  repo rejected (legit in manufacturing results).

Offline: pure stdlib, no AWS, no network.
"""
from __future__ import annotations

from pathlib import Path

import agents.determinism as determinism
from agents.determinism import score_tool_calls, value_tokens


def _calls(*names: str) -> list[dict]:
    return [{"name": n, "toolUseId": f"t{i + 1}"} for i, n in enumerate(names)]


# --------------------------------------------------------------------------- #
# (1) all-KG trace ⇒ 100%
# --------------------------------------------------------------------------- #
def test_all_kg_trace_scores_100():
    calls = _calls("explore_graph", "digital-thread___explore_graph",
                   "get_system_schema", "digital-thread___get_system_schema")
    score = score_tool_calls(calls, {}, {})
    assert all(c["grounded"] for c in score["per_call"])
    assert score["wasted_steps"] == []
    assert score["deterministic_calls"] == 4
    assert score["deterministic_pct"] == 100


# --------------------------------------------------------------------------- #
# (2) system call grounded in an earlier result's value token
# --------------------------------------------------------------------------- #
def test_system_call_grounded_by_earlier_result_token():
    calls = _calls("explore_graph", "erp___query")
    inputs = {"t2": '{"identifier": "PUMP-STATION-0007"}'}
    results = {"t1": '{"asset_tag": "PUMP-STATION-0007", "rows": 1}'}
    score = score_tool_calls(calls, inputs, results)
    assert score["per_call"][1]["grounded"] is True
    assert score["deterministic_pct"] == 100


# --------------------------------------------------------------------------- #
# (3) ungrounded first-call system query ⇒ not grounded
# --------------------------------------------------------------------------- #
def test_first_call_system_query_is_not_grounded():
    calls = _calls("erp___query")
    score = score_tool_calls(
        calls, {"t1": '{"sql": "SELECT status FROM orders_table"}'},
        {"t1": "3 rows"})
    assert score["per_call"][0]["grounded"] is False
    assert score["deterministic_calls"] == 0
    assert score["deterministic_pct"] == 0
    # Not wasted either — it succeeded on first attempt.
    assert score["wasted_steps"] == []


# --------------------------------------------------------------------------- #
# (4) failed-and-retried ⇒ wasted; failed WITHOUT retry ⇒ not wasted
# --------------------------------------------------------------------------- #
def test_failed_then_retried_same_tool_is_wasted():
    calls = _calls("erp___query", "erp___query")
    results = {"t1": "Error: column not found", "t2": "5 rows"}
    inputs = {"t1": '{"q": 1}', "t2": '{"q": 2}'}
    score = score_tool_calls(calls, inputs, results)
    assert [c["step"] for c in score["wasted_steps"]] == [1]


def test_failed_without_retry_is_not_wasted():
    calls = _calls("erp___query", "mes___query")
    results = {"t1": "no results", "t2": "5 rows"}
    score = score_tool_calls(calls, {"t1": '{"q": 1}', "t2": '{"q": 2}'}, results)
    assert score["wasted_steps"] == []


# --------------------------------------------------------------------------- #
# (5) exact duplicate ⇒ wasted (even when both succeeded)
# --------------------------------------------------------------------------- #
def test_exact_duplicate_call_is_wasted():
    calls = _calls("explore_graph", "explore_graph")
    inputs = {"t1": '{"query": "MATCH (n) RETURN n"}',
              "t2": '{"query": "MATCH (n) RETURN n"}'}
    results = {"t1": "10 nodes", "t2": "10 nodes"}
    score = score_tool_calls(calls, inputs, results)
    assert [c["step"] for c in score["wasted_steps"]] == [2]
    # The wasted duplicate is EXCLUDED from deterministic_calls even though
    # it is a grounded KG call.
    assert score["deterministic_calls"] == 1
    assert score["deterministic_pct"] == 50


# --------------------------------------------------------------------------- #
# (6) timestamp format drift still grounds (digits-only token form)
# --------------------------------------------------------------------------- #
def test_timestamp_format_drift_still_grounds():
    calls = _calls("cmms___query", "sitewise___query")
    results = {"t1": '{"completed_at": "2026-07-19T17:19:50Z"}'}
    inputs = {"t1": '{"completed": true}',
              "t2": '{"end_time": "2026-07-19 17:19:50"}'}
    # t1 grounded? No (first system call) — but its RESULT grounds t2.
    score = score_tool_calls(calls, inputs, results)
    assert score["per_call"][1]["grounded"] is True


def test_value_tokens_cross_format_timestamp():
    # Pure set intersection misses the drift (ISO = one 14-digit run; spaced
    # = separate date/time runs) — the digits-substring grounding pass covers
    # it. Pin BOTH facts so a future "simplify to `&`" regression is caught.
    iso = value_tokens("2026-07-19T17:19:50Z")
    spaced = value_tokens("2026-07-19 17:19:50")
    assert not (iso & spaced)
    assert determinism._tokens_ground(spaced, iso) is True
    assert determinism._tokens_ground(iso, spaced) is True


# --------------------------------------------------------------------------- #
# failure detection uses the SHARED envelope markers, not bare "error"
# --------------------------------------------------------------------------- #
def test_bare_error_word_in_data_is_not_failed():
    # "error" appears legitimately in manufacturing results (defect reasons,
    # error codes, error_rate columns) — a retried call whose first result
    # merely CONTAINS the word must not be scored as wasted.
    calls = _calls("mes___query", "mes___query")
    results = {"t1": "top defect reason: error code E42", "t2": "5 rows"}
    score = score_tool_calls(calls, {"t1": '{"q": 1}', "t2": '{"q": 2}'}, results)
    assert score["per_call"][0]["failed"] is False
    assert score["wasted_steps"] == []


# --------------------------------------------------------------------------- #
# defensive: empty trace, missing keys
# --------------------------------------------------------------------------- #
def test_empty_trace_is_zero_safe():
    score = score_tool_calls([], {}, {})
    assert score == {"per_call": [], "wasted_steps": [],
                     "deterministic_calls": 0, "deterministic_pct": 0}
    assert score_tool_calls(None, {}, {})["per_call"] == []  # type: ignore[arg-type]


def test_missing_call_keys_tolerated():
    score = score_tool_calls([{}], {}, {})
    assert score["per_call"][0]["grounded"] is False


# --------------------------------------------------------------------------- #
# source tripwires — the two deliberate deviations from the reference module
# --------------------------------------------------------------------------- #
_SRC = Path(determinism.__file__).read_text(encoding="utf-8")


def test_no_ref_hardcoded_kg_tool_tuple():
    # The ref's KG_TOOLS tuple names tools that do not exist here; membership
    # must delegate to the shared graph-query predicate instead.
    assert "query_neptune_graph" not in _SRC.replace(
        "``query_neptune_graph``", "")  # docstring mention of the ref allowed
    assert "KG_TOOLS" not in _SRC
    assert "is_graph_query" in _SRC


def test_no_ref_bare_error_markers():
    # No private FAIL_MARKERS copy — failure detection is the shared
    # envelope-marker helper (the bare-word "error" marker stays rejected).
    assert "FAIL_MARKERS" not in _SRC
    assert "is_failed_result" in _SRC


def test_module_is_import_pure():
    # Stdlib-only (ADR-0020/0021), like query_intelligence.
    assert "import boto3" not in _SRC
    assert "import pg8000" not in _SRC and "import pgdriver" not in _SRC
