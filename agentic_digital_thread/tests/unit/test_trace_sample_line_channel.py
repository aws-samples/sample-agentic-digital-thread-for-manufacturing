# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The trace shows a telemetry sample line only from the stored trace entry.

A system agent's answer carries one engine line naming each telemetry series
a history read returned a sample of (``telemetry_coverage.sample_line``). The
tool activity summary of the call cuts a prose answer at 2,000 characters and
summarizes a JSON answer by its shape, so a person reading the trace step
lost the line.

The trace step now shows the line the system runtime stored for the call in
its investigation-trace entry (``system_agent._write_investigation_trace``),
which is written under the per-call capability the orchestrator issues
(``provenance_store.issue_provenance_arg``) and read back only through it
(``provenance_store.read_entry``). The live cloud stream, the local chat path
and the Recent sessions reload each send that stored line as the step's own
``sample_line`` field, shown below the summary. The summary is what the
outcome classifiers read, so it never holds the line: a copy of the line the
summary ends with, whole or cut, is dropped when the stored line is shown.
Nothing in a tool result's text produces the line, and a tool's name only
decides whether to read the store: a tool the runtime wrote no entry for,
whatever it is called, gets none. The summary itself is the structural
summary any tool gets, with no engine line added for any tool.

Each test says whether it reproduces a reported defect, pins the channel or
guards a risk. Every name is made up. Fully offline (no AWS, no sockets).
"""
from __future__ import annotations

import datetime as dt
import json
import queue
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import pytest

import agents.investigation_trace as it
import agents.investigation_trace_store as its
import agents.provenance_store as provenance_store
import agents.route_context_store as route_context_store
import agents.system_agent as sa
from agents import aws_session, id_name_resolver
from agents import telemetry_coverage as tc
from agents import tool_result_capture as trc
from agents.chat_client import (ThinkingFilter, _CloudStreamState, _dict_to_sse_event,
                                _ToolResultHook)
from agents.engine_names import printable_id
from agents.events import ToolResultEvent
from agents.internal_lines import MARKERS
from agents.job_store import JobStore
from agents.provenance import mint_query_ref
from agents.query_intelligence import failure_marker
from agents.runtime_asgi import provenance_context_var, provenance_write_token_var
from agents.tool_result_summary import summarize_tool_result
from tests.unit._shared_table import SharedTable
from tests.unit.test_memory_events_rehydrate import _event, _route
from tests.unit.test_mode_a_result_cut_line import _run_query

#: The session the reload helper (``test_memory_events_rehydrate._route``) reads.
SID = "s1"
TOOL_ID = "t-77"
_HISTORY = "sitewise_get_asset_property_history"
_START = "2026-05-11T09:14:37Z"
_END = "2026-05-11T10:14:37Z"
_PROSE_CUT = 2000
_PROSE_UNIT = "Readings for the window follow in time order with their quality flags. "

#: Two systems with nothing in common: the tool name, the asset and the series.
_SCHEMAS = {
    "kappa": ("sys-kappa___query_sys_kappa", "rig-5d1e", ("axis-u", "axis-v", "axis-w")),
    "renamed": ("plant-q___query_plant_q", "press-07", ("ram-force", "ram-temp")),
}
#: A Direct system whose asset and property ids are UUIDs, which the trace
#: names through the graph (``id_name_resolver``).
_ASSET_UUID = "3f2b9c1e-7a4d-4e8b-9c21-5d6e7f8a9b0c"
_PROP_UUID = "5a6b7c8d-1e2f-4a3b-8c4d-9e0f1a2b3c4d"
_UUID_SCHEMA = {"uuid": ("sys-uuid___query_sys_uuid", _ASSET_UUID, (_PROP_UUID,))}


@pytest.fixture(autouse=True)
def _stores(monkeypatch):
    data = SharedTable()
    monkeypatch.setattr(its, "_job_store", JobStore(its._KIND, table=data))
    monkeypatch.setattr(provenance_store, "_job_store",
                        JobStore(provenance_store._KIND, table=data))
    monkeypatch.setattr(provenance_store, "_cap_store",
                        JobStore(provenance_store._CAP_KIND, table=SharedTable()))
    monkeypatch.setattr(route_context_store, "_job_store",
                        JobStore(kind="route-context", table=None))
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    context = provenance_context_var.set(None)
    write_token = provenance_write_token_var.set(None)
    yield
    provenance_context_var.reset(context)
    provenance_write_token_var.reset(write_token)


def _prose(length: int) -> str:
    return (_PROSE_UNIT * (length // len(_PROSE_UNIT) + 1))[:length].rstrip()


# --------------------------------------------------------------------------- #
# A genuine sampled read, through the real tool, capture hook and query path
# --------------------------------------------------------------------------- #

class _GrowingClient:
    """Serves ``counts[n]`` GOOD values per entry on the n-th history request
    (the last count repeats), spread over the window, one page each."""

    def __init__(self, counts: list[int]):
        self.counts, self.calls = counts, 0

    def batch_get_asset_property_value_history(self, entries, maxResults, nextToken=None):  # noqa: N803
        n = self.counts[min(self.calls, len(self.counts) - 1)]
        self.calls += 1
        base = int(dt.datetime.fromisoformat(_START.replace("Z", "+00:00")).timestamp())
        points = [{"value": {"doubleValue": 0.5 + i / 1000},
                   "timestamp": {"timeInSeconds": base + int(i * 3600 / n)},
                   "quality": "GOOD"} for i in range(n)]
        return {"successEntries": [{"entryId": e["entryId"], "assetPropertyValueHistory": points}
                                   for e in entries],
                "errorEntries": [], "skippedEntries": []}

    def get_asset_property_value_history(self, **kwargs):
        return {"assetPropertyValueHistory": []}


def _capture(state: dict, raw: str, tool_input: dict) -> None:
    event = SimpleNamespace(
        selected_tool=None, tool_use={"toolUseId": "t-1", "name": _HISTORY, "input": tool_input},
        invocation_state=state, result={"status": "success", "content": [{"text": raw}]},
        exception=None, retry=False, agent=SimpleNamespace(messages=[]))
    trc.InvocationToolResultCapture(system_id="sys-x")._on_after_tool_call(event)


def _issue_capability(tool_id: str = TOOL_ID) -> str:
    """Record the call's capability, as the orchestrator does before a
    delegated call, and hand its token to the runtime; the call's query_ref."""
    ref = mint_query_ref(SID, tool_id)
    arg = provenance_store.issue_provenance_arg(SID, ref)
    provenance_context_var.set((SID, ref))
    provenance_write_token_var.set(arg.get("write_token"))
    return ref


