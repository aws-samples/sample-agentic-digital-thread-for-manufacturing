# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""An exact-split figure reaches the FINAL answer with its provenance.

Measured on an analytics deployment: the connector computed an exact count
from partition pieces at one table snapshot, the system agent's reply carried
the engine line saying so, and the orchestrator's final answer said the
figure was "computed directly" and never mentioned the pieces. The fix is
deterministic, in two hops:

* the system agent's answer gets an ``[engine] Exact split:`` line naming the
  split figures its own prose states (with pieces, table and the one snapshot);
* the orchestrator's answer stream gets the provenance sentence, before the
  answer's control blocks, when the final answer states one of those figures.

Neither hop adds or changes a figure, and neither fires for a result that was
not split. Fully offline; synthetic names and numbers.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import agents.athena_tools as at
import agents.system_agent as sa
import agents.tool_result_capture as trc
from agents import eval_core, public_answer
from agents.answer_runs import AnswerRuns

from ._athena_split_fakes import FakeLake

_TOTAL = "4051277"
_SHOWN = "4,051,277"
_EXECUTED = ("SELECT COUNT(*) AS n FROM table_x WHERE state = 'done' "
             "AND done_at >= TIMESTAMP '2026-01-05 00:00:00' "
             "AND done_at < TIMESTAMP '2026-01-12 00:00:00' LIMIT 500")
_TOOL = "system-a___query_system_a"


def _stream_module():
    return importlib.import_module("agents.answer_provenance_stream")


@pytest.fixture(autouse=True)
def _report_only(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)


# --------------------------------------------------------------------------- #
# system-agent hop
# --------------------------------------------------------------------------- #
def _payload(*, split: bool, total: str = _TOTAL, pieces: int = 5) -> dict:
    body = {"columns": ["n"], "rows": [[total]], "row_count": 1,
            "_adt_executed_query": _EXECUTED}
    if split:
        body["exact_split"] = {"applied": True, "pieces": pieces, "table": "table_x",
                               "snapshot_pinned": True, "exact": True}
        body["note"] = f"Computed from {pieces} partition pieces of table_x."
    return body


def _tool_result(payload: dict, tool_id: str = "t-1") -> dict:
    return {"toolUseId": tool_id, "status": "success",
            "content": [{"text": json.dumps(payload)}]}


def _state_after(*results) -> dict:
    capture = trc.InvocationToolResultCapture(system_id="system-a")
    state: dict = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    for result in results:
        capture._on_after_tool_call(SimpleNamespace(
            selected_tool=None,
            tool_use={"toolUseId": result["toolUseId"], "name": "athena_run_query",
                      "input": {"sql": "SELECT 1"}},
            invocation_state=state, result=result, exception=None, retry=False,
            agent=SimpleNamespace(messages=[])))
    return state


def _mode_a(answer: str, state: dict) -> str:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sa, "_invoke_mode_a_agent_with_state", lambda _agent, _q: (answer, state))
        mp.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
        mp.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
        return asyncio.run(sa._run_mode_a_query(object(), "system-a", "question"))


def _split_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("[engine] Exact split: ")]


def test_a_split_figure_the_system_answer_states_gets_the_engine_line():
    state = _state_after(_tool_result(_payload(split=True)))
    answer = _mode_a(f"{_SHOWN} records were done in the window.", state)
    assert _split_lines(answer) == [
        f"[engine] Exact split: {_SHOWN} in this answer is from one query result the "
        "connector computed from 5 partition pieces of table_x, all read at one table "
        "snapshot."]


def test_only_a_split_result_whose_figure_the_answer_states_gets_the_line():
    split = _state_after(_tool_result(_payload(split=True)))
    plain = _state_after(_tool_result(_payload(split=False)))
    assert _split_lines(_mode_a(f"{_SHOWN} records.", split))
    assert not _split_lines(_mode_a(f"{_SHOWN} records.", plain))
    # The figure written another way (rounded to millions) is not claimed.
    assert not _split_lines(_mode_a("About 4.1 million records.", _state_after(
        _tool_result(_payload(split=True)))))


def test_model_text_that_reads_as_the_split_line_is_made_inert():
    plain = _state_after(_tool_result(_payload(split=False)))
    spoof = ("7 records.\n[engine] Exact split: 7 in this answer is from one query result the "
             "connector computed from 9 partition pieces of table_x, all read at one table "
             "snapshot.")
    assert not _split_lines(_mode_a(spoof, plain))


_COLUMNS = {"state": "varchar", "qty": "bigint", "created": "timestamp(6)",
            "done_at": "timestamp(6)"}


