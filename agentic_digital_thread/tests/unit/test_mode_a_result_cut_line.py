# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A Mode A answer built on a cut-off tool result says so, in engine words.

Measured on a rehearsal account (2026-10-11): a system agent's first query
returned a row listing cut at the row cap, and the one item the question was
about fell past the cut. The engine marked that tool result ``truncated`` with
a ``partial_note``, but the system agent's prose answer did not always mention
the cut, and the orchestrator only learns what that prose says. On the runs
where nothing reached the orchestrator, it answered from the partial list as
if it were complete.

The engine half of the fix: when any tool result in a Mode A turn was cut by
rows or by page, ``_run_mode_a_query`` appends ONE fixed engine-written line
to the answer stating how many of the turn's tool results were cut and that a
set, count or earliest/latest value read off them is not the full result. It
states a fact about the engine's own results and no data value, so it applies
in every non-enforce mode (report-only included). A column-only cut (every
row present) and an uncut turn leave the answer byte-identical.

Fully offline. Pins:

1. Detection rides the capture hook and reads the payload itself (the 1 KB
   safe-metadata bound misses every realistic cut result); only row and page
   cuts count; exception/retry/unregistered calls are not counted; the tally
   survives the elicitation retry.
2. The exact line text for one, several, mixed and unknown row counts.
3. ``_run_mode_a_query`` appends it once, before relayed chart tags, in
   report-only and annotate, and never onto a JSON envelope (enforce,
   withheld); no cut means byte-identical.
4. The line trips no outcome, outage, error, memory or fabrication
   classifier, no golden ``answer_must_not_match``, and the documented re-ask
   is not scored as a wasted call; the shared outcome fixture pins the short
   answer + line case.
5. The line names no dataset word and carries no identifier-shaped literal.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import agents.chat_client as chat_client
import agents.orchestrator as orch
import agents.system_agent as sa
import agents.tool_result_capture as trc
from agents import determinism, eval_core, query_intelligence as qi
from agents.demo import story_lookup
import agents.response_integrity_query as riq
from agents.response_integrity_query import (
    MAX_QUERY_COLUMNS,
    bounded_query_result,
    strict_json_dumps,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import smoke_prompts as smoke  # noqa: E402

_FIXTURE = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "test"
            / "fixtures" / "tool-result-outcome-cases.json")
_GOLDENS = (Path(__file__).resolve().parents[2] / "agents" / "demo"
            / "golden_evals.json")
_FIXTURE_CASE_ID = "mode-a-short-answer-with-cut-line"
_SHORT_ANSWER = "The query matched 1,193 rows across 25 groups; 14 groups are listed."


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _tool_result(payload: dict, *, sql: str = "SELECT a, b FROM t ORDER BY b") -> dict:
    """A tool result shaped like the direct SQL tool's (text JSON content)."""
    body = {**payload, "_adt_executed_query": sql}
    return {"toolUseId": "t-1", "status": "success",
            "content": [{"text": strict_json_dumps(body)}]}


def _rows(n: int) -> list[list]:
    return [[f"v{i:05d}", f"2026-01-01T00:{i % 60:02d}:00Z"] for i in range(n)]


def _row_cut(n: int = 500) -> dict:
    return _tool_result(bounded_query_result(["a", "b"], _rows(n), truncated=True))


def _uncut(n: int = 3) -> dict:
    return _tool_result(bounded_query_result(["a", "b"], _rows(n), truncated=False))


def _page_cut(n: int = 100) -> dict:
    return _tool_result(bounded_query_result(["a", "b"], _rows(n), truncated=True,
                                             reason="page_limit"))


def _column_cut() -> dict:
    columns = [f"c{i}" for i in range(MAX_QUERY_COLUMNS + 1)]
    rows = [list(range(len(columns))) for _ in range(3)]
    result = bounded_query_result(columns, rows, truncated=False)
    assert result["partial_reason"] == "column_limit"
    return _tool_result(result)


def _event(state: dict, result, *, name: str = "query_public", exception=None,
           retry: bool = False):
    return SimpleNamespace(
        selected_tool=None,
        tool_use={"toolUseId": "t-1", "name": name, "input": {"sql": "SELECT 1"}},
        invocation_state=state, result=result, exception=exception, retry=retry,
        agent=SimpleNamespace(messages=[]),
    )


def _capture(**kwargs) -> trc.InvocationToolResultCapture:
    return trc.InvocationToolResultCapture(system_id="sys-a", **kwargs)


def _state_after(*results) -> dict:
    capture = _capture()
    state: dict = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    for result in results:
        capture._on_after_tool_call(_event(state, result))
    return state


def _line(results: int, rows: list) -> str:
    return trc.result_cut_line({trc.RESULT_CUTS_STATE_KEY: {
        "results": results, "cut": len(rows), "rows": list(rows)}})


def _run_query(answer: str, state: dict, agent=None) -> str:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sa, "_invoke_mode_a_agent_with_state",
                   lambda _agent, _q: (answer, state))
        mp.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
        return asyncio.run(sa._run_mode_a_query(agent or object(), "sys-a", "question"))


@pytest.fixture(autouse=True)
def _report_only(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)


