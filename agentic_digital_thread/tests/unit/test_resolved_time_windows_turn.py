# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Resolved time windows reach the turn: the orchestrator's and the delegation's.

With a calendar policy in the graph, a question carrying a date expression
gets a RESOLVED TIME WINDOWS block computed before the model runs:

* in the orchestrator's user turn, in the preamble before the documented
  separator, so the question is still recovered byte-for-byte for display
  (also when the route seed is empty);
* in every system delegation's ``kg_context``, added by the gateway client
  (whether or not the model passed a ``kg_context``), inside the system
  agent's bound and ahead of concept notes;
* both prompts say to use exactly those windows.

Without a policy, or without a date expression, the turn and the delegation
are byte-identical to before. These tests use only seams that predate the
feature (the graph read, the route-seed builder, the gateway client), so
they are red by assertion on the code before it.

Offline: the graph read and the route seed are fakes; fixtures are generic.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import agents.db as db
import agents.orchestrator as orch
from agents import memory_labels, runtime_app
from agents import system_agent as sa

POLICY_ROW = {
    "bare_date": "utc_day", "relative_days": "site_day", "site_day_start": "06:30",
    "sites_json": json.dumps([{"site": "site-a", "timezone": "America/New_York"}]),
    "updated_by": "steward@example.com", "updated_at": "2026-10-15T00:00:00+00:00",
}
TIME_LINE = "[Temporal context: Current UTC time: 2026-10-15T23:34:45Z (Thursday).]"
QUESTION = "How many units completed on 2026-08-20 at site-a?"
PROMPT = f"{TIME_LINE} {QUESTION}"
WINDOW = "[2026-08-20 00:00:00 UTC, 2026-08-21 00:00:00 UTC)"
SEED = "[ROUTE SEED \u2014 connected-system routing was resolved before delegation]\nROUTE SEED RESULT:\n{}"


def _graph(monkeypatch, row=POLICY_ROW):
    calls = []

    def query(cypher, params=None):
        calls.append(cypher)
        return [dict(row)] if row is not None and "CalendarPolicy" in cypher else []

    monkeypatch.setattr(db, "neptune_query_checked", query)
    monkeypatch.setattr(db, "neptune_query", query)
    return calls


@pytest.fixture(autouse=True)
def _mode_a(monkeypatch):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")


def _prelude(monkeypatch, prompt=PROMPT, seed=SEED):
    monkeypatch.setattr(orch, "build_route_seed", lambda **_kw: seed)
    return runtime_app._graph_first_prelude(prompt)[0]


# --------------------------------------------------------------------------- #
# The orchestrator's turn
# --------------------------------------------------------------------------- #
def test_the_seeded_turn_carries_the_windows_before_the_separator(monkeypatch):
    _graph(monkeypatch)
    composed = _prelude(monkeypatch)
    preamble, separator, rest = composed.partition("\n\n---\n\n")
    assert separator and rest == PROMPT
    assert preamble.startswith(SEED)
    assert "[RESOLVED TIME WINDOWS" in preamble and WINDOW in preamble
    assert memory_labels.strip_turn_preambles(composed) == QUESTION


def test_an_unseeded_turn_still_carries_the_windows(monkeypatch):
    _graph(monkeypatch)
    composed = _prelude(monkeypatch, seed=None)
    assert composed.startswith("[RESOLVED TIME WINDOWS") and WINDOW in composed
    assert composed.endswith("\n\n---\n\n" + PROMPT)
    assert memory_labels.strip_turn_preambles(composed) == QUESTION


def test_without_a_policy_the_turn_is_byte_identical(monkeypatch):
    _graph(monkeypatch, row=None)
    assert _prelude(monkeypatch) == f"{SEED}\n\n---\n\n{PROMPT}"
    assert _prelude(monkeypatch, seed=None) == PROMPT


