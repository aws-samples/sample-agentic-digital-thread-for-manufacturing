# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""LIVE cloud-path provenance pins — fixture derived from a REAL captured
runtime stream (Management env, captured 2026-08-04 via a
direct ``invoke_agent_runtime`` call; session ``repro-raw-1785829400``).

Why this file exists: the earlier sidecar tests pinned a SIMULATED stream
shape. The live investigation showed the runtime's frames DO match the
interceptor (`data: {"provenance_queries": ...}` top-level), and the real
gaps were elsewhere:

1. DURABILITY — ``JobStore`` silently ran per-process memory-only because
   the app ECS task lacked ``ONBOARDING_SESSIONS_TABLE`` (fixed in
   ``stacks/adt_compute_stack.py``): a redemption GET on another
   worker/replica or after a restart 404'd for entries that WERE stored.
2. VISIBLE-REF DISJOINTNESS — in default Mode A the only query-bearing call
   is the bare ``explore_graph`` (never cited, D2), so its stored ref had no
   client-visible handle while every cited record's ref was honestly absent
   (D6). Fixed by the ADDITIVE ``graph_queries`` payload key (refs + names
   only — query text stays sidecar-only, D6 unchanged).

The fixture lines below are structurally VERBATIM from the capture (same
wrapping, same key spellings); only free text is shortened. If the live
runtime shape drifts, THESE tests are the canary. Offline: no AWS/network.
"""
from __future__ import annotations

import json

import pytest

import agents.provenance_store as provenance_store
from agents.chat_client import ThinkingFilter, _CloudStreamState
from agents.events import MetadataEvent, ToolResultEvent, ToolUseEvent
from agents.job_store import JobStore
from agents.provenance import mint_query_ref, uncited_query_refs

LIVE_SESSION = "repro-raw-1785829400"
LIVE_TOOL_ID = "tooluse_Hf27mP9pBTHISLP5g9CYVC"
LIVE_CITED_ID = "tooluse_e4zKJPxQTRxrujNvKmPf2q"
LIVE_CYPHER = (
    "\nMATCH (a:Asset {name:'P-101'})\nRETURN a.source_id, a.name, a.path, "
    "a.system, a.asset_type, a.level\nUNION\nMATCH (vm:ValueMapping)\nWHERE "
    "vm.from_value = 'P-101' OR vm.to_value = 'P-101'\nRETURN vm.from_value "
    "AS source_id, vm.from_field AS name, vm.from_system AS path, "
    "vm.to_system AS system, vm.concept AS asset_type, vm.confidence AS level\n"
)
#: The ref the live runtime emitted — pins the D3 derivation against the
#: real capture (sha256("repro-raw-1785829400|tooluse_Hf27..." )[:12]).
LIVE_REF = "q-e186fa585a45"


def _live_lines() -> list[str]:
    """The captured stream's structural line sequence (free text shortened)."""
    return [
        # graph-first synthetic seed (dict input — forwarded verbatim live)
        'data: ' + json.dumps({"current_tool_use": {
            "toolUseId": "mode-a-route-seed", "name": "explore_graph",
            "input": {"question": "Which systems hold this data?"}}}),
        'data: ' + json.dumps({"message": {"role": "user", "content": [
            {"toolResult": {"toolUseId": "mode-a-route-seed",
                            "status": "success",
                            "content": [{"text": "Graph routing seed applied."}]}}]}}),
        # assistant announces the real calls (message wrapping, live shape)
        'data: ' + json.dumps({"message": {"role": "assistant", "content": [
            {"text": "Checking the graph and CMMS."},
            {"toolUse": {"toolUseId": LIVE_TOOL_ID, "name": "explore_graph",
                         "input": {"cypher": LIVE_CYPHER}}},
            {"toolUse": {"toolUseId": LIVE_CITED_ID, "name": "cmms___query_cmms",
                         "input": {"question": "open work orders for P-101"}}}]}}),
        'data: ' + json.dumps({"message": {"role": "user", "content": [
            {"toolResult": {"toolUseId": LIVE_TOOL_ID, "status": "success",
                            "content": [{"text": '{"results": [], "empty": true}'}]}},
            {"toolResult": {"toolUseId": LIVE_CITED_ID, "status": "success",
                            "content": [{"text": "No asset tagged P-101."}]}}]}}),
        'data: {"data": "Answer text."}',
        # done-time frames, live order: summary -> provenance -> sidecar -> marker
        'data: ' + json.dumps({"intelligence_summary": {
            "total_tool_calls": 3, "neptune_queries": 2, "system_queries": 1,
            "wasted_calls": 0, "rules_applied": []}}),
        'data: ' + json.dumps({"provenance": {"records": [{
            "citation_num": 1, "tool_id": LIVE_CITED_ID,
            "source_system": "cmms", "tool_name": "cmms___query_cmms",
            "entity": {"table": "", "columns": []},
            "scope": {"filters": [], "window": None},
            "query_ref": mint_query_ref(LIVE_SESSION, LIVE_CITED_ID),
            "result_shape": {"rows": None, "truncated": False, "partial_reason": ""},
            "kg_route": {"rules_applied": [], "value_mappings": []},
            "captured_at": "2026-08-04T07:48:24Z",
            "verification": "unverified"}], "truncated": False,
            # ADDITIVE key (this fix): refs only, never query text.
            "graph_queries": [{"query_ref": LIVE_REF,
                               "tool_name": "explore_graph"}]}}),
        'data: ' + json.dumps({"provenance_queries": {LIVE_REF: {
            "system_id": "", "tool_name": "explore_graph",
            "query_text": LIVE_CYPHER, "kg_artifact_ids": [],
            "captured_at": "2026-08-04T07:48:24Z"}}}),
        'data: {"response_integrity": {"mode": "report-only"}}',
    ]