def _genuine_answer(monkeypatch, schema: str, body: str,
                    tool_id: str = TOOL_ID) -> tuple[str, str, str]:
    """``(tool name, the answer the orchestrator receives, its sample line)``
    for one delegated call (``tool_id``): two reads of the same window (716
    and then 720 stored values per series) through the real history tool and
    capture hook, then the Mode A query path with the call's capability,
    which appends the engine lines and writes the trace entry."""
    tool, asset, props = {**_SCHEMAS, **_UUID_SCHEMA}[schema]
    client = _GrowingClient([716, 720])
    monkeypatch.setattr(aws_session, "boto3_client_for",
                        lambda service, cfg, default_region=None: client)
    history = next(t for t in sa._make_sitewise_direct_tool(
        region="us-east-1", connection_config={}, system_id="sys-x") if t.tool_name == _HISTORY)
    tool_input = {"asset_ids_json": json.dumps([asset]),
                  "property_ids_json": json.dumps(list(props)),
                  "start_iso": _START, "end_iso": _END}
    state: dict = {}
    for _ in range(2):
        _capture(state, history._tool_func(**tool_input), tool_input)
    _issue_capability(tool_id)
    answer = _run_query(body, state)
    return tool, answer, tc.sample_line(state, printable_id)


# --------------------------------------------------------------------------- #
# The three trace surfaces
# --------------------------------------------------------------------------- #

class _SilentLog:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def debug(self, *a, **k): pass
    def error(self, *a, **k): pass


class Shown(NamedTuple):
    """One trace step as the browser receives it: the ``summary`` the
    outcome is classified from, the stored ``sample`` line and the stored
    ``coverage`` lines, each sent as its own field (empty when none)."""
    summary: str
    sample: str = ""
    coverage: tuple = ()

    @property
    def display(self) -> str:
        """What the step shows, top to bottom: the coverage lines, the
        summary, then the sample line."""
        return "\n\n".join([*self.coverage, self.summary, *([self.sample] if self.sample else [])])


def _from_sse(event: ToolResultEvent) -> Shown:
    payload = json.loads(event.to_sse_format().split("data: ", 1)[1])
    return Shown(payload["summary"], payload.get("sample_line") or "",
                 tuple(payload.get("telemetry_coverage") or ()))


def _cloud(tool: str, text: str, *, integrity: bool = False) -> Shown:
    """The trace step the live cloud stream sends for the call."""
    state = _CloudStreamState(ThinkingFilter(), integrity_mode=integrity, log=_SilentLog(),
                              owner_id="u1", session_id=SID, runtime_session_id="rt-k")
    state.events_for({"message": {"role": "assistant", "content": [
        {"toolUse": {"toolUseId": TOOL_ID, "name": tool, "input": {}}}]}})
    events = state.events_for({"message": {"role": "user", "content": [
        {"toolResult": {"toolUseId": TOOL_ID, "status": "success",
                        "content": [{"text": text}]}}]}})
    state.close()
    return _from_sse(next(e for e in events if isinstance(e, ToolResultEvent)))


def _local(tool: str, text: str, *, integrity: bool = False) -> Shown:
    """The trace step the local chat path sends for the call."""
    q: queue.Queue = queue.Queue()
    _ToolResultHook(q, integrity_mode=integrity, session_id=SID)._on_tool_done(SimpleNamespace(
        tool_use={"name": tool, "toolUseId": TOOL_ID, "input": {}},
        result={"toolUseId": TOOL_ID, "status": "success", "content": [{"text": text}]}))
    emitted = []
    while not q.empty():
        emitted.append(q.get_nowait())
    item = [item for item in emitted if isinstance(item, dict)
            and item.get("event") == "tool_result"][-1]
    return _from_sse(_dict_to_sse_event(item))


def _stored_turn(tool: str, text: str, tool_id: str = TOOL_ID) -> list:
    return [
        _event("e1", 0, "user", [{"text": "What did the window hold?"}]),
        _event("e2", 1, "assistant", [
            {"toolUse": {"toolUseId": tool_id, "name": tool, "input": {"question": "q"}}}]),
        _event("e3", 2, "user", [
            {"toolResult": {"toolUseId": tool_id, "status": "success",
                            "content": [{"text": text}]}}]),
        _event("e4", 3, "assistant", [{"text": "Done."}]),
    ]


def _replay(tool: str, text: str, *, integrity: bool = False) -> Shown:
    """The trace step a reopened chat (Recent sessions) shows for the call."""
    resp, _ = _route(_stored_turn(tool, text),
                     integrity="enforce" if integrity else "report-only")
    [block] = [b for m in resp["messages"] for b in m["blocks"] if b["type"] == "tool_result"]
    return Shown(block["summary"], block.get("sample_line") or "",
                 tuple(block.get("telemetry_coverage") or ()))


_SURFACES = {"cloud": _cloud, "local": _local, "replay": _replay}


def _structural(surface: str, tool: str, text: str) -> Shown:
    """The step the surface shows for any tool's result: the structural
    summary and no line. The cloud stream summarizes without the tool name,
    the other two with it."""
    return Shown(summarize_tool_result("" if surface == "cloud" else tool,
                                       {"content": [{"text": text}]}))


def _holds_a_copy(summary: str) -> bool:
    """Whether a line of ``summary`` is the sample line's prefix, whole or cut."""
    return any(line.startswith(tc.SAMPLE_LINE_PREFIX)
               or (line and tc.SAMPLE_LINE_PREFIX.startswith(line))
               for line in summary.split("\n"))


# --------------------------------------------------------------------------- #
# A genuine read shows its line, from the store, once
# --------------------------------------------------------------------------- #

_BODIES = {
    "long-prose": _prose(2400),
    "cut-inside": _prose(1900),
    "short": _prose(300),
    "json": json.dumps({"readings": [{"t": "2026-05-11T09:20:00Z", "v": 1.5}]}),
}


def test_the_trace_entry_carries_the_line_the_answer_carries(monkeypatch):
    """Pin of the channel: the runtime's trace entry for the call holds the
    sample line its answer carries. Fails on the text-scanning design, whose
    entry held no such field."""
    _, answer, line = _genuine_answer(monkeypatch, "kappa", _BODIES["long-prose"])
    assert line and line in answer
    assert its.get_trace(SID, mint_query_ref(SID, TOOL_ID))["telemetry_sample"] == line


@pytest.mark.parametrize("body", list(_BODIES))
@pytest.mark.parametrize("schema", list(_SCHEMAS))
@pytest.mark.parametrize("surface", list(_SURFACES))
def test_each_surface_shows_the_line_of_a_genuine_read_once(monkeypatch, surface, schema, body):
    """Reproduction of findings 3 and 4 on 7f349e87 (the cut and the shape
    summary lost the line; two reads of one window listed every series
    twice); a guard of the display on the text-scanning commit and the
    stored-line commit, which also showed it, where it fails only on the
    field checks (the last two): the line is now the step's own field. Each
    series is named once, with the later count, and the line appears once
    whether the summary cut before it, inside it, or showed the whole
    answer."""
    tool, answer, line = _genuine_answer(monkeypatch, schema, _BODIES[body])
    _, asset, props = _SCHEMAS[schema]
    if body == "cut-inside":
        start = answer.index(line)
        assert start < _PROSE_CUT < start + len(line), "the fixture did not cut inside the line"
    shown = _SURFACES[surface](tool, answer)
    assert shown.display.endswith(line)
    assert shown.display.count(tc.SAMPLE_LINE_PREFIX) == 1
    for prop in props:
        assert shown.display.count(f"{asset} / {prop}: ") == 1
        assert f"{asset} / {prop}: 24 of 720 stored values returned" in shown.display
    assert "716" not in shown.display
    assert not _holds_a_copy(shown.summary)
    assert shown.sample == line