# --------------------------------------------------------------------------- #
# 1. Detection on the capture hook
# --------------------------------------------------------------------------- #
def test_a_row_cut_result_is_recorded_with_the_rows_it_kept():
    result = _row_cut(500)
    # The measured shape is far larger than the 1 KB safe-metadata bound, so
    # the existing capture never sees its ``truncated`` key.
    assert len(result["content"][0]["text"]) > 10 * trc.TOOL_RESULT_CAPTURE_CHARS
    meta = trc.safe_tool_result_metadata(result, tool_name="query_public",
                                         max_chars=trc.TOOL_RESULT_CAPTURE_CHARS)
    assert "truncated" not in meta
    assert trc.result_row_cut(result) == (True, 500)
    state = _state_after(result)
    assert state[trc.RESULT_CUTS_STATE_KEY] == {"results": 1, "cut": 1, "rows": [500]}


def test_a_full_page_is_a_cut():
    assert trc.result_row_cut(_page_cut(100)) == (True, 100)


@pytest.mark.parametrize("reason", ["byte_limit", "cell_limit", "invalid_row",
                                    "invalid_numeric", "row_limit"])
def test_every_row_cut_reason_counts(reason):
    payload = {"columns": ["a"], "rows": [[1]] * 4, "row_count": 4,
               "truncated": True, "partial_reason": reason}
    assert trc.result_row_cut(_tool_result(payload)) == (True, 4)


@pytest.mark.parametrize("reason", ["column_limit", "response_limit", "source_partial"])
def test_cuts_that_are_not_row_cuts_do_not_count(reason):
    payload = {"columns": ["a"], "rows": [[1]], "row_count": 1,
               "truncated": True, "partial_reason": reason}
    assert trc.result_row_cut(_tool_result(payload)) == (False, None)


def test_a_column_only_cut_keeps_every_row_and_is_not_a_cut():
    assert trc.result_row_cut(_column_cut()) == (False, None)


@pytest.mark.parametrize("result", [
    _uncut(), {"status": "success", "content": [{"text": "plain prose"}]},
    {"status": "success", "content": [{"json": {"truncated": "yes", "rows": []}}]},
    {"status": "error", "content": [{"text": "Error: boom"}]}, None, "text",
])
def test_uncut_or_unparseable_results_are_not_cuts(result):
    assert trc.result_row_cut(result) == (False, None)


def test_a_json_content_block_is_read_too():
    payload = bounded_query_result(["a"], [[1]] * 7, truncated=True)
    result = {"status": "success", "content": [{"json": payload}]}
    assert trc.result_row_cut(result) == (True, 7)


def test_a_byte_cut_at_the_size_cap_with_a_long_query_is_still_seen():
    big = "x" * 8_000
    payload = bounded_query_result(["a"], [[big]] * 100, truncated=False)
    assert payload["partial_reason"] == "byte_limit"
    result = _tool_result(payload, sql="SELECT a FROM t WHERE " + " OR ".join(  # nosec B608 - test fixture SQL
        f"a = 'k{i:06d}'" for i in range(9_000)))
    assert len(result["content"][0]["text"]) > 300_000
    assert trc.result_row_cut(result) == (True, payload["row_count"])


def test_exception_retry_and_unregistered_calls_are_not_counted():
    capture = _capture(registered_tools={"query_public"})
    state: dict = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    capture._on_after_tool_call(_event(state, None, exception=RuntimeError("x")))
    capture._on_after_tool_call(_event(state, _row_cut(), retry=True))
    capture._on_after_tool_call(_event(state, _row_cut(), name="other_tool"))
    assert state.get(trc.RESULT_CUTS_STATE_KEY) in (None, {"results": 0, "cut": 0,
                                                          "rows": []})
    capture._on_after_tool_call(_event(state, _uncut()))
    assert state[trc.RESULT_CUTS_STATE_KEY] == {"results": 1, "cut": 0, "rows": []}


def test_the_tally_survives_the_elicitation_retry_reset():
    capture = _capture()
    state = _state_after(_row_cut(), _uncut())
    # The bounded "produce your final answer NOW" retry is a second
    # invocation on the same state; the hook re-initialises its own keys.
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    assert state[trc.RESULT_CUTS_STATE_KEY] == {"results": 2, "cut": 1, "rows": [500]}


