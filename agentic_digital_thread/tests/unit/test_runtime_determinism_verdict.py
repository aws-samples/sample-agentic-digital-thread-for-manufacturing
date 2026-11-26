# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Runtime determinism verdict — legacy orchestrator entrypoint.

Pins the ref semantics the summary/analysis block adopted:

* the intelligence summary carries the ADDITIVE mechanical keys
  (``grounded_steps`` / ``deterministic_pct``) and the mechanical
  ``wasted_calls`` override;
* a CLEAN run (zero mechanically detected waste) yields a COMPUTED
  ``agent_analysis`` positive verdict with ZERO LLM calls — no bedrock
  client is ever constructed;
* when waste EXISTS, the analysis LLM receives the mechanically flagged
  steps as its review focus, its ``deterministic_pct`` is OVERRIDDEN by the
  computed score, and its wasted-call rows are filtered to the flagged set
  (the LLM explains mechanical findings, never re-decides).

Offline: fake BedrockAgentCoreApp, stubbed agent stream, patched boto3.
"""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import boto3

import agents.orchestrator as orchestrator
import agents.runtime_app as runtime_app
import agents.self_learning as self_learning


class _FakeRuntimeApp:
    last = None

    def __init__(self):
        self.invoke = None
        _FakeRuntimeApp.last = self

    def entrypoint(self, fn):
        self.invoke = fn
        return fn

    def run(self):
        return None


def _tool_events(trace):
    """(tool_id, name, input_str, result_text) → the legacy stream events."""
    for tool_id, name, input_str, result_text in trace:
        yield {"current_tool_use": {"toolUseId": tool_id, "name": name,
                                    "input": input_str}}
        yield {"message": {"role": "user", "content": [{"toolResult": {
            "toolUseId": tool_id, "status": "success",
            "content": [{"text": result_text}],
        }}]}}


def _agent_for(trace):
    class _Agent:
        async def stream_async(self, _prompt, **_kwargs):
            for event in _tool_events(trace):
                yield event
            yield {"result": SimpleNamespace()}
    return _Agent()


def _run_legacy(monkeypatch, trace, prompt="compare pump status across systems"):
    """Drive the LEGACY (report-only) entrypoint over a fixed tool trace."""
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.setitem(sys.modules, "bedrock_agentcore",
                        SimpleNamespace(BedrockAgentCoreApp=_FakeRuntimeApp))
    monkeypatch.setattr(orchestrator, "build_orchestrator",
                        lambda **_k: _agent_for(trace))
    monkeypatch.setattr(runtime_app, "_graph_first_prelude", lambda p: (p, []))
    for hook in ("_record_rule_usage_hook", "_check_for_discoveries",
                 "_check_for_reason_code_mappings",
                 "_persist_column_not_found_guidance"):
        monkeypatch.setattr(runtime_app, hook, lambda *_a, **_k: None)
    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: [])
    monkeypatch.setattr(self_learning, "get_all_mappings", lambda: [])
    monkeypatch.setattr(self_learning, "known_systems", lambda: [])
    monkeypatch.setattr(self_learning, "get_accepted_rules_by_node_ids",
                        lambda _ids: [])

    runtime_app._run_orchestrator()
    invoke = _FakeRuntimeApp.last.invoke

    async def collect():
        return [e async for e in invoke(
            {"prompt": prompt, "sessionId": "s", "userId": "u"},
            SimpleNamespace(session_id="rt"),
        )]

    return asyncio.run(collect())


def _one(events, key):
    matches = [e[key] for e in events if isinstance(e, dict) and key in e]
    assert len(matches) == 1, f"expected exactly one {key}, got {len(matches)}"
    return matches[0]


_CLEAN_TRACE = [
    ("t1", "explore_graph", '{"query": "MATCH (a:Asset) RETURN a"}',
     '{"asset_tag": "PUMP-STATION-0007", "source_id": "f8c1d2e3"}'),
    ("t2", "get_system_schema", '{"system": "cmms"}',
     "columns: work_order_id, asset_tag"),
    ("t3", "cmms___query", '{"asset": "PUMP-STATION-0007"}',
     '{"row_count": 2, "status": "ok"}'),
]


def test_clean_run_yields_computed_verdict_and_no_bedrock_client(monkeypatch):
    constructed: list = []
    monkeypatch.setattr(boto3, "client",
                        lambda *a, **k: constructed.append(a))

    events = _run_legacy(monkeypatch, _CLEAN_TRACE)

    summary = _one(events, "intelligence_summary")
    assert summary["grounded_steps"] == 3
    assert summary["deterministic_pct"] == 100
    assert summary["wasted_calls"] == 0  # mechanical override

    analysis = _one(events, "agent_analysis")
    assert analysis["deterministic_pct"] == 100
    assert analysis["wasted_calls"] == []
    assert analysis["recommendations"] == []
    assert "no wasted calls detected" in analysis["summary"]
    # All grounded ⇒ the stronger sentence.
    assert "exact identifiers resolved from the graph" in analysis["summary"]

    assert constructed == [], "clean run must NOT construct a bedrock client"
    assert not any("error" in e for e in events if isinstance(e, dict))


_WASTE_TRACE = [
    ("t1", "erp___query", '{"q": 1}', "Error: relation missing"),
    ("t2", "erp___query", '{"q": 2}', "4 rows"),
    ("t3", "explore_graph", '{"query": "MATCH (n) RETURN n"}', "10 nodes"),
]


class _FakeBedrock:
    def __init__(self, record: list):
        self._record = record

    def converse(self, **kwargs):
        self._record.append(kwargs)
        return {"output": {"message": {"content": [{"text": json.dumps({
            "summary": "step 1 queried a missing relation, then retried",
            "wasted_calls": [
                {"step": 1, "tool": "erp___query",
                 "reason": "missing relation", "fix": "annotate the table"},
                {"step": 3, "tool": "explore_graph",
                 "reason": "LLM-invented critique of a clean step", "fix": "-"},
            ],
            "recommendations": [],
            "deterministic_pct": 55,  # junk — must be overridden
        })}]}}}


def test_waste_run_feeds_flagged_steps_and_overrides_llm_score(monkeypatch):
    converse_calls: list = []
    fake = _FakeBedrock(converse_calls)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: fake)

    events = _run_legacy(monkeypatch, _WASTE_TRACE)

    # Mechanical: step 1 failed AND erp___query was retried ⇒ wasted;
    # grounded = explore_graph only ⇒ 1/3 = 33%.
    summary = _one(events, "intelligence_summary")
    assert summary["wasted_calls"] == 1
    assert summary["grounded_steps"] == 1
    assert summary["deterministic_pct"] == 33

    # The LLM ran exactly once, with the flagged steps as the review focus
    # and WITHOUT being asked to produce the score.
    assert len(converse_calls) == 1
    prompt_text = converse_calls[0]["messages"][0]["content"][0]["text"]
    assert "MECHANICALLY DETECTED WASTED STEPS" in prompt_text
    assert '"step": 1' in prompt_text
    assert "Integer 0-100" not in prompt_text

    analysis = _one(events, "agent_analysis")
    assert analysis["deterministic_pct"] == 33  # computed, not the LLM's 55
    # Rows filtered to the mechanically flagged set — the invented step-3
    # critique is dropped.
    assert [w["step"] for w in analysis["wasted_calls"]] == [1]
    assert "no wasted calls detected" not in analysis.get("summary", "")


def test_validated_wasted_calls_flagged_steps_filter():
    calls = [{"name": "erp___query", "toolUseId": "a"},
             {"name": "erp___query", "toolUseId": "b"},
             {"name": "explore_graph", "toolUseId": "c"}]
    results = {"a": "3 rows", "b": "3 rows", "c": "10 nodes"}
    raw = [{"step": 2, "reason": "exact duplicate"},
           {"step": 3, "reason": "invented"},
           {"step": 99, "reason": "out of range"}]
    # Flagged-set authority: keeps the duplicate row (which SUCCEEDED, so the
    # legacy failed-result check would drop it) and drops unflagged/invalid.
    kept = runtime_app._validated_wasted_calls(
        raw, calls, results, flagged_steps={2})
    assert kept == [raw[0]]
    # Legacy behavior unchanged when no flagged set is supplied.
    assert runtime_app._validated_wasted_calls(raw, calls, results) == []
