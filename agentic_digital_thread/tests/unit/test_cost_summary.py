# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""E7 measured-cost summary key (Task 2.2, ADR-0053 D-E7).

The additive ``intelligence_summary.cost`` key carries MEASURED orchestrator
token usage only, read from the verified strands 1.42.0 surface
(``AgentResult.metrics.accumulated_usage`` — ``inputTokens``/``outputTokens``/
``totalTokens`` ints; ``accumulated_metrics.latencyMs``). Honesty pins:

- ABSENT when unmeasured — never zeros-as-absence (strands zero-initializes
  ``accumulated_usage``, so an all-zero usage means "no model call measured");
- scope stamped ``orchestrator-model-only`` (system agents run as separate
  AgentCore runtimes behind the gateway — their usage is not visible here);
- NO dollar/price fields anywhere — the plan forbids fabricated estimates;
- BOTH emitters (legacy ``_build_summary_payload`` — the report-only default
  path — and the integrity consumer's ``final_events``) emit the same shape
  through the ONE builder kwarg.

Offline: no AWS, no network.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import agents.query_intelligence as query_intelligence
import agents.runtime_app as runtime_app
import agents.self_learning as self_learning


def _metrics(input_t=5230, output_t=812, total=6042, latency=41210):
    """Fake strands EventLoopMetrics (the re-verified 1.42.0 runtime shape:
    both accumulators are plain dicts on the metrics object)."""
    return SimpleNamespace(
        accumulated_usage={
            "inputTokens": input_t, "outputTokens": output_t,
            "totalTokens": total,
        },
        accumulated_metrics={"latencyMs": latency},
    )


_PINNED_COST = {
    "input_tokens": 5230, "output_tokens": 812, "total_tokens": 6042,
    "latency_ms": 41210, "scope": "orchestrator-model-only",
}


def _summary(**kwargs) -> dict:
    return query_intelligence.build_intelligence_summary(
        tool_names=["explore_graph", "query_erp"], wasted_calls=0,
        rules_applied=[], **kwargs)


# --------------------------------------------------------------------------- #
# The pinned cost payload
# --------------------------------------------------------------------------- #
def test_cost_key_present_with_usage():
    cost = runtime_app._usage_cost_payload(_metrics())
    summary = _summary(cost=cost)
    assert summary["cost"] == _PINNED_COST


def test_cost_key_absent_without_metrics():
    """The pinned key-set contract: no metrics ⇒ byte-identical summary."""
    baseline = _summary()
    assert _summary(cost=None) == baseline
    assert _summary(cost=runtime_app._usage_cost_payload(None)) == baseline
    assert "cost" not in baseline
    # Malformed / partial / all-zero inputs are all "unmeasured" ⇒ None.
    assert runtime_app._usage_cost_payload(SimpleNamespace()) is None
    assert runtime_app._usage_cost_payload(
        SimpleNamespace(accumulated_usage="not-a-dict")) is None
    assert runtime_app._usage_cost_payload(
        SimpleNamespace(accumulated_usage={"inputTokens": 5})) is None
    assert runtime_app._usage_cost_payload(
        SimpleNamespace(accumulated_usage={
            "inputTokens": 1, "outputTokens": "2", "totalTokens": 3})) is None


def test_all_zero_usage_is_unmeasured_never_zeros():
    """strands zero-initializes Usage — zeros mean unmeasured, key ABSENT."""
    zeroed = _metrics(input_t=0, output_t=0, total=0, latency=0)
    assert runtime_app._usage_cost_payload(zeroed) is None
    assert "cost" not in _summary(cost=runtime_app._usage_cost_payload(zeroed))


def test_latency_ms_absent_when_not_measured():
    m = _metrics()
    m.accumulated_metrics = {}
    payload = runtime_app._usage_cost_payload(m)
    assert "latency_ms" not in payload
    assert payload["total_tokens"] == 6042
    # A zero-initialized latencyMs is equally unmeasured ⇒ key absent.
    m.accumulated_metrics = {"latencyMs": 0}
    assert "latency_ms" not in runtime_app._usage_cost_payload(m)


def test_scope_stamped_orchestrator_only():
    payload = runtime_app._usage_cost_payload(_metrics())
    assert payload["scope"] == "orchestrator-model-only"
    assert _summary(cost=payload)["cost"]["scope"] == "orchestrator-model-only"


def test_no_dollar_fields_anywhere():
    """The honesty pin: measured tokens only — no fabricated cost math."""
    summary = _summary(cost=runtime_app._usage_cost_payload(_metrics()))
    dumped = json.dumps(summary).lower()
    for forbidden in ("usd", "dollar", "price"):
        assert forbidden not in dumped, forbidden


# --------------------------------------------------------------------------- #
# Both emitters — same shape through the ONE builder
# --------------------------------------------------------------------------- #
def _legacy_turn_state():
    return SimpleNamespace(
        prompt="compare ERP and MES yields", session_id="s-1",
        tool_calls_log=[{"name": "explore_graph", "toolUseId": "t1"}],
        tool_inputs={}, tool_results={},
        rule_ids=set(), matched_rows=[], planned_with=None,
        agent_metrics=_metrics(),
    )


def test_both_emitters_emit_same_shape(monkeypatch):
    """Legacy _build_summary_payload and integrity final_events carry the
    IDENTICAL pinned cost dict (the Wave-2.1 lesson: report-only routes
    through the LEGACY entrypoint — both emitters must be wired)."""
    # Legacy emitter (report-only default path).
    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: [])
    legacy = runtime_app._build_summary_payload(_legacy_turn_state(), None)
    assert legacy["cost"] == _PINNED_COST

    # Integrity emitter: a fabricated invocation whose terminal AgentResult
    # carries the same metrics; the surrounding machinery is stubbed.
    import agents.response_integrity as response_integrity
    import agents.runtime_integrity as runtime_integrity
    state = runtime_app._RuntimeIntegrityInvocation("q", set())
    state.agent_result = SimpleNamespace(metrics=_metrics())
    monkeypatch.setattr(response_integrity, "gate_final_result",
                        lambda result, context: ("answer md", SimpleNamespace()))
    monkeypatch.setattr(runtime_integrity, "persist_typed_proposals",
                        lambda integrity, tool_calls_count=0: 0)
    monkeypatch.setattr(runtime_integrity, "integrity_metadata",
                        lambda integrity: {"response_integrity": {"mode": "enforce"}})
    monkeypatch.setattr(runtime_app, "_integrity_rule_usage",
                        lambda context: (set(), []))
    events = state.final_events()
    summaries = [e["intelligence_summary"] for e in events
                 if "intelligence_summary" in e]
    assert len(summaries) == 1
    assert summaries[0]["cost"] == legacy["cost"] == _PINNED_COST


def test_legacy_emitter_absent_cost_before_terminal_result(monkeypatch):
    """A turn that dies before the terminal result event stays honest: the
    defensive agent_metrics=None init yields an ABSENT cost key."""
    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: [])
    s = _legacy_turn_state()
    s.agent_metrics = None
    assert "cost" not in runtime_app._build_summary_payload(s, None)


def test_filter_stream_event_captures_terminal_metrics():
    s = _legacy_turn_state()
    s.agent_metrics = None
    result = SimpleNamespace(metrics=_metrics())
    out = runtime_app._filter_stream_event({"result": result}, s)
    assert out == {"result": result}
    assert s.agent_metrics is result.metrics
