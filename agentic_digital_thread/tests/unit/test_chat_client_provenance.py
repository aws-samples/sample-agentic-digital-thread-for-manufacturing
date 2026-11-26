# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""chat_client half of the provenance transport.

Pins the two seams this task added:

  * cloud pass-through — ``_parse_metadata_event`` allowlists the additive
    ``provenance`` key (acceptance a) while the allowlist posture stays
    intact: an unknown sibling key is still dropped (acceptance b);
  * local emit — the ``_consume_local_events`` done branch yields the
    ``intelligence_summary`` frame AND a second ``provenance`` frame built by
    the ONE shared builder (``agents.provenance``), legacy ordering preserved
    (acceptance c); with zero tool calls the key is ABSENT — never an empty
    frame (acceptance d). The integrity path emits the frame WITH the D4
    verdict before done, and exactly one frame flows per turn either way.

Fully offline: ``build_orchestrator`` is stubbed, the summarizer is stubbed,
and the self_learning lookup is monkeypatched (same harness as
``test_local_intelligence_summary.py``).
"""
from __future__ import annotations

import queue
import types

import agents.chat_client as chat_client
import agents.runtime_integrity as runtime_integrity
import agents.self_learning as self_learning
from agents.chat_client import (
    LocalChatClient, _build_local_provenance, _parse_metadata_event,
    _ToolResultHook,
)
from agents.events import DoneEvent, ErrorEvent, MetadataEvent
from agents.provenance import mint_query_ref

RID = "subj-a1b2c3d4e5f60718"  # subj-<16 hex> applied-node id


def _fake_event(name: str, tool_id: str, result,
                tool_input=None) -> types.SimpleNamespace:
    """Duck-typed AfterToolCallEvent: tool_use dict + result."""
    return types.SimpleNamespace(
        tool_use={"name": name, "toolUseId": tool_id,
                  "input": {"q": 1} if tool_input is None else tool_input},
        result=result,
    )


# ── (a)+(b) cloud pass-through: _parse_metadata_event allowlist ────────────

def test_parse_metadata_event_passes_provenance_key_through():
    payload = {"records": [{"citation_num": 1, "tool_id": "t1"}], "truncated": False}
    event = _parse_metadata_event({"provenance": payload})
    assert isinstance(event, MetadataEvent)
    assert event.data == {"provenance": payload}


def test_parse_metadata_event_unknown_sibling_key_still_dropped():
    # Allowlist posture intact: an unknown key returns None (silently dropped).
    assert _parse_metadata_event({"mystery_sidecar": {"x": 1}}) is None


def test_parse_metadata_event_existing_keys_unchanged():
    for key in ("response_integrity", "intelligence_summary", "agent_analysis"):
        event = _parse_metadata_event({key: {"k": "v"}})
        assert isinstance(event, MetadataEvent)
        assert event.data == {key: {"k": "v"}}


# ── hook capture: additive tool_metadata; 2 KB strings byte-identical ──────

def test_hook_captures_tool_metadata_and_tool_results_unchanged(monkeypatch):
    monkeypatch.setattr(chat_client, "_summarize_tool_result", lambda n, r: "SUM")
    q: queue.Queue = queue.Queue()
    hook = _ToolResultHook(q)
    raw = "3 rows returned"
    hook._on_tool_done(_fake_event("erp___query", "t1", raw))
    # Existing 2 KB capture byte-identical.
    assert hook.tool_results["t1"] == raw[:2000]
    assert hook.tool_inputs["t1"] == "{'q': 1}"
    # New ADDITIVE map: bounded safe_tool_result_metadata shape per call.
    assert "t1" in hook.tool_metadata
    assert isinstance(hook.tool_metadata["t1"], dict)
    assert "app_status" in hook.tool_metadata["t1"]


# ── local stream: done branch emits summary AND provenance (c) ─────────────

class _StubOrchestrator:
    """Captures the added hook and fires it per fake tool call on invoke."""

    def __init__(self, tool_events):
        self._tool_events = tool_events
        self.callback_handler = None
        self._hook = None
        self.hooks = types.SimpleNamespace(add_hook=self._add_hook)

    def _add_hook(self, hook):
        self._hook = hook

    def __call__(self, prompt, **_kwargs):
        for event in self._tool_events:
            name, tool_id, result = event[:3]
            tool_input = event[3] if len(event) > 3 else None
            self._hook._on_tool_done(_fake_event(name, tool_id, result, tool_input))
        return "final answer"


def _run_stream(monkeypatch, tool_events, session_id="sess-1"):
    monkeypatch.setattr(chat_client, "_summarize_tool_result", lambda n, r: "SUM")
    monkeypatch.setattr(chat_client, "build_orchestrator",
                        lambda *a, **k: _StubOrchestrator(tool_events))
    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids",
                        lambda ids: [])
    return list(LocalChatClient().stream(
        prompt="p", session_id=session_id, user_id="u"))


def _metas_with(events, key):
    return [e for e in events if isinstance(e, MetadataEvent) and key in e.data]


def test_stream_done_branch_emits_summary_then_provenance(monkeypatch):
    # D2 parity mix: bare explore_graph is NOT cited; the gateway-prefixed
    # graph spelling and get_system_schema ARE (client predicate).
    events = _run_stream(monkeypatch, [
        ("explore_graph", "t1", f"graph hop used {RID}"),
        ("x___explore_graph", "t2", "graph rows"),
        ("get_system_schema", "t3", "columns: a, b"),
        ("erp___query", "t4", "3 rows"),
    ])
    assert not any(isinstance(e, ErrorEvent) for e in events)
    summaries = _metas_with(events, "intelligence_summary")
    provs = _metas_with(events, "provenance")
    assert len(summaries) == 1 and len(provs) == 1
    # Legacy ordering preserved: summary → provenance → done.
    order = [events.index(summaries[0]), events.index(provs[0]),
             next(i for i, e in enumerate(events) if isinstance(e, DoneEvent))]
    assert order == sorted(order)

    payload = provs[0].data["provenance"]
    records = payload["records"]
    # Bare explore_graph (t1) is never cited ⇒ 3 records, ordinals 1..3.
    assert [r["tool_id"] for r in records] == ["t2", "t3", "t4"]
    assert [r["citation_num"] for r in records] == [1, 2, 3]
    # D3: query_ref derives from (session_id, tool_id) — stable and shared
    # with the Wave-3 sidecar derivation.
    assert records[2]["query_ref"] == mint_query_ref("sess-1", "t4")
    # kg_route reuses the summary's resolved rows (empty lookup ⇒ id label).
    assert payload["truncated"] is False


def test_stream_zero_tool_calls_provenance_key_absent(monkeypatch):
    events = _run_stream(monkeypatch, [])
    assert len(_metas_with(events, "intelligence_summary")) == 1
    assert _metas_with(events, "provenance") == []  # ABSENT, not empty (d)
    assert any(isinstance(e, DoneEvent) for e in events)


def test_stream_builder_failure_swallowed_frame_omitted(monkeypatch):
    def _boom(**_kwargs):
        raise RuntimeError("builder exploded")

    monkeypatch.setattr(chat_client, "build_provenance_records", _boom)
    events = _run_stream(monkeypatch, [("erp___query", "t1", "3 rows")])
    assert _metas_with(events, "provenance") == []
    assert not any(isinstance(e, ErrorEvent) for e in events)
    assert any(isinstance(e, DoneEvent) for e in events)


# ── helper-level: rules reuse, verdict flow, honest absence ────────────────

def _hook_with_calls(session_id="s1") -> _ToolResultHook:
    hook = _ToolResultHook(queue.Queue(), session_id=session_id)
    hook.tool_calls = [{"name": "erp___query", "toolUseId": "t1"}]
    hook.tool_inputs = {"t1": "{'q': 1}"}
    hook.tool_metadata = {"t1": {"app_status": "success", "row_count": 3,
                                 "truncated": False}}
    return hook


def test_build_local_provenance_partial_from_captured_metadata():
    payload = _build_local_provenance(
        _hook_with_calls(), rules_applied=[{"id": RID, "label": "join path"}])
    record = payload["records"][0]
    assert record["verification"] == "partial"  # capture, no typed evidence
    assert record["result_shape"] == {"rows": 3, "truncated": False,
                                      "partial_reason": ""}
    assert record["source_system"] == "erp"
    assert record["kg_route"]["rules_applied"] == [{"id": RID, "label": "join path"}]
    assert record["query_ref"] == mint_query_ref("s1", "t1")


def test_build_local_provenance_none_when_no_tool_calls():
    hook = _ToolResultHook(queue.Queue(), session_id="s1")
    assert _build_local_provenance(hook) is None


def test_build_local_provenance_rules_default_degrades_to_id_labels():
    hook = _hook_with_calls()
    hook.rule_ids = {RID}
    payload = _build_local_provenance(hook)  # no rows passed (integrity path)
    assert payload["records"][0]["kg_route"]["rules_applied"] == [
        {"id": RID, "label": RID}]


# ── integrity local path: frame with the verdict, before done ──────────────

class _FakeAnswer:
    def quality_metadata(self):
        return {"verdict": "rejected"}


def test_integrity_path_emits_provenance_with_verdict_before_done(monkeypatch):
    monkeypatch.setattr(chat_client, "gate_final_result",
                        lambda result, ctx: ("GATED", _FakeAnswer()))
    monkeypatch.setattr(runtime_integrity, "persist_typed_proposals",
                        lambda *a, **k: None)
    q: queue.Queue = queue.Queue()
    hook = _hook_with_calls(session_id="s2")
    hook.integrity_mode = True
    handler = types.SimpleNamespace(tool_count=1)

    chat_client._emit_local_integrity_result(q, "raw", hook, handler)

    items = []
    while not q.empty():
        items.append(q.get_nowait())
    kinds = [(i["event"], next(iter(i["data"])) if i["event"] == "metadata" else "")
             for i in items]
    assert kinds == [("token", ""), ("metadata", "response_integrity"),
                     ("metadata", "provenance"), ("done", "")]
    record = items[2]["data"]["provenance"]["records"][0]
    # D4: rejected verdict ⇒ unverified, rendered honestly (never hidden).
    assert record["verification"] == "unverified"


def test_integrity_path_keeps_a_rejected_record_unverified_with_a_stored_entry(monkeypatch):
    """Reproduction, local emitter: the sidecar merge upgraded a
    rejected record to partial when the store held an entry for its call."""
    monkeypatch.setattr(chat_client, "gate_final_result",
                        lambda result, ctx: ("GATED", _FakeAnswer()))
    monkeypatch.setattr(runtime_integrity, "persist_typed_proposals",
                        lambda *a, **k: None)
    monkeypatch.setattr("agents.provenance_store.get_query", lambda sid, ref: {
        "system_id": "erp", "tool_name": "query_erp", "executed_query": "SELECT 1",
        "kg_artifacts": [], "captured_at": "2026-08-03T12:00:00Z",
        "entity": {"table": "work_orders", "columns": ["wo_status"]},
        "row_count": 3, "statement_count": 1, "origin": "system-agent"})
    q: queue.Queue = queue.Queue()
    hook = _hook_with_calls(session_id="s2")
    hook.integrity_mode = True
    chat_client._emit_local_integrity_result(q, "raw", hook, types.SimpleNamespace(tool_count=1))
    frames = []
    while not q.empty():
        item = q.get_nowait()
        if item["event"] == "metadata" and "provenance" in item["data"]:
            frames.append(item["data"]["provenance"])
    [record] = frames[0]["records"]
    assert record["verification"] == "unverified"
    assert "verdict" not in record  # round 3: the rejection is not repeated per record
    assert record["entity"] == {"table": "", "columns": []}


def test_integrity_mode_done_branch_does_not_duplicate_frame():
    # The consume loop must NOT add a second provenance frame in integrity
    # mode (the integrity emitter already queued one) — exactly one per turn.
    q: queue.Queue = queue.Queue()
    hook = _hook_with_calls()
    hook.integrity_mode = True
    q.put({"event": "metadata", "data": {"provenance": {"records": [],
                                                        "truncated": False}}})
    q.put({"event": "done", "data": {"text": "x"}})
    events = list(chat_client._consume_local_events(q, hook))
    assert len(_metas_with(events, "provenance")) == 1
    assert len(_metas_with(events, "intelligence_summary")) == 1


# ═══ Task 3.3 — Wave-3 sidecar intercept/strip + store write ═══════════════
#
# D6 never-broadcast: the ``provenance_queries`` sidecar is server-only —
# the cloud path persists and DROPS the frame (it never becomes an SSE
# event); the local path writes the store directly at done (no frame
# in-process). Store failures are swallow-guarded: the answer stream is
# never affected.

import agents.provenance_store as provenance_store
from agents.chat_client import (
    ThinkingFilter, _CloudStreamState, _local_provenance_query_entries,
)
from agents.provenance import executed_query_from_input


class _SilentLog:
    def info(self, *_a, **_k):
        pass

    def warning(self, *_a, **_k):
        pass


def _cloud_state(session_id="sess-cloud"):
    return _CloudStreamState(
        ThinkingFilter(), integrity_mode=False, log=_SilentLog(), owner_id="u",
        session_id=session_id, runtime_session_id="runtime-x",
    )


def _capture_puts(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(provenance_store, "put_turn_queries",
                        lambda sid, entries: calls.append((sid, entries)) or len(entries))
    return calls


_SIDE_ENTRY = {"system_id": "erp", "tool_name": "erp___query",
               "query_text": "SELECT a FROM t", "kg_artifact_ids": [RID],
               "captured_at": "2026-08-03T19:00:00Z"}


# ── never-broadcast pins (D6) ──────────────────────────────────────────────

def test_parse_metadata_event_strips_provenance_queries():
    # Defense in depth: even a parse path without session context never
    # turns the sidecar into an SSE frame.
    assert _parse_metadata_event(
        {"provenance_queries": {"q-abcdefabcdef": _SIDE_ENTRY}}) is None


def test_cloud_sidecar_frame_never_broadcast_and_store_written(monkeypatch):
    calls = _capture_puts(monkeypatch)
    state = _cloud_state()
    ref = mint_query_ref("sess-cloud", "t1")
    events = state.events_for({"provenance_queries": {ref: _SIDE_ENTRY}})
    state.close()
    assert events == []  # frame dropped — never an SSE event
    assert calls == [("sess-cloud", {ref: _SIDE_ENTRY})]


def test_cloud_stream_end_to_end_no_sidecar_leak(monkeypatch):
    # Full iter_stream proof: a stream carrying the sidecar plus normal
    # frames yields NO event containing the key, while siblings pass.
    import json as _json
    calls = _capture_puts(monkeypatch)
    ref = mint_query_ref("sess-cloud", "t1")
    lines = [
        "data: " + _json.dumps({"provenance": {"records": [], "truncated": False}}),
        "data: " + _json.dumps({"provenance_queries": {ref: _SIDE_ENTRY}}),
        "data: " + _json.dumps({"data": "hello"}),
    ]
    stream = types.SimpleNamespace(iter_lines=lambda **_k: iter(lines))
    state = _cloud_state()
    events = list(state.iter_stream(stream))
    state.close()
    for event in events:
        payload = getattr(event, "data", None)
        assert not (isinstance(payload, dict) and "provenance_queries" in payload)
    assert len([e for e in events
                if isinstance(e, MetadataEvent) and "provenance" in e.data]) == 1
    assert calls == [("sess-cloud", {ref: _SIDE_ENTRY})]
    assert "SELECT" not in " ".join(str(getattr(e, "data", "")) + str(getattr(e, "content", ""))
                                    for e in events)


def test_cloud_store_write_failure_swallowed_stream_unaffected(monkeypatch):
    def _boom(_sid, _entries):
        raise RuntimeError("store down")

    monkeypatch.setattr(provenance_store, "put_turn_queries", _boom)
    state = _cloud_state()
    events = state.events_for(
        {"provenance_queries": {mint_query_ref("sess-cloud", "t1"): _SIDE_ENTRY}})
    state.close()
    assert events == []  # dropped, not broadcast — and no exception


def test_cloud_malformed_sidecar_payload_dropped_without_store_call(monkeypatch):
    calls = _capture_puts(monkeypatch)
    state = _cloud_state()
    assert state.events_for({"provenance_queries": "not-a-dict"}) == []
    assert state.events_for({"provenance_queries": {}}) == []
    state.close()
    assert calls == []


# ── local entry builder (D6: query-bearing inputs only) ────────────────────

def test_query_text_from_input_accepts_sql_query_cypher_and_rejects_nl():
    """The local path uses the ONE extractor home (agents.provenance) with
    the literal-eval tolerance its str()-of-dict captures need."""
    def local(raw):
        return executed_query_from_input(raw, literal_eval_fallback=True)

    assert local("{'sql': 'SELECT a FROM t'}") == "SELECT a FROM t"
    assert local('{"query": "SELECT b FROM u"}') == "SELECT b FROM u"
    assert local("{'cypher': 'MATCH (n) RETURN n'}") == "MATCH (n) RETURN n"
    # Mode-A NL sub-questions are NOT query text (D6).
    assert local("{'question': 'how many orders?'}") == ""
    # Unparseable/truncated input ⇒ honest omission, never a half-parse.
    assert local("{'sql': 'SELECT a FR") == ""
    assert local(None) == ""
    assert local("plain text") == ""


def test_local_entries_built_only_for_query_bearing_calls():
    hook = _ToolResultHook(queue.Queue(), session_id="s1")
    hook.rule_ids = {RID}
    hook.tool_calls = [
        {"name": "erp___query", "toolUseId": "t1"},
        {"name": "erp___ask", "toolUseId": "t2"},
        {"name": "explore_graph", "toolUseId": "t3"},
    ]
    hook.tool_inputs = {
        "t1": "{'sql': 'SELECT a FROM t'}",
        "t2": "{'question': 'how many?'}",  # Mode-A NL — omitted (D6)
        "t3": "{'cypher': 'MATCH (n) RETURN n'}",
    }
    entries = _local_provenance_query_entries(hook)
    assert set(entries) == {mint_query_ref("s1", "t1"), mint_query_ref("s1", "t3")}
    sql_entry = entries[mint_query_ref("s1", "t1")]
    assert sql_entry["query_text"] == "SELECT a FROM t"
    assert sql_entry["system_id"] == "erp"
    assert sql_entry["tool_name"] == "erp___query"
    assert sql_entry["kg_artifact_ids"] == [RID]
    assert sql_entry["captured_at"]
    # Bare tool name (no gateway prefix) ⇒ honestly empty system_id.
    assert entries[mint_query_ref("s1", "t3")]["system_id"] == ""


# ── local done path writes the store directly ──────────────────────────────

def test_local_done_branch_writes_store_for_query_bearing_calls(monkeypatch):
    calls = _capture_puts(monkeypatch)
    events = _run_stream(monkeypatch, [
        ("erp___query", "t1", "3 rows", {"sql": "SELECT a FROM t"}),
        ("mes___ask", "t2", "answer text", {"question": "how many?"}),
    ])
    assert any(isinstance(e, DoneEvent) for e in events)
    assert len(calls) == 1
    sid, entries = calls[0]
    assert sid == "sess-1"
    ref = mint_query_ref("sess-1", "t1")
    assert set(entries) == {ref}  # NL call omitted (D6)
    assert entries[ref]["query_text"] == "SELECT a FROM t"
    # Never broadcast: no yielded event carries the sidecar key or the SQL.
    for event in events:
        payload = getattr(event, "data", None)
        assert not (isinstance(payload, dict) and "provenance_queries" in payload)
        assert "SELECT a FROM t" not in str(payload)


def test_local_done_branch_no_query_inputs_no_store_write(monkeypatch):
    calls = _capture_puts(monkeypatch)
    events = _run_stream(monkeypatch, [("erp___ask", "t1", "42 units",
                                        {"question": "count?"})])
    assert any(isinstance(e, DoneEvent) for e in events)
    assert calls == []  # nothing query-bearing ⇒ no write at all


def test_local_store_write_failure_swallowed_stream_completes(monkeypatch):
    def _boom(_sid, _entries):
        raise RuntimeError("store down")

    monkeypatch.setattr(provenance_store, "put_turn_queries", _boom)
    events = _run_stream(monkeypatch, [
        ("erp___query", "t1", "3 rows", {"sql": "SELECT a FROM t"}),
    ])
    assert not any(isinstance(e, ErrorEvent) for e in events)
    assert any(isinstance(e, DoneEvent) for e in events)
    # Legacy frames unaffected by the failed sidecar write.
    assert len(_metas_with(events, "intelligence_summary")) == 1
    assert len(_metas_with(events, "provenance")) == 1


def test_local_integrity_mode_done_branch_also_writes_store(monkeypatch):
    # The consume loop's done branch persists in BOTH modes — enforce (Mode
    # B) is exactly where SQL inputs exist.
    calls = _capture_puts(monkeypatch)
    q: queue.Queue = queue.Queue()
    hook = _hook_with_calls()  # t1 with input "{'q': 1}" — not query-bearing
    hook.tool_inputs = {"t1": "{'sql': 'SELECT a FROM t'}"}
    hook.integrity_mode = True
    q.put({"event": "done", "data": {"text": "x"}})
    events = list(chat_client._consume_local_events(q, hook))
    assert any(isinstance(e, DoneEvent) for e in events)
    assert len(calls) == 1
    assert set(calls[0][1]) == {mint_query_ref("s1", "t1")}
