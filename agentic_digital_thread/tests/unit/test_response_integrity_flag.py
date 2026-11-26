# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RESPONSE_INTEGRITY_MODE opt-in flag (ADR-0047).

Enforcement of the ADR-0046 typed-evidence contract is opt-in: the default
``report-only`` mode restores the legacy model-authored answer flow (capture
machinery still runs in shadow); ``enforce`` activates the full gate.
"""
from __future__ import annotations

import json

import pytest

import agents.system_agent as sa
from agents.config import response_integrity_enforced, response_integrity_mode
from agents.chat_client import _cloud_mode_a, _local_integrity_mode


# ---------------------------------------------------------------------------
# Flag helpers (agents/config.py — single source of truth)
# ---------------------------------------------------------------------------

def test_mode_defaults_to_report_only_when_unset(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    assert response_integrity_mode() == "report-only"
    assert response_integrity_enforced() is False


def test_mode_enforce_is_recognized_case_and_whitespace_insensitive(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "  ENForce  ")
    assert response_integrity_mode() == "enforce"
    assert response_integrity_enforced() is True


def test_mode_report_only_explicit(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "report-only")
    assert response_integrity_mode() == "report-only"
    assert response_integrity_enforced() is False


@pytest.mark.parametrize("value", ["", "yes", "true", "1", "enforced", "shadow", "bogus"])
def test_mode_unrecognized_values_fall_back_to_report_only(monkeypatch, value):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", value)
    assert response_integrity_mode() == "report-only"
    assert response_integrity_enforced() is False


# ---------------------------------------------------------------------------
# chat_client gating — Mode A alone is no longer enough
# ---------------------------------------------------------------------------

def test_local_integrity_mode_false_by_default_in_mode_a(monkeypatch):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    assert _local_integrity_mode() is False
    assert _cloud_mode_a() is False


def test_local_integrity_mode_true_in_mode_a_when_enforced(monkeypatch):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    assert _local_integrity_mode() is True
    assert _cloud_mode_a() is True


def test_mode_b_stays_false_even_when_enforced(monkeypatch):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    assert _local_integrity_mode() is False
    assert _cloud_mode_a() is False


# ---------------------------------------------------------------------------
# system_agent._invoke_mode_a_agent — report-only restores legacy answers
# ---------------------------------------------------------------------------

class _FakeAgent:
    """Records invocation kwargs and returns canned strands-shaped results.

    Accepts one text per expected call (last text repeats if called again) so
    tests can model the ADR-0047 elicitation retry.
    """

    def __init__(self, *texts: str):
        self._texts = list(texts)
        self.calls: list[dict] = []

    def __call__(self, question, **kwargs):
        index = min(len(self.calls), len(self._texts) - 1)
        self.calls.append(kwargs)
        message = {"role": "assistant", "content": [{"text": self._texts[index]}]}

        class _Result:
            pass

        result = _Result()
        result.message = message
        result.stop_reason = "end_turn"
        result.structured_output = None
        return result


def test_report_only_returns_model_text_without_structured_output(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    agent = _FakeAgent("Line 3 produced 42 units today.")
    response = sa._invoke_mode_a_agent(agent, "How many units?")
    assert response == "Line 3 produced 42 units today."
    assert len(agent.calls) == 1
    kwargs = agent.calls[0]
    assert "structured_output_model" not in kwargs
    assert kwargs["limits"] == {"turns": sa._SYSTEM_AGENT_MAX_TURNS}
    assert "invocation_state" in kwargs


def test_report_only_blank_text_retries_once_then_returns_retry_text(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    agent = _FakeAgent("   ", "The queries failed due to a schema mismatch.")
    response = sa._invoke_mode_a_agent(agent, "How many units?")
    assert response == "The queries failed due to a schema mismatch."
    assert len(agent.calls) == 2
    retry_kwargs = agent.calls[1]
    assert "structured_output_model" not in retry_kwargs
    assert retry_kwargs["limits"] == {"turns": 2}


def test_report_only_blank_after_retry_returns_plain_language_not_json(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    agent = _FakeAgent("   ", "")
    response = sa._invoke_mode_a_agent(agent, "How many units?")
    assert len(agent.calls) == 2
    # Plain language, never a raw JSON envelope an upstream model would paste.
    assert not response.strip().startswith("{")
    assert "could not complete" in response


def test_plain_language_incomplete_summarizes_tool_metadata():
    envelope = json.dumps({
        "status": "error", "error_type": "incomplete",
        "tool_results": [
            {"tool": "athena_run_query", "app_status": "error"},
            {"tool": "athena_run_query", "app_status": "error"},
        ],
    })
    line = sa._plain_language_incomplete(envelope)
    assert "2 data queries" in line
    assert "failed" in line
    assert not line.strip().startswith("{")


def test_enforce_still_passes_structured_output_model(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    agent = _FakeAgent("Model prose must be DISCARDED under enforcement.")
    response = sa._invoke_mode_a_agent(agent, "How many units?")
    kwargs = agent.calls[0]
    assert kwargs["structured_output_model"] is sa.SystemAnswerDraft
    # No valid draft -> deterministic metadata-only envelope, never model prose.
    envelope = json.loads(response)
    assert envelope["error_type"] == "incomplete"
    assert "DISCARDED" not in response


# ---------------------------------------------------------------------------
# Prompt segment gating — typed-evidence instructions only under enforce
# ---------------------------------------------------------------------------

def _compose(**overrides):
    kwargs = dict(
        system_id="system-x", system_name="System X", schema="schema_x",
        system_prompt="", tables_desc="", connection_type="PostgreSQL",
    )
    kwargs.update(overrides)
    return sa._compose_system_prompt(**kwargs)


def test_typed_evidence_prompt_absent_in_report_only(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    assert "TYPED EVIDENCE OUTPUT" not in _compose()


def test_typed_evidence_prompt_present_under_enforce(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    prompt = _compose()
    assert "TYPED EVIDENCE OUTPUT" in prompt
    # The legacy base prompt is unchanged underneath the appended segment.
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    assert prompt.startswith(_compose())


# ---------------------------------------------------------------------------
# Orchestrator prompt gating — Mode A typed-contract block only under enforce
# ---------------------------------------------------------------------------

def test_orchestrator_integrity_block_absent_in_report_only(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    from agents import orchestrator
    prompt = orchestrator._build_orchestrator_prompt("agent")
    assert "MODE A TYPED RESPONSE CONTRACT" not in prompt
    assert "SystemAnswer JSON envelopes" not in prompt


def test_orchestrator_integrity_block_present_under_enforce(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    from agents import orchestrator
    prompt = orchestrator._build_orchestrator_prompt("agent")
    assert "MODE A TYPED RESPONSE CONTRACT" in prompt
    # The legacy base prompt is unchanged underneath the appended block.
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    assert prompt.startswith(orchestrator._build_orchestrator_prompt("agent"))


def test_orchestrator_integrity_block_never_in_mode_b(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    from agents import orchestrator
    assert "MODE A TYPED RESPONSE CONTRACT" not in orchestrator._build_orchestrator_prompt("orchestrator")


# ---------------------------------------------------------------------------
# Runtime entrypoint selection — legacy implementation under default mode
# ---------------------------------------------------------------------------

def _entrypoint_selection(monkeypatch) -> list:
    """Run the selected entrypoint and record which implementation handled it."""
    import asyncio
    from agents import runtime_app

    calls: list = []

    async def legacy(payload, context):
        calls.append("legacy")
        yield {"data": "legacy"}

    async def integrity(payload, context, build_orchestrator):
        calls.append("integrity")
        yield {"data": "integrity"}

    monkeypatch.setattr(runtime_app, "_invoke_integrity_runtime", integrity)
    selected = runtime_app._runtime_entrypoint(legacy, build_orchestrator=object())

    async def drain():
        async for _ in selected({}, None):
            pass

    asyncio.run(drain())
    return calls


def test_runtime_entrypoint_selects_legacy_in_report_only(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    assert _entrypoint_selection(monkeypatch) == ["legacy"]


def test_runtime_entrypoint_selects_integrity_under_enforce(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    assert _entrypoint_selection(monkeypatch) == ["integrity"]