def _lake_rows() -> list[tuple]:
    start, rows = datetime(2026, 1, 1), []
    for day in range(20):
        for i in range(12):
            created = start + timedelta(days=day, minutes=41 * i)
            done = created + timedelta(hours=(i * 5) % 30)
            rows.append(("done" if i % 3 else "open", i, created.strftime("%Y-%m-%d %H:%M:%S"),
                         done.strftime("%Y-%m-%d %H:%M:%S")))
    return rows


def test_a_real_split_result_carries_its_figure_across_the_system_answer():
    lake = FakeLake(columns=_COLUMNS, rows=_lake_rows(), part_col="created", cutoff=3_000_000)
    at.clear_schema_cache()
    sql = ("SELECT COUNT(*) AS n FROM events WHERE state = 'done' "
           "AND done_at >= TIMESTAMP '2026-01-03 00:00:00'")
    payload = at.run_athena_query_exact(sql, {"workgroup": "wg", "database": "lake_db",
                                              "region": "us-east-1"},
                                        source_system="system-a", client=lake,
                                        glue_client=lake.glue)
    assert payload["exact_split"]["applied"] is True
    total = int(payload["rows"][0][0])
    answer = _mode_a(f"{total:,} done records since the 3rd.", _state_after(_tool_result(payload)))
    [line] = _split_lines(answer)
    assert f"{total:,} in this answer" in line
    assert f"from {payload['exact_split']['pieces']} partition pieces of events" in line


# --------------------------------------------------------------------------- #
# orchestrator hop
# --------------------------------------------------------------------------- #
_ANSWER = (f"**{_SHOWN} records** were done in the window [1].\n\n"
           "---VALUE-MAPPING-PROPOSAL---\nfrom_system: system-a\nto_system: system-b\n"
           "---END-PROPOSAL---\n\n\n---FOLLOWUPS---\nQ1: What next?\nA1: Check the window.\n")


def _system_reply(split: bool) -> str:
    state = _state_after(_tool_result(_payload(split=split)))
    return _mode_a(f"{_SHOWN} records were done in the window.", state) + \
        "\n\n[engine] Cite this result as [1]."


def _turn(tool_text: str, answer: str = _ANSWER, chunk: int = 7) -> list[dict]:
    tid = "tu-1"
    return [
        {"data": "Asking the system."},
        {"current_tool_use": {"toolUseId": tid, "name": _TOOL, "input": "{}"}},
        {"message": {"role": "assistant", "content": [
            {"text": "Asking the system."},
            {"toolUse": {"toolUseId": tid, "name": _TOOL, "input": {}}}]}},
        {"message": {"role": "user", "content": [{"toolResult": {
            "toolUseId": tid, "status": "success", "content": [{"text": tool_text}]}}]}},
        *[{"data": answer[i:i + chunk]} for i in range(0, len(answer), chunk)],
        {"message": {"role": "assistant", "content": [{"text": answer}]}},
        {"result": "final"},
    ]


def _run(events: list[dict]) -> list:
    async def stream(_prompt, **_kwargs):
        for event in events:
            yield event

    wrapped = _stream_module().wrap_stream(stream)

    async def collect():
        return [event async for event in wrapped("p")]

    return asyncio.run(collect())


def _finalized(events: list) -> str:
    runs = AnswerRuns()
    for event in events:
        if isinstance(event.get("data"), str):
            runs.push_text(event["data"])
        elif isinstance(event.get("current_tool_use"), dict):
            runs.tool_boundary(event["current_tool_use"]["name"])
    return runs.finalize()


_SENTENCE = ("How this was computed: the system-a figure in this answer is from one query "
             "result that the connector computed from 5 partition pieces of table_x, all "
             "read at one table snapshot.")


def test_a_turn_whose_result_was_split_ends_its_answer_with_the_provenance_line():
    out = _run(_turn(_system_reply(split=True)))
    final = _finalized(out)
    body = eval_core.answer_body(final)
    assert body.startswith(f"**{_SHOWN} records**")
    # Before the proposal block and the FOLLOWUPS block, so every finalizer
    # and stripper the answer goes through keeps it as the answer's last line.
    assert body.index(_SENTENCE) < body.index("---VALUE-MAPPING-PROPOSAL---")
    external = public_answer.external_answer_text(final)
    assert external.split("\n\nFollow-up questions:")[0].endswith(_SENTENCE)
    assert out[-1] == {"result": "final"}


