# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A chat turn's long-term-memory search uses the user's question, not the composed turn.

The strands ``AgentCoreMemorySessionManager.retrieve_customer_context`` searches
every retrieval namespace with the text of the turn's first content block. On
an interactive turn that block is the COMPOSED turn,
``{route seed}\\n\\n---\\n\\n[Temporal context: ...] {question}``
(``runtime_app._graph_first_prelude``), and AgentCore Memory rejects a
``searchCriteria.searchQuery`` over 10,000 characters. Live on adt-demo
(2026-10-26) every interactive turn logged
"Value at 'searchCriteria.searchQuery' failed to satisfy constraint: Member
must have length less than or equal to 10000" once per namespace and injected
no memory at all, for at least the 14 days the logs covered: a preference stated
in one chat never reached the next.

The orchestrator now wraps the session manager's memory client so the search
query is the question ``memory_labels.strip_turn_preambles`` recovers (the one
home for that rule), cut to the service limit. What is injected and where it is
injected are unchanged; only the search text changes. (The turn carries only the
memory block: the rule for reading it is standing prompt text, pinned in
tests/unit/test_memory_authority_standing_rule.py.)
"""

import functools
import inspect
import types

import pytest

import agents.orchestrator as orch

QUESTION = "How have I asked you to report scrap rates?"
PREFERENCE = {"content": {"text": "Scrap rates are reported as a percentage of good parts."}, "score": 0.45}


def _composed_turn(seed_chars: int = 12_000, question: str = QUESTION) -> str:
    seed = "[ROUTE SEED — connected-system routing was resolved before delegation]\n" + "x" * seed_chars
    return (f"{seed}\n\n---\n\n[Temporal context: Today is Monday 2026-10-26 (UTC).] "
            f"{question}")


def _wrapped_manager(returned=(PREFERENCE,)):
    """A session manager with the installed SDK's real retrieve_customer_context
    bound to it and a stub memory client, wrapped exactly as the orchestrator does."""
    sm_mod = pytest.importorskip("bedrock_agentcore.memory.integrations.strands.session_manager")
    rc_mod = pytest.importorskip("bedrock_agentcore.memory.integrations.strands.config")
    calls = []

    class _MemoryClient:
        def retrieve_memories(self, **kwargs):
            calls.append(kwargs)
            return list(returned)

    sm = types.SimpleNamespace(
        config=types.SimpleNamespace(
            retrieval_config={"/preferences/{actorId}/": rc_mod.RetrievalConfig(top_k=5, relevance_score=0.0)},
            actor_id="user-1", session_id="chat-1", memory_id="mem-1", context_tag="user_context"),
        memory_client=_MemoryClient(),
        create_message=lambda *a, **k: None,
    )
    sm.retrieve_customer_context = functools.partial(
        sm_mod.AgentCoreMemorySessionManager.retrieve_customer_context, sm)
    orch._wrap_session_manager(sm)
    return sm, calls


def _turn(sm, text):
    agent = types.SimpleNamespace(messages=[{"role": "user", "content": [{"text": text}]}])
    sm.retrieve_customer_context(types.SimpleNamespace(agent=agent))
    return agent.messages


def test_composed_turn_searches_with_the_question():
    sm, calls = _wrapped_manager()
    composed = _composed_turn()
    assert len(composed) > 10_000
    messages = _turn(sm, composed)
    assert [c["query"] for c in calls] == [QUESTION]
    # the injected context still reaches the turn, and the turn itself is untouched
    texts = [b["text"] for b in messages[-1]["content"]]
    assert any("percentage of good parts" in t for t in texts)
    assert composed in texts
    assert messages[-1]["role"] == "user"


def test_a_question_longer_than_the_limit_is_cut_to_it():
    sm, calls = _wrapped_manager()
    long_question = "Why " + "y" * 15_000
    _turn(sm, _composed_turn(question=long_question))
    assert [len(c["query"]) for c in calls] == [10_000]
    assert calls[0]["query"] == long_question[:10_000]


def test_a_plain_question_is_searched_as_typed():
    sm, calls = _wrapped_manager()
    _turn(sm, QUESTION)
    assert [c["query"] for c in calls] == [QUESTION]


@pytest.mark.parametrize("header_attr", ["_GRAPH_FIRST_SEED_HEADER", "_ROUTE_SEED_HEADER"])
def test_both_modes_seeded_turns_search_with_the_question(header_attr):
    """The real seed headers (Mode B graph-first, Mode A route seed) through the
    real composer (runtime_app._compose_seeded_turn), with a schema-sized
    payload: only the user's question reaches the search."""
    from agents import runtime_app
    header = getattr(orch, header_attr, None)
    if header is None:
        pytest.skip(f"orchestrator has no {header_attr}")
    seed = header + '{"systems": "' + "s" * 12_000 + '"}'
    composed = runtime_app._compose_seeded_turn(seed, QUESTION)
    sm, calls = _wrapped_manager()
    _turn(sm, composed)
    assert [c["query"] for c in calls] == [QUESTION]


def test_a_seed_only_turn_still_searches_within_the_limit():
    """No separator, so no question to recover: the search falls back to the
    turn's own text, cut to the limit, rather than failing the service check."""
    sm, calls = _wrapped_manager()
    seed_only = "[ROUTE SEED — x]\n" + "z" * 20_000
    _turn(sm, seed_only)
    assert len(calls) == 1
    assert len(calls[0]["query"]) <= 10_000


def test_the_limit_is_the_service_limit():
    assert orch.MEMORY_SEARCH_QUERY_MAX_CHARS == 10_000


def test_every_seed_header_is_a_prefix_the_peel_recognises():
    """Each seed the orchestrator composes ahead of the question opens with a
    prefix memory_labels strips, so a new seed header can't silently put its
    text back into the search (and the session label)."""
    from agents import memory_labels
    prefixes = (memory_labels._ROUTE_SEED_PREFIX, memory_labels._GRAPH_FIRST_PREFIX)
    for header in (orch._ROUTE_SEED_HEADER, orch._GRAPH_FIRST_SEED_HEADER):
        assert header.startswith(prefixes), header[:60]


def test_a_mode_b_turn_is_labelled_with_its_question():
    """The same rule names the session in the picker."""
    from agents import runtime_app
    from agents.memory_labels import strip_turn_preambles
    composed = runtime_app._compose_seeded_turn(orch._GRAPH_FIRST_SEED_HEADER + "{}", QUESTION)
    assert strip_turn_preambles(composed) == QUESTION


def test_a_manager_without_a_memory_client_is_left_alone():
    sm = types.SimpleNamespace(retrieve_customer_context=lambda event: None,
                               create_message=lambda *a, **k: None)
    orch._wrap_session_manager(sm)
    assert not hasattr(sm, "memory_client")


@pytest.mark.parametrize("fn", ["_memory_search_query", "_wrap_memory_search", "_wrap_session_manager"])
def test_helpers_stay_under_fifty_lines(fn):
    assert len(inspect.getsource(getattr(orch, fn)).splitlines()) <= 50
