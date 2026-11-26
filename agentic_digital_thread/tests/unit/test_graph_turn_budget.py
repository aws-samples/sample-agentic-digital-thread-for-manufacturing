# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The orchestrator's graph tool has a per-turn budget.

Measured on an analytics deployment: in one turn the orchestrator called
explore_graph 17 times, mostly repeating searches that had come back empty
(the empty result already says not to broaden or retry), never asked a
connected system, and then answered that no question had come through.

Two deterministic limits now apply to the orchestrator's own explore_graph
(Mode A and the Mode B variant), counted per agent invocation and reset with
the next one:

* after :data:`GRAPH_CALLS_PER_TURN` graph queries in a turn, a further call
  is not run and returns a structured ``graph_budget_spent`` result telling the
  model to delegate to a connected system or answer from what it has;
* a search whose shape (the query with case and spacing folded and its
  RETURN projection left out; every MATCH/WHERE value, numbers included, is
  kept) has come back empty twice is not run a third time.

The tool's name, description and input schema are unchanged, and the gateway
copy of the tool (shared docstring) has no budget.

Offline: graph reads are fakes; fixtures are synthetic and generic.
"""
from __future__ import annotations

import asyncio
import importlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from strands import tool

import agents.db as db
import agents.graph_tools as graph_tools
import agents.orchestrator as orch


def _budget():
    try:
        return importlib.import_module("agents.graph_turn_budget")
    except ImportError:
        return None


LIMIT = getattr(_budget(), "GRAPH_CALLS_PER_TURN", 12)


class _Graph:
    """Fake graph: rows for any query unless its text holds an ``empty`` marker."""

    def __init__(self):
        self.ran: list = []

    def checked(self, cypher, parameters=None):
        self.ran.append(cypher)
        return [] if "nothing_here" in cypher.lower() else [{"n": len(self.ran)}]

    def plain(self, cypher, parameters=None):
        return [{"label": "Asset", "count": 3}]


@pytest.fixture
def graph(monkeypatch):
    fake = _Graph()
    monkeypatch.setattr(graph_tools, "neptune_query_checked", fake.checked)
    monkeypatch.setattr(graph_tools, "neptune_query", fake.plain)
    monkeypatch.setattr(db, "neptune_query", lambda *_a, **_k: [])
    return fake


def _call(graph_tool, cypher: str, state: dict) -> dict:
    """Run ``graph_tool`` the way the agent loop does, inside one invocation."""
    state.setdefault("agent", object())
    use = {"toolUseId": f"t-{len(state)}", "name": "explore_graph", "input": {"cypher": cypher}}

    async def _run():
        last = None
        async for event in graph_tool.stream(use, state):
            last = event
        return last

    result = asyncio.run(_run())
    payload = result.get("tool_result") if isinstance(result, dict) else result.tool_result
    return json.loads(payload["content"][0]["text"])


def _distinct(n: int) -> str:
    return f"MATCH (a:Asset) WHERE a.name = 'asset_{chr(97 + n % 26)}{n // 26}' RETURN a.name"


def test_a_turn_runs_at_most_the_limit_of_graph_queries(graph):
    state: dict = {}
    results = [_call(orch.explore_graph, _distinct(n), state) for n in range(LIMIT + 2)]

    assert len(graph.ran) == LIMIT
    assert all("results" in r and not r.get("graph_budget_spent") for r in results[:LIMIT])
    for spent in results[LIMIT:]:
        assert spent["graph_budget_spent"] is True and spent["not_run"] is True
        assert spent["reason"] == "turn_limit"
        assert spent["graph_calls_this_turn"] == LIMIT and spent["limit"] == LIMIT
        low = spent["note"].lower()
        for part in ("not run", "delegate", "connected system", "answer", "question"):
            assert part in low, part


def test_the_count_resets_with_the_next_turn(graph):
    first: dict = {}
    for n in range(LIMIT + 1):
        _call(orch.explore_graph, _distinct(n), first)
    assert _call(orch.explore_graph, _distinct(0), first)["graph_budget_spent"] is True

    second: dict = {}
    assert "graph_budget_spent" not in _call(orch.explore_graph, _distinct(0), second)
    assert len(graph.ran) == LIMIT + 1


def test_a_search_that_came_back_empty_twice_is_not_run_a_third_time(graph):
    state: dict = {}
    first = "MATCH (a:Asset) WHERE toLower(a.name) CONTAINS 'nothing_here' RETURN a.name LIMIT 20"
    # The same search with another projection, case, spacing and limit.
    second = ("match (a:Asset)\nWHERE toLower(a.name)  CONTAINS 'NOTHING_HERE'\n"
              "RETURN a.name, a.role, a.path LIMIT 50")
    third = "MATCH (a:Asset) WHERE toLower(a.name) CONTAINS 'nothing_here' RETURN a"

    assert _call(orch.explore_graph, first, state)["empty"] is True
    assert _call(orch.explore_graph, second, state)["empty"] is True
    refused = _call(orch.explore_graph, third, state)

    assert len(graph.ran) == 2
    assert refused["graph_budget_spent"] is True and refused["not_run"] is True
    assert refused["reason"] == "repeated_empty"
    assert "empty twice" in refused["note"].lower()
    # Another search still runs, and so does another value in the same pattern.
    other = "MATCH (c:Concept) WHERE toLower(c.id) CONTAINS 'nothing_here' RETURN c.id"
    assert _call(orch.explore_graph, other, state)["empty"] is True
    assert "results" in _call(orch.explore_graph, _distinct(1), state)
    # A repeat of a search that found rows is never refused for repeating.
    for _ in range(3):
        assert "graph_budget_spent" not in _call(orch.explore_graph, _distinct(1), state)
    assert len(graph.ran) == 7


def test_concurrent_calls_in_one_turn_never_run_past_the_limit(graph):
    budget = _budget()
    assert budget is not None, "agents.graph_turn_budget is missing"
    state: dict = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda n: json.loads(budget.run_budgeted(graph_tools.explore_graph, _distinct(n), state)),
            range(LIMIT + 8)))

    assert len(graph.ran) == LIMIT
    assert sum(1 for r in results if r.get("graph_budget_spent")) == 8


def test_both_orchestrator_graph_tools_keep_their_spec_and_share_the_budget(graph):
    assert orch.explore_graph.tool_spec == tool(graph_tools.explore_graph).tool_spec
    assert orch.explore_graph_with_schema.tool_spec == tool(
        orch._explore_graph_with_schema, name="explore_graph",
        description=graph_tools.explore_graph.__doc__).tool_spec
    # A direct call outside an agent turn is not counted.
    for n in range(LIMIT + 1):
        assert "graph_budget_spent" not in json.loads(orch.explore_graph(_distinct(n)))

    state: dict = {}
    results = [_call(orch.explore_graph_with_schema, _distinct(n), state)
               for n in range(LIMIT + 1)]
    assert results[-1]["graph_budget_spent"] is True


def test_the_limit_sits_above_every_measured_normal_turn():
    """The highest count of graph calls the orchestrator made in a turn that
    passed, across the recorded golden and smoke runs, is 10; the default
    routing flow (seed, at most one batched translation lookup, at most two
    empty attempts, a concept read) needs far fewer."""
    budget = _budget()
    assert budget is not None, "agents.graph_turn_budget is missing"
    assert budget.GRAPH_CALLS_PER_TURN == 12
    assert budget.EMPTY_REPEATS_PER_SHAPE == 2


def test_an_agent_turn_is_stopped_at_the_limit_and_the_next_turn_starts_fresh(graph):
    """Through the real Strands agent loop: the model keeps calling the graph,
    the calls past the limit come back as graph_budget_spent, and the next
    invocation of the same agent counts from zero again."""
    from strands import Agent
    from strands.models.model import Model

    def _graph_call(n):
        return [{"messageStart": {"role": "assistant"}},
                {"contentBlockStart": {"start": {"toolUse": {
                    "toolUseId": f"g{n}", "name": "explore_graph"}}}},
                {"contentBlockDelta": {"delta": {"toolUse": {
                    "input": json.dumps({"cypher": _distinct(n)})}}}},
                {"contentBlockStop": {}},
                {"messageStop": {"stopReason": "tool_use"}}]

    def _final(text):
        return [{"messageStart": {"role": "assistant"}},
                {"contentBlockDelta": {"delta": {"text": text}}},
                {"contentBlockStop": {}},
                {"messageStop": {"stopReason": "end_turn"}}]

    class _ScriptedModel(Model):
        def __init__(self, scripts):
            self.scripts = list(scripts)

        def update_config(self, **_config):
            return None

        def get_config(self):
            return {}

        def structured_output(self, *_a, **_k):
            raise NotImplementedError

        async def stream(self, messages, tool_specs=None, system_prompt=None, **_kw):
            for event in self.scripts.pop(0):
                yield event

    first = [_graph_call(n) for n in range(LIMIT + 2)] + [_final("one")]
    second = [_graph_call(100), _final("two")]
    agent = Agent(model=_ScriptedModel(first + second), tools=[orch.explore_graph],
                  callback_handler=None)

    agent("first question")
    texts = [block["toolResult"]["content"][0]["text"] for message in agent.messages
             for block in message.get("content", []) if "toolResult" in block]
    assert len(graph.ran) == LIMIT
    assert [json.loads(t).get("graph_budget_spent") for t in texts[LIMIT:]] == [True, True]

    agent("second question")
    assert len(graph.ran) == LIMIT + 1


# --------------------------------------------------------------------------- #
# A different number in MATCH or WHERE is a different search
# --------------------------------------------------------------------------- #
def _level(value) -> str:
    return (f"MATCH (a:Asset) WHERE a.tag = 'nothing_here' AND a.level = {value} "
            "RETURN a.name LIMIT 20")


def test_searches_that_differ_only_in_a_number_are_different_searches(graph):
    state: dict = {}
    for value in (1, 2, 99):
        assert _call(orch.explore_graph, _level(value), state)["empty"] is True
    assert len(graph.ran) == 3

    budget = _budget()
    assert budget.query_shape(_level(1)) != budget.query_shape(_level(2))
    # The projection's pagination still does not make a new search.
    assert budget.query_shape(_level(1)) == budget.query_shape(
        _level(1).replace("LIMIT 20", "ORDER BY a.name SKIP 5 LIMIT 50"))


# --------------------------------------------------------------------------- #
# One budget per runtime turn, through the output-cap continuation
# --------------------------------------------------------------------------- #
def _graph_call_script(n):
    return [{"messageStart": {"role": "assistant"}},
            {"contentBlockStart": {"start": {"toolUse": {
                "toolUseId": f"g{n}", "name": "explore_graph"}}}},
            {"contentBlockDelta": {"delta": {"toolUse": {
                "input": json.dumps({"cypher": _distinct(n)})}}}},
            {"contentBlockStop": {}},
            {"messageStop": {"stopReason": "tool_use"}}]


def _text_script(text, stop="end_turn"):
    return [{"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"text": text}}},
            {"contentBlockStop": {}},
            {"messageStop": {"stopReason": stop}}]


def _scripted_model(scripts):
    from strands.models.model import Model

    class _ScriptedModel(Model):
        def update_config(self, **_config):
            return None

        def get_config(self):
            return {}

        def structured_output(self, *_a, **_k):
            raise NotImplementedError

        async def stream(self, messages, tool_specs=None, system_prompt=None, **_kw):
            for event in scripts.pop(0):
                yield event

    return _ScriptedModel()


def _reset_turn():
    """The runtime turn's reset (a no-op where it does not exist)."""
    getattr(_budget(), "reset_turn_budget", lambda: None)()