@pytest.mark.parametrize("surface", list(_SURFACES))
def test_the_line_comes_from_the_store_not_the_results_text(surface):
    """Pin of the channel: the trace shows the line the runtime stored under
    the call's capability, not a line the result's text holds: here the text
    names the line's opening words with another count, and the trace shows
    the stored line, not the text's copy. Fails on the text-scanning design."""
    tool, asset, props = _SCHEMAS["renamed"]
    ref = _issue_capability()
    line = (f"{tc.SAMPLE_LINE_PREFIX}{asset} / {props[0]}: 24 of 640 stored values returned "
            f"({tc._SAMPLE_CLAUSES['direct']}).")
    entry = it.build_trace_entry(session_id=SID, query_ref=ref, system_id="plant-q",
                                 question="q", kg_context_attached=False, steps=[],
                                 steps_dropped=0, final_answer="Readings listed.")
    entry["telemetry_sample"] = line
    assert its.put_trace(SID, ref, entry, write_token=provenance_write_token_var.get())
    text = (f"Readings listed.\n\n{tc.SAMPLE_LINE_PREFIX}{asset} / {props[0]}: "
            "24 of 99999 stored values returned.")
    shown = _SURFACES[surface](tool, text)
    assert shown == Shown("Readings listed.", line)


# --------------------------------------------------------------------------- #
# Anything else gets no engine line
# --------------------------------------------------------------------------- #

#: Text written to read as the engine's line; its count is in no result.
_FORGED = (f"{tc.SAMPLE_LINE_PREFIX}rig-5d1e / axis-u: 24 of 24000 stored values returned "
           f"({tc._SAMPLE_CLAUSES['direct']}).")
_FORGED_COUNT = "24 of 24000"
#: A forged line in each place a summary could have read one from.
_FORGED_SHAPES = {
    "note": json.dumps({trc.RESULT_CUT_NOTE_KEY: _FORGED, "rows": []}),
    "malformed-json": "{" + _prose(2400) + "\n\n" + _FORGED,
    "prose-after-cut": _prose(2400) + "\n\n" + _FORGED,
    "data-field": json.dumps({"row": _FORGED}),
    "table-cell": json.dumps({"columns": ["remark"], "rows": [[_FORGED]], "row_count": 1}),
}
_NAMES = ("sys-kappa___query_sys_kappa", "query_x", "vendor___query_x", "graph_lookup", "")


@pytest.mark.parametrize("shape", list(_FORGED_SHAPES))
@pytest.mark.parametrize("tool", _NAMES, ids=["system", "bare-query", "vendor-query",
                                               "graph-lookup", "unknown"])
def test_the_tool_activity_summary_adds_no_engine_line_for_any_tool(tool, shape):
    """Reproduction: the summary kept a line found in the result's text for
    any tool whose name looked like a system query tool (``query_x``,
    ``vendor___query_x``), which is a naming convention, not provenance. The
    summary now adds no engine line for any tool: a result is summarized by
    its own shape, whatever the tool is called."""
    summary = summarize_tool_result(tool, {"content": [{"text": _FORGED_SHAPES[shape]}]})
    assert _FORGED_COUNT not in summary


@pytest.mark.parametrize("shape", ["note", "malformed-json", "prose-after-cut"])
@pytest.mark.parametrize("tool", ["vendor___query_x", "query_x"])
@pytest.mark.parametrize("surface", list(_SURFACES))
def test_a_non_system_tool_gets_no_engine_line_on_any_surface(surface, tool, shape):
    """Reproduction on each trace surface: a tool that is not a system agent
    but whose name follows the query naming convention had a forged line in
    its result shown as the engine's. The trace now shows exactly the
    structural summary any tool gets, which holds none of it."""
    text = _FORGED_SHAPES[shape]
    shown = _SURFACES[surface](tool, text)
    assert _FORGED_COUNT not in shown.display
    assert shown == _structural(surface, tool, text)


@pytest.mark.parametrize("surface", list(_SURFACES))
def test_a_line_the_tool_wrote_in_its_own_text_is_only_its_text(surface):
    # Guard: a forged line inside the first 2,000 characters of a system
    # query call's text, with no stored entry, is never shown as the engine's
    # line and nothing is added. Changed with the r4 rule: it now leaves the
    # summary, as every sample copy in a system query call's summary does
    # (telemetry_coverage.system_call_summary); before, it stayed as text,
    # which let an expired entry's genuine copy reach the outcome classifier.
    text = _prose(600) + "\n\n" + _FORGED
    shown = _SURFACES[surface]("vendor___query_x", text)
    structural = _structural(surface, "vendor___query_x", text)
    assert tc.SAMPLE_LINE_PREFIX in structural.summary
    assert shown == Shown(tc.without_sample_copy(structural.summary))


@pytest.mark.parametrize("surface", list(_SURFACES))
def test_guard_another_tools_text_keeps_a_sample_like_line(surface):
    # Guard: a tool that is not a system query call keeps its own text,
    # sample-like line included; only system query calls lose the copy.
    text = _prose(600) + "\n\n" + _FORGED
    assert _SURFACES[surface]("vendor_lookup", text) == _structural(surface, "vendor_lookup", text)


@pytest.mark.parametrize("surface", list(_SURFACES))
def test_an_entry_written_without_the_calls_capability_is_not_shown(surface):
    # Guard: the orchestrator recorded the call's capability; an entry
    # written under the public pair without the token (the legacy key) is
    # never read, so its line is never shown.
    tool = "vendor___query_x"
    ref = _issue_capability()
    entry = it.build_trace_entry(session_id=SID, query_ref=ref, system_id="vendor",
                                 question="q", kg_context_attached=False, steps=[],
                                 steps_dropped=0, final_answer="x")
    entry["telemetry_sample"] = _FORGED
    assert its.put_trace(SID, ref, entry)
    text = _FORGED_SHAPES["prose-after-cut"]
    shown = _SURFACES[surface](tool, text)
    assert _FORGED_COUNT not in shown.display
    assert shown == _structural(surface, tool, text)


@pytest.mark.parametrize("surface", list(_SURFACES))
def test_the_gated_trace_shows_no_line(monkeypatch, surface):
    # Guard: under the response-integrity gate the trace shows metadata only.
    tool, answer, _ = _genuine_answer(monkeypatch, "kappa", _BODIES["long-prose"])
    assert tc.SAMPLE_LINE_PREFIX not in _SURFACES[surface](tool, answer, integrity=True).display


@pytest.mark.parametrize("surface", list(_SURFACES))
def test_a_failing_store_leaves_the_summary_as_it_is(monkeypatch, surface):
    # Guard: a store fault never breaks or changes the trace step.
    tool, answer, _ = _genuine_answer(monkeypatch, "kappa", _BODIES["long-prose"])

    def boom(*_a, **_k):
        raise RuntimeError("store down")
    monkeypatch.setattr(its, "get_trace", boom)
    assert _SURFACES[surface](tool, answer) == _structural(surface, tool, answer)


