# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A delegated call whose write capability exists but under which the system
agent wrote nothing is logged when the turn's provenance frame is assembled.

The orchestrator records a capability for each delegated call before it is
sent, and the runtime that answers writes its query entry and its
investigation trace under that capability before its result returns. The
reader reads only the capability key once a capability exists, so evidence a
runtime wrote under the legacy key (a runtime older than the orchestrator)
never reaches the record. The record must stay unverified; the frame
assembly logs one warning, with digests and counts only, so the miss is not
silent.

Generic names only.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

import agents.provenance_store as provenance_store
from agents import investigation_trace_store as its
from agents import runtime_app
from agents.investigation_trace import build_trace_entry
from agents.job_store import JobStore
from agents.provenance import build_provenance_records, mint_query_ref
from agents.runtime_asgi import provenance_context_var, provenance_write_token_var
from tests.unit._shared_table import SharedTable

SID = "session-1800000000000-skew0001"
TOOL_USES = ("tooluse-ledger-0001", "tooluse-ledger-0002")
REFS = tuple(mint_query_ref(SID, t) for t in TOOL_USES)
SQL = "SELECT item_code, qty FROM ledger_items WHERE qty > 5"
MARKER = "PROVENANCE_CAPABILITY_MISS"


@pytest.fixture(autouse=True)
def _stores(monkeypatch):
    cap_table, data_table = SharedTable(), SharedTable()
    monkeypatch.setattr(provenance_store, "_cap_store",
                        JobStore(provenance_store._CAP_KIND, table=cap_table))
    monkeypatch.setattr(provenance_store, "_job_store",
                        JobStore(provenance_store._KIND, table=data_table))
    monkeypatch.setattr(its, "_job_store", JobStore(its._KIND, table=data_table))
    return cap_table


def _query_entry() -> dict:
    return {"system_id": "ledger", "tool_name": "query_ledger", "executed_query": SQL,
            "kg_artifacts": [], "captured_at": "2026-09-01T10:00:00Z",
            "entity": {"table": "ledger_items", "columns": ["item_code", "qty"]},
            "row_count": 3, "statement_count": 1, "origin": "system-agent"}


def _trace_entry(ref: str) -> dict:
    return build_trace_entry(session_id=SID, query_ref=ref, system_id="ledger",
                             question="how many items", kg_context_attached=False,
                             steps=[], steps_dropped=0, final_answer="Three items.")


def _issue(ref: str) -> str:
    """The orchestrator's side of one delegated call: the capability record."""
    token = provenance_store.issue_provenance_arg(SID, ref).get("write_token")
    assert token, "the capability should land on a shared-table store"
    return token


def _old_runtime_writes(ref: str) -> None:
    """A runtime that predates the capability: both stores, legacy key."""
    provenance_store.put_turn_queries(SID, {ref: _query_entry()})
    its.put_trace(SID, ref, _trace_entry(ref))


def _current_runtime_writes(ref: str, token: str, *, query: bool = True) -> None:
    """A current runtime: under the capability; ``query=False`` is a call
    that ran no query (only the trace is written)."""
    if query:
        provenance_store.put_turn_queries(SID, {ref: _query_entry()}, write_token=token)
    its.put_trace(SID, ref, _trace_entry(ref), write_token=token)


def _frame(n: int = 1) -> dict:
    """The report-only runtime's frame for ``n`` delegated calls, enriched."""
    payload = build_provenance_records(
        session_id=SID,
        tool_calls=[{"name": "ledger___query_ledger", "toolUseId": t} for t in TOOL_USES[:n]],
        tool_inputs={}, tool_results_metadata={}, evidence_by_tool={},
        rules_applied=[], verdict=None, captured_at="2026-09-01T10:00:05Z")
    return runtime_app._enriched_provenance_payload(payload, SID)


def _misses(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if MARKER in r.getMessage()]


