# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Output-limit stop and compact SiteWise reads: the edge cases.

1. The no-tools "final answer now" retry can itself stop at the output cap;
   the fallback must describe that LAST attempt, not the first one.
2. An output-limit stop after a failed data query reached the system: only a
   turn with no tool attempt may say the system was not reached, and the
   captured failure must stay visible so a real transient failure is still
   recognisable as one.
3. The output-limit case lives in the ONE per-system call budget (MISS
   FOLLOW-UP), so every composed prompt (full and Quick, both modes, workshop
   on and off) carries the same rule and no second rule contradicts it.
4. Any non-blank entries_json (string or not) is the legacy form.
5. The static SiteWise tool table used when discovery fails lists the real
   tool surface.

Offline: fake or scripted models only; no AWS, no Bedrock.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from strands import Agent, tool
from strands.models.model import Model
from strands.types.exceptions import MaxTokensReachedException

import agents.orchestrator as orch
import agents.sitewise_read as sitewise_read
import agents.system_agent as sa
import agents.tool_result_capture as trc
import agents.workflow_guidance as wg
from agents import onboard_agent

_FAILED = {"tool": "sitewise_batch_get_property_values", "transport_status": "error",
           "app_status": "error", "safe_error_category": "transport_error"}


def _flat(text: str) -> str:
    return " ".join(text.split())


def _result(*, content=None, stop_reason="end_turn"):
    return SimpleNamespace(message={"role": "assistant", "content": content or []},
                           structured_output=None, interrupts=None,
                           stop_reason=stop_reason)


class _ScriptedModel(Model):
    """Plays one scripted stream per model call (offline strands model)."""

    def __init__(self, *streams):
        self.streams = list(streams)
        self.calls = 0

    def update_config(self, **model_config):
        return None

    def get_config(self):
        return {}

    def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        events = self.streams[self.calls]
        self.calls += 1
        for event in events:
            yield event


_EMPTY_END_TURN = [{"messageStart": {"role": "assistant"}},
                   {"messageStop": {"stopReason": "end_turn"}}]
_TRUNCATED_TOOL_CALL = [
    {"messageStart": {"role": "assistant"}},
    {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "t-9", "name": "read_values"}}}},
    {"contentBlockDelta": {"delta": {"toolUse": {"input": '{"entries_json": "[{\\"asse'}}}},
    {"contentBlockStop": {}},
    {"messageStop": {"stopReason": "max_tokens"}},
]


def _complete_tool_call(name: str):
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "t-1", "name": name}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
    ]