def test_a_recording_bug_never_breaks_the_hook(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(trc, "record_result_cut", _boom)
    state = _state_after(_row_cut())
    assert state[trc.TOOL_RESULTS_STATE_KEY]  # the existing capture still ran


def test_hostile_tally_state_is_tolerated():
    for hostile in (None, "x", [], {"results": "3"}, {"results": 2, "cut": 1,
                                                       "rows": "500"}):
        state = {trc.RESULT_CUTS_STATE_KEY: hostile}
        trc.record_result_cut(state, _row_cut(500))
        assert state[trc.RESULT_CUTS_STATE_KEY]["cut"] >= 1
        assert trc.result_cut_line({trc.RESULT_CUTS_STATE_KEY: hostile}) == ""


# --------------------------------------------------------------------------- #
# 1b. Every Mode A connector's own partial shape
# --------------------------------------------------------------------------- #
def _json_result(payload) -> dict:
    """A tool result whose text is the connector's JSON payload as-is."""
    return {"toolUseId": "t-1", "status": "success",
            "content": [{"text": json.dumps(payload, default=str)}]}


class _TreeClient:
    """``ListAssociatedAssets`` double: ``tree[asset_id]`` is the list of child
    ids; a child id that is itself a key has children of its own; ids in
    ``fail`` raise when their children are listed."""

    def __init__(self, tree: dict, fail: frozenset = frozenset()):
        self.tree, self.fail = tree, fail

    def get_paginator(self, name):
        assert name == "list_associated_assets"
        client = self

        class _Pages:
            @staticmethod
            def paginate(**kwargs):
                asset_id = kwargs["assetId"]
                if asset_id in client.fail:
                    raise RuntimeError("children read failed")
                yield {"assetSummaries": [
                    {"id": c, "name": c.upper(), "assetModelId": "m",
                     "hierarchies": [{"id": "h"}] if c in client.tree else []}
                    for c in client.tree.get(asset_id, [])]}
        return _Pages()


def _associated(monkeypatch, tree: dict, fail=frozenset(), max_assets: int = 3) -> dict:
    """The REAL sitewise_read.list_associated_assets output for a fake tree."""
    from agents import aws_session, sitewise_read as sw

    client = _TreeClient(tree, frozenset(fail))
    monkeypatch.setattr(aws_session, "boto3_client_for",
                        lambda service, cfg, default_region=None: client)
    return sw.list_associated_assets({}, "us-east-1", "root", max_assets=max_assets)


_FIVE_LEAVES = {"root": ["c0", "c1", "c2", "c3", "c4"]}
_TWO_BRANCHES = {"root": ["b0", "b1"], "b0": ["l0"], "b1": ["l1", "l2", "l3"]}


def test_the_sitewise_asset_cap_is_a_cut(monkeypatch):
    payload = _associated(monkeypatch, _FIVE_LEAVES)
    assert payload["reason"] == "max_assets" and payload["status"] == "partial"
    assert trc.result_row_cut(_json_result(payload)) == (True, None)


def test_the_sitewise_asset_cap_with_a_failed_branch_read_is_a_cut(monkeypatch):
    payload = _associated(monkeypatch, _TWO_BRANCHES, fail={"b0"})
    assert payload["reason"] == "max_assets,read_failed"
    assert trc.result_row_cut(_json_result(payload)) == (True, None)


def test_a_failed_sitewise_branch_read_alone_is_a_cut(monkeypatch):
    # The assets under the unread branches are missing from the list, so a
    # set or count read off it is not the full result.
    payload = _associated(monkeypatch, _TWO_BRANCHES, fail={"b0"}, max_assets=50)
    assert payload["reason"] == "read_failed"
    assert trc.result_row_cut(_json_result(payload)) == (True, None)


def test_an_uncut_sitewise_asset_list_is_not_a_cut(monkeypatch):
    payload = _associated(monkeypatch, _TWO_BRANCHES, max_assets=50)
    assert payload["truncated"] is False
    assert trc.result_row_cut(_json_result(payload)) == (False, None)


class _HistoryClient:
    """Serves one point per entry per page with an endless continuation."""

    def __init__(self):
        self.calls = 0

    def batch_get_asset_property_value_history(self, entries, maxResults, nextToken=None):  # noqa: N803
        self.calls += 1
        stamp = 1_790_000_000 + self.calls * 60
        return {"successEntries": [{
                    "entryId": e["entryId"], "assetPropertyValueHistory": [{
                        "value": {"doubleValue": 1.0},
                        "timestamp": {"timeInSeconds": stamp}, "quality": "GOOD"}]}
                    for e in entries],
                "errorEntries": [], "skippedEntries": [],
                "nextToken": f"t{self.calls}"}


def _history(monkeypatch, n_entries: int = 2) -> dict:
    from agents import aws_session, sitewise_read as sw

    client = _HistoryClient()
    monkeypatch.setattr(aws_session, "boto3_client_for",
                        lambda service, cfg, default_region=None: client)
    entries = [{"assetId": "asset-1", "propertyId": f"prop-{i}"}
               for i in range(n_entries)]
    return sw.get_property_history({}, "us-east-1", entries,
                                   start_iso="2026-09-20T00:00:00Z",
                                   end_iso="2026-09-21T00:00:00Z")


def test_a_sitewise_history_page_limit_is_a_cut(monkeypatch):
    from agents import sitewise_read as sw

    monkeypatch.setattr(sw, "HISTORY_MAX_PAGES_PER_CHUNK", 2)
    payload = _history(monkeypatch)
    item = payload["asset-1_prop-0"]
    assert item["status"] == "partial" and item["reason"] == "page_limit"
    assert trc.result_row_cut(_json_result(payload)) == (True, None)


def test_a_sitewise_history_call_limit_is_a_cut(monkeypatch):
    from agents import sitewise_read as sw

    monkeypatch.setattr(sw, "HISTORY_MAX_TOTAL_CALLS", 1)
    payload = _history(monkeypatch)
    assert {v["reason"] for k, v in payload.items() if not k.startswith("_")} == {"call_limit"}
    assert trc.result_row_cut(_json_result(payload)) == (True, None)


@pytest.mark.parametrize("reason", [
    "call_limit", "page_limit", "raw_point_limit", "response_size_limit",
    "invalid_response", "continuation_error", "history_read_error",
    "empty_continuation", "invalid_token", "repeated_token"])
def test_every_sitewise_history_partial_reason_is_a_cut(reason):
    from agents import sitewise_read as sw

    partial = sw._history_partial(reason, None, "2026-09-20T00:00:00Z",
                                  "2026-09-21T00:00:00Z")
    payload = {"asset-1_prop-0": [{"value": 1.0, "timestamp": 1}],
               "asset-1_prop-1": partial}
    assert trc.result_row_cut(_json_result(payload)) == (True, None)


def test_a_complete_sitewise_history_is_not_a_cut():
    payload = {"asset-1_prop-0": [{"value": 1.0, "timestamp": 1, "quality": "GOOD"}],
               "asset-1_prop-1": {"status": "before_first_value", "points": [],
                                  "data_starts_at": "2026-09-22T00:00:00+00:00"},
               "asset-1_prop-2": {"error": "x", "error_type": "sitewise_error"}}
    assert trc.result_row_cut(_json_result(payload)) == (False, None)


def test_the_sitewise_history_tool_output_with_source_metadata_is_a_cut(monkeypatch):
    # What the system agent's history tool actually returns: the reader's map
    # plus the engine's source-metadata member at the root.
    from agents import sitewise_read as sw
    from agents.response_integrity_capture import attach_source_metadata

    monkeypatch.setattr(sw, "HISTORY_MAX_PAGES_PER_CHUNK", 2)
    payload = attach_source_metadata(_history(monkeypatch), boundary="sitewise",
                                     system_id="sys-a")
    assert trc.result_row_cut(
        {"status": "success", "content": [{"text": sa._sitewise_json(payload)}]}
    ) == (True, None)


class _PgCursor:
    def __init__(self, n_rows):
        self.description = [("a",), ("b",)]
        self._rows = iter([(f"v{i}", i) for i in range(n_rows)])

    def execute(self, *_args, **_kwargs):
        return None

    def fetchone(self):
        return next(self._rows, None)


class _PgConn:
    def __init__(self, n_rows):
        self._cursor = _PgCursor(n_rows)

    def set_session(self, **_kwargs):
        return None

    def cursor(self):
        return self._cursor

    def close(self):
        return None


def test_the_postgres_tool_output_is_a_cut(monkeypatch):
    monkeypatch.setattr(sa.pgdriver, "connect", lambda **_kw: _PgConn(sa._MAX_ROWS + 5))
    tool = sa._make_direct_query_tool("public", {"host": "h"}, "sys-a")
    text = tool._tool_func(sql="SELECT a, b FROM t ORDER BY b")
    assert trc.result_row_cut({"status": "success", "content": [{"text": text}]}) == (
        True, sa._MAX_ROWS)


def test_the_athena_capped_output_is_a_cut(monkeypatch):
    import agents.athena_tools as at

    monkeypatch.setattr(at, "_collect_results", lambda _athena, _qid, max_rows: {
        "columns": ["a"], "rows": [[1]] * max_rows, "row_count": max_rows})
    out = at._athena_result(None, "q", 3, True, "sys-a", None, "SELECT a FROM t LIMIT 3")
    assert trc.result_row_cut(_json_result(out)) == (True, 3)


def test_the_tulip_full_page_output_is_a_cut():
    class _Tulip:
        def get(self, _path, params=None):
            return [{"id": f"r{i}"} for i in range(params["limit"])]

    text = sa._tulip_records_impl(_Tulip(), "t", 100, 0, "", "")
    assert trc.result_row_cut({"status": "success", "content": [{"text": text}]}) == (
        True, 100)


def _real_history_partial(reason: str = "page_limit") -> dict:
    from agents import sitewise_read as sw

    return sw._history_partial(reason, None, "2026-09-20T00:00:00Z",
                               "2026-09-21T00:00:00Z")


def _real_asset_cap(monkeypatch) -> dict:
    return _associated(monkeypatch, _FIVE_LEAVES)


# Complete business records that carry a connector's colliding key set, at the
# root and nested. Each one was counted as a cut by the walk's first version.
_COLLIDING_RECORDS = {
    "vendor-keys-on-a-root-record": lambda _mp: {
        "partial": True, "returned": 3, "state": "work in progress"},
    "vendor-keys-on-a-nested-record": lambda _mp: {
        "record": {"partial": True, "returned": 3, "state": "work in progress"}},
    "vendor-keys-and-a-list-without-the-envelope": lambda _mp: {
        "partial": True, "returned": 2, "lots": [{"id": "a"}, {"id": "b"}]},
    "vendor-keys-on-a-record-inside-a-complete-envelope": lambda _mp: {
        "success": True, "returned": 1,
        "data": [{"id": "a", "partial": True, "returned": 1}]},
    "vendor-envelope-nested-under-a-record": lambda _mp: {
        "result": {"success": True, "partial": True, "returned": 1,
                   "data": [{"id": "a"}]}},
    "vendor-envelope-whose-count-is-not-its-list": lambda _mp: {
        "success": True, "partial": True, "returned": 3, "data": [{"id": "a"}]},
    "window-keys-on-a-nested-record": lambda _mp: {
        "op-1": {"status": "partial", "truncated": True, "reason": "operator stop",
                 "requested_start": "2026-10-01", "covered_start": "2026-10-01"}},
    "full-window-key-set-with-a-business-note": lambda _mp: {
        "job-7": {**_real_history_partial(), "note": "operator paused the run"}},
    "real-window-outcome-nested-deeper": lambda _mp: {
        "orders": [_real_history_partial()]},
    "real-window-outcome-beside-a-scalar": lambda _mp: {
        "a_p": _real_history_partial(), "site": "plant"},
    "asset-list-keys-on-a-root-record": lambda _mp: {
        "assets": [{"id": "a"}], "truncated": True, "status": "partial",
        "reason": "max_assets"},
    "real-asset-list-cut-nested-under-a-record": lambda mp: {
        "result": _real_asset_cap(mp)},
    "bounded-result-keys-nested-under-a-record": lambda _mp: {
        "report": {"columns": ["a"], "rows": [[1]], "row_count": 1,
                   "truncated": True, "partial_reason": "row_limit"}},
}


@pytest.mark.parametrize("case", sorted(_COLLIDING_RECORDS))
def test_a_complete_record_with_a_colliding_key_set_is_not_a_cut(monkeypatch, case):
    payload = _COLLIDING_RECORDS[case](monkeypatch)
    assert trc.result_row_cut(_json_result(payload)) == (False, None)
    state = _state_after(_json_result(payload))
    assert _run_query("Complete answer.", state) == "Complete answer."


@pytest.mark.parametrize("payload", [
    # a nested flag that is not a row or window cut
    {"rows": [[1]], "row_count": 1, "meta": {"truncated": True}},
    {"rows": [[1]], "row_count": 1, "note_truncated": True},
    {"k": {"status": "partial", "truncated": True, "reason": "source_partial",
           "requested_start": "a", "covered_start": None}},
    {"k": {"status": "partial", "truncated": True, "reason": "column_limit",
           "requested_start": "a", "covered_start": None}},
    {"k": {"truncated": True, "reason": "page_limit"}},
    {"k": {"status": "partial", "reason": "page_limit", "requested_start": "a",
           "covered_start": None}},
    # a top-level partial status with no truncated flag, and a bare partial
    {"status": "partial", "rows": [[1]]},
    {"partial": True, "data": []},
    {"partial": "yes", "returned": 3},
    # a SiteWise-shaped list cut whose reason is not a list cut
    {"assets": [], "truncated": True, "status": "partial", "reason": "depth"},
])
def test_an_unrelated_nested_or_partial_flag_is_not_a_cut(payload):
    assert trc.result_row_cut(_json_result(payload)) == (False, None)


def test_a_window_outcome_counts_only_directly_under_a_property_key():
    # The reader puts each property's outcome directly under its key in the
    # result map; the same outcome one level deeper is someone else's data.
    partial = _real_history_partial()
    assert trc.result_row_cut(_json_result({"a_p": partial})) == (True, None)
    assert trc.result_row_cut(_json_result({"a_p": {"inner": partial}})) == (False, None)


def test_a_map_larger_than_any_history_result_is_not_read_as_one():
    # A history read returns at most HISTORY_MAX_ENTRIES keys, so a map over
    # the scan cap is not a history result and is not walked.
    from agents import sitewise_read as sw

    assert trc._MAX_HISTORY_OUTCOMES >= sw.HISTORY_MAX_ENTRIES
    crowded = {f"k{i:05d}": [] for i in range(trc._MAX_HISTORY_OUTCOMES + 1)}
    crowded["zzz"] = _real_history_partial()
    assert trc.result_row_cut(_json_result(crowded)) == (False, None)


# --------------------------------------------------------------------------- #
# 2. The exact line
# --------------------------------------------------------------------------- #
def test_one_cut_line_is_exact():
    assert _line(3, [500]) == (
        "[engine] 1 of 3 tool results this turn was cut off: 500 rows came back "
        "and the rows after them were not returned. " + riq.INCOMPLETE_FROM_ROWS)


def test_two_cuts_with_the_same_row_count():
    assert _line(2, [500, 500]) == (
        "[engine] 2 of 2 tool results this turn were cut off: 500 rows came back "
        "from each and the rows after them were not returned. " + riq.INCOMPLETE_FROM_ROWS)


def test_cuts_with_different_row_counts_list_each_once_in_order():
    assert _line(5, [500, 100, 500, 7]) == (
        "[engine] 4 of 5 tool results this turn were cut off: 500, 100 and 7 rows "
        "came back and the rows after them were not returned. " + riq.INCOMPLETE_FROM_ROWS)


_UNKNOWN_COUNT_CLOSING = (
    "A list of distinct values, a count, or an earliest or latest value read "
    "off the returned data is not the full result.")


def test_an_unknown_row_count_drops_the_count_rather_than_guess():
    assert _line(2, [500, None]) == (
        "[engine] 2 of 2 tool results this turn were cut off: the full rows or "
        "time window did not come back. " + _UNKNOWN_COUNT_CLOSING)


def test_a_window_cut_line_has_no_row_count():
    # A SiteWise history or asset-list cut has no "first R rows": the line
    # says the full rows or window did not come back, never a made-up count.
    assert _line(4, [None]) == (
        "[engine] 1 of 4 tool results this turn was cut off: the full rows or "
        "time window did not come back. " + _UNKNOWN_COUNT_CLOSING)


def test_the_unknown_count_line_is_true_when_nothing_came_back(monkeypatch):
    # A call-limited history read can return no points at all, and a failed
    # branch read can return no assets: the line must not say part came back.
    from agents import sitewise_read as sw

    monkeypatch.setattr(sw, "HISTORY_MAX_TOTAL_CALLS", 0)
    payload = _history(monkeypatch)
    assert all(v["points"] == [] for k, v in payload.items() if not k.startswith("_"))
    line = trc.result_cut_line(_state_after(_json_result(payload)))
    assert line == ("[engine] 1 of 1 tool results this turn was cut off: the full "
                    "rows or time window did not come back. " + _UNKNOWN_COUNT_CLOSING)
    assert "part of" not in line and "came back" not in line
    assert "these rows" not in line


def test_no_cut_means_no_line():
    assert _line(3, []) == ""
    assert trc.result_cut_line({}) == ""
    assert trc.result_cut_line(None) == ""


def test_the_line_reuses_the_partial_note_wording():
    assert riq.INCOMPLETE_FROM_ROWS == (
        "A list of distinct values, a count, or an earliest or latest value read "
        "off these rows is not the full result.")


# --------------------------------------------------------------------------- #
# 3. _run_mode_a_query
# --------------------------------------------------------------------------- #
def test_a_cut_turn_appends_exactly_one_line_in_report_only():
    state = _state_after(_row_cut(500))
    answer = _run_query("Fourteen groups found.", state)
    assert answer == "Fourteen groups found.\n\n" + _line(1, [500])
    assert answer.count("[engine]") == 1


@pytest.mark.parametrize("results", [(_uncut(),), (_column_cut(),), ()])
def test_an_uncut_or_column_only_turn_is_byte_identical(results):
    state = _state_after(*results)
    text = "Three rows.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    assert _run_query(text, state) == text


def test_a_cut_followed_by_an_uncut_query_still_appends():
    state = _state_after(_row_cut(500), _uncut())
    assert _run_query("Answer.", state) == "Answer.\n\n" + _line(2, [500])


def test_annotate_mode_appends_too(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "annotate")
    state = _state_after(_row_cut(500))
    assert _run_query("Answer.", state).endswith(_line(1, [500]))


def test_enforce_mode_json_envelope_is_untouched(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    envelope = json.dumps({"answer": "gated", "claims": [], "evidence": []})
    assert _run_query(envelope, _state_after(_row_cut())) == envelope


def test_the_engine_withheld_envelope_is_untouched():
    # annotate's fabrication block is the engine's own terminal JSON verdict;
    # it carries no data, so there is nothing to qualify.
    payload = json.dumps({"status": "withheld", "retryable": False, "answer": "x"})
    assert _run_query(payload, _state_after(_row_cut())) == payload


@pytest.mark.parametrize("mode", ["report-only", "annotate"])
def test_a_json_object_answer_carries_the_cut_fact_as_one_reserved_member(
        monkeypatch, mode):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", mode)
    answer = json.dumps({"answer": "Fourteen groups.", "groups": ["g1", "g2"]})
    out = _run_query(answer, _state_after(_row_cut(500)))
    parsed = json.loads(out)
    assert parsed == {"answer": "Fourteen groups.", "groups": ["g1", "g2"],
                      trc.RESULT_CUT_NOTE_KEY: _line(1, [500])}
    assert list(parsed)[-1] == trc.RESULT_CUT_NOTE_KEY
    assert out.count("[engine]") == 1


def test_a_json_array_answer_is_wrapped_with_the_cut_fact():
    answer = '  [{"a": 1}, {"a": 2}]  '
    out = _run_query(answer, _state_after(_row_cut(500)))
    assert json.loads(out) == {trc.RESULT_CUT_ITEMS_KEY: [{"a": 1}, {"a": 2}],
                               trc.RESULT_CUT_NOTE_KEY: _line(1, [500])}


def test_the_reserved_member_overwrites_a_model_spelling_of_it():
    answer = json.dumps({"answer": "x", trc.RESULT_CUT_NOTE_KEY: "nothing was cut"})
    out = json.loads(_run_query(answer, _state_after(_row_cut(500))))
    assert out[trc.RESULT_CUT_NOTE_KEY] == _line(1, [500])


@pytest.mark.parametrize("answer", [
    json.dumps({"answer": "x"}), "[1, 2]", '{"a": {"b": [1]}}'])
def test_an_uncut_json_answer_stays_byte_identical(answer):
    assert _run_query(answer, _state_after(_uncut())) == answer


@pytest.mark.parametrize("answer", [
    'Here is the result:\n```json\n{"a": 1}\n```',
    '```json\n[{"a": 1}]\n```',
    '<chart-block type="line" title="t" labels="[1]" data="[2]"/>\n{"a": 1}',
    '{"a": 1} and some prose after it',
])
def test_text_that_is_not_one_json_document_gets_the_plain_line(answer):
    out = _run_query(answer, _state_after(_row_cut(500)))
    assert out == answer + "\n\n" + _line(1, [500])


def test_json_answers_with_the_fact_keep_every_orchestrator_side_parser_working():
    """The orchestrator-side readers of a query_<id> result that parse JSON:
    the activity-trace summary (summarize_tool_result, whose text the UI
    outcome classifier reads), the reason-code learner's row extractor
    (runtime_app._extract_result_rows) and the wasted-call/determinism
    classifier on the raw text. None may turn a successful answer into a
    failure, and the extractor must still find the rows."""
    from agents import runtime_app
    from agents.tool_result_summary import summarize_tool_result

    rows = [{"lot": "l1", "result": "PASS"}, {"lot": "l2", "result": "FAIL"}]
    # (A dict with a "rows" list of objects and no "columns" already
    # summarizes as "No records found." without this change; not used here.)
    for answer in (json.dumps(rows), json.dumps({"answer": "x", "records": rows})):
        out = _run_query(answer, _state_after(_row_cut(500)))
        assert runtime_app._extract_result_rows(out) == rows
        summary = summarize_tool_result("erp___query_erp", {"content": [{"text": out}]})
        assert qi.failure_marker(summary) is None, summary
        assert not qi.is_failed_result(out)


def test_relayed_chart_tags_stay_last():
    tag = '<chart-block type="line" title="series-a" labels="[1,2]" data="[10,20]"/>'
    agent = SimpleNamespace(_pending_chart_blocks=[tag])
    out = _run_query("Trend rose.", _state_after(_row_cut(500)), agent=agent)
    assert out == "Trend rose.\n\n" + _line(1, [500]) + "\n\n" + tag
    # The orchestrator's redaction still finds and queues the relayed tag.
    hook = orch.ChartRedactionHook()
    outer = SimpleNamespace(_pending_chart_blocks=[])
    message = {"role": "user", "content": [{"toolResult": {
        "toolUseId": "q-1", "status": "success", "content": [{"text": out}]}}]}
    hook._on_message_added(SimpleNamespace(message=message, agent=outer))
    redacted = message["content"][0]["toolResult"]["content"][0]["text"]
    assert "<chart-block" not in redacted
    assert redacted.startswith("Trend rose.\n\n" + _line(1, [500]))
    assert outer._pending_chart_blocks == [tag]


def test_the_investigation_trace_gets_the_answer_as_returned(monkeypatch):
    seen = {}
    monkeypatch.setattr(sa, "_write_investigation_trace",
                        lambda _s, _id, _q, answer, **_k: seen.setdefault("a", answer))
    out = _run_query("Answer.", _state_after(_row_cut(500)))
    assert seen["a"] == out


def test_an_append_failure_returns_the_answer_unchanged(monkeypatch):
    def _boom(_state):
        raise RuntimeError("boom")

    monkeypatch.setattr(trc, "result_cut_line", _boom)
    assert _run_query("Answer.", _state_after(_row_cut())) == "Answer."


# --------------------------------------------------------------------------- #
# 4. Interactions: classifiers, goldens, determinism, shared fixture
# --------------------------------------------------------------------------- #
# (results, rows kept per cut) — resolved at test time so a missing helper
# fails each test rather than collection.
_LINE_SPECS = [(1, [500]), (3, [500]), (2, [500, 500]), (4, [500, 100, 7]),
               (2, [None, 500]), (1, [None]), (3, [None, None])]


@pytest.mark.parametrize("spec", _LINE_SPECS)
def test_a_short_answer_with_the_line_is_neither_failed_nor_empty(spec):
    line = _line(*spec)
    text = _SHORT_ANSWER + "\n\n" + line
    # The line starts inside the 240-char envelope head of a short answer.
    assert text.index("[engine]") < qi.ENVELOPE_HEAD_CHARS
    assert qi.failure_marker(text) is None and not qi.is_failed_result(text)
    assert qi.failure_marker(line) is None
    assert qi.count_wasted_calls([text, line]) == 0


@pytest.mark.parametrize("spec", _LINE_SPECS)
def test_the_line_trips_no_outage_error_memory_or_fabrication_signal(spec):
    line = _line(*spec)
    low = line.lower()
    hits = [m for m in orch._TRANSIENT_TOOL_ERROR_MARKERS + orch._AUTH_TOOL_ERROR_MARKERS
            if m in low]
    hits += [c for c, pattern in chat_client._ERROR_TEXT_CLASSIFIERS
             if pattern.search(line)]
    hits += smoke.classify_error_labels([line])
    hits += smoke.detect_outage_fallback_signals(line)
    hits += smoke.detect_memory_reference_signals(line)
    hits += smoke.detect_fabrication_signals(line)
    hits += [w for w in ("unavailable", "outage", "error", "failed", "timed out")
             if w in low]
    assert hits == []


def test_the_line_matches_no_golden_must_not_and_no_fabrication_flag():
    goldens = json.loads(_GOLDENS.read_text(encoding="utf-8"))
    questions = goldens["questions"] if isinstance(goldens, dict) else goldens
    assert questions
    for question in questions:
        expect = question.get("expect") or {}
        for line in (_line(*spec) for spec in _LINE_SPECS):
            text = _SHORT_ANSWER + "\n\n" + line
            for pattern in expect.get("answer_must_not_match") or []:
                assert not re.search(pattern, line), (question["id"], pattern)
            assert eval_core.fabrication_flags(expect, question.get("text", ""),
                                               text) == [], question["id"]


def test_the_documented_reask_is_not_scored_as_a_wasted_call():
    first = _SHORT_ANSWER + "\n\n" + _line(1, [500])
    calls = [{"toolUseId": "a", "name": "erp___query_erp"},
             {"toolUseId": "b", "name": "erp___query_erp"}]
    inputs = {"a": '{"question": "list every line this week"}',
              "b": '{"question": "distinct values with a count each, same window"}'}
    results = {"a": first, "b": "| v | n |\n|---|---|\n| x | 3 |"}
    score = determinism.score_tool_calls(calls, inputs, results)
    assert score["wasted_steps"] == []
    assert [c["failed"] for c in score["per_call"]] == [False, False]


def test_the_shared_outcome_fixture_pins_the_short_answer_with_the_line():
    cases = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]
    case = next(c for c in cases if c["id"] == _FIXTURE_CASE_ID)
    assert case["text"] == _SHORT_ANSWER + "\n\n" + _line(1, [500])
    assert case["ui"] == "success" and case["server"] is False
    assert not case.get("divergent")


_WINDOW_FIXTURE_TEXT = (
    "The readings rose through the morning shift.\n\n[engine] 1 of 2 tool results "
    "this turn was cut off: the full rows or time window did not come back. A list "
    "of distinct values, a count, or an earliest or latest value read off the "
    "returned data is not the full result.")


def test_the_shared_outcome_fixture_pins_the_window_cut_line():
    cases = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]
    case = next(c for c in cases if c["id"] == "mode-a-short-answer-with-window-cut-line")
    assert case["text"] == _WINDOW_FIXTURE_TEXT
    assert case["text"].endswith("\n\n" + _line(2, [None]))
    assert case["ui"] == "success" and case["server"] is False
    assert not case.get("divergent") and not qi.is_failed_result(case["text"])


