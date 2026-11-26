# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Made-up turns for the route-facts tests, on two differently named schemas.

Each :class:`World` has a run-tracking system (the target), an order system
that returns the run ids under its own field (the peer), and a third system
the seed has nothing for. The seed carries two identity rules from the
target's fields to the SAME peer field, both for the carried concept, plus a
rule for another concept; a confirmed join over each of the two first rules;
and one unconfirmed join. The run ids and field names are ones the integrity
gate's capture admits (no long digit run, no ``_key`` field), so the same
worlds serve the enforce tests. The real per-turn gateway client runs with a
fake MCP client at the network edge, and calls go through Strands' own
``MCPAgentTool.stream``. Offline."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from mcp.types import Tool as MCPTool
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool

import agents.concept_notes as cn
import agents.orchestrator as orch
from agents.chart_intent import RESERVED_CHART_ARG


@dataclass(frozen=True)
class World:
    name: str
    target: str
    peer: str
    other: str
    concept: str
    concept_name: str
    shared_other: str
    vocab: tuple
    target_field: str
    second_target_field: str
    peer_field: str
    join: dict
    second_join: dict
    unconfirmed: dict
    identity_rules: tuple
    #: The peer's answer listing the run ids under its field.
    peer_answer: str
    run_ids: tuple
    #: The peer-style question (matches the carried concept by its words).
    peer_question: str
    #: The target-style question that names its field and lists the run ids.
    target_question: str

    def tool(self, system_id: str) -> str:
        return f"{system_id}___query_{system_id.replace('-', '_')}"

    @property
    def join_line(self) -> str:
        j = self.join
        return (f"join_rule: {j['id']} {j['from_system']}.{j['from_field']} -> "
                f"{j['to_system']}.{j['to_field']}")


def _rule(rid, concept, source, s_field, target, t_field, method="value_exact_match"):
    return {"id": rid, "concept": concept, "source_system": source, "source_field": s_field,
            "target_system": target, "target_field": t_field, "source_pattern": "",
            "target_pattern": "", "transform": "identity", "confidence": 0.85,
            "method": method}


def _join(jid, source, s_field, target, t_field, method="value_exact_match"):
    return {"id": jid, "from_system": source, "from_field": s_field, "to_system": target,
            "to_field": t_field, "transform": "identity", "method": method,
            "confidence": 0.95}


def _world_a() -> World:
    t, p = "line-log", "order-book"
    return World(
        name="line-log", target=t, peer=p, other="sensor-hub",
        concept="batch-lot", concept_name="Batch Lot", shared_other="work-cell",
        vocab=(
            cn.Concept(id="batch-lot", name="Batch Lot", description="One production batch of a lot.",
                       systems=(t, p)),
            cn.Concept(id="consignment", name="Consignment",
                       description="Goods sent to a customer.", systems=(p,)),
            cn.Concept(id="gauge-check", name="Gauge Check",
                       description="A measured check of a part.", systems=(t,)),
            cn.Concept(id="work-cell", name="Work Cell", description="A group of machines.",
                       systems=(t, p)),
        ),
        target_field="run_code", second_target_field="order_code", peer_field="lot_ref",
        join=_join("jp-lr-1", t, "floor.runs.run_code", p, "books.consignment_lines.lot_ref"),
        second_join=_join("jp-lr-3", t, "floor.orders.order_code", p,
                          "books.lot_tracking.lot_ref"),
        unconfirmed=_join("jp-lr-2", t, "floor.runs.cell", p, "books.cells.cell_id",
                          "onboarding-llm"),
        identity_rules=(_rule("ir-lr-1", "batch-lot", t, "run_code", p, "lot_ref"),
                        _rule("ir-lr-2", "batch-lot", t, "order_code", p, "lot_ref"),
                        _rule("ir-lr-3", "work-cell", t, "cell_code", p, "cell_ref")),
        peer_answer=("Two consignments shipped in the window:\n\n"
                     "| Consignment | Ship Date | Lot Ref | Qty |\n|---|---|---|---|\n"
                     "| CN-20261022-0042 | 2026-10-22 | LR-42-0001 | 12 |\n"
                     "| CN-20261023-0051 | 2026-10-23 | LR-43-0003 | 30 |\n"),
        run_ids=("LR-42-0001", "LR-43-0003"),
        peer_question="Which batch lots went out on open consignments in the last 7 days?",
        target_question=("For runs (run_code) LR-42-0001 and LR-43-0003: which line "
                         "made each, and did any of them fail a gauge check?"),
    )


def _world_b() -> World:
    t, p = "press-floor", "ledger"
    return World(
        name="press-floor", target=t, peer=p, other="meter-bank",
        concept="crate-lot", concept_name="Crate Lot", shared_other="bay",
        vocab=(
            cn.Concept(id="crate-lot", name="Crate Lot", description="One crate lot of parts.",
                       systems=(t, p)),
            cn.Concept(id="dispatch", name="Dispatch", description="An outbound delivery.",
                       systems=(p,)),
            cn.Concept(id="torque-test", name="Torque Test",
                       description="A fastening torque test.", systems=(t,)),
            cn.Concept(id="bay", name="Bay", description="A press bay.", systems=(t, p)),
        ),
        target_field="tag_no", second_target_field="ticket_no", peer_field="lot_code",
        join=_join("jp-jb-1", p, "fin.dispatch_rows.lot_code", t, "plant.tags.tag_no"),
        second_join=_join("jp-jb-3", t, "plant.tickets.ticket_no", p,
                          "fin.lot_trace.lot_code"),
        unconfirmed=_join("jp-jb-2", t, "plant.tags.bay_no", p, "fin.bays.bay_ref",
                          "onboarding-llm"),
        identity_rules=(_rule("ir-jb-1", "crate-lot", p, "lot_code", t, "tag_no"),
                        _rule("ir-jb-2", "crate-lot", t, "ticket_no", p, "lot_code"),
                        _rule("ir-jb-3", "bay", t, "bay_no", p, "bay_ref")),
        peer_answer=("## Dispatches\n\n"
                     "1. **Dispatch:** D-0091\n   - **lot_code:** J-77-0001\n"
                     "2. **Dispatch:** D-0094\n   - **lot_code:** J-77-0002\n"),
        run_ids=("J-77-0001", "J-77-0002"),
        peer_question="Which crate lots left on open dispatches this week?",
        target_question=("For tags (tag_no) J-77-0001, J-77-0002: who ran each, and did "
                         "any fail a torque test?"),
    )