# --------------------------------------------------------------------------- #
# 1. The retry's own output-limit stop drives the fallback
# --------------------------------------------------------------------------- #
class _EmptyThenCap:
    def __init__(self):
        self.calls = 0

    def __call__(self, prompt, *, invocation_state=None, limits=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return _result(stop_reason="end_turn")
        raise MaxTokensReachedException(message="max_tokens limit")


@pytest.mark.parametrize("mode", ["", "annotate"])
def test_retry_that_stops_at_the_output_limit_drives_the_fallback(monkeypatch, mode):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", mode)
    agent = _EmptyThenCap()
    answer = sa._invoke_mode_a_agent(agent, "read every asset")
    assert agent.calls == 2
    assert answer == f"{trc.OUTPUT_LIMIT_MESSAGE} {trc.OUTPUT_LIMIT_GUIDANCE}"


def test_real_strands_retry_that_stops_at_the_output_limit(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "annotate")
    model = _ScriptedModel(_EMPTY_END_TURN, _TRUNCATED_TOOL_CALL)
    agent = Agent(model=model, tools=[], callback_handler=None)
    answer = sa._invoke_mode_a_agent(agent, "read every asset")
    assert model.calls == 2
    assert trc.OUTPUT_LIMIT_MESSAGE in answer and trc.OUTPUT_LIMIT_GUIDANCE in answer


# --------------------------------------------------------------------------- #
# 2. Failed tool attempt, then the output limit
# --------------------------------------------------------------------------- #
def _failed_state(*items):
    return {trc.TOOL_RESULTS_STATE_KEY: [dict(item) for item in items or (_FAILED,)]}


def test_failed_tool_then_output_limit_envelope_does_not_claim_the_system_was_not_reached():
    out = json.loads(trc.extract_mode_a_result(_result(stop_reason="max_tokens"),
                                               _failed_state()))
    assert out["status"] == "error"
    assert out["error_type"] == "incomplete"
    assert out["stop_reason"] == "max_tokens"
    assert out["message"] == trc.OUTPUT_LIMIT_FAILED_MESSAGE
    assert out["guidance"] == trc.OUTPUT_LIMIT_FAILED_GUIDANCE
    for claim in ("not reached", "not known to be down", "before it could be sent"):
        assert claim not in out["message"] and claim not in out["guidance"]
    # The captured failure stays in the envelope.
    assert out["tool_results"][0]["safe_error_category"] == "transport_error"
    assert out["tool_results"][0]["transport_status"] == "error"


def test_failed_tool_then_output_limit_plain_line_keeps_the_failure_visible():
    envelope = trc.extract_mode_a_result(_result(stop_reason="max_tokens"), _failed_state())
    line = sa._plain_language_incomplete(envelope)
    assert line == (f"{trc.OUTPUT_LIMIT_FAILED_MESSAGE} Failed attempts: transport_error. "
                    f"{trc.OUTPUT_LIMIT_FAILED_GUIDANCE}")
    assert "not reached" not in line


def test_a_timed_out_attempt_is_listed_for_the_model_and_never_code_retried():
    """The Mode A query tool returns this line as an ordinary (successful) MCP
    tool result, so no code-level retry ever applies; the failure category is
    there for the orchestrator's model, whose prompt sends it to the one
    narrower follow-up (a query_timeout is the agent's own slow query)."""
    timed_out = {**_FAILED, "transport_status": "success", "safe_error_category": "query_timeout"}
    envelope = trc.extract_mode_a_result(_result(stop_reason="max_tokens"),
                                         _failed_state(timed_out))
    line = sa._plain_language_incomplete(envelope)
    assert "Failed attempts: query_timeout." in line
    mcp_result = {"status": "success", "content": [{"text": line}]}
    assert orch._retry_backoffs_for(mcp_result) is None
    assert "retried as the same call" not in line


def test_unrecognised_attempt_then_output_limit_lists_no_failure_category():
    unknown = {"tool": "read_values", "transport_status": "success", "app_status": "unknown"}
    envelope = trc.extract_mode_a_result(_result(stop_reason="max_tokens"),
                                         _failed_state(unknown))
    assert json.loads(envelope)["message"] == trc.OUTPUT_LIMIT_FAILED_MESSAGE
    line = sa._plain_language_incomplete(envelope)
    assert line == f"{trc.OUTPUT_LIMIT_FAILED_MESSAGE} {trc.OUTPUT_LIMIT_FAILED_GUIDANCE}"


def test_real_strands_failed_tool_then_output_limit(monkeypatch):
    """End to end: a real tool fails, the real capture hook records it, the
    next model turn stops at the output cap."""
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "annotate")

    @tool(name="read_values")
    def read_values() -> str:
        """Read values."""
        raise RuntimeError("connection reset")

    model = _ScriptedModel(_complete_tool_call("read_values"), _TRUNCATED_TOOL_CALL)
    hook = sa._InvocationToolResultCapture(system_id="sys-x", boundary="sitewise",
                                           registered_tools={"read_values"})
    agent = Agent(model=model, tools=[read_values], hooks=[hook], callback_handler=None)
    answer = sa._invoke_mode_a_agent(agent, "read every asset")
    assert model.calls == 2
    assert answer.startswith(trc.OUTPUT_LIMIT_FAILED_MESSAGE)
    assert "Failed attempts:" in answer
    assert "not reached" not in answer


# --------------------------------------------------------------------------- #
# 3. ONE rule inside MISS FOLLOW-UP, in every composed prompt
# --------------------------------------------------------------------------- #
_RULE = "MISS FOLLOW-UP — ONE, THEN STOP"
_TOO_LARGE = ("If the follow-up also stops at the output limit, tell the user the request "
              "was too large to read in one turn and suggest a narrower question; that is "
              "not the system being down.")


def _composed_prompts(monkeypatch, workshop: bool):
    monkeypatch.setenv("ADT_WORKSHOP", "1" if workshop else "")
    # The Quick prompts slice the import-time ORCHESTRATOR_PROMPT; rebuild it
    # under the flag so both workshop states are really exercised.
    monkeypatch.setattr(orch, "ORCHESTRATOR_PROMPT", orch._build_orchestrator_prompt("agent"))
    for mode in ("agent", "orchestrator"):
        yield f"full-{mode}", orch._build_orchestrator_prompt(mode)
        yield f"quick-{mode}", wg.compose_quick_system_prompt(mode)


