# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which API reads can be evidence, decided at the tool definition.

A system agent that ran no SQL writes a source-side read entry for its call
when it made a successful DATA read (``system_agent._successful_api_reads``),
and that entry moves the call's record from unverified to partial. Review of
the first version found three ways a result that is not evidence got
counted:

* discovery calls (list, describe, hierarchy, schema and metadata reads)
  never run SQL, so nothing marked them as catalog probes, and a call whose
  every data read failed still read as corroborated;
* a retried attempt (``AfterToolCallEvent.retry``: strands discards its
  result and calls the tool again) was retained like a kept one;
* ``{"count": 0}`` was not an empty result.

Each API tool factory now declares the kind of read its tools make
(``tool_result_capture.declare_read``), every tool of every REST-family and
SiteWise (Direct) toolset is pinned here, so a new tool fails until it is
classified, and the capture hook keeps that kind on the retained metadata.
A retried attempt is not retained at all, so no reader of the retained
metadata sees a result strands threw away.

Ids are made up. Offline: no AWS, no network (the services are faked).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import agents.system_agent as sa
import agents.tool_result_capture as trc
from agents import aws_session

#: The attribute and values as the capture layer spells them; the defaults
#: keep this file importable where the declaration does not exist yet.
KIND_ATTR = getattr(trc, "READ_KIND_ATTR", "_adt_read_kind")
KIND_KEY = getattr(trc, "READ_KIND_KEY", "_read_kind")
DATA, DISCOVERY = "data", "discovery"

SITEWISE_DIRECT = {
    "sitewise_list_asset_models": DISCOVERY,
    "sitewise_list_assets": DISCOVERY,
    "sitewise_describe_asset_model": DISCOVERY,
    "sitewise_get_asset_property_value": DATA,
    "sitewise_batch_get_property_values": DATA,
    "sitewise_describe_asset": DISCOVERY,
    "sitewise_list_associated_assets": DISCOVERY,
    "sitewise_get_asset_property_history": DATA,
}
I3X = {
    "sitewise_list_asset_models": DISCOVERY,
    "sitewise_list_assets": DISCOVERY,
    "sitewise_get_related_objects": DISCOVERY,
    "sitewise_get_asset_current_values": DATA,
    "sitewise_get_asset_history": DATA,
    "sitewise_get_assets_history": DATA,
    "sitewise_get_asset_telemetry": DATA,
    "sitewise_get_telemetry_chart": DATA,
    "sitewise_list_namespaces": DISCOVERY,
}
TULIP = {
    "tulip_list_tables": DISCOVERY,
    "tulip_get_table_columns": DISCOVERY,
    "tulip_query_records": DATA,
}


def _toolsets() -> dict:
    """Every API toolset a system agent runs with, built offline."""
    return {
        "sitewise_direct": sa._make_sitewise_direct_tool(
            region="us-east-1", connection_config={}, system_id="line-telemetry"),
        "i3x": sa._make_i3x_tools("https://i3x.invalid", token="unused"),
        "tulip": sa._make_tulip_tools("https://tulip.invalid", token="unused"),
    }


EXPECTED = {"sitewise_direct": SITEWISE_DIRECT, "i3x": I3X, "tulip": TULIP}


@pytest.mark.parametrize("toolset", sorted(EXPECTED))
def test_every_api_tool_declares_the_kind_of_read_it_makes(toolset):
    """A new tool in any of these toolsets fails here until it is classified:
    discovery (what exists: lists, descriptions, hierarchy, schema, mappings)
    or data (readings, records, status, events). Only a data read can be
    evidence for an answer."""
    tools = _toolsets()[toolset]
    assert {t.tool_name: getattr(t, KIND_ATTR, None) for t in tools} == EXPECTED[toolset]


# --------------------------------------------------------------------------- #
# The production capture hook keeps the declared kind; retried attempts are
# never retained
# --------------------------------------------------------------------------- #
class _SiteWise:
    """A SiteWise client stand-in: two current values per batch read."""

    def batch_get_asset_property_value(self, entries, **_kwargs):
        return {"successEntries": [{"entryId": e["entryId"], "assetPropertyValue": {
            "value": {"doubleValue": 2.5}, "timestamp": {"timeInSeconds": 1772438400},
            "quality": "GOOD"}} for e in entries], "errorEntries": [], "skippedEntries": []}


VALUES_TOOL = "sitewise_batch_get_property_values"
VALUES_INPUT = {"entries_json": json.dumps([{"assetId": "unit-1", "propertyId": p}
                                            for p in ("chan-a", "chan-b")])}


def _capture_hook(monkeypatch):
    """The capture hook exactly as a SiteWise (Direct) system agent gets it
    (``system_agent._system_agent_hooks``), with the service faked."""
    monkeypatch.setattr(aws_session, "boto3_client_for",
                        lambda service, cfg, default_region=None: _SiteWise())
    tools = sa._make_sitewise_direct_tool(region="us-east-1", connection_config={},
                                          system_id="line-telemetry")
    hook = sa._system_agent_hooks("line-telemetry", "SiteWise (Direct)", "", tools)[1]
    return hook, {t.tool_name: t for t in tools}


