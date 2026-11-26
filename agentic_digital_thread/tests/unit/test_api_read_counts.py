# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each API data tool reports how much data it read, from its own parse.

A system agent that ran no SQL writes a source-side read entry for its call
when it made a successful data read, and that entry moves the call's record
from unverified to partial. Two reviews found the count of "a successful data
read" inferred from the generic result summarizer wrong both ways: a single
SiteWise current value (no count field in its result) never counted, and an
empty vendor page counted (a list-valued wrapper field read as one success).

The count now comes from the tool itself. Each declared data tool reports,
from its own parse of the source's response, how many values, points or
records it read (``tool_result_capture.report_read_count``), into a slot the
capture hook opens for the call (BeforeToolCallEvent) and reads back after it.
The hook keeps that count per call, for declared data tools, kept calls only.
Nothing in the source's JSON is read as a count, so a source that answers
with a field named like one cannot raise a chip.

Pinned here: every declared data tool of every API toolset reports a
positive count for an answer that holds data and zero for one that holds
none or only count-named fields; the wiring through the hook; a lookup
inside a call is not that call's read; and the production strands executor
end to end. Ids are made up. Offline: every source is faked.
"""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

import agents.provenance_store as provenance_store
import agents.system_agent as sa
import agents.tool_result_capture as trc
from agents import aws_session
from agents.job_store import JobStore
from agents.provenance import mint_query_ref
from agents.runtime_asgi import provenance_context_var, provenance_write_token_var
from tests.unit._api_reads import run_tool
from tests.unit._shared_table import SharedTable

KIND_ATTR = getattr(trc, "READ_KIND_ATTR", "_adt_read_kind")
START, END = "2026-03-02T08:00:00Z", "2026-03-02T09:00:00Z"
EPOCH = 1772438400  # 2026-03-02T08:00Z
ENTRIES = json.dumps([{"assetId": "unit-1", "propertyId": "chan-a"},
                      {"assetId": "unit-1", "propertyId": "chan-b"}])
#: What a source can put in a response to look like it counted something.
FORGED = {"count": 99, "row_count": 99, "item_count": 99, "success_count": 99,
          "total": 99, "totalCount": 99, "total_count": 99, "returned": 99}


# --------------------------------------------------------------------------- #
# Fake sources, each answering with real envelope shapes
# --------------------------------------------------------------------------- #
class _SiteWise:
    """boto3 ``iotsitewise`` stand-in. ``mode``: ``rows`` (a value per
    property, two points per series), ``empty`` or ``forged``."""

    def __init__(self, mode: str):
        self.mode = mode

    def _value(self) -> dict:
        if self.mode != "rows":
            return {}
        return {"value": {"doubleValue": 2.5}, "timestamp": {"timeInSeconds": EPOCH},
                "quality": "GOOD"}

    def get_asset_property_value(self, **_kwargs):
        out = {"propertyValue": self._value()} if self.mode == "rows" else {}
        return {**out, **FORGED} if self.mode == "forged" else out

    def batch_get_asset_property_value(self, entries, **_kwargs):
        return {"successEntries": [{"entryId": e["entryId"], "assetPropertyValue": self._value()}
                                   for e in entries], "errorEntries": [], "skippedEntries": [],
                **(FORGED if self.mode == "forged" else {})}

    def batch_get_asset_property_value_history(self, entries, maxResults, nextToken=None):  # noqa: N803
        points = [{"value": {"doubleValue": 1.0 + i}, "quality": "GOOD",
                   "timestamp": {"timeInSeconds": EPOCH + 60 * i}}
                  for i in range(2 if self.mode == "rows" else 0)]
        return {"successEntries": [{"entryId": e["entryId"], "assetPropertyValueHistory": points}
                                   for e in entries], "errorEntries": [], "skippedEntries": [],
                **(FORGED if self.mode == "forged" else {})}

    def get_asset_property_value_history(self, **_kwargs):
        return {"assetPropertyValueHistory": []}


def _vqt(i: int, value=1.0) -> dict:
    return {"value": value, "quality": "Good", "timestamp": f"2026-03-02T08:{i:02d}:00Z"}


class _I3x(sa._I3xClient):
    """An i3X client whose server is faked, with the envelope already
    stripped as ``request`` returns it."""

    def __init__(self, mode: str):
        super().__init__("https://i3x.invalid", "", "unused", "bearer")
        self.mode = mode

    def request(self, method, path, payload, body_cap=0):
        ids = payload.get("elementIds") or ["unit-1:chan-a"]
        if self.mode == "forged":
            return dict(FORGED)
        if path == "/objects/value":
            value = 7.5 if self.mode == "rows" else None
            return [{"elementId": e, "result": {**_vqt(0, value),
                                                 "quality": "Good" if value else "GoodNoData"}}
                    for e in ids]
        if path == "/objects/history":
            points = [_vqt(i, 1.0 + i) for i in range(3 if self.mode == "rows" else 0)]
            return [{"elementId": e, "result": {"values": points}} for e in ids]
        if path == "/objects/list":
            return [{"elementId": e, "displayName": "Unit 1"} for e in ids]
        raise AssertionError(f"unexpected i3X path {path}")


def _toolset(name: str, mode: str, monkeypatch) -> dict:
    """Every tool of one API toolset over a source answering in ``mode``."""
    monkeypatch.delenv("GRAPH_IDENTIFIER", raising=False)
    if name == "sitewise_direct":
        monkeypatch.setattr(aws_session, "boto3_client_for",
                            lambda service, cfg, default_region=None: _SiteWise(mode))
        tools = sa._make_sitewise_direct_tool(region="us-east-1", connection_config={},
                                              system_id="line-telemetry")
    elif name == "i3x":
        client = _I3x(mode)
        monkeypatch.setattr(sa, "_I3xClient", lambda *a, **k: client)
        monkeypatch.setattr(sa, "_i3x_composite_resolve", lambda asset, prop: (
            [{"source_id": "unit-1:chan-a", "name": "pressure", "asset_path": "Site/Unit 1"}],
            "Unit 1", "exact", ""))
        tools = sa._make_i3x_tools("https://i3x.invalid", token="unused")
    else:
        records = ([{"id": "r1", "qty": 3}, {"id": "r2", "qty": 4}] if mode == "rows"
                   else [] if mode == "empty" else {"records": [], **FORGED})
        monkeypatch.setattr(sa._TulipClient, "get", lambda self, path, params=None: records)
        tools = sa._make_tulip_tools("https://tulip.invalid", token="unused")
    return {t.tool_name: t for t in tools}


#: One call of every declared data tool, per toolset. A data tool missing
#: here fails the pin below until it is listed (and so exercised).
DATA_CALLS = {
    "sitewise_direct": {
        "sitewise_get_asset_property_value": {"asset_id": "unit-1", "property_id": "chan-a"},
        "sitewise_batch_get_property_values": {"entries_json": ENTRIES},
        "sitewise_get_asset_property_history": {"entries_json": ENTRIES, "start_iso": START,
                                                "end_iso": END},
    },
    "i3x": {
        "sitewise_get_asset_current_values": {"element_id": "unit-1"},
        "sitewise_get_asset_history": {"element_id": "unit-1:chan-a", "start_time": START,
                                       "end_time": END},
        "sitewise_get_assets_history": {"element_ids": ["unit-1:chan-a"], "start_time": START,
                                        "end_time": END},
        "sitewise_get_asset_telemetry": {"asset_name": "Unit 1", "start_time": START,
                                         "end_time": END},
        "sitewise_get_telemetry_chart": {"element_id": "unit-1", "start_time": START,
                                         "end_time": END},
    },
    "tulip": {"tulip_query_records": {"table_id": "tbl1"}},
}


def _reported(tool, kwargs):
    """The count ``tool`` reports for one call (``None``: it reported none)."""
    trc.open_read_slot()
    try:
        tool._tool_func(**kwargs)
    finally:
        count = trc.close_read_slot()
    return count


@pytest.mark.parametrize("toolset", sorted(DATA_CALLS))
def test_every_declared_data_tool_reports_a_count_for_data_it_read(toolset, monkeypatch):
    """A data tool that reports no count, or a new data tool not listed in
    ``DATA_CALLS``, fails here."""
    tools = _toolset(toolset, "rows", monkeypatch)
    declared = {name for name, t in tools.items() if getattr(t, KIND_ATTR, None) == "data"}
    assert set(DATA_CALLS[toolset]) == declared
    counts = {name: _reported(tools[name], kwargs) for name, kwargs in DATA_CALLS[toolset].items()}
    assert {name: isinstance(c, int) and c > 0 for name, c in counts.items()} == {
        name: True for name in counts}, counts


@pytest.mark.parametrize("toolset", sorted(DATA_CALLS))
def test_an_answer_without_data_reports_zero(toolset, monkeypatch):
    """An answer that holds no value, point or record reports zero."""
    tools = _toolset(toolset, "empty", monkeypatch)
    counts = {name: _reported(tools[name], kwargs) for name, kwargs in DATA_CALLS[toolset].items()}
    assert counts == {name: 0 for name in counts}


@pytest.mark.parametrize("toolset", sorted(DATA_CALLS))
def test_count_fields_in_the_source_answer_never_make_a_read(toolset, monkeypatch):
    """A source that fills its answer with fields named like counts
    (``FORGED``) and holds no data: every data tool reports zero, or nothing
    when it could not parse the answer at all. Never a positive count."""
    tools = _toolset(toolset, "forged", monkeypatch)
    counts = {name: _reported(tools[name], kwargs) for name, kwargs in DATA_CALLS[toolset].items()}
    assert {name: c or 0 for name, c in counts.items()} == {name: 0 for name in counts}, counts


# --------------------------------------------------------------------------- #
# A lookup inside a call is not the call's read
# --------------------------------------------------------------------------- #
def _tag_answer(lookup_rows: int, rerun_rows: int):
    """cmms-api answers for an ``asset``-filtered asset-maint read given a
    tag: no rows for the tag itself, ``lookup_rows`` Assets carrying that
    tag, and ``rerun_rows`` rows for the resolved key (the Asset's ``id``,
    which asset-maint.asset holds)."""
    def answer(entity, filters):
        if entity == "Asset":
            rows = [{"id": f"A-{n}", "asset_id": n, "tag_number": "TAG-9"}
                    for n in range(lookup_rows)]
            return {"success": True, "msg": "", "Assets": rows, "totalCount": len(rows),
                    "returned": len(rows)}
        rows = [{"id": n, "asset": "A-0"} for n in range(rerun_rows)] \
            if filters.get("asset") == "A-0" else []
        return {"success": True, "msg": "", "asset_maints": rows, "totalCount": len(rows),
                "returned": len(rows)}
    return answer


def test_the_charts_a_history_read_carries_are_not_its_read(monkeypatch):
    """A batch history read renders charts for its asset with a second
    history read (the guaranteed-charts suffix); the count is the read the
    answer carries, not the chart read after it."""
    tools = _toolset("i3x", "rows", monkeypatch)
    tool = tools["sitewise_get_assets_history"]
    kwargs = DATA_CALLS["i3x"]["sitewise_get_assets_history"]
    trc.open_read_slot()
    try:
        raw = tool._tool_func(**kwargs)
    finally:
        count = trc.close_read_slot()
    assert "<chart-block" in raw  # the chart read happened
    assert count == 3


# --------------------------------------------------------------------------- #
# The capture hook keeps a reported count per kept data call
# --------------------------------------------------------------------------- #
def _hook():
    return trc.InvocationToolResultCapture(
        system_id="line-telemetry",
        read_kinds={"read_values": "data", "list_things": "discovery"})


def _call(hook, state, name, *, report=None, retry=False, exception=None,
          status="success", text='{"ok": true}'):
    """One call through ``hook``: the tool reports ``report`` (``None``: no
    report) between the Before and After events."""
    tool_use = {"toolUseId": f"t-{name}-{len(state.get('_calls', []))}", "name": name,
                "input": {}}
    state.setdefault("_calls", []).append(name)
    hook._on_before_tool_call(SimpleNamespace(selected_tool=None, tool_use=tool_use,
                                              invocation_state=state))
    if report is not None:
        trc.report_read_count(report)
    hook._on_after_tool_call(SimpleNamespace(
        selected_tool=None, tool_use=tool_use, invocation_state=state,
        result={"status": status, "content": [{"text": text}]}, exception=exception,
        retry=retry, agent=SimpleNamespace(messages=[])))


def test_the_hook_keeps_a_reported_count_only_for_a_kept_data_call():
    hook = _hook()
    for kwargs in ({"name": "list_things", "report": 4},          # discovery
                   {"name": "read_values", "report": 3, "retry": True},
                   {"name": "read_values", "report": 3, "exception": RuntimeError("x")},
                   {"name": "read_values", "report": 3, "status": "error"},
                   {"name": "read_values", "report": 0},
                   {"name": "read_values"}):                         # reported nothing
        state: dict = {}
        _call(hook, state, **kwargs)
        assert sa._successful_api_reads(state) == 0, kwargs
    state = {}
    _call(hook, state, "read_values", report=3)
    _call(hook, state, "read_values", report=0)
    _call(hook, state, "read_values", report=5)
    assert sa._successful_api_reads(state) == 2


def test_a_result_full_of_count_fields_is_not_a_read():
    """Reproduction: the count was read from the result's summary, so a
    source that answers with count-named fields made a read. The result's
    content is never read for the count."""
    state: dict = {}
    _call(_hook(), state, "read_values", text=json.dumps(FORGED))
    assert sa._successful_api_reads(state) == 0


def test_a_report_outside_a_call_goes_nowhere():
    """A tool run outside the hook (Mode B, onboarding checks, tests) has no
    slot to report into; reporting is a no-op there."""
    assert trc.close_read_slot() is None
    trc.report_read_count(3)
    assert trc.close_read_slot() is None


# --------------------------------------------------------------------------- #
# End to end: the production strands executor, the production hooks
# --------------------------------------------------------------------------- #
SID = "sess-api-reads"


@pytest.fixture
def stores(monkeypatch):
    monkeypatch.setattr(provenance_store, "_job_store",
                        JobStore(provenance_store._KIND, table=SharedTable()))
    monkeypatch.setattr(provenance_store, "_cap_store",
                        JobStore(provenance_store._CAP_KIND, table=SharedTable()))
    token = provenance_context_var.set(None)
    wtoken = provenance_write_token_var.set(None)
    yield
    provenance_context_var.reset(token)
    provenance_write_token_var.reset(wtoken)


def _scripted_model(tool_name: str, tool_input: dict):
    from strands.models.model import Model

    class _Scripted(Model):
        """Calls ``tool_name`` once, then answers."""

        def __init__(self):
            self.turns = 0

        def update_config(self, **_config):
            return None

        def get_config(self):
            return {}

        def structured_output(self, *_a, **_k):
            raise NotImplementedError

        async def stream(self, messages, tool_specs=None, system_prompt=None, **_kw):
            self.turns += 1
            yield {"messageStart": {"role": "assistant"}}
            if self.turns == 1:
                yield {"contentBlockStart": {"start": {"toolUse": {
                    "toolUseId": "inner-1", "name": tool_name}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {
                    "input": json.dumps(tool_input)}}}}
                yield {"contentBlockStop": {}}
                yield {"messageStop": {"stopReason": "tool_use"}}
                return
            yield {"contentBlockDelta": {"delta": {"text": "Here is what was read."}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}

    return _Scripted()


def _answer_through_a_real_agent(monkeypatch, toolset, connection_type, tool_name, mode):
    """One Mode A call of a system agent built like production (its tools,
    ``_system_agent_hooks``), the model scripted to call ``tool_name`` once;
    the store entry its call wrote, or None."""
    from strands import Agent
    tools = list(_toolset(toolset, mode, monkeypatch).values())
    system_id = "line-telemetry"
    agent = Agent(model=_scripted_model(tool_name, DATA_CALLS[toolset][tool_name]),
                  tools=tools, callback_handler=None,
                  hooks=sa._system_agent_hooks(system_id, connection_type, "", tools))
    ref = mint_query_ref(SID, f"orch-{toolset}-{mode}")
    arg = provenance_store.issue_provenance_arg(SID, ref)
    provenance_context_var.set((SID, ref))
    provenance_write_token_var.set(arg.get("write_token"))
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
    monkeypatch.setattr(sa, "_concept_mappings_block", lambda *_a: "")
    answer = asyncio.run(sa._run_mode_a_query(agent, system_id, "what was read?"))
    assert answer.startswith("Here is what was read.")
    return provenance_store.get_query(SID, ref)