@pytest.mark.parametrize("workshop", [False, True], ids=["workshop-off", "workshop-on"])
def test_every_prompt_carries_the_output_limit_case_inside_the_one_budget(monkeypatch, workshop):
    case = _flat(wg._OUTPUT_LIMIT_MISS)
    seen = []
    for name, prompt in _composed_prompts(monkeypatch, workshop):
        flat = _flat(prompt)
        assert flat.count(case) == 1, name
        rule_at = flat.index(_RULE)
        assert rule_at < flat.index(case) < flat.index("After the follow-up, stop querying", rule_at), name
        # A second output-limit answer is reported as too large, never as the
        # system being down (the unavailable-system contract must not apply).
        assert flat.count(_TOO_LARGE) == 1, name
        assert "a transient one is retried as the same call" not in flat, name
        seen.append(name)
    assert len(seen) == 4


def test_the_output_limit_case_is_its_own_kind_of_miss():
    case = _flat(wg._OUTPUT_LIMIT_MISS).lower()
    assert "output limit" in case
    assert "not a capability limit" in case
    assert "never repeat it" in case and "narrower" in case
    # A listed failed data query goes to the same narrower follow-up, never a
    # same-call retry: the turn that hit the cap would hit it again.
    assert "not transient, even when it lists a failed data query" in case
    assert "retried as the same call" not in case


def test_failed_guidance_never_asks_for_a_same_call_retry():
    guidance = trc.OUTPUT_LIMIT_FAILED_GUIDANCE
    assert "retried as the same call" not in guidance
    assert "narrower slice" in guidance
    assert "repeating the same request" in guidance


def test_no_separate_orchestrator_output_limit_rule_remains():
    assert not hasattr(orch, "_OUTPUT_LIMIT_RETRY_RULE")
    for mode in ("agent", "orchestrator"):
        assert "OUTPUT LIMIT IS NOT A SYSTEM FAILURE" not in orch._build_orchestrator_prompt(mode)


def test_quick_skill_doc_carries_the_output_limit_case():
    assert _flat(wg.render_quick_skill_markdown()).count(_flat(wg._OUTPUT_LIMIT_MISS)) == 1


# --------------------------------------------------------------------------- #
# 4. Any non-blank entries_json is the legacy form
# --------------------------------------------------------------------------- #
def _tool(name):
    tools = sa._make_sitewise_direct_tool(region="us-east-1", connection_config={},
                                          system_id="sys-x")
    return next(t for t in tools if t.tool_name == name)


@pytest.fixture
def no_reads(monkeypatch):
    def _never(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("reader must not be called for a rejected request")

    monkeypatch.setattr(sitewise_read, "batch_get_property_values", _never)
    monkeypatch.setattr(sitewise_read, "get_property_history", _never)


_NON_STRINGS = [{"assetId": "asset-1"}, [{"assetId": "asset-1", "propertyId": "prop-a"}], 7]
_TOOLS = ["sitewise_batch_get_property_values", "sitewise_get_asset_property_history"]


@pytest.mark.parametrize("name", _TOOLS)
@pytest.mark.parametrize("value", _NON_STRINGS, ids=["dict", "list", "int"])
def test_non_string_entries_json_alone_is_an_invalid_request(no_reads, name, value):
    out = json.loads(_tool(name)._tool_func(entries_json=value))
    assert out == {"error": "entries_json must be a JSON string", "error_type": "invalid_request"}


@pytest.mark.parametrize("name", _TOOLS)
@pytest.mark.parametrize("value", _NON_STRINGS + [" x "], ids=["dict", "list", "int", "str"])
def test_any_entries_json_mixed_with_the_pair_is_rejected(no_reads, name, value):
    out = json.loads(_tool(name)._tool_func(entries_json=value, asset_ids_json='["asset-1"]',
                                            property_ids_json='["prop-a"]'))
    assert out == {"error": "pass either entries_json or asset_ids_json + property_ids_json, "
                            "not both", "error_type": "invalid_request"}


# --------------------------------------------------------------------------- #
# 5. The discovery fallback lists the real tool surface
# --------------------------------------------------------------------------- #
def test_sitewise_fallback_tables_match_the_real_tool_schemas():
    real = {t.tool_name: set(t.tool_spec["inputSchema"]["json"]["properties"])
            for t in sa._make_sitewise_direct_tool(region="us-east-1", connection_config={})}
    fallback = {row["name"]: {c["name"] for c in row["columns"]}
                for row in onboard_agent._sitewise_fallback_tables()}
    assert fallback == real
    assert {"asset_ids_json", "property_ids_json"} <= fallback["sitewise_batch_get_property_values"]
    assert {"start_iso", "end_iso", "asset_ids_json", "property_ids_json"} <= (
        fallback["sitewise_get_asset_property_history"])


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