class _FakeStream:
    def __init__(self, lines):
        self._lines = [line.encode() for line in lines]

    def iter_lines(self, chunk_size=1):
        return iter(self._lines)


class _SilentLog:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


@pytest.fixture()
def memory_store(monkeypatch):
    store = JobStore(kind="provenance-query", table=None)
    monkeypatch.setattr(provenance_store, "_job_store", store)
    return store


def _run_live_stream(session_id=LIVE_SESSION):
    state = _CloudStreamState(
        ThinkingFilter(), integrity_mode=False, log=_SilentLog(),
        owner_id="u", session_id=session_id, runtime_session_id="rt-x")
    events = list(state.iter_stream(_FakeStream(_live_lines())))
    state.close()
    return events


def test_live_ref_pins_d3_derivation():
    assert mint_query_ref(LIVE_SESSION, LIVE_TOOL_ID) == LIVE_REF


def test_live_stream_sidecar_intercepted_stored_and_never_broadcast(memory_store):
    events = _run_live_stream()
    # D6: the sidecar frame NEVER becomes an SSE event on the live shape.
    for event in events:
        data = getattr(event, "data", None)
        assert not (isinstance(data, dict) and "provenance_queries" in data)
    # ...and the entry is redeemable exactly as the D5 route reads it.
    entry = provenance_store.get_query(LIVE_SESSION, LIVE_REF)
    assert entry is not None
    assert entry["executed_query"] == LIVE_CYPHER
    assert entry["tool_name"] == "explore_graph"


def test_live_stream_provenance_frame_passes_with_graph_queries(memory_store):
    events = _run_live_stream()
    frames = [e.data["provenance"] for e in events
              if isinstance(e, MetadataEvent) and isinstance(e.data, dict)
              and "provenance" in e.data]
    assert len(frames) == 1
    payload = frames[0]
    [record] = payload["records"]
    assert record["query_ref"] == mint_query_ref(LIVE_SESSION, LIVE_CITED_ID)
    # ADDITIVE surfacing: the turn's redeemable (uncited) graph ref rides
    # the frame — refs and names only, no query text anywhere in the frame.
    assert payload["graph_queries"] == [
        {"query_ref": LIVE_REF, "tool_name": "explore_graph"}]
    assert "P-101" not in json.dumps(payload)


def test_live_stream_tool_events_still_parse(memory_store):
    events = _run_live_stream()
    starts = [e for e in events if isinstance(e, ToolUseEvent)]
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert {e.tool_use_id for e in starts} >= {LIVE_TOOL_ID, LIVE_CITED_ID}
    assert {e.tool_use_id for e in results} >= {LIVE_TOOL_ID, LIVE_CITED_ID}


def test_live_store_round_trip_survives_normalization(memory_store):
    # The stored entry is D5-shaped (query_text -> executed_query at write).
    provenance_store.put_turn_queries(LIVE_SESSION, {LIVE_REF: {
        "system_id": "", "tool_name": "explore_graph",
        "query_text": LIVE_CYPHER, "kg_artifact_ids": [],
        "captured_at": "2026-08-04T07:48:24Z"}})
    entry = provenance_store.get_query(LIVE_SESSION, LIVE_REF)
    assert set(entry) == {"query_ref", "session_id", "system_id", "tool_name",
                          "executed_query", "kg_artifacts", "captured_at"}


# ---------------------------------------------------------------------------
# uncited_query_refs — the pure helper feeding the additive key
# ---------------------------------------------------------------------------

_LIVE_CALLS = [
    {"name": "explore_graph", "toolUseId": "mode-a-route-seed"},
    {"name": "explore_graph", "toolUseId": LIVE_TOOL_ID},
    {"name": "cmms___query_cmms", "toolUseId": LIVE_CITED_ID},
]
_LIVE_INPUTS = {
    "mode-a-route-seed": json.dumps({"question": "Which systems hold this?"}),
    LIVE_TOOL_ID: json.dumps({"cypher": LIVE_CYPHER}),
    LIVE_CITED_ID: json.dumps({"question": "open work orders for P-101"}),
}