# --------------------------------------------------------------------------- #
# 4b. The activity-trace summary of an annotated JSON answer
# --------------------------------------------------------------------------- #
_SUMMARY_CASES = {
    # answer the system agent returned -> its trace summary, with or without
    # the engine note
    "json-array": ('[{"name": "a"}, {"name": "b"}]', "2 items: a, b"),
    "sitewise-map": (json.dumps({"a_p": [{"value": 1.0, "timestamp": 1}],
                                 "b_p": [{"value": 2.0, "timestamp": 2}]}),
                     "2 time-series: 2 data points total"),
    "json-object": (json.dumps({"answer": "x", "records": [{"id": "r1"}]}),
                    "Result with 2 fields: answer, records"),
}


def _summary(text: str) -> str:
    from agents.tool_result_summary import summarize_tool_result

    return summarize_tool_result("erp___query_erp", {"content": [{"text": text}]})


@pytest.mark.parametrize("case", sorted(_SUMMARY_CASES))
def test_the_trace_summary_reads_an_annotated_answer_like_the_plain_one(case):
    answer, expected = _SUMMARY_CASES[case]
    assert _summary(answer) == expected  # before: no engine note
    annotated = _run_query(answer, _state_after(_row_cut(500)))
    assert trc.RESULT_CUT_NOTE_KEY in annotated
    assert _summary(annotated) == expected  # after: same summary