def test_a_question_without_dates_reads_no_policy(monkeypatch):
    calls = _graph(monkeypatch)
    prompt = f"{TIME_LINE} Which line ran the most units?"
    assert _prelude(monkeypatch, prompt=prompt) == f"{SEED}\n\n---\n\n{prompt}"
    assert not [c for c in calls if "CalendarPolicy" in c]


def test_the_time_line_itself_is_not_read_as_a_question_date(monkeypatch):
    _graph(monkeypatch)
    prompt = f"{TIME_LINE} Which line ran the most units?"
    assert "RESOLVED TIME WINDOWS" not in _prelude(monkeypatch, prompt=prompt)


# --------------------------------------------------------------------------- #
# The delegation
# --------------------------------------------------------------------------- #
class _FakeClient:
    def __init__(self):
        self.calls = []
        self.list_tools_sync = lambda *a, **k: []

        async def _call_tool_async(tool_use_id, name, arguments=None,
                                   read_timeout_seconds=None, meta=None):
            self.calls.append({"name": name, "arguments": arguments})
            return {"status": "success", "content": [{"text": "ok"}]}

        self.call_tool_async = _call_tool_async


def _gateway(monkeypatch):
    class _Cfg:
        has_gateway = True
        agentcore_gateway_url = "https://gw.example/mcp"

    client = _FakeClient()
    monkeypatch.setattr(orch, "get_config", lambda: _Cfg())
    monkeypatch.setattr(orch, "_resolve_gateway_bearer", lambda cfg, tok: "bearer")
    monkeypatch.setattr(orch, "_create_mcp_client", lambda url, bearer_token=None: client)
    return client, orch._get_orchestrator_tools(user_access_token=None, session_id="s-1")[-1]


def _call(gateway, name, args):
    asyncio.run(gateway.call_tool_async("t1", name, args))


def test_the_delegation_kg_context_carries_the_windows(monkeypatch):
    _graph(monkeypatch)
    _prelude(monkeypatch)
    client, gateway = _gateway(monkeypatch)
    route = "ROUTE:\ntarget_system: sys-a"
    _call(gateway, "sys-a___query_sys_a", {"question": "count them", "kg_context": route})
    sent = client.calls[0]["arguments"]
    assert sent["question"] == "count them"
    assert sent["kg_context"].startswith(route + "\n\n[RESOLVED TIME WINDOWS")
    assert WINDOW in sent["kg_context"]
    # The chart-intent bit rides a SEPARATE reserved arg, never kg_context.
    from agents import chart_intent as ci
    assert "[CHART INTENT]" not in sent["kg_context"]
    assert sent.get(ci.RESERVED_CHART_ARG) in ("requested", "none")
    assert len(sent["kg_context"]) <= sa._KG_CONTEXT_MAX_CHARS


def test_a_delegation_without_kg_context_gets_one(monkeypatch):
    _graph(monkeypatch)
    _prelude(monkeypatch)
    client, gateway = _gateway(monkeypatch)
    _call(gateway, "sys-a___query_sys_a", {"question": "count them"})
    assert WINDOW in client.calls[0]["arguments"]["kg_context"]


def test_a_full_kg_context_still_carries_the_no_window_note(monkeypatch):
    """A route context near the system agent's bound leaves no room for the
    windows: the delegation still gets the engine's no-window note, whole,
    and only the model-written context is trimmed to make room. The
    chart-intent bit rides a SEPARATE reserved arg, so kg_context is
    unaffected and still ends with the note."""
    from agents import chart_intent as ci
    from agents import time_windows as tw
    _graph(monkeypatch)
    _prelude(monkeypatch)
    client, gateway = _gateway(monkeypatch)
    route = "ROUTE:\ntarget_system: sys-a\n" + "x" * 1960
    _call(gateway, "sys-a___query_sys_a", {"question": "count them", "kg_context": route})
    sent = client.calls[0]["arguments"]["kg_context"]
    assert sent.endswith(tw.NO_ROOM_NOTE) and tw.TRIM_MARKER in sent
    assert "[CHART INTENT]" not in sent                 # the bit never rides kg_context
    assert client.calls[0]["arguments"].get(ci.RESERVED_CHART_ARG) in ("requested", "none")
    assert sent.startswith("ROUTE:\ntarget_system: sys-a")
    assert len(sent) <= sa._KG_CONTEXT_MAX_CHARS
    assert sa._sanitize_kg_context(sent).endswith(tw.NO_ROOM_NOTE)