def test_uncited_query_refs_live_shape():
    refs = uncited_query_refs(session_id=LIVE_SESSION, tool_calls=_LIVE_CALLS,
                              tool_inputs=_LIVE_INPUTS)
    # ONLY the bare graph call with query text: the seed (NL question) and
    # the cited Mode-A call are both excluded.
    assert refs == [{"query_ref": LIVE_REF, "tool_name": "explore_graph"}]


def test_uncited_query_refs_cited_calls_never_listed():
    # A prefixed graph spelling IS cited (D2) — never in graph_queries.
    refs = uncited_query_refs(
        session_id=LIVE_SESSION,
        tool_calls=[{"name": "x___explore_graph", "toolUseId": "g2"}],
        tool_inputs={"g2": json.dumps({"cypher": "MATCH (n) RETURN n"})})
    assert refs == []


def test_uncited_query_refs_empty_and_malformed():
    assert uncited_query_refs(session_id="s", tool_calls=[], tool_inputs={}) == []
    assert uncited_query_refs(session_id="s", tool_calls=[{"name": "explore_graph"}],
                              tool_inputs={}) == []
    assert uncited_query_refs(session_id="s",
                              tool_calls=[{"name": "explore_graph",
                                           "toolUseId": "g1"}],
                              tool_inputs={"g1": "{'cypher': 'x'"}) == []


def test_uncited_query_refs_bounded():
    calls = [{"name": "explore_graph", "toolUseId": f"g{i}"} for i in range(12)]
    inputs = {f"g{i}": json.dumps({"cypher": f"MATCH (n{i}) RETURN n{i}"})
              for i in range(12)}
    refs = uncited_query_refs(session_id="s", tool_calls=calls, tool_inputs=inputs)
    assert len(refs) == 8  # MAX_GRAPH_QUERY_REFS


# ---------------------------------------------------------------------------
# Emitters — the additive key rides every path's frame
# ---------------------------------------------------------------------------

def _legacy_live_state():
    from types import SimpleNamespace
    return SimpleNamespace(
        session_id=LIVE_SESSION, prompt="which work orders are open",
        tool_calls_log=list(_LIVE_CALLS), tool_inputs=dict(_LIVE_INPUTS),
        tool_results={LIVE_CITED_ID: "No asset tagged P-101."},
        rule_ids=set(), matched_rows=[])


def test_legacy_emitter_frame_carries_graph_queries():
    from agents import runtime_app
    [frame] = runtime_app._provenance_events(_legacy_live_state())
    payload = frame["provenance"]
    assert payload["graph_queries"] == [
        {"query_ref": LIVE_REF, "tool_name": "explore_graph"}]
    # D6 leak pin: no query text anywhere in the broadcast frame.
    assert "MATCH" not in json.dumps(payload)


def test_legacy_emitter_no_graph_queries_key_when_none_qualify():
    state = _legacy_live_state()
    state.tool_inputs = {LIVE_CITED_ID: _LIVE_INPUTS[LIVE_CITED_ID]}
    from agents import runtime_app
    [frame] = runtime_app._provenance_events(state)
    assert "graph_queries" not in frame["provenance"]


def test_legacy_emitter_graph_only_turn_still_emits_frame():
    # Zero cited records but a redeemable graph ref ⇒ the frame must ship
    # (pre-fix it was dropped, leaving the stored entry unreachable).
    state = _legacy_live_state()
    state.tool_calls_log = [{"name": "explore_graph", "toolUseId": LIVE_TOOL_ID}]
    state.tool_inputs = {LIVE_TOOL_ID: _LIVE_INPUTS[LIVE_TOOL_ID]}
    state.tool_results = {}
    from agents import runtime_app
    [frame] = runtime_app._provenance_events(state)
    assert frame["provenance"]["records"] == []
    assert frame["provenance"]["graph_queries"] == [
        {"query_ref": LIVE_REF, "tool_name": "explore_graph"}]


def test_local_emitter_frame_carries_graph_queries():
    from types import SimpleNamespace

    from agents import chat_client
    hook = SimpleNamespace(
        session_id=LIVE_SESSION, tool_calls=list(_LIVE_CALLS),
        # Local capture is str(dict) — python literal (fallback tolerance).
        tool_inputs={LIVE_TOOL_ID: str({"cypher": LIVE_CYPHER})},
        tool_metadata={}, rule_ids=set())
    payload = chat_client._build_local_provenance(hook, rules_applied=[])
    assert payload["graph_queries"] == [
        {"query_ref": LIVE_REF, "tool_name": "explore_graph"}]
    assert "MATCH" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# 50-line gate on the functions this fix touched (project invariant)
# ---------------------------------------------------------------------------

def test_touched_functions_stay_under_the_50_line_gate():
    import inspect

    from agents import chat_client, job_store, runtime_app
    from agents import provenance as prov
    for func in (prov.uncited_query_refs, prov.build_provenance_records,
                 runtime_app._provenance_events,
                 runtime_app._integrity_provenance_events,
                 chat_client._build_local_provenance,
                 job_store.JobStore._get_table):
        assert len(inspect.getsource(func).splitlines()) <= 50, func.__name__