def test_a_model_object_that_is_only_an_items_list_keeps_its_items():
    # {"items": [...]} plus the note reads as the list it carries; nothing is
    # dropped from the summary.
    annotated = _run_query(json.dumps({"items": [{"name": "a"}]}),
                           _state_after(_row_cut(500)))
    assert _summary(annotated) == "1 items: a"


def test_the_shared_outcome_fixture_pins_the_annotated_summaries():
    cases = {c["id"]: c for c in json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]}
    for case_id, text in (("trace-summary-annotated-json-array", "2 items: a, b"),
                          ("trace-summary-annotated-sitewise-map",
                           "2 time-series: 2 data points total")):
        case = cases[case_id]
        assert case["text"] == text
        assert case["ui"] == "success" and case["server"] is False
        assert not case.get("divergent") and not qi.is_failed_result(text)


# --------------------------------------------------------------------------- #
# 5. Neutrality
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spec", [(1, [7]), (3, [7, 3]), (2, [None])])
def test_the_line_names_no_dataset_word_or_literal(spec):
    line = _line(*spec)
    low = line.lower()
    for token in (story_lookup.STORY_EQUIPMENT_ID, story_lookup.STORY_MES_LINE,
                  story_lookup.STORY_CMMS_ASSET_NUM, story_lookup.DEFECT_CHECK_TYPE,
                  story_lookup.BEARING_FAILURE_CODE):
        assert token.lower() not in low, token
    for word in ("shipment", "customer", "supplier", "machine", "batch", "lot"):
        assert not re.search(rf"\b{word}s?\b", low), word
    # Digits only from the counts passed in.
    assert set(re.findall(r"\d+", line)) <= {"1", "2", "3", "7"}
    assert not re.search(r"\b[A-Za-z0-9]+[-_][A-Za-z0-9]+\b", line)
    assert not re.findall(r"\b[A-Z]{2,}\b", line)