def test_the_output_cap_continuation_shares_the_turn_s_budget(graph):
    """A reply cut at the output cap is continued by a second agent invocation
    in the same turn; the continuation must not start a fresh count."""
    from strands import Agent
    from agents.output_cap_recovery import stream_with_output_cap_recovery

    first = [_graph_call_script(n) for n in range(8)] + [_text_script("partial", "max_tokens")]
    second = [_graph_call_script(n) for n in range(8, 16)] + [_text_script("done")]
    agent = Agent(model=_scripted_model(first + second), tools=[orch.explore_graph],
                  callback_handler=None)

    async def turn():
        _reset_turn()
        return [e async for e in stream_with_output_cap_recovery(agent, "question")]

    asyncio.run(turn())

    assert len(graph.ran) == LIMIT
    texts = [block["toolResult"]["content"][0]["text"] for message in agent.messages
             for block in message.get("content", []) if "toolResult" in block]
    assert sum(1 for t in texts if json.loads(t).get("graph_budget_spent")) == 16 - LIMIT


def test_the_runtime_entrypoint_starts_every_turn_with_a_fresh_budget(graph, monkeypatch):
    from agents import runtime_app
    ran_per_turn: list = []

    async def legacy(payload, context):
        before = len(graph.ran)
        for n in range(LIMIT + 2):
            _budget_or_plain(_distinct(n))
        ran_per_turn.append(len(graph.ran) - before)
        yield {"data": "x"}

    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    selected = runtime_app._runtime_entrypoint(legacy, build_orchestrator=object())

    async def run():
        return [event async for event in selected({"prompt": "q"}, None)]

    asyncio.run(run())
    asyncio.run(run())
    assert ran_per_turn == [LIMIT, LIMIT]


def _budget_or_plain(cypher):
    """A graph call made inside a runtime turn, with no agent invocation state."""
    budget = _budget()
    if budget is None:
        return graph_tools.explore_graph(cypher)
    return budget.run_budgeted(graph_tools.explore_graph, cypher, None)


def test_a_call_outside_a_runtime_turn_still_counts_per_invocation(graph):
    # No runtime turn is open in this thread, so the invocation state counts.
    assert getattr(_budget(), "current_turn_budget", lambda: None)() is None
    state: dict = {}
    for n in range(LIMIT + 1):
        _call(orch.explore_graph, _distinct(n), state)
    assert len(graph.ran) == LIMIT