def _run(hook, state: dict, name: str, tool_input: dict, text: str, *,
         retry: bool = False, exception: "Exception | None" = None) -> None:
    hook._on_after_tool_call(SimpleNamespace(
        selected_tool=None, tool_use={"toolUseId": "t-1", "name": name, "input": tool_input},
        invocation_state=state, result={"status": "success", "content": [{"text": text}]},
        exception=exception, retry=retry, agent=SimpleNamespace(messages=[])))


def _values(tools) -> str:
    return tools[VALUES_TOOL]._tool_func(**VALUES_INPUT)


def test_the_production_hook_knows_each_tools_declared_read_kind(monkeypatch):
    """The capture hook a SiteWise (Direct) system agent gets holds every
    tool's declared kind, which decides whose reported count it keeps
    (round 3: the kind is no longer copied onto the retained metadata, which
    no longer decides a read)."""
    hook, tools = _capture_hook(monkeypatch)
    assert hook.read_kinds == SITEWISE_DIRECT
    state: dict = {}
    _run(hook, state, VALUES_TOOL, VALUES_INPUT, _values(tools))
    assert all(KIND_KEY not in item for item in state[trc.TOOL_RESULTS_STATE_KEY])


def test_a_retried_attempt_mints_no_typed_evidence():
    """Reproduction: a retried attempt (strands discards its
    result) still fed the enforce gate's typed-evidence capture while every
    other path dropped it. Only the kept attempt is captured."""
    from agents.response_integrity_contracts import PRIVATE_SOURCE_STATE_KEY
    result = json.dumps({"columns": ["part_no"], "rows": [["P-1"]], "row_count": 1,
                         "_adt_executed_query": "SELECT part_no FROM orders"})
    hook = trc.InvocationToolResultCapture(system_id="erp", boundary="sql")
    state: dict = {}
    _run(hook, state, "query_erp", {"sql": "SELECT part_no FROM orders"}, result, retry=True)
    captured = state.get(PRIVATE_SOURCE_STATE_KEY)
    assert captured is None or (captured.tool_calls, captured.observations) == (0, [])
    _run(hook, state, "query_erp", {"sql": "SELECT part_no FROM orders"}, result)
    assert state[PRIVATE_SOURCE_STATE_KEY].tool_calls == 1


def test_a_retried_attempt_is_not_retained(monkeypatch):
    """strands discards a retried attempt's result and calls the tool again,
    so only the attempt that was kept is retained."""
    hook, tools = _capture_hook(monkeypatch)
    state: dict = {}
    _run(hook, state, VALUES_TOOL, VALUES_INPUT, _values(tools), retry=True)
    assert state.get(trc.TOOL_RESULTS_STATE_KEY, []) == []
    _run(hook, state, VALUES_TOOL, VALUES_INPUT, "", exception=RuntimeError("throttled"))
    assert [item["app_status"] for item in state[trc.TOOL_RESULTS_STATE_KEY]] == ["error"]


def test_a_retried_sql_attempt_records_no_statement():
    """The SQL counterpart: a retried attempt's executed query is not a
    statement the answer rests on, so it records no sidecar row (the one a
    provenance entry and the executed-filters line are built from)."""
    state: dict = {}
    result = json.dumps({"columns": ["part_no"], "rows": [["P-1"]], "row_count": 1,
                         "_adt_executed_query": "SELECT part_no FROM orders"})
    hook = trc.InvocationToolResultCapture(system_id="erp")
    _run(hook, state, "query_erp", {"sql": "SELECT part_no FROM orders"}, result, retry=True)
    assert trc.collect_sidecar_statements(state) == []
    _run(hook, state, "query_erp", {"sql": "SELECT part_no FROM orders"}, result)
    assert [row["query_text"] for row in trc.collect_sidecar_statements(state)] == [
        "SELECT part_no FROM orders"]


def test_the_incomplete_envelope_reads_only_the_kept_attempt(monkeypatch):
    """A reader of the retained metadata: the metadata-only envelope for a
    turn with no final text. Its retried success was thrown away and its
    kept attempt failed, so it reports a failure, not partial work."""
    hook, tools = _capture_hook(monkeypatch)
    state: dict = {}
    _run(hook, state, VALUES_TOOL, VALUES_INPUT, _values(tools), retry=True)
    _run(hook, state, VALUES_TOOL, VALUES_INPUT, "", exception=RuntimeError("throttled"))
    no_text = SimpleNamespace(message={"content": []}, stop_reason="end_turn")
    envelope = json.loads(trc.extract_mode_a_result(no_text, state))
    assert envelope["status"] == "error"
    assert [item["app_status"] for item in envelope["tool_results"]] == ["error"]


def test_the_no_data_line_reads_only_the_kept_attempt(monkeypatch):
    """Another reader: the no-data engine line. A retried read that returned
    values was thrown away and the kept read raised, so the line says no read
    returned data."""
    hook, tools = _capture_hook(monkeypatch)
    state: dict = {}
    _run(hook, state, VALUES_TOOL, VALUES_INPUT, _values(tools), retry=True)
    _run(hook, state, VALUES_TOOL, VALUES_INPUT, "", exception=RuntimeError("throttled"))
    assert sa._no_data_fact(state) != ""