WORLDS = (_world_a(), _world_b())


def _inventory_row(system_id):
    return {"system_id": system_id, "system_name": system_id.title(), "plants": ["site-2"],
            "capabilities": f"{system_id} records", "connection_type": "PostgreSQL",
            "query_guidance": "Ask bounded questions."}


def _rule_row(world: World, system_id: str, peer, rules=(), joins=()):
    concepts = (world.concept, world.shared_other)
    shared = [{"concept": c, "peer_system": peer} for c in concepts] if peer else []
    return {"system_id": system_id, "shared_concepts": shared, "join_paths": list(joins),
            "value_mappings": [], "identity_rules": list(rules),
            "mapped_concepts": list(concepts) if peer else []}


def graph_for(world: World):
    def _graph(cypher, params=None):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return [_inventory_row(s) for s in (world.target, world.peer, world.other)]
        if cypher == orch._ROUTE_SEED_CYPHER:
            joins = (world.join, world.second_join, world.unconfirmed)
            return [_rule_row(world, world.target, world.peer, world.identity_rules, joins),
                    _rule_row(world, world.peer, world.target),
                    _rule_row(world, world.other, None)]
        return []
    return _graph


def seed(world: World, question: "str | None" = None):
    text = orch.build_route_seed(query=graph_for(world), concepts=world.vocab,
                                 question=question or world.peer_question)
    assert text and world.join["id"] in text and world.identity_rules[0]["id"] in text
    return text


def result(text, status="success"):
    return {"status": status, "toolUseId": "x", "content": [{"text": text}]}


class Client:
    """The MCP client at the network edge: records what each call carried."""

    def __init__(self, results=()):
        self.results = list(results)
        self.calls = []

        async def _call(tool_use_id, name, arguments=None, read_timeout_seconds=None,
                        meta=None):
            self.calls.append((name, arguments))
            return self.results.pop(0) if self.results else result("Here are the rows.")

        self.call_tool_async = _call
        self.list_tools_sync = lambda *a, **k: []


class _Cfg:
    has_gateway = True
    agentcore_gateway_url = "https://gw.example/mcp"


_SCHEMA = {"type": "object",
           "properties": {"question": {"type": "string"},
                          "kg_context": {"type": "string", "default": ""}},
           "required": ["question"]}


@pytest.fixture
def isolated_turn():
    """Each test starts with no concept notes from an earlier turn."""
    token = cn._TURN_NOTES.set(())
    yield
    cn._TURN_NOTES.reset(token)


def gateway(monkeypatch, fake):
    monkeypatch.setattr(orch, "get_config", lambda: _Cfg())
    monkeypatch.setattr(orch, "_resolve_gateway_bearer", lambda cfg, tok: "bearer")
    monkeypatch.setattr(orch, "_create_mcp_client", lambda url, bearer_token=None: fake)
    return orch._build_gateway_client(_Cfg(), None, "sess-1")


def stream(client, tool_name, tool_input, tool_use_id):
    """One delegation through Strands' own MCP tool adapter."""
    tool = MCPAgentTool(MCPTool(name=tool_name, inputSchema=_SCHEMA), client)

    async def _run():
        return [event async for event in tool.stream(
            {"toolUseId": tool_use_id, "name": tool_name, "input": tool_input}, {})]

    return asyncio.run(_run())


def sent(fake, index):
    """What call ``index`` carried, without the gateway-owned reserved
    arguments: the chart-intent bit, the identifier-sources ask and the
    provenance capability all ride every delegation in the composed stack."""
    from agents.identifier_provenance import RESERVED_ARG as RESERVED_IDENTIFIER_ARG
    from agents.provenance import RESERVED_PROVENANCE_ARG
    reserved = {RESERVED_CHART_ARG, RESERVED_IDENTIFIER_ARG, RESERVED_PROVENANCE_ARG}
    return {k: v for k, v in fake.calls[index][1].items() if k not in reserved}


def block(kg_context):
    """The leading ROUTE block, closing fence included."""
    end = kg_context.find("\n```", len("```ROUTE:"))
    return kg_context[:end + 4] if end >= 0 else kg_context


def label(kg_context, name):
    return [line for line in block(kg_context).splitlines() if line.startswith(f"{name}:")]


def peer_then_target(monkeypatch, world: World, target_input: dict, peer_status="success"):
    """The peer answers its question with its run ids, then the target gets
    ``target_input``. Returns the fake client."""
    seed(world)
    fake = Client([result(world.peer_answer, peer_status)])
    client = gateway(monkeypatch, fake)
    stream(client, world.tool(world.peer), {"question": world.peer_question}, "t-peer")
    stream(client, world.tool(world.target), dict(target_input), "t-target")
    return fake
