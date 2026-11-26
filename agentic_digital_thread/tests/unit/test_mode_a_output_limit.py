# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A system agent that stops at its own output-token cap is not an outage.

The observed failure: a SiteWise system agent began ONE batch read whose
request (one id pair per entry) ran past its 8192-token output cap mid tool
call. strands replaced the truncated tool use with an error and raised
MaxTokensReachedException ("Agent has reached an unrecoverable state due to
max_tokens limit"). Nothing caught it, so it crossed the query boundary as a
raw framework error; the orchestrator treated it as a transient outage,
repeated the identical call three times, and told the user the system was
unavailable. The system was never asked.

The fix: the Mode A invoke path turns that stop into a fixed, metadata-only
incomplete answer saying the request outgrew the agent's output limit, the
system was not reached and is not known to be down, and a narrower ask is
needed; the orchestrator prompt makes that answer ineligible for an identical
retry. Every string here is fixed text, never model text, and must not trip
any outage/transient classifier.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from strands import Agent
from strands.models.model import Model
from strands.types.exceptions import MaxTokensReachedException

import agents.chat_client as chat_client
import agents.orchestrator as orch
import agents.system_agent as sa
import agents.tool_result_capture as trc

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import smoke_prompts as smoke  # noqa: E402


def _result(*, content=None, stop_reason="end_turn"):
    return SimpleNamespace(message={"role": "assistant", "content": content or []},
                           structured_output=None, interrupts=None,
                           stop_reason=stop_reason)


def _success_state():
    return {trc.TOOL_RESULTS_STATE_KEY: [{
        "tool": "read_values", "transport_status": "success",
        "app_status": "success", "row_count": 3,
    }]}


# --------------------------------------------------------------------------- #
# 1. The premise: strands RAISES on a max_tokens stop (it never returns)
# --------------------------------------------------------------------------- #
class _TruncatingModel(Model):
    """Streams a tool call whose input is cut off by the output-token cap."""

    def update_config(self, **model_config):
        return None

    def get_config(self):
        return {}

    def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {"toolUse": {
            "toolUseId": "t-1", "name": "read_values"}}}}
        yield {"contentBlockDelta": {"delta": {"toolUse": {
            "input": '{"entries_json": "[{\\"assetId\\": \\"asset-1\\"'}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "max_tokens"}}


def test_strands_raises_when_the_model_stops_at_its_output_cap():
    agent = Agent(model=_TruncatingModel(), tools=[], callback_handler=None)
    with pytest.raises(MaxTokensReachedException):
        agent("read every asset")


@pytest.mark.parametrize("mode", ["annotate", "enforce"])
def test_real_strands_agent_at_its_output_cap_yields_the_fixed_answer(monkeypatch, mode):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", mode)
    agent = Agent(model=_TruncatingModel(), tools=[], callback_handler=None)
    answer = sa._invoke_mode_a_agent(agent, "read every asset")
    assert trc.OUTPUT_LIMIT_MESSAGE in answer
    assert trc.OUTPUT_LIMIT_GUIDANCE in answer
    assert "unrecoverable" not in answer


# --------------------------------------------------------------------------- #
# 2. The Mode A invoke path turns that stop into the fixed answer
# --------------------------------------------------------------------------- #
class _CapAgent:
    """Raises MaxTokensReachedException like a real agent at its output cap."""

    def __init__(self):
        self.calls = 0

    def __call__(self, prompt, *, invocation_state=None, limits=None,
                 structured_output_model=None):
        self.calls += 1
        raise MaxTokensReachedException(message="max_tokens limit")


@pytest.mark.parametrize("mode", ["report-only", "annotate"])
def test_legacy_modes_return_the_fixed_line_without_an_elicitation_retry(monkeypatch, mode):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "" if mode == "report-only" else mode)
    agent = _CapAgent()
    answer = sa._invoke_mode_a_agent(agent, "read every asset")
    # The elicitation retry would only hit the same cap: one call, fixed line.
    assert agent.calls == 1
    assert answer == f"{trc.OUTPUT_LIMIT_MESSAGE} {trc.OUTPUT_LIMIT_GUIDANCE}"


