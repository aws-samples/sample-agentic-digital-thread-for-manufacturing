# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``bedrock_call`` speaks Converse: one request shape, one response reader.

The raw InvokeModel path drifted from a newer model twice in two days on a
workshop package: ``temperature`` rejected (2026-10-01), then
``content[0]["text"]`` raising KeyError because block zero of the response was
not a text block (event 22, 2026-10-02), so every map escalation during the
seed fell back to the weak mapping. The Strands agents on the same model
needed nothing because they go through Converse. ``bedrock_call``,
``prompts._invoke_llm`` and ``video_briefing._generate_script`` now all go
through ``db.converse_text``: the request is ``messages`` + ``inferenceConfig``
for every family, the text is every text block of ``output.message.content``
in order, and a response with no text block fails naming what came back.

Offline: fake clients only.
"""
from __future__ import annotations

import inspect

import pytest

import agents.db as db

SONNET_5 = "us.anthropic.claude-sonnet-5"
NOVA = "us.amazon.nova-pro-v1:0"
_LEADING_BLOCK = {"reasoningContent": {"reasoningText": {"text": "thinking..."}}}


class _Client:
    def __init__(self, response: dict) -> None:
        self.response, self.calls = response, []

    def converse(self, **kw):
        self.calls.append(kw)
        return self.response

    def invoke_model(self, **kw):  # pragma: no cover - must never be reached
        raise AssertionError("invoke_model is retired; bedrock_call speaks Converse")


def _resp(*blocks, stop="end_turn"):
    return {"output": {"message": {"role": "assistant", "content": list(blocks)}},
            "stopReason": stop}


# --------------------------------------------------------------------------- #
# request shape
# --------------------------------------------------------------------------- #

def test_request_is_converse_messages_for_both_families():
    for model in (NOVA, "us.anthropic.claude-sonnet-4-20250514-v1:0"):
        req = db._converse_request("p", model)
        assert req["modelId"] == model
        assert req["messages"] == [{"role": "user", "content": [{"text": "p"}]}]
        assert req["inferenceConfig"]["temperature"] == 0.0
        assert req["inferenceConfig"]["maxTokens"] > 0
        assert "system" not in req


def test_temperature_is_omitted_where_the_catalog_says_the_model_rejects_it(monkeypatch):
    import agents.model_catalog as mc
    monkeypatch.setattr(mc, "accepts_temperature", lambda m: False)
    req = db._converse_request("p", SONNET_5)
    assert "temperature" not in req["inferenceConfig"]
    assert req["inferenceConfig"]["maxTokens"] > 0


def test_system_prompt_and_max_tokens_ride_the_same_request():
    req = db._converse_request("p", SONNET_5, system="be brief", max_tokens=321)
    assert req["system"] == [{"text": "be brief"}]
    assert req["inferenceConfig"]["maxTokens"] == 321


# --------------------------------------------------------------------------- #
# response reader
# --------------------------------------------------------------------------- #

def test_text_is_taken_from_the_text_blocks_not_block_zero():
    client = _Client(_resp(_LEADING_BLOCK, {"text": '[{"a": 1}]'}))
    assert db._invoke_bedrock(client, "p", SONNET_5, 0) == '[{"a": 1}]'
    assert client.calls[0]["modelId"] == SONNET_5


def test_several_text_blocks_are_joined_in_order():
    client = _Client(_resp({"text": "one "}, _LEADING_BLOCK, {"text": "two"}))
    assert db._invoke_bedrock(client, "p", SONNET_5, 0) == "one two"


def test_nova_reads_through_the_same_reader():
    client = _Client(_resp({"text": "nova"}))
    assert db._invoke_bedrock(client, "p", NOVA, 0) == "nova"


def test_no_text_block_fails_loudly_naming_the_shape():
    client = _Client(_resp(_LEADING_BLOCK, stop="max_tokens"))
    with pytest.raises(ValueError, match="reasoningContent.*max_tokens"):
        db._invoke_bedrock(client, "p", SONNET_5, 0)


def test_converse_text_is_the_one_entry_for_system_prompted_callers():
    client = _Client(_resp({"text": "  hello  "}))
    assert db.converse_text(client, SONNET_5, "p", system="s", max_tokens=99) == "  hello  "
    req = client.calls[0]
    assert req["system"] == [{"text": "s"}] and req["inferenceConfig"]["maxTokens"] == 99


# --------------------------------------------------------------------------- #
# no second reader anywhere
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("modname", ["agents.db", "agents.prompts", "agents.video_briefing"])
def test_no_module_hand_builds_an_invoke_model_body(modname):
    import importlib
    src = inspect.getsource(importlib.import_module(modname))
    assert "anthropic_version" not in src, modname
    assert '["content"][0]["text"]' not in src, modname


def test_prompts_and_video_briefing_call_converse_text():
    from agents import prompts, video_briefing
    for mod in (prompts, video_briefing):
        src = inspect.getsource(mod)
        assert "converse_text(" in src, mod.__name__
        assert ".invoke_model(" not in src, mod.__name__
