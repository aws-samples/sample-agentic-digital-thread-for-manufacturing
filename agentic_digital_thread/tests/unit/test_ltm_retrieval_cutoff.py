# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The long-term-memory retrieval cutoff a chat turn declares is the one it gets.

The chat path retrieves through bedrock_agentcore's strands
``AgentCoreMemorySessionManager.retrieve_customer_context``. SDK 1.1.1 asked
the service for ``top_k`` records and injected every one it got back: it never
read ``RetrievalConfig.relevance_score``. From 1.2 (the pinned 1.18.1
included) the SDK filters by it (``score >= relevance_score`` when the value
is truthy).
The orchestrator used to declare 0.5 for preferences and summaries and 0.3 for
facts, so the config claimed a cutoff the app never applied, and an SDK bump
would have started applying it silently.

The service's scores do not support a 0.5 cutoff: in a 2026-10-25/26 run on
adt-demo (us-east-1, 8 conversations, 3 runs each, five extraction models) the
preference that answered a follow-up question scored 0.35 to 0.54, and with a
0.5 cutoff it would have reached the turn in 0 to 2 of 12 cases per model
instead of 9 to 12. So every namespace declares 0.0, no cutoff, which is what
happens today and what both SDK behaviours keep.

Pinned here: the declared values, a preference at a measured low score still
reaching the turn under an SDK that applies the cutoff, and the pinned SDK
injecting every record the service returns.
"""

import sys
import types

import pytest

import agents.orchestrator as orch

_PREF_NS = "/preferences/{actorId}/"
_FACT_NS = "/facts/{actorId}/"
_SUMMARY_NS = "/summaries/{actorId}/{sessionId}/"

#: A preference record as the service returned it in the adt-demo run: the
#: right record for the follow-up, at a score in the measured 0.35-0.54 range.
_LOW_SCORE_PREFERENCE = {
    "content": {"text": '{"context":"Setting measurement units for future answers.",'
                        '"preference":"Always show quantities in metric units.",'
                        '"categories":["measurement","units"]}'},
    "score": 0.41,
}


class _RetrievalConfig:
    """Stand-in with the SDK's field names (top_k, relevance_score, strategy_id)."""

    def __init__(self, top_k, relevance_score=0.2, strategy_id=None):
        self.top_k = top_k
        self.relevance_score = relevance_score
        self.strategy_id = strategy_id


@pytest.fixture(autouse=True)
def _actor_scope(monkeypatch):
    monkeypatch.delenv("MEMORY_LTM_SCOPE", raising=False)


def _configs(session_id="chat-1"):
    return orch._ltm_retrieval_config(_RetrievalConfig, session_id)


def test_every_namespace_declares_no_cutoff():
    configs = _configs()
    assert set(configs) == {_FACT_NS, _PREF_NS, _SUMMARY_NS}
    assert {ns: rc.relevance_score for ns, rc in configs.items()} == {
        _FACT_NS: 0.0, _PREF_NS: 0.0, _SUMMARY_NS: 0.0}


def test_session_scope_summaries_declare_no_cutoff(monkeypatch):
    monkeypatch.setenv("MEMORY_LTM_SCOPE", "session")
    configs = _configs()
    assert {ns: rc.relevance_score for ns, rc in configs.items()} == {_SUMMARY_NS: 0.0}


def test_top_k_is_unchanged():
    assert {ns: rc.top_k for ns, rc in _configs().items()} == {
        _FACT_NS: 10, _PREF_NS: 5, _SUMMARY_NS: 3}


def _filtering_sdk_keeps(records, rc):
    """The later SDK's per-namespace rule, verbatim in effect."""
    if rc.relevance_score:
        return [m for m in records if m.get("score", 0.0) >= rc.relevance_score]
    return records


def test_low_scoring_preference_survives_an_sdk_that_applies_the_cutoff():
    rc = _configs()[_PREF_NS]
    assert _filtering_sdk_keeps([_LOW_SCORE_PREFERENCE], rc) == [_LOW_SCORE_PREFERENCE]


def _call_pinned_sdk(retrieval_config, returned):
    """Run the installed SDK's retrieve_customer_context with a stub memory
    client (no AWS call) and return what it injected into the conversation."""
    sm_mod = pytest.importorskip("bedrock_agentcore.memory.integrations.strands.session_manager")
    calls = []

    class _MemoryClient:
        def retrieve_memories(self, **kwargs):
            calls.append(kwargs)
            return list(returned)

    fake_self = types.SimpleNamespace(
        config=types.SimpleNamespace(
            retrieval_config=retrieval_config, actor_id="user-1", session_id="chat-1",
            memory_id="mem-1", context_tag="user_context"),
        memory_client=_MemoryClient())
    agent = types.SimpleNamespace(messages=[
        {"role": "user", "content": [{"text": "What units should quantities be shown in?"}]}])
    sm_mod.AgentCoreMemorySessionManager.retrieve_customer_context(
        fake_self, types.SimpleNamespace(agent=agent))
    injected = " ".join(block.get("text", "") for message in agent.messages
                        for block in message.get("content", []))
    return injected, calls


def test_pinned_sdk_injects_the_low_scoring_preference():
    configs = {_PREF_NS: _configs()[_PREF_NS]}
    injected, calls = _call_pinned_sdk(configs, [_LOW_SCORE_PREFERENCE])
    assert [c.get("top_k") for c in calls] == [5]
    assert "Always show quantities in metric units." in injected


def test_the_sdk_import_in_the_test_is_the_real_one():
    """The check above runs the installed SDK, not a stand-in another test
    module left in sys.modules."""
    sm_mod = pytest.importorskip("bedrock_agentcore.memory.integrations.strands.session_manager")
    assert getattr(sm_mod, "__file__", None), sys.modules.get(sm_mod.__name__)