def test_a_capability_with_only_a_legacy_entry_stays_unverified():
    """The fail-closed reader is kept: the legacy entry never reaches the
    record."""
    _issue(REFS[0])
    _old_runtime_writes(REFS[0])
    [record] = _frame()["records"]
    assert record["verification"] == "unverified"
    assert record["entity"] == {"table": "", "columns": []}
    assert record["result_shape"]["rows"] is None


def test_a_capability_with_only_legacy_entries_logs_one_warning_per_turn(caplog):
    caplog.set_level(logging.INFO)
    tokens = [_issue(ref) for ref in REFS]
    for ref in REFS:
        _old_runtime_writes(ref)
    _frame(n=2)
    [line] = _misses(caplog)
    message = line.getMessage()
    assert line.levelno == logging.WARNING
    assert f"sid_sha={provenance_store.digest12(SID)}" in message
    assert "refs=2" in message and "legacy=2" in message
    assert "version mismatch" in message
    for secret in (SID, *REFS, *tokens, SQL,
                   *(provenance_store.capability_suffix(t, SID, r)
                     for t, r in zip(tokens, REFS))):
        assert secret not in message


def test_a_capability_with_nothing_written_logs_without_claiming_a_mismatch(caplog):
    caplog.set_level(logging.INFO)
    _issue(REFS[0])
    [record] = _frame()["records"]
    assert record["verification"] == "unverified"
    [line] = _misses(caplog)
    assert "refs=1" in line.getMessage() and "legacy=0" in line.getMessage()
    assert "version mismatch" not in line.getMessage()


def test_no_warning_when_the_capability_entry_exists(caplog):
    caplog.set_level(logging.INFO)
    _current_runtime_writes(REFS[0], _issue(REFS[0]))
    [record] = _frame()["records"]
    assert record["verification"] == "partial"
    assert record["entity"]["table"] == "ledger_items"
    assert _misses(caplog) == []


def test_no_warning_for_a_call_that_ran_no_query(caplog):
    """A current runtime that ran no query writes only its trace under the
    capability: nothing is missing, so nothing is logged."""
    caplog.set_level(logging.INFO)
    _current_runtime_writes(REFS[0], _issue(REFS[0]), query=False)
    [record] = _frame()["records"]
    assert record["verification"] == "unverified"
    assert _misses(caplog) == []


class _SeriesClient:
    """A SiteWise client stand-in: 24 points per series on one page."""

    def batch_get_asset_property_value_history(self, entries, maxResults, nextToken=None):  # noqa: N803
        values = [{"value": {"doubleValue": 1.0 + i}, "quality": "GOOD",
                   "timestamp": {"timeInSeconds": 1788253200 + 60 * i}} for i in range(24)]
        return {"successEntries": [{"entryId": e["entryId"], "assetPropertyValueHistory": values}
                                   for e in entries], "errorEntries": [], "skippedEntries": []}

    def get_asset_property_value_history(self, **_kwargs):
        return {"assetPropertyValueHistory": []}


def _history_state(monkeypatch, *, raised: bool = False) -> dict:
    """A call that ran no SQL: one SiteWise (Direct) history read over three
    series, 24 points each, the real tool run through the system agent's real
    capture hook (the tool reports the points it parsed). With ``raised`` the
    read raised instead."""
    import agents.system_agent as sa
    from agents import aws_session
    from tests.unit._api_reads import run_tool
    monkeypatch.setattr(aws_session, "boto3_client_for",
                        lambda service, cfg, default_region=None: _SeriesClient())
    tools = {t.tool_name: t for t in sa._make_sitewise_direct_tool(
        region="us-east-1", connection_config={}, system_id="ledger")}
    hook = sa._system_agent_hooks("ledger", "SiteWise (Direct)", "", list(tools.values()))[1]
    entries = [{"assetId": "unit-1", "propertyId": p} for p in ("chan-a", "chan-b", "chan-c")]
    tool_input = {"entries_json": json.dumps(entries), "start_iso": "2026-09-01T09:00:00Z",
                  "end_iso": "2026-09-01T10:00:00Z"}
    state: dict = {}
    if not raised:
        run_tool(hook, state, tools["sitewise_get_asset_property_history"], tool_input)
        return state
    hook._on_after_tool_call(SimpleNamespace(
        selected_tool=None,
        tool_use={"toolUseId": "t-1", "name": "sitewise_get_asset_property_history",
                  "input": tool_input},
        invocation_state=state, result={"status": "error", "content": []},
        exception=RuntimeError("throttled"), retry=False, agent=SimpleNamespace(messages=[])))
    return state