def test_enforce_mode_returns_the_fixed_incomplete_envelope(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    out = json.loads(sa._invoke_mode_a_agent(_CapAgent(), "read every asset"))
    assert out == {"status": "error", "error_type": "incomplete",
                   "message": trc.OUTPUT_LIMIT_MESSAGE,
                   "guidance": trc.OUTPUT_LIMIT_GUIDANCE, "stop_reason": "max_tokens"}


def test_other_exceptions_still_propagate(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "annotate")

    class _Broken:
        def __call__(self, prompt, **kwargs):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        sa._invoke_mode_a_agent(_Broken(), "q")


# --------------------------------------------------------------------------- #
# 3. extract_mode_a_result: fixed max_tokens text, taxonomy unchanged
# --------------------------------------------------------------------------- #
def test_max_tokens_without_success_says_the_system_was_not_reached():
    out = json.loads(trc.extract_mode_a_result(_result(stop_reason="max_tokens"), {}))
    assert out["status"] == "error"
    assert out["error_type"] == "incomplete"
    assert out["stop_reason"] == "max_tokens"
    assert out["message"] == trc.OUTPUT_LIMIT_MESSAGE
    assert out["guidance"] == trc.OUTPUT_LIMIT_GUIDANCE
    lowered = out["message"].lower()
    assert "output limit" in lowered and "not reached" in lowered
    assert "not known to be down" in lowered
    assert "narrower" in out["guidance"]


def test_max_tokens_after_successful_work_stays_partial_with_fixed_text():
    out = json.loads(trc.extract_mode_a_result(_result(stop_reason="max_tokens"),
                                               _success_state()))
    assert out["status"] == "partial"
    assert out["error_type"] == "incomplete"
    assert out["message"] == trc.OUTPUT_LIMIT_PARTIAL_MESSAGE
    assert out["guidance"] == trc.OUTPUT_LIMIT_GUIDANCE
    assert out["tool_results"][0]["row_count"] == 3


@pytest.mark.parametrize("stop", ["end_turn", "tool_use", "unknown"])
def test_other_stop_reasons_keep_the_generic_envelope(stop):
    out = json.loads(trc.extract_mode_a_result(_result(stop_reason=stop), {}))
    assert out == {"status": "error", "error_type": "incomplete",
                   "message": "Agent produced no final text and no usable tool "
                              "result containing successful data.",
                   "stop_reason": stop}
    partial = json.loads(trc.extract_mode_a_result(_result(stop_reason=stop),
                                                   _success_state()))
    assert partial["message"] == "Agent completed some tool work but produced no final text."
    assert "guidance" not in partial


def test_plain_language_line_carries_the_fixed_max_tokens_text():
    error = trc.extract_mode_a_result(_result(stop_reason="max_tokens"), {})
    assert sa._plain_language_incomplete(error) == (
        f"{trc.OUTPUT_LIMIT_MESSAGE} {trc.OUTPUT_LIMIT_GUIDANCE}")
    partial = trc.extract_mode_a_result(_result(stop_reason="max_tokens"), _success_state())
    assert sa._plain_language_incomplete(partial) == (
        f"{trc.OUTPUT_LIMIT_PARTIAL_MESSAGE} {trc.OUTPUT_LIMIT_GUIDANCE}")
    # A non-max_tokens envelope renders exactly as before.
    generic = trc.extract_mode_a_result(_result(stop_reason="end_turn"), {})
    assert sa._plain_language_incomplete(generic) == (
        "This system could not complete the request (no result produced).")


# --------------------------------------------------------------------------- #
# 4. No new fixed string trips an outage / transient / error classifier
# --------------------------------------------------------------------------- #
def _new_fixed_strings():
    """Resolved at test time so a missing name fails one test, not collection."""
    import agents.workflow_guidance as wg
    strings = [trc.OUTPUT_LIMIT_MESSAGE, trc.OUTPUT_LIMIT_PARTIAL_MESSAGE,
               trc.OUTPUT_LIMIT_GUIDANCE, trc.OUTPUT_LIMIT_FAILED_MESSAGE,
               trc.OUTPUT_LIMIT_FAILED_GUIDANCE, wg._OUTPUT_LIMIT_MISS]
    strings += [sa._sitewise_invalid(message)
                for message in sa.SITEWISE_COMPACT_ERROR_MESSAGES]
    strings.append(sa._sitewise_compact_too_large(5000, 1000))
    # A failed attempt with a neutral category: the guard checks the FIXED
    # text; a listed category (e.g. query_timeout) is closed-taxonomy metadata
    # shown on purpose (test_output_limit_review).
    failed = {trc.TOOL_RESULTS_STATE_KEY: [{
        "tool": "read_values", "transport_status": "success", "app_status": "error",
        "safe_error_category": "tool_error"}]}
    for stop_state in ({}, _success_state(), failed):
        envelope = trc.extract_mode_a_result(_result(stop_reason="max_tokens"), stop_state)
        strings += [envelope, sa._plain_language_incomplete(envelope)]
    return strings


def _classifier_hits(text):
    lowered = text.lower()
    hits = [m for m in orch._TRANSIENT_TOOL_ERROR_MARKERS + orch._AUTH_TOOL_ERROR_MARKERS
            if m in lowered]
    hits += [c for c, pattern in chat_client._ERROR_TEXT_CLASSIFIERS if pattern.search(text)]
    hits += smoke.classify_error_labels([text]) + smoke.detect_outage_fallback_signals(text)
    hits += [w for w in ("unavailable", "outage") if w in lowered]
    return hits


def test_fixed_strings_trip_no_outage_or_transient_classifier():
    strings = _new_fixed_strings()
    assert len(strings) >= 8
    assert {text: _classifier_hits(text) for text in strings
            if _classifier_hits(text)} == {}


def test_classifier_probe_still_detects_a_real_outage_phrase():
    # Guards the probe above against silently matching nothing.
    assert _classifier_hits("the system is currently unavailable (timed out)")


def test_fixed_envelopes_are_not_retried_by_the_gateway_wrapper():
    for stop_state in ({}, _success_state()):
        envelope = trc.extract_mode_a_result(_result(stop_reason="max_tokens"), stop_state)
        mcp_result = {"status": "error", "content": [{"text": envelope}]}
        assert orch._retry_backoffs_for(mcp_result) is None


# --------------------------------------------------------------------------- #
# 5. The prompt side lives in the MISS FOLLOW-UP contract: see
#    test_output_limit_review (one rule in every composed prompt).
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
