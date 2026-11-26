# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Local emitter intelligence summary (Task 4 — rules-applied metric).

Pins the ``chat_client`` half of the intelligence-summary rework:

  * ``_ToolResultHook`` accumulates per-query state from the RAW tool result
    (rule node ids + wasted-call count), swallow-guarded so a scan failure can
    never break the ``tool_result`` event — acceptance (a);
  * ``LocalChatClient.stream`` emits ONE ``intelligence_summary`` metadata
    event at done, built via ``query_intelligence.build_intelligence_summary``
    with labels resolved best-effort through
    ``self_learning.get_accepted_rules_by_node_ids`` (failure degrades to
    ``label == id``, never errors the stream) — acceptance (b). With a
    captured trace the payload gains the mechanical keys
    (``grounded_steps`` / ``deterministic_pct``, ``wasted_calls``
    overridden); a hook with NO trace keeps the legacy Decision-2 shape;
  * source tripwire: ``chat_client`` never hand-rolls the determinism keys
    (they arrive only via the shared builder) and never bumps usage
    (Decision 4) — acceptance (c).

Fully offline: ``build_orchestrator`` is stubbed, the summarizer is stubbed,
and the self_learning lookup is monkeypatched.
"""
from __future__ import annotations

import queue
import types
from pathlib import Path

import agents.chat_client as chat_client
import agents.self_learning as self_learning
from agents.chat_client import LocalChatClient, _build_local_summary, _ToolResultHook
from agents.events import DoneEvent, ErrorEvent, MetadataEvent

DECISION_2_KEYS = {
    "total_tool_calls", "neptune_queries", "system_queries",
    "graph_coverage_pct", "wasted_calls", "rules_applied",
    "new_proposed_mappings",
}

RID = "subj-a1b2c3d4e5f60718"  # subj-<16 hex> applied-node id


def _fake_event(name: str, tool_id: str, result) -> types.SimpleNamespace:
    """Duck-typed AfterToolCallEvent: tool_use dict + result."""
    return types.SimpleNamespace(
        tool_use={"name": name, "toolUseId": tool_id, "input": {"q": 1}},
        result=result,
    )


# ── (a) hook accumulation ─────────────────────────────────────────────────

def test_hook_accumulates_ids_and_wasted_and_event_shape_unchanged(monkeypatch):
    monkeypatch.setattr(chat_client, "_summarize_tool_result", lambda n, r: "SUM")
    q: queue.Queue = queue.Queue()
    hook = _ToolResultHook(q)

    hook._on_tool_done(_fake_event("explore_graph", "t1", f"joined via {RID} ok"))
    hook._on_tool_done(_fake_event("erp___query", "t2", "Error: connection refused"))

    assert hook.rule_ids == {RID}
    assert hook.wasted_count == 1
    # Determinism trace: call order + bounded inputs/results captured.
    assert hook.tool_calls == [
        {"name": "explore_graph", "toolUseId": "t1"},
        {"name": "erp___query", "toolUseId": "t2"},
    ]
    assert hook.tool_inputs["t1"] == "{'q': 1}"
    assert hook.tool_results["t2"] == "Error: connection refused"
    # The queue receives the tool_result event shape — unchanged apart from the
    # DELIBERATE server-truth `ts` addition (streaming forensics wave): the
    # emitter stamps epoch-ms at result receipt for server-side pill timing.
    first = q.get_nowait()
    assert isinstance(first["data"].pop("ts"), int)
    assert first == {"event": "tool_result", "data": {
        "tool": "explore_graph", "tool_id": "t1",
        "summary": "SUM", "input": {"q": 1},
    }}
    second = q.get_nowait()
    assert second["data"]["tool"] == "erp___query"
    assert q.empty()


def test_hook_scan_failure_is_swallowed_event_still_emitted(monkeypatch):
    monkeypatch.setattr(chat_client, "_summarize_tool_result", lambda n, r: "SUM")

    def _boom(texts):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(chat_client, "extract_rule_node_ids", _boom)
    q: queue.Queue = queue.Queue()
    hook = _ToolResultHook(q)
    hook._on_tool_done(_fake_event("erp___query", "t1", f"data {RID}"))  # must not raise
    assert hook.rule_ids == set()
    assert q.get_nowait()["event"] == "tool_result"


def test_hook_scan_is_bounded(monkeypatch):
    # A rule id past the scan bound is not extracted (the scan stays cheap),
    # but the tool_result event still flows.
    monkeypatch.setattr(chat_client, "_summarize_tool_result", lambda n, r: "SUM")
    q: queue.Queue = queue.Queue()
    hook = _ToolResultHook(q)
    huge = ("x" * _ToolResultHook._SCAN_BOUND) + f" {RID}"
    hook._on_tool_done(_fake_event("erp___query", "t1", huge))
    assert hook.rule_ids == set()
    assert q.get_nowait()["event"] == "tool_result"


# ── (b) stream-level: done branch builds the Decision-2 payload ───────────

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
        for name, tool_id, result in self._tool_events:
            self._hook._on_tool_done(_fake_event(name, tool_id, result))
        return "final answer"


def _run_stream(monkeypatch, tool_events, lookup):
    monkeypatch.setattr(chat_client, "_summarize_tool_result", lambda n, r: "SUM")
    monkeypatch.setattr(chat_client, "build_orchestrator",
                        lambda *a, **k: _StubOrchestrator(tool_events))
    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids", lookup)
    return list(LocalChatClient().stream(prompt="p", session_id="s", user_id="u"))


def _summary_from(events) -> dict:
    metas = [e for e in events if isinstance(e, MetadataEvent)
             and "intelligence_summary" in e.data]
    assert len(metas) == 1, f"expected exactly one intelligence_summary, got {len(metas)}"
    return metas[0].data["intelligence_summary"]


def test_stream_emits_decision_2_summary_with_resolved_labels(monkeypatch):
    row = {"neptune_node_id": RID, "proposed_type": "join_path",
           "from_system": "erp", "from_field": "part_id",
           "to_system": "mes", "to_field": "material_no"}
    events = _run_stream(monkeypatch, [
        ("explore_graph", "t1", f"graph hop used {RID}"),
        ("get_system_schema", "t2", "columns: a, b"),
        ("erp___query", "t3", "no results"),
    ], lookup=lambda ids: [row])

    s = _summary_from(events)
    # With a captured trace the payload gains the mechanical keys.
    assert set(s.keys()) == DECISION_2_KEYS | {"grounded_steps",
                                               "deterministic_pct"}
    assert s["total_tool_calls"] == 3
    assert s["neptune_queries"] == 1
    # Decision 1 corollary: schema calls are NOT special-cased any more.
    assert s["system_queries"] == 2
    assert s["graph_coverage_pct"] == 33
    # Mechanical override: the "no results" envelope FAILED but was
    # never retried and is no duplicate ⇒ zero hard-evidence waste (the
    # legacy envelope-scan count of 1 is overridden).
    assert s["wasted_calls"] == 0
    # Grounded: both KG reads; the erp query's input shares no value token
    # with earlier results.
    assert s["grounded_steps"] == 2
    assert s["deterministic_pct"] == 66
    assert s["rules_applied"] == [
        {"id": RID, "label": "join path: erp.part_id -> mes.material_no"},
    ]
    assert s["new_proposed_mappings"] == 0
    # The summary precedes the DoneEvent; nothing errored.
    assert not any(isinstance(e, ErrorEvent) for e in events)
    meta_idx = next(i for i, e in enumerate(events) if isinstance(e, MetadataEvent))
    done_idx = next(i for i, e in enumerate(events) if isinstance(e, DoneEvent))
    assert meta_idx < done_idx


def test_stream_raising_lookup_degrades_to_label_eq_id(monkeypatch):
    def _boom(ids):
        raise RuntimeError("no dynamodb here")

    events = _run_stream(monkeypatch, [
        ("explore_graph", "t1", f"used {RID}"),
    ], lookup=_boom)

    s = _summary_from(events)
    assert s["rules_applied"] == [{"id": RID, "label": RID}]
    assert not any(isinstance(e, ErrorEvent) for e in events)
    assert any(isinstance(e, DoneEvent) for e in events)


def test_stream_no_rules_no_lookup_call(monkeypatch):
    # Empty rule_ids → zero lookup calls (no table access in pure-local dev).
    def _never(ids):
        raise AssertionError("lookup must not be called with no rule ids")

    events = _run_stream(monkeypatch, [
        ("erp___query", "t1", "3 rows"),
    ], lookup=_never)
    s = _summary_from(events)
    assert s["rules_applied"] == []
    assert s["total_tool_calls"] == 1


# ── helper-level (plan's fallback lane — kept as direct coverage) ─────────

def test_build_local_summary_missing_row_falls_back_to_id(monkeypatch):
    q: queue.Queue = queue.Queue()
    hook = _ToolResultHook(q)
    other = "subj-ffffffffffffffff"
    hook.rule_ids = {RID, other}
    hook.wasted_count = 2
    row = {"neptune_node_id": RID, "proposed_type": "value_mapping",
           "from_system": "erp", "from_field": "status", "from_value": "OPEN",
           "to_system": "mes", "to_field": "state", "to_value": "ACTIVE"}
    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids",
                        lambda ids: [row])

    s = _build_local_summary(["explore_graph", "erp___query"], hook)
    # No captured trace on this hand-built hook ⇒ determinism OMITTED — the
    # payload keeps the legacy Decision-2 shape and the envelope-scan count.
    assert set(s.keys()) == DECISION_2_KEYS
    by_id = {r["id"]: r["label"] for r in s["rules_applied"]}
    assert by_id[RID].startswith("value mapping: erp.status 'OPEN'")
    assert by_id[other] == other  # no matching row → label == id (Decision 3)
    assert s["wasted_calls"] == 2


# ── (c) source tripwire ───────────────────────────────────────────────────

def test_chat_client_source_has_no_removed_keys_and_no_usage_bump():
    src = Path(chat_client.__file__).read_text()
    # The determinism keys arrive ONLY via the shared builder's
    # determinism= kwarg — chat_client never spells (hand-rolls) them.
    assert "deterministic_pct" not in src
    assert "grounded_steps" not in src
    assert "graph_guided_steps" not in src
    # Decision 4: the local path never bumps usage — recording stays in the
    # runtime finally-block hook.
    assert "record_rule_usage" not in src