def test_no_orchestrator_only_line_rides_with_the_stored_line(monkeypatch):
    # Guard: the trace adds the stored sample line only; the orchestrator-only
    # lines an answer can carry stay off the trace as before.
    tool, answer, line = _genuine_answer(monkeypatch, "kappa", _BODIES["long-prose"])
    tail = "\n\n".join(f"{marker} rig-5d1e-q9 was stated but no result returned it."
                       for marker in MARKERS)
    for surface in _SURFACES:
        shown = _SURFACES[surface](tool, answer + "\n\n" + tail)
        assert shown.sample == line
        assert not any(marker in shown.display for marker in MARKERS)


# --------------------------------------------------------------------------- #
# The summary keeps no copy, and the line never reaches a classifier
# --------------------------------------------------------------------------- #

def _cut_at(answer: str, line: str, offset: int) -> str:
    """``answer`` with its prose resized so the summary's 2,000-character cut
    lands ``offset`` characters into the sample line."""
    start = answer.index(line)
    prose = answer[:start].rstrip("\n")
    gap, tail = answer[len(prose):start], answer[start:]
    want = _PROSE_CUT - offset - len(gap)
    body = next(b for b in (_prose(n) for n in range(want, want + 4)) if len(b) == want)
    return body + gap + tail


@pytest.fixture
def _named_uuids(monkeypatch):
    """The graph names the Direct asset UUID ``Mixer``."""
    monkeypatch.setattr(id_name_resolver, "_CACHE", {})
    monkeypatch.setattr(id_name_resolver, "resolve_id_names",
                        lambda ids, query=None: {_ASSET_UUID: "Mixer"} if _ASSET_UUID in ids else {})


@pytest.mark.parametrize("offset", [len(tc.SAMPLE_LINE_PREFIX) + 10, 5],
                         ids=["inside-the-uuid", "inside-the-prefix"])
@pytest.mark.parametrize("surface", list(_SURFACES))
def test_a_cut_copy_of_the_line_never_shows_beside_the_whole_line(
        monkeypatch, _named_uuids, surface, offset):
    """Reproduction of the annotation defect on the cloud and local surfaces:
    the summary is named (``Mixer (3f2b9c1e...)``) before the stored line is
    added, so a cut inside the asset UUID left a raw partial UUID that no
    longer started the named stored line, and the step showed the prefix
    twice. The copy is now dropped by the prefix, whatever ids were named.
    The other four cases are guards of the display (a cut inside the prefix
    holds no id); on the old code they fail only on the last two checks,
    since the line was not yet its own field. The reload names the stored
    line too (:func:`test_a_reopened_chat_names_the_lines_as_the_live_trace_does`),
    so every surface shows the named line."""
    tool, answer, line = _genuine_answer(monkeypatch, "uuid", _prose(400))
    text = _cut_at(answer, line, offset)
    cut = summarize_tool_result("", {"content": [{"text": text}]}).rsplit("\n", 1)[-1]
    assert line.startswith(cut) and len(cut) == offset, "the fixture did not cut where it says"
    shown = _SURFACES[surface](tool, text)
    named = line.replace(_ASSET_UUID, "Mixer (3f2b9c1e\u2026)")
    assert shown.display.count(tc.SAMPLE_LINE_PREFIX) == 1
    assert shown.display.endswith(named)
    assert not _holds_a_copy(shown.summary)
    assert shown.sample == named


@pytest.fixture
def _named_asset_and_series(monkeypatch):
    """The graph names the Direct asset UUID ``Mixer`` and its property UUID
    ``Torque``; every lookup is counted."""
    calls: list = []

    def resolve(ids, query=None):
        calls.append(list(ids))
        known = {_ASSET_UUID: "Mixer", _PROP_UUID: "Torque"}
        return {i: known[i] for i in ids if i in known}

    monkeypatch.setattr(id_name_resolver, "_CACHE", {})
    monkeypatch.setattr(id_name_resolver, "resolve_id_names", resolve)
    return calls


def _named(text: str) -> str:
    return (text.replace(_ASSET_UUID, "Mixer (3f2b9c1e\u2026)")
            .replace(_PROP_UUID, "Torque (5a6b7c8d\u2026)"))


@pytest.mark.parametrize("surface", list(_SURFACES))
def test_a_reopened_chat_names_the_lines_as_the_live_trace_does(
        monkeypatch, _named_asset_and_series, surface):
    """Reproduction of walk r3 check 3 on 978721f2 (the replay case): the live
    trace names the graph-known ids in a call's stored coverage and sample
    lines (``Mixer (3f2b9c1e...) / Torque (5a6b7c8d...)``), but a chat
    reopened from Recent sessions showed the bare UUIDs, so a reader could
    not tell one series from another. The cloud and local cases are guards:
    they named the lines already (the local chat path sends the sample line
    only, never coverage lines)."""
    tool, answer, line = _genuine_answer(monkeypatch, "uuid", _prose(400))
    coverage = tuple(its.get_trace(SID, mint_query_ref(SID, TOOL_ID))["telemetry_coverage"])
    assert coverage and all(_ASSET_UUID in c for c in coverage), \
        "the fixture stored no coverage line naming the asset"
    shown = _SURFACES[surface](tool, answer)
    assert shown.sample == _named(line)
    assert shown.coverage == (() if surface == "local" else tuple(_named(c) for c in coverage))
    assert _ASSET_UUID not in shown.display and _PROP_UUID not in shown.display


def test_a_reopened_chat_names_every_calls_lines_in_one_lookup(monkeypatch, _named_asset_and_series):
    """Reproduction (fails on 978721f2, which named nothing on reload): two
    sampled calls in one reopened chat both show named lines, and the reload
    asks the graph once for the ids of all of them, so a session's lines
    never cost one graph read per line."""
    tool, answer, line = _genuine_answer(monkeypatch, "uuid", _prose(400))
    _, second, second_line = _genuine_answer(monkeypatch, "uuid", _prose(500), tool_id="t-78")
    events = _stored_turn(tool, answer) + [
        _event("e5", 4, "assistant", [
            {"toolUse": {"toolUseId": "t-78", "name": tool, "input": {"question": "q2"}}}]),
        _event("e6", 5, "user", [
            {"toolResult": {"toolUseId": "t-78", "status": "success",
                            "content": [{"text": second}]}}]),
        _event("e7", 6, "assistant", [{"text": "Done again."}]),
    ]
    _named_asset_and_series.clear()
    resp, _ = _route(events, integrity="report-only")
    blocks = [b for m in resp["messages"] for b in m["blocks"] if b["type"] == "tool_result"]
    assert [b.get("sample_line") for b in blocks] == [_named(line), _named(second_line)]
    assert len(_named_asset_and_series) == 1


def test_guard_a_failing_name_lookup_keeps_the_stored_lines(monkeypatch):
    """Guard (passes on 978721f2): the naming is fail-soft, so a graph that
    cannot be reached leaves the reopened lines as stored."""
    tool, answer, line = _genuine_answer(monkeypatch, "uuid", _prose(400))

    def unreachable(ids, query=None):
        raise RuntimeError("graph unreachable")

    monkeypatch.setattr(id_name_resolver, "_CACHE", {})
    monkeypatch.setattr(id_name_resolver, "resolve_id_names", unreachable)
    shown = _replay(tool, answer)
    assert shown.sample == line
    assert shown.coverage and all(_ASSET_UUID in c for c in shown.coverage)


