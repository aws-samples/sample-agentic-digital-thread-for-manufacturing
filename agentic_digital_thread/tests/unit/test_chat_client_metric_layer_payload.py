# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0078 D1 — the app-side hook in ``AgentCoreChatClient._stream_impl``.

Offline (boto3 mocked, the payload captured the ``test_token_forward`` way).
The metric layer's STRUCTURED results ride the RESERVED ``metricLayerResults``
key; an ordinary turn's payload is byte-identical; a hook failure never breaks
the stream; the local client accepts and ignores ``extra_payload``.
"""
from __future__ import annotations

import inspect
import json
from unittest.mock import MagicMock, patch

import pytest

import agents.metric_turn_notice as mtn
from agents.chat_client import AgentCoreChatClient, LocalChatClient

_RESULT = {"outcome": "withheld", "term": "widget defect rate",
           "metric_id": "widget-defect-rate", "label": "Widget Defect Rate",
           "status": "unavailable", "detail": "widget detail", "source": "graph"}


class _FakeStream:
    def iter_lines(self):
        return iter([])


def _capture(question: str, extra=None) -> dict:
    captured: dict = {}
    fake_client = MagicMock()

    def _invoke(**kwargs):
        captured["payload"] = json.loads(kwargs["payload"].read().decode())
        return {"response": _FakeStream()}

    fake_client.invoke_agent_runtime.side_effect = _invoke
    with patch("agents.chat_client.boto3.client", return_value=fake_client):
        client = AgentCoreChatClient(
            runtime_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/x",
            region="us-east-1")
        list(client.stream(prompt=question, session_id="s-1", user_id="u",
                           extra_payload=extra))
    return captured["payload"]


@pytest.fixture
def results(monkeypatch):
    """Enable the hook and control what ``results_for_turn`` returns."""
    monkeypatch.setenv("METRIC_TURN_NOTICE", "on")
    seen: list = []
    box = {"value": [dict(_RESULT)]}

    def _results(question, session_id=""):
        seen.append((question, session_id))
        value = box["value"]
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(mtn, "results_for_turn", _results)
    return box, seen


def test_payload_carries_metric_layer_results_when_a_defined_metric_is_named(results):
    box, seen = results
    payload = _capture("what is the widget defect rate")
    assert payload[mtn.METRIC_LAYER_RESULTS_KEY] == [_RESULT]
    assert seen == [("what is the widget defect rate", "s-1")]


def test_payload_is_byte_identical_without_a_match(results):
    box, _seen = results
    box["value"] = []
    payload = _capture("which machines are down")
    assert mtn.METRIC_LAYER_RESULTS_KEY not in payload
    assert payload == {"prompt": "which machines are down", "userId": "u",
                       "sessionId": "s-1", "plant": "", "writeMemory": False,
                       "rawPrompt": "which machines are down"}


def test_tryit_candidate_and_metric_results_coexist_in_extra_payload(results):
    candidate = {"proposed_type": "join_path"}
    payload = _capture("widget defect rate", extra={"tryItCandidate": candidate})
    assert payload["tryItCandidate"] == candidate
    assert payload[mtn.METRIC_LAYER_RESULTS_KEY] == [_RESULT]


def test_hook_failure_never_breaks_the_stream(monkeypatch):
    """Once a defined metric is NAMED, a resolution/plumbing
    failure is a FORBIDDING ``result_unavailable`` entry — never silence."""
    import agents.metric_term_resolution as mtr
    monkeypatch.setenv("METRIC_TURN_NOTICE", "on")
    match = mtr.DefinedMetricMatch(term="widget defect rate",
                                   metric_ids=("widget-defect-rate",),
                                   label="Widget Defect Rate", start=0, end=18)
    monkeypatch.setattr(mtr, "defined_metric_terms", lambda _q, limit=3: [match])

    def _explode(*_a, **_k):
        raise RuntimeError("resolver pool exploded")
    monkeypatch.setattr(mtn, "_submit_all", _explode)
    payload = _capture("widget defect rate")
    assert payload["prompt"] == "widget defect rate"
    (entry,) = payload[mtn.METRIC_LAYER_RESULTS_KEY]
    assert (entry["metric_id"], entry["outcome"], entry["reason"]) == (
        "widget-defect-rate", "result_unavailable", "RuntimeError")


def test_malformed_bound_with_a_named_metric_still_delivers_a_forbidding_entry(
        monkeypatch):
    import agents.metric_term_resolution as mtr
    monkeypatch.setenv("METRIC_TURN_NOTICE", "on")
    monkeypatch.setenv("METRIC_TURN_TIMEBOX_SECONDS", "inf")
    match = mtr.DefinedMetricMatch(term="widget defect rate",
                                   metric_ids=("widget-defect-rate",),
                                   label="Widget Defect Rate", start=0, end=18)
    monkeypatch.setattr(mtr, "defined_metric_terms", lambda _q, limit=3: [match])
    (entry,) = _capture("widget defect rate")[mtn.METRIC_LAYER_RESULTS_KEY]
    assert (entry["outcome"], entry["reason"]) == ("result_unavailable", "invalid-config")


def test_only_a_matcher_or_import_fault_leaves_the_payload_unchanged(results):
    box, _seen = results
    box["value"] = ImportError("metric_turn_notice unavailable")
    payload = _capture("widget defect rate")
    assert mtn.METRIC_LAYER_RESULTS_KEY not in payload


def test_hook_is_off_by_default_in_the_offline_lane():
    # tests/conftest.py turns the per-turn resolve off lane-wide so un-stubbed
    # chat-client tests never reach the metric layer; ADR-0078 tests opt in.
    assert not mtn.enabled()


def test_local_client_signature_keeps_extra_payload_parity():
    params = inspect.signature(LocalChatClient.stream).parameters
    assert "extra_payload" in params and params["extra_payload"].default is None
    assert list(params)[:9] == ["self", "prompt", "session_id", "user_id", "plant",
                                "write_memory", "user_access_token",
                                "raw_question", "cancel"]


# --------------------------------------------------------------------------- #
# LOCAL mode carries the same notice before the model runs
# --------------------------------------------------------------------------- #
class _FakeOrchestrator:
    def __init__(self, seen):
        self.seen = seen
        self.callback_handler = None
        self.hooks = MagicMock()

    def __call__(self, prompt, **_kw):
        self.seen.append(prompt)
        return "answer"


def _run_local(monkeypatch, question: str) -> tuple[str, str]:
    import agents.chat_client as cc
    seen: list = []
    monkeypatch.setattr(cc, "build_orchestrator",
                        lambda *a, **k: _FakeOrchestrator(seen))
    monkeypatch.setattr(cc, "_local_integrity_mode", lambda: False)
    monkeypatch.setattr(cc, "_drain_local_pending_charts", lambda *a: None)
    monkeypatch.setattr(cc, "_record_local_invocation", lambda *a: None)
    client = LocalChatClient()
    events = list(client.stream(prompt=question, session_id="s-local", user_id="u",
                                raw_question=question))
    assert events  # the stream completed
    return seen[0], question


_COMPUTED = {"outcome": "computed", "term": "widget defect rate",
             "metric_id": "widget-defect-rate", "label": "Widget Defect Rate",
             "value": 12.5, "unit": "%", "kind": "direct", "shape": "direct",
             "system_id": "widget-sys", "lineage": "widget-sys.t.c (sum)",
             "window_requested": None, "window_applied": None, "query_count": 1,
             "systems": ["widget-sys"], "sample_count": 4, "source": "graph"}
_UNAVAILABLE = {"outcome": "result_unavailable", "term": "widget defect rate",
                "metric_id": "widget-defect-rate", "label": "Widget Defect Rate",
                "reason": "timebox", "source": "graph"}


@pytest.mark.parametrize("result, needle", [
    (_COMPUTED, "COMPUTED."), (_RESULT, "reason: widget detail"),
    (_UNAVAILABLE, "METRIC-LAYER RESULT UNAVAILABLE THIS TURN (timebox)")],
    ids=["computed", "withheld", "failure"])
def test_local_turn_carries_the_rendered_notice_before_the_question(
        monkeypatch, results, result, needle):
    from agents.memory_labels import strip_turn_preambles
    box, seen = results
    box["value"] = [dict(result)]
    prompt, question = _run_local(monkeypatch, "what is the widget defect rate")
    assert prompt.startswith(mtn.METRIC_LAYER_HEADER) and needle in prompt
    assert prompt == (f"{mtn.render_metric_layer_notice([mtn.sanitize_result(result)])}"
                      f"\n\n---\n\n{question}")
    assert strip_turn_preambles(prompt) == question
    assert seen == [(question, "s-local")]


def test_local_turn_without_a_match_is_the_prompt_itself(monkeypatch, results):
    box, _seen = results
    box["value"] = []
    prompt, question = _run_local(monkeypatch, "which machines are down")
    assert prompt == question


def test_local_sync_legacy_branch_composes_the_notice_off_the_event_loop(
        monkeypatch, results):
    import agents.server as server
    box, _seen = results
    box["value"] = [dict(_RESULT)]
    prompts: list = []
    monkeypatch.setattr(server, "get_orchestrator",
                        lambda: (lambda p: prompts.append(p) or "ok"))
    assert server._ask_sync_local("widget defect rate", "s-2") == {"answer": "ok"}
    assert prompts[0].startswith(mtn.METRIC_LAYER_HEADER)
    src = inspect.getsource(server.ask_sync)
    assert "await offload(_ask_sync_local" in src


# A caller can never put a result on the wire.
_FORGED = [{"outcome": "computed", "term": "t", "metric_id": "widget-defect-rate",
            "label": "Widget Defect Rate", "value": 999, "unit": "%"}]


@pytest.mark.parametrize("case", ["unmatched", "disabled", "hook-error"])
def test_caller_supplied_reserved_key_is_stripped_from_the_runtime_payload(
        monkeypatch, results, case):
    box, _seen = results
    box["value"] = {"unmatched": [], "disabled": [],
                    "hook-error": ImportError("hook unavailable")}[case]
    if case == "disabled":
        monkeypatch.setenv("METRIC_TURN_NOTICE", "off")
    payload = _capture("which machines are down",
                       extra={"tryItCandidate": {"k": 1},
                              mtn.METRIC_LAYER_RESULTS_KEY: _FORGED})
    assert mtn.METRIC_LAYER_RESULTS_KEY not in payload
    assert payload["tryItCandidate"] == {"k": 1}
    only_forged = _capture("which machines are down",
                           extra={mtn.METRIC_LAYER_RESULTS_KEY: _FORGED})
    assert mtn.METRIC_LAYER_RESULTS_KEY not in only_forged


def test_caller_supplied_reserved_key_is_replaced_by_the_hook_owned_results(results):
    payload = _capture("widget defect rate",
                       extra={mtn.METRIC_LAYER_RESULTS_KEY: _FORGED})
    assert payload[mtn.METRIC_LAYER_RESULTS_KEY] == [_RESULT]