def test_non_query_tools_are_untouched(monkeypatch):
    _graph(monkeypatch)
    _prelude(monkeypatch)
    client, gateway = _gateway(monkeypatch)
    args = {"cypher": "MATCH (n) RETURN n"}
    _call(gateway, "adt-digital-thread___explore_graph", dict(args))
    assert client.calls[0]["arguments"] == args


def test_without_a_policy_the_delegation_is_unchanged(monkeypatch):
    """No time-window policy: the windows add nothing, so kg_context is
    exactly the model's. The chart-intent bit rides a separate reserved arg
    (its own feature); kg_context never carries it."""
    from agents import chart_intent as ci
    _graph(monkeypatch, row=None)
    _prelude(monkeypatch)
    client, gateway = _gateway(monkeypatch)
    args = {"question": "count them", "kg_context": "ROUTE"}
    _call(gateway, "sys-a___query_sys_a", dict(args))
    sent = client.calls[0]["arguments"]
    assert sent["question"] == "count them"
    assert sent["kg_context"] == "ROUTE"                     # kg_context unchanged
    assert sent.get(ci.RESERVED_CHART_ARG) == "none"         # no chart asked in this prompt
    assert "RESOLVED TIME WINDOWS" not in sent["kg_context"]


def test_each_invocation_starts_without_windows(monkeypatch):
    """The runtime entrypoint clears the hand-off before the turn's prelude,
    so a turn never delivers another turn's windows. The chart-intent bit
    rides a separate reserved arg (its own feature); the ROUTE context and
    windows do not change, and kg_context never carries the bit."""
    from agents import chart_intent as ci
    _graph(monkeypatch)
    _prelude(monkeypatch)
    seen = []

    async def legacy(payload, context):
        client, gateway = _gateway(monkeypatch)
        await gateway.call_tool_async("t1", "sys-a___query_sys_a",
                                      {"question": "q", "kg_context": "ROUTE"})
        seen.append(client.calls[0]["arguments"])
        yield {"data": "x"}

    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    selected = runtime_app._runtime_entrypoint(legacy, build_orchestrator=object())

    async def run():
        return [event async for event in selected({"prompt": "q"}, None)]

    asyncio.run(run())
    (sent,) = seen
    assert "RESOLVED TIME WINDOWS" not in sent["kg_context"]
    assert sent["kg_context"] == "ROUTE"
    assert "[CHART INTENT]" not in sent["kg_context"]
    assert sent.get(ci.RESERVED_CHART_ARG) == "none"      # "q" asks for no chart


# --------------------------------------------------------------------------- #
# The prompts
# --------------------------------------------------------------------------- #
def test_the_orchestrator_prompt_says_to_use_the_windows():
    for mode in ("agent", "orchestrator"):
        prompt = orch._build_orchestrator_prompt(mode)
        assert "RESOLVED TIME WINDOWS block" in prompt, mode
        assert "do not re-derive them" in prompt, mode


@pytest.mark.parametrize("connection_type", ["Athena", "PostgreSQL", "SiteWise (Direct)"])
def test_the_system_agent_prompt_says_to_use_the_windows(connection_type):
    prompt = sa._compose_system_prompt("sys-a", "System A", "schema_a", "", "",
                                       connection_type)
    assert "RESOLVED TIME WINDOWS block" in prompt
    assert "do not re-derive them" in prompt


def test_the_calendar_policy_route_is_registered_on_the_app():
    from agents import server
    paths = server.app.openapi()["paths"]
    assert set(paths["/api/admin/calendar-policy"]) == {"get", "post"}
    assert set(paths["/api/admin/calendar-policy/clear"]) == {"post"}