def test_only_a_trailing_copy_is_dropped():
    # Pin of what is dropped: the last line holding the prefix or a start of
    # it, with only engine lines after it (the no-data line a cut summary
    # keeps). A copy with the tool's own text after it stays, as does a
    # summary with no copy at all.
    no_data = "[engine] No query this call ran returned data (no rows)."
    cut = tc.SAMPLE_LINE_PREFIX + "rig-5d1e / ax"
    assert tc.without_sample_copy(f"Body.\n\n{cut}") == "Body."
    assert tc.without_sample_copy(f"Body.\n\n{cut}\n\n{no_data}") == f"Body.\n\n{no_data}"
    assert tc.without_sample_copy("Body.\n\n[engine] Tele") == "Body."
    assert tc.without_sample_copy(cut) == ""
    assert tc.without_sample_copy(f"{cut}\n\nMore text.") == f"{cut}\n\nMore text."
    assert tc.without_sample_copy(f"Body.\n\n{no_data}") == f"Body.\n\n{no_data}"


#: Short summaries whose stored line names an id that reads as an error
#: marker, shared with the UI's test (trace-sample-line.test.tsx).
_HEAD_CASES = json.loads((Path(__file__).resolve().parents[2] / "frontend" / "src" / "test"
                          / "fixtures" / "trace-sample-line-cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", _HEAD_CASES, ids=[c["id"] for c in _HEAD_CASES])
@pytest.mark.parametrize("surface", list(_SURFACES))
def test_the_line_never_moves_the_classifier_head(surface, case):
    """Reproduction of the classifier-head defect: the stored line was
    appended to the summary, and after a short summary it fell inside the
    240 characters the outcome classifiers read, so an id such as
    ``sensor_error`` or ``comm_exception`` made a successful read fail. The
    line now travels as its own field and the summary is the structural one.
    The case file is shared with the UI's test. The records-found case's
    verdict is a guard (a summary with rows is never a failure); on the old
    code it fails only on the field check."""
    record = case["record"]
    state = _records((record["asset"], record["property"], record["returned"],
                      record["read"], record["kind"], False))
    assert tc.sample_line(state, printable_id) == case["sample_line"]
    appended = failure_marker(f"{case['summary']}\n\n{case['sample_line']}")
    assert (appended is not None) == (case["appended_outcome"] == "failed")
    _issue_capability()
    answer = _run_query(case["answer"], state)
    shown = _SURFACES[surface](_SCHEMAS["kappa"][0], answer)
    assert failure_marker(shown.summary) is None
    assert shown == Shown(case["summary"], case["sample_line"])


_COVERAGE_CASES = json.loads((Path(__file__).resolve().parents[2] / "frontend" / "src" / "test"
                              / "fixtures" / "trace-sample-line-cases.json").read_text())[
    "coverage_cases"]


@pytest.mark.parametrize("case", _COVERAGE_CASES, ids=[c["id"] for c in _COVERAGE_CASES])
def test_coverage_lines_never_move_the_classifier_head(case):
    """Reproduction: the cloud stream put the call's stored coverage lines in
    front of its summary, inside the 240 characters the outcome classifiers
    read. A line naming the i3X channel ``sensor_error:raw`` made a
    successful read fail, and three lines pushed a failed answer's error out
    of the head so it read as a success. The lines now travel as the step's
    own ``telemetry_coverage`` field, shown above the summary. The case file
    is shared with the UI's test."""
    ref = _issue_capability()
    entry = it.build_trace_entry(session_id=SID, query_ref=ref, system_id="kappa", question="q",
                                 kg_context_attached=False, steps=[], steps_dropped=0,
                                 final_answer="a", telemetry_coverage=case["telemetry_coverage"])
    assert its.put_trace(SID, ref, entry, write_token=provenance_write_token_var.get())
    prefixed = failure_marker("\n\n".join(case["telemetry_coverage"]) + "\n\n" + case["summary"])
    assert (prefixed is not None) == (case["prefixed_outcome"] == "failed")
    shown = _cloud(_SCHEMAS["kappa"][0], case["answer"])
    assert (failure_marker(shown.summary) is not None) == (case["outcome"] == "failed")
    assert shown == Shown(case["summary"], "", tuple(case["telemetry_coverage"]))


def test_the_tool_result_frame_carries_the_line_only_when_there_is_one():
    # Pin of the frame: ``sample_line`` is its own key, sent only when set.
    def payload(**fields):
        return json.loads(ToolResultEvent(tool_name="t", **fields).to_sse_format()
                          .split("data: ", 1)[1])
    assert "sample_line" not in payload(summary="1 row")
    assert "sample_line" not in payload(summary="1 row", sample_line="")
    assert payload(summary="1 row", sample_line=_FORGED) == {
        "tool": "t", "tool_id": "", "summary": "1 row", "input": None, "sample_line": _FORGED}


# --------------------------------------------------------------------------- #
# The store and the reload read
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value, kept", [
    (_FORGED, True),
    (7, False),
    (_FORGED[:-1] + "\nsecond line.", False),
    (tc.SAMPLE_LINE_PREFIX + "x" * tc.MAX_SAMPLE_LINE_CHARS + ".", False),
    (_FORGED[len("[engine] "):], False),
    ("", False),
], ids=["line", "not-text", "two-lines", "over-bound", "no-prefix", "empty"])
def test_the_store_keeps_only_one_bounded_sample_line(value, kept):
    """Pin of the channel's bound: the store keeps the field only when it is
    one line of the shape the engine writes, at most the line's own bound.
    The kept case fails on the text-scanning design, whose store dropped the
    field."""
    ref = "q-0123456789ab"
    entry = it.build_trace_entry(session_id=SID, query_ref=ref, system_id="x", question="q",
                                 kg_context_attached=False, steps=[], steps_dropped=0,
                                 final_answer="a")
    entry["telemetry_sample"] = value
    assert its.put_trace(SID, ref, entry)
    assert its.get_trace(SID, ref).get("telemetry_sample", "") == (value if kept else "")


_DIRECT_ENTRY = "rig-5d1e / axis-u: 24 of 720 stored values returned"
_I3X_ENTRY = "rig-5d1e / axis-v: 3 of 720 values read returned"
_TAIL = "a series read again over the same window counts once."
#: One-line values that open with the prefix but are not the engine's line.
_MALFORMED = {
    "prose": "not an engine-rendered sample",
    "no-clause": f"{_DIRECT_ENTRY}.",
    "other-kinds-clause": f"{_DIRECT_ENTRY} ({tc._SAMPLE_CLAUSES['i3x']}).",
    "made-up-clause": f"{_DIRECT_ENTRY} (every value is here).",
    "returned-not-fewer": f"rig-5d1e / axis-u: 720 of 720 stored values returned "
                          f"({tc._SAMPLE_CLAUSES['direct']}).",
    "leading-zero": f"rig-5d1e / axis-u: 024 of 720 stored values returned "
                    f"({tc._SAMPLE_CLAUSES['direct']}).",
    "name-with-space": f"rig 5d1e / axis-u: 24 of 720 stored values returned "
                       f"({tc._SAMPLE_CLAUSES['direct']}).",
    "text-after-the-stop": f"{_FORGED[len(tc.SAMPLE_LINE_PREFIX):]} Every value was read.",
    "kinds-out-of-order": f"{_I3X_ENTRY} ({tc._SAMPLE_CLAUSES['i3x']}); {_DIRECT_ENTRY} "
                          f"({tc._SAMPLE_CLAUSES['direct']}).",
    "kind-twice": f"{_I3X_ENTRY} ({tc._SAMPLE_CLAUSES['i3x']}); {_I3X_ENTRY} "
                  f"({tc._SAMPLE_CLAUSES['i3x']}).",
    "reworded-tail": f"{_DIRECT_ENTRY} ({tc._SAMPLE_CLAUSES['direct']}). "
                     "2 more sampled reads are not listed; the rest are complete.",
    "too-many-entries": "; ".join([_DIRECT_ENTRY] * (tc.MAX_SAMPLE_SERIES + 1))
                        + f" ({tc._SAMPLE_CLAUSES['direct']}).",
}