def _system_agent_writes(ref: str, token: str, state: dict) -> None:
    """The system agent's own out-of-band writes for one answered call, under
    the capability: its query entry (when its capture has one) and its trace."""
    import agents.system_agent as sa
    context = provenance_context_var.set((SID, ref))
    write_token = provenance_write_token_var.set(token)
    try:
        sa._write_provenance_sidecar(state, "ledger")
        sa._write_investigation_trace(state, "ledger", "how did the line run", "It ran.")
    finally:
        provenance_context_var.reset(context)
        provenance_write_token_var.reset(write_token)


def test_a_call_that_read_through_the_api_reads_partial_with_no_rows(caplog, monkeypatch):
    """A call that ran no SQL and read 72 points through the source's API:
    the system agent's read entry makes the record partial (bounded capture
    shows a successful result), with no row count (points are not rows) and
    no table, and no miss is logged."""
    caplog.set_level(logging.INFO)
    _system_agent_writes(REFS[0], _issue(REFS[0]), _history_state(monkeypatch))
    [record] = _frame()["records"]
    assert record["verification"] == "partial"
    assert record["result_shape"]["rows"] is None
    assert record["entity"] == {"table": "", "columns": []}
    assert _misses(caplog) == []


def test_a_call_whose_api_reads_raised_stays_unverified(caplog, monkeypatch):
    """Negative guard: every read raised, so the system agent writes only its
    trace. The record stays unverified, and that is not a miss."""
    caplog.set_level(logging.INFO)
    _system_agent_writes(REFS[0], _issue(REFS[0]), _history_state(monkeypatch, raised=True))
    [record] = _frame()["records"]
    assert record["verification"] == "unverified"
    assert _misses(caplog) == []


def test_no_warning_without_a_capability(caplog, monkeypatch):
    """Local mode: no durable capability store, the legacy key is the key."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(provenance_store, "_cap_store",
                        JobStore(provenance_store._CAP_KIND, table=None))
    _old_runtime_writes(REFS[0])
    [record] = _frame()["records"]
    assert record["verification"] == "partial"
    assert _misses(caplog) == []


def test_an_unreadable_capability_store_is_not_reported_as_a_miss(caplog, _stores):
    """A transient capability-store error fails closed to a miss, as before,
    and is not logged as a capability miss."""
    caplog.set_level(logging.INFO)
    _issue(REFS[0])
    _old_runtime_writes(REFS[0])
    _stores.fail_reads = True
    [record] = _frame()["records"]
    assert record["verification"] == "unverified"
    assert _misses(caplog) == []


def test_the_local_emitter_logs_the_same_miss(caplog):
    from agents.chat_client import _build_local_provenance

    class _Hook:
        session_id = SID
        tool_calls = [{"name": "ledger___query_ledger", "toolUseId": TOOL_USES[0]}]
        tool_inputs: dict = {}
        tool_metadata: dict = {}
        rule_ids: set = set()

    caplog.set_level(logging.INFO)
    _issue(REFS[0])
    _old_runtime_writes(REFS[0])
    payload = _build_local_provenance(_Hook(), rules_applied=[], verdict=None)
    assert payload["records"][0]["verification"] == "unverified"
    [line] = _misses(caplog)
    assert "legacy=1" in line.getMessage()