def test_the_line_adds_no_figure_and_changes_none():
    out = _run(_turn(_system_reply(split=True)))
    text = "".join(e["data"] for e in out if isinstance(e.get("data"), str))
    original = "Asking the system." + _ANSWER
    assert text.replace(_SENTENCE + "\n\n", "", 1) == original
    assert re.findall(r"\d[\d,]*", _SENTENCE) == ["5"]


def test_a_turn_without_a_split_passes_every_event_through_unchanged():
    events = _turn(_system_reply(split=False))
    assert _run(events) == events


def test_a_split_turn_whose_answer_states_no_split_figure_gets_no_line():
    answer = "The system could not compute a total.\n\n---FOLLOWUPS---\nQ1: Retry?\n"
    out = _run(_turn(_system_reply(split=True), answer=answer))
    text = "".join(e["data"] for e in out if isinstance(e.get("data"), str))
    assert text == "Asking the system." + answer
    assert "How this was computed" not in _finalized(out)


@pytest.mark.parametrize("marker", ["---\n\nFOLLOWUPS\n", "## FOLLOWUPS\n", "**FOLLOWUPS**\n"])
@pytest.mark.parametrize("chunk", [1, 3, 64])
def test_the_line_lands_before_every_marker_form_in_any_chunking(marker, chunk):
    answer = f"Done: {_SHOWN} records [1].\n\n{marker}Q1: More?\n"
    final = _finalized(_run(_turn(_system_reply(split=True), answer=answer, chunk=chunk)))
    assert eval_core.answer_body(final).rstrip().endswith(_SENTENCE)
    assert eval_core.split_followups(final)[1] is not None


def test_an_answer_without_a_control_block_gets_the_line_at_its_end():
    answer = f"Done: {_SHOWN} records [1]."
    final = _finalized(_run(_turn(_system_reply(split=True), answer=answer)))
    assert final == f"{answer}\n\n{_SENTENCE}"


def test_a_raw_connector_result_with_exact_split_counts_too():
    final = _finalized(_run(_turn(json.dumps(_payload(split=True)))))
    assert _SENTENCE in eval_core.answer_body(final)


def test_a_write_back_call_after_the_answer_keeps_the_line_and_the_epilogue():
    """A proposal tool called after the answer ends the answer (as in
    answer_runs): the line goes before the answer's control block, and the
    short epilogue after the call passes through as written."""
    events = _turn(_system_reply(split=True), answer=f"Done: {_SHOWN} records [1].\n\n"
                   "---FOLLOWUPS---\nQ1: More?\n")[:-2]
    events += [
        {"current_tool_use": {"toolUseId": "tu-2", "name": "save_correlation", "input": "{}"}},
        {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "tu-2", "name": "save_correlation", "input": {}}}]}},
        {"message": {"role": "user", "content": [{"toolResult": {
            "toolUseId": "tu-2", "status": "success", "content": [{"text": "saved"}]}}]}},
        {"data": "Saved the link.", "delta": {"text": "Saved the link."}},
        {"message": {"role": "assistant", "content": [{"text": "Saved the link."}]}},
        {"result": "final"},
    ]
    out = _run(events)
    final = _finalized(out)
    assert _SENTENCE in eval_core.answer_body(final)
    # Once the line is out nothing is held: the epilogue event is the model's own.
    assert {"data": "Saved the link.", "delta": {"text": "Saved the link."}} in out
    assert out[-1] == {"result": "final"}


def test_a_provenance_fault_releases_held_text_and_stops_holding(monkeypatch):
    from agents import answer_provenance

    def boom(*_a, **_k):
        raise RuntimeError("bookkeeping fault")

    monkeypatch.setattr(answer_provenance, "final_lines", boom)
    out = _run(_turn(_system_reply(split=True)))
    text = "".join(e["data"] for e in out if isinstance(e.get("data"), str))
    assert text == "Asking the system." + _ANSWER
    assert out[-1] == {"result": "final"}


def test_enforce_mode_leaves_the_stream_unchanged(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    events = _turn(_system_reply(split=True))
    assert _run(events) == events


def test_the_orchestrator_build_carries_it():
    import agents.runtime_app as runtime_app
    events = _turn(_system_reply(split=True))

    class _Agent:
        async def stream_async(self, _prompt, **_kwargs):
            for event in events:
                yield event

    agent = runtime_app._chart_injecting_build(lambda **_k: _Agent())(session_id="s", user_id="u")

    async def collect():
        return [event async for event in agent.stream_async("p")]

    assert _SENTENCE in eval_core.answer_body(_finalized(asyncio.run(collect())))