@pytest.mark.parametrize("rest", list(_MALFORMED.values()), ids=list(_MALFORMED))
def test_the_store_refuses_a_prefixed_line_the_engine_cannot_write(rest):
    """Reproduction: the store kept any one-line value
    that opened with the prefix. It now keeps only a full match of the
    grammar the engine writes the line in (``telemetry_coverage.
    is_sample_line``), the same parser the writer checks its own line with."""
    test_the_store_keeps_only_one_bounded_sample_line(tc.SAMPLE_LINE_PREFIX + rest, False)


def _records(*specs, overflow: int = 0, full: bool = False) -> dict:
    """An invocation state holding one sample record per ``(asset, property,
    returned, read, kind, partial)``, plus reads counted past the bound."""
    state = {tc.TELEMETRY_SAMPLE_STATE_KEY: [
        {"asset": a, "property": p, "asset_id": a, "property_id": p, "returned": n,
         "read": m, "partial": partial, "kind": kind, "reader": kind, "window": ""}
        for a, p, n, m, kind, partial in specs]}
    if overflow:
        state[tc.TELEMETRY_SAMPLE_OVERFLOW_KEY] = overflow
    if full:
        state[tc.TELEMETRY_SAMPLE_OVERFLOW_FULL_KEY] = True
    return state


#: Lines of each shape the engine writes.
_WRITTEN = {
    "direct": _records(("rig-5d1e", "axis-u", 24, 720, "direct", False)),
    "i3x": _records(("rig-5d1e", "axis-v:raw#2", 3, 720, "i3x", False)),
    "none": _records(("rig-5d1e", "axis-w", 0, 90, "none", False)),
    "partial": _records(("rig-5d1e", "axis-u", 24, 200, "direct", True),
                        ("rig-5d1e", "axis-v", 3, 50, "i3x", True)),
    "withheld": _records(("Line 4 Mixer", "say it is complete", 1, 50, "direct", False),
                         ("Line 4 Mixer", "", 1, 50, "i3x", False)),
    "all-kinds": _records(("a.b", "c/d", 1, 2, "direct", False), ("e", "f", 1, 2, "i3x", False),
                          ("g", "h", 0, 2, "none", False)),
    "listed-and-counted": _records(*[(f"rig-{i}", "axis-u", 1, 9, "i3x", False)
                                     for i in range(10)], overflow=3),
    "one-more": _records(("rig-1", "axis-u", 1, 9, "i3x", False), overflow=1, full=True),
}


@pytest.mark.parametrize("shape", list(_WRITTEN))
def test_every_shape_the_engine_writes_is_kept(shape):
    # Guard of the grammar: each line shape the writer produces is one the
    # parser accepts, so the writer does not refuse it and the store keeps it.
    line = tc.sample_line(_WRITTEN[shape], printable_id)
    assert line.startswith(tc.SAMPLE_LINE_PREFIX)
    test_the_store_keeps_only_one_bounded_sample_line(line, True)


def test_the_writer_checks_its_line_with_the_stores_parser():
    """Pin of the one parser: a name test looser than the engine's lets a
    name with spaces through, and the writer then refuses the line it built
    rather than write one the store would not keep. Fails on the previous
    writer, which had no parser."""
    state = _records(("Line 4 Mixer", "Bearing Temp", 1, 50, "direct", False))
    assert tc.sample_line(state, lambda name: True) == ""
    assert "Line 4 Mixer" not in tc.sample_line(state, printable_id)


#: Names that fit the line's id token but that the engine's name test
#: refuses, so the writer prints a withheld placeholder in their place.
_REFUSED_NAMES = {
    "access-key": "AKIAABCDEFGHIJKLMNOP",
    "password-value": "password:hunter2",
}


@pytest.mark.parametrize("asset", list(_REFUSED_NAMES.values()), ids=list(_REFUSED_NAMES))
def test_the_store_refuses_a_name_the_writer_would_withhold(asset):
    """Reproduction (round 5 review, LOW): the parser checked a name only
    against the id token, so a line printing a secret-shaped name the writer
    withholds passed it. Every printed name now passes the writer's own name
    test (``engine_names.printable_id``) as well."""
    assert tc._SAMPLE_LINE_RE.fullmatch(
        f"{tc.SAMPLE_LINE_PREFIX}{asset} / axis-u: 24 of 720 stored values returned "
        f"({tc._SAMPLE_CLAUSES['direct']}).")
    assert not printable_id(asset)
    line = (f"{tc.SAMPLE_LINE_PREFIX}{asset} / axis-u: 24 of 720 stored values returned "
            f"({tc._SAMPLE_CLAUSES['direct']}).")
    test_the_store_keeps_only_one_bounded_sample_line(line, False)


@pytest.mark.parametrize("asset", list(_REFUSED_NAMES.values()), ids=list(_REFUSED_NAMES))
def test_the_writer_withholds_a_refused_name_and_the_store_keeps_its_line(asset):
    # Guard of the round trip: the writer prints the placeholder for a
    # refused name, and the store keeps the line it wrote.
    line = tc.sample_line(_records((asset, "axis-u", 24, 720, "direct", False)), printable_id)
    assert asset not in line and "an asset whose name is withheld" in line
    test_the_store_keeps_only_one_bounded_sample_line(line, True)


def test_the_name_token_is_the_engine_name_tests_id():
    # Pin: the line's name token is the id shape the engine-text name test
    # passes (engine_names._ID_RE within engine_names.MAX_CHARS).
    from agents import engine_names
    assert engine_names._ID_RE.pattern.endswith("]*")
    assert tc._ID_TOKEN == engine_names._ID_RE.pattern[:-1] + f"{{0,{engine_names.MAX_CHARS - 1}}}"


@pytest.mark.parametrize("value", [
    tc.SAMPLE_LINE_PREFIX + "rig-5d1e: 1 of 2 values read returned; " * 45,
    tc.SAMPLE_LINE_PREFIX + "a" * 127 + "/" + "a" * 1800,
    tc.SAMPLE_LINE_PREFIX + "x" * 2_000_000,
], ids=["many-entries-no-clause", "long-run", "2-MB"])
def test_a_line_that_does_not_parse_is_refused_quickly(value):
    # Guard of the parser's cost on hostile text: one bounded pass. The first
    # two are also reproductions: the previous store kept them.
    import time
    started = time.monotonic()
    assert tc.bounded_sample_line(value, printable_id) == ""
    assert time.monotonic() - started < 0.5


def test_a_read_failing_after_a_hit_reads_no_line(monkeypatch):
    """Reproduction: a store read failing after an
    earlier hit returned that hit, so a reload came back partly enriched.
    The read is now all or nothing."""
    def lookup(session_id, query_ref):
        if query_ref == mint_query_ref(SID, "a"):
            return [], _FORGED
        raise RuntimeError("store down")
    monkeypatch.setattr(its, "get_trace_telemetry", lookup)
    assert its.sample_lines_for(SID, ["a", "b"]) == {}


def test_a_reload_whose_second_read_fails_is_left_as_it_was(monkeypatch):
    """Reproduction through the reload route: two
    calls name the line, the first read finds it and the second raises. No
    block takes a line, and both summaries are the structural ones."""
    tool = _SCHEMAS["kappa"][0]
    text = _prose(2400) + "\n\n" + _FORGED
    real = its.get_trace

    def get_trace(session_id, query_ref):
        if query_ref == mint_query_ref(SID, "c1"):
            raise RuntimeError("store down")
        return real(session_id, query_ref)
    ref = mint_query_ref(SID, "c0")
    entry = it.build_trace_entry(session_id=SID, query_ref=ref, system_id="kappa", question="q",
                                 kg_context_attached=False, steps=[], steps_dropped=0,
                                 final_answer="a")
    entry["telemetry_sample"] = _FORGED
    assert its.put_trace(SID, ref, entry)
    monkeypatch.setattr(its, "get_trace", get_trace)
    events = [_event("e0", 0, "user", [{"text": "What did the window hold?"}])]
    for i in range(2):
        events += [_event(f"u{i}", 2 * i + 1, "assistant", [
                       {"toolUse": {"toolUseId": f"c{i}", "name": tool, "input": {}}}]),
                   _event(f"r{i}", 2 * i + 2, "user", [
                       {"toolResult": {"toolUseId": f"c{i}", "status": "success",
                                       "content": [{"text": text}]}}])]
    resp, _ = _route(events)
    blocks = [b for m in resp["messages"] for b in m["blocks"] if b["type"] == "tool_result"]
    assert len(blocks) == 2
    for block in blocks:
        assert "sample_line" not in block
        assert block["summary"] == summarize_tool_result(tool, {"content": [{"text": text}]})


def test_an_entry_without_a_sample_line_keeps_its_shape():
    # Guard: an entry for a call with no sample stays as it was.
    entry = it.build_trace_entry(session_id=SID, query_ref="q-0123456789ab", system_id="x",
                                 question="q", kg_context_attached=False, steps=[],
                                 steps_dropped=0, final_answer="a")
    assert "telemetry_sample" not in entry


def test_the_reload_reads_only_system_query_calls_and_a_bounded_number(monkeypatch):
    """Pin of the reload's read bound: a reopened chat looks the stored trace
    telemetry up only for system query calls, the calls whose runtime writes
    a trace entry (the tool name only decides whether to look; the lines come
    from the entry), and for at most ``MAX_SAMPLE_READS`` of them. A call of
    any other tool is never read. Changed from the earlier pin, which read
    only calls whose result named the sample line: that missed coverage
    lines, which a result never names (integration review 1026)."""
    reads: list = []
    real = its.get_trace
    monkeypatch.setattr(its, "get_trace", lambda s, r: reads.append(r) or real(s, r))
    bound = its.MAX_SAMPLE_READS
    system_tool, other_tool = _SCHEMAS["kappa"][0], "erp_lookup_table"
    calls = bound + 20
    system = {i for i in range(calls) if i % 2 == 0 or i > 20}
    events = [_event("e0", 0, "user", [{"text": "Q?"}])]
    for i in range(calls):
        tool = system_tool if i in system else other_tool
        events += [_event(f"u{i}", 2 * i + 1, "assistant", [
                       {"toolUse": {"toolUseId": f"c{i}", "name": tool, "input": {}}}]),
                   _event(f"r{i}", 2 * i + 2, "user", [
                       {"toolResult": {"toolUseId": f"c{i}", "status": "success",
                                       "content": [{"text": f"Answer {i}."}]}}])]
    assert len(system) > bound
    _route(events)
    assert len(reads) == bound
    assert set(reads) <= {mint_query_ref(SID, f"c{i}") for i in system}


_COVERAGE = "Telemetry read: rig-5d1e / axis-u, 2026-05-11T09:00:00Z to 2026-05-11T10:00:00Z, 24 points"


def test_a_sampled_call_after_the_read_bound_of_other_system_calls_is_read_first():
    """Reproduction (integration confirm review 1026 r2, MED): the reload read
    the first ``MAX_SAMPLE_READS`` system query calls in session order, so a
    sampled call after that many coverage-free system calls was never read.
    It kept its sample copy in the summary, and an id such as
    ``sensor_error`` made a successful read show as failed. A call whose
    stored result names the sample line is now read first; the result text
    only orders the reads, and the lines shown are the entry's."""
    case = next(c for c in _HEAD_CASES if c["id"] == "time-series-sensor-error")
    coverage = next(c for c in _COVERAGE_CASES
                    if c["id"] == "coverage-names-an-error-id")["telemetry_coverage"]
    record = case["record"]
    state = _records((record["asset"], record["property"], record["returned"],
                      record["read"], record["kind"], False))
    state[tc.TELEMETRY_COVERAGE_STATE_KEY] = list(coverage)
    ref = _issue_capability()
    answer = _run_query(case["answer"], state)
    assert its.get_trace_telemetry(SID, ref) == (coverage, case["sample_line"])
    tool = _SCHEMAS["kappa"][0]
    bound = its.MAX_SAMPLE_READS
    events = [_event("e0", 0, "user", [{"text": "Q?"}])]
    for i, (tool_id, text) in enumerate([(f"c{i}", f"Answer {i}.") for i in range(bound)]
                                        + [(TOOL_ID, answer)]):
        events += [_event(f"u{i}", 2 * i + 1, "assistant", [
                       {"toolUse": {"toolUseId": tool_id, "name": tool, "input": {}}}]),
                   _event(f"r{i}", 2 * i + 2, "user", [
                       {"toolResult": {"toolUseId": tool_id, "status": "success",
                                       "content": [{"text": text}]}}])]
    resp, _ = _route(events)
    [block] = [b for m in resp["messages"] for b in m["blocks"]
               if b["type"] == "tool_result" and b["tool_id"] == TOOL_ID]
    assert block.get("sample_line") == case["sample_line"]
    assert block.get("telemetry_coverage") == coverage
    assert block["summary"] == case["summary"]
    assert failure_marker(block["summary"]) is None


_PROSE_ANSWER = "The window held one reading at 09:20 of 1.5."


def test_a_sampled_call_past_the_read_bound_keeps_a_structural_summary():
    """Reproduction (integration confirm review 1026 r3, MED): with more
    sampled system calls than ``MAX_SAMPLE_READS``, a call past the bound was
    not read, kept its sample copy in the summary, and an id such as
    ``sensor_error`` made a successful read show as failed. A sampled system
    call's summary now loses the copy whether or not its entry is read; only
    a read call shows the stored lines as their own fields. A prose answer
    carries the engine line as text (a JSON answer carries it as a member the
    summary never shows), so the copy reaches the summary."""
    case = next(c for c in _HEAD_CASES if c["id"] == "time-series-sensor-error")
    record = case["record"]
    state = _records((record["asset"], record["property"], record["returned"],
                      record["read"], record["kind"], False))
    ref = _issue_capability()
    answer = _run_query(_PROSE_ANSWER, state)
    assert its.get_trace_telemetry(SID, ref)[1] == case["sample_line"]
    bound = its.MAX_SAMPLE_READS
    ids = [f"s{i}" for i in range(bound)] + [TOOL_ID]
    for tool_id in ids[:-1]:
        _store_entry(tool_id, coverage=[_COVERAGE], sample=case["sample_line"])
    tool = _SCHEMAS["kappa"][0]
    events = [_event("e0", 0, "user", [{"text": "Q?"}])]
    for i, tool_id in enumerate(ids):
        events += [_event(f"u{i}", 2 * i + 1, "assistant", [
                       {"toolUse": {"toolUseId": tool_id, "name": tool, "input": {}}}]),
                   _event(f"r{i}", 2 * i + 2, "user", [
                       {"toolResult": {"toolUseId": tool_id, "status": "success",
                                       "content": [{"text": answer}]}}])]
    resp, _ = _route(events)
    blocks = {b["tool_id"]: b for m in resp["messages"] for b in m["blocks"]
              if b["type"] == "tool_result"}
    assert all(blocks[i].get("sample_line") == case["sample_line"] for i in ids[:-1])
    last = blocks[TOOL_ID]
    assert "sample_line" not in last and "telemetry_coverage" not in last
    assert last["summary"] == _PROSE_ANSWER
    assert failure_marker(last["summary"]) is None


def test_a_failed_reload_read_keeps_structural_summaries(monkeypatch):
    # Reproduction (same review): when the bulk read failed, a sampled system
    # call kept its sample copy in the summary. It now gets no stored line and
    # no copy. A call whose entry was read and holds no line keeps the
    # result's text as the tool's own (the forged-line guard above).
    case = next(c for c in _HEAD_CASES if c["id"] == "time-series-sensor-error")
    record = case["record"]
    state = _records((record["asset"], record["property"], record["returned"],
                      record["read"], record["kind"], False))
    _issue_capability()
    answer = _run_query(_PROSE_ANSWER, state)
    monkeypatch.setattr(its, "get_trace_telemetry",
                        lambda s, r: (_ for _ in ()).throw(RuntimeError("store down")))
    shown = _replay(_SCHEMAS["kappa"][0], answer)
    assert shown == Shown(_PROSE_ANSWER)
    assert failure_marker(shown.summary) is None



@pytest.mark.parametrize("entry", ["missing", "expired"])
@pytest.mark.parametrize("surface", list(_SURFACES))
def test_a_sampled_call_whose_entry_is_gone_keeps_a_structural_summary(surface, entry, monkeypatch):
    """Reproduction (integration confirm review 1026 r4, MED): a sampled
    system call whose trace entry is missing (a fail-soft write that did not
    land) or expired (the 24-hour TTL, a reopened chat) read as an entry with
    no lines, so its summary kept the sample copy and an id such as
    ``sensor_error`` made a successful read show as failed, live and on
    reload. A system query call's summary now never holds the copy on any
    surface, whatever the read returns; the line is shown only from the
    stored entry."""
    case = next(c for c in _HEAD_CASES if c["id"] == "time-series-sensor-error")
    text = _PROSE_ANSWER + "\n\n" + case["sample_line"]
    assert failure_marker(summarize_tool_result("", {"content": [{"text": text}]})) is not None
    if entry == "expired":
        _store_entry(TOOL_ID, coverage=[_COVERAGE], sample=case["sample_line"])
        monkeypatch.setattr(its, "get_trace", lambda session_id, query_ref: None)
    shown = _SURFACES[surface](_SCHEMAS["kappa"][0], text)
    assert shown == Shown(_PROSE_ANSWER)
    assert failure_marker(shown.summary) is None


def _store_entry(tool_id: str, *, coverage=(), sample: str = "") -> None:
    ref = mint_query_ref(SID, tool_id)
    entry = it.build_trace_entry(session_id=SID, query_ref=ref, system_id="kappa", question="q",
                                 kg_context_attached=False, steps=[], steps_dropped=0,
                                 final_answer="a", telemetry_coverage=list(coverage),
                                 telemetry_sample=sample)
    assert its.put_trace(SID, ref, entry)


def test_a_reload_shows_the_stored_coverage_lines_with_the_sample_line():
    """Reproduction (integration review 1026, MED): the live stream sent a
    telemetry call's stored coverage lines as ``telemetry_coverage``, but a
    reopened chat read only the sample line from the same entry, so the
    coverage lines were lost. Both now come back, from the entry, as their
    own fields; the summary stays the structural one."""
    tool = _SCHEMAS["kappa"][0]
    _store_entry(TOOL_ID, coverage=[_COVERAGE], sample=_FORGED)
    text = "1 time-series answer.\n\n" + _FORGED
    shown = _replay(tool, text)
    assert shown.coverage == (_COVERAGE,)
    assert shown.sample == _FORGED
    assert shown.summary == tc.without_sample_copy(
        summarize_tool_result(tool, {"content": [{"text": text}]}))


def test_a_reload_shows_coverage_for_a_call_whose_result_names_no_sample_line():
    """Reproduction: a complete read stores coverage lines and no sample
    line, and its result never names the sample line, so the reload never
    looked its entry up. A system query call's entry is now read either way."""
    tool = _SCHEMAS["kappa"][0]
    _store_entry(TOOL_ID, coverage=[_COVERAGE])
    shown = _replay(tool, "Every value in the window was read.")
    assert shown.coverage == (_COVERAGE,)
    assert shown.sample == ""
    assert shown.summary == summarize_tool_result(
        tool, {"content": [{"text": "Every value in the window was read."}]})


def test_guard_a_gated_reload_and_another_tool_get_no_coverage():
    # Guard: a gated reload stays gated, and a call of a tool whose runtime
    # writes no trace entry gets no coverage even when an entry exists.
    _store_entry(TOOL_ID, coverage=[_COVERAGE])
    assert _replay(_SCHEMAS["kappa"][0], "Answer.", integrity=True).coverage == ()
    assert _replay("vendor_lookup", "Answer.").coverage == ()


def test_the_bulk_read_is_all_or_nothing_for_coverage_too(monkeypatch):
    """Reproduction of the all-or-nothing rule on the bulk read that now
    returns coverage and sample lines together."""
    def lookup(session_id, query_ref):
        if query_ref == mint_query_ref(SID, "a"):
            return [_COVERAGE], ""
        raise RuntimeError("store down")
    monkeypatch.setattr(its, "get_trace_telemetry", lookup)
    assert its.telemetry_for(SID, ["a", "b"]) == {}
    monkeypatch.setattr(its, "get_trace_telemetry", lambda s, r: ([_COVERAGE], _FORGED))
    assert its.telemetry_for(SID, ["a"]) == {"a": ([_COVERAGE], _FORGED)}
    assert its.sample_lines_for(SID, ["a"]) == {"a": _FORGED}
