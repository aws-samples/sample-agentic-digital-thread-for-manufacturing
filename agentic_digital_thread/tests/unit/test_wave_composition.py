# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The wave's delegation wrappers compose into one gateway client stack.

Five separate changes this wave wrap the per-turn gateway client:

* route facts merge the turn's route-seed facts into each delegation's
  ``kg_context`` and keep what each call carried for the reload;
* the decline guard reads that merged block's canonical concept and refuses a
  repeat of a rule-declined concept;
* identifier provenance asks each system which columns its values came from,
  strips that engine line from the result before anything reads or stores it,
  and refuses a value sent to a system the graph does not join to its source;
* the write-capability provenance context stamps a per-call ``_adt_provenance``
  the system runtime needs to record evidence;
* the response-integrity identifier screen rides a separate reserved argument.

The stack is built through the orchestrator's own ``_build_gateway_client``,
with a fake MCP client at the network edge and calls driven through Strands'
``MCPAgentTool.stream``. The schema here is made up (a shop-floor system keeps
a part code, a ledger keeps a design code, a drawing register keys by drawing
number; the graph joins into the register only from the ledger's design code).
Offline: no AWS, LLM, gateway or Neptune.
"""
from __future__ import annotations

import asyncio

import pytest
from mcp.types import Tool as MCPTool
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool

import agents.concept_notes as cn
import agents.provenance_store as provenance_store
from agents import answer_ids, answer_names
from agents import delegation_decline as dd
from agents import identifier_provenance as ip
from agents import identity_affinity as ia
from agents import investigation_trace_store as its
from agents import orchestrator as orch
from agents import provenance as prov
from agents import route_facts as rf
from agents.chart_intent import RESERVED_CHART_ARG
from agents.job_store import JobStore
from agents.runtime_asgi import provenance_context_var, provenance_write_token_var
from tests.unit._shared_table import SharedTable

PART = "AB-CD-1234"
DESIGN = "DS-1234-V3"
QUESTION = "Which design version was job JOB-77001 built to, and is it approved?"
SYSTEMS = ("shop-floor", "ledger", "drawings")
TOOL = {s: f"{s}___query_{s.replace('-', '_')}" for s in SYSTEMS}

#: Vocabulary the route seed matches delegated questions with.
VOCAB = (
    cn.Concept(id="design-code", name="Design Code", description="A design code of an item.",
               systems=("ledger", "drawings")),
    cn.Concept(id="part", name="Part", description="A manufactured part.",
               systems=("shop-floor",)),
)

_SCHEMA = {"type": "object",
           "properties": {"question": {"type": "string"},
                          "kg_context": {"type": "string", "default": ""}},
           "required": ["question"]}


def _reasoning(concept: str) -> str:
    """The exact-agreement pass's own reasoning for a join of ``concept``."""
    side = ia.SampledField("a", "t", "c", concept, ("X-1",) * 20)
    return ia._exact_reasoning(side, side, "AA-9", set(range(5)), concept)


def _join(jid, a, a_field, b, b_field, provenance=""):
    return {"id": jid, "from_system": a, "from_field": a_field, "to_system": b,
            "to_field": b_field, "method": "value_exact_match", "transform": "identity",
            "confidence": 0.95, "provenance": provenance}


#: The only recorded join into the register: from the ledger's design code.
JOIN_INTO_DRAWINGS = _join("jp-1", "ledger", "ledger_db.items.design_code",
                           "drawings", "drawings_db.records.drawing_no",
                           provenance=_reasoning("part"))
JOIN_SHOP_LEDGER = _join("jp-2", "shop-floor", "shop_db.jobs.job_no",
                         "ledger", "ledger_db.items.job_ref")


def _shared(system: str):
    if system == "ledger":
        return [{"concept": "design-code", "peer_system": "drawings"}]
    if system == "drawings":
        return [{"concept": "design-code", "peer_system": "ledger"}]
    return []


def _rule_rows(identity_rules=(), extra_joins=()):
    joins = (JOIN_INTO_DRAWINGS, JOIN_SHOP_LEDGER, *extra_joins)
    return [{"system_id": s, "shared_concepts": _shared(s),
             "join_paths": [j for j in joins if s in (j["from_system"], j["to_system"])],
             "identity_rules": [r for r in identity_rules if r["target_system"] == s],
             "value_mappings": []} for s in SYSTEMS]


def _seed_query(rule_rows):
    inventory = [{"system_id": r["system_id"], "system_name": r["system_id"]}
                 for r in rule_rows]

    def query(cypher, params=None):
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            return inventory
        if cypher == orch._ROUTE_SEED_CYPHER:
            return rule_rows
        return []
    return query


def _seed(rule_rows=None):
    orch.build_route_seed(query=_seed_query(rule_rows or _rule_rows()), concepts=VOCAB,
                          question=QUESTION)


def _ident_line(answer: str, field: str, values, concepts) -> str:
    """The identifier-sources engine line a system appends when the call asked
    for it: positions of ``values`` in ``answer``, never a value."""
    return "\n\n" + ip.line_for(answer, [(field, list(values), list(concepts))])


class _Edge:
    """The MCP client at the network edge. Records each call's name and the
    arguments that reached it (after every wrapper), and answers like the real
    system agents: a chosen extra engine line (a decline marker) appended to a
    named tool's answer, then the identifier-sources line only when the call
    asked for it. A named tool can answer with another status (an error of
    its own)."""

    def __init__(self, answers, *, ident=None, extra=None, status=None):
        self.answers = answers
        self.ident = ident or {}
        self.extra = extra or {}
        self.status = status or {}
        self.calls = []

        async def _call(tool_use_id, name, arguments=None, read_timeout_seconds=None,
                        meta=None):
            self.calls.append((name, dict(arguments or {})))
            text = self.answers.get(name, "No rows.")
            if name in self.extra:
                text = text + self.extra[name]
            if (arguments or {}).get(ip.RESERVED_ARG) == ip.REQUESTED and name in self.ident:
                field, values, concepts = self.ident[name]
                text = text + _ident_line(text, field, values, concepts)
            return {"status": self.status.get(name, "success"), "toolUseId": tool_use_id,
                    "content": [{"text": text}]}

        self.call_tool_async = _call
        self.list_tools_sync = lambda *a, **k: []


class _Cfg:
    has_gateway = True
    agentcore_gateway_url = "https://gw.example/mcp"


def _gateway(monkeypatch, edge, *, identified=False):
    """The real per-turn gateway client over ``edge``. ``identified`` adds the
    write-capability provenance wrapper (a forwarded end-user token), with its
    identity-claim secret absent so that wrapper is a fail-soft no-op."""
    monkeypatch.setattr(orch, "get_config", lambda: _Cfg())
    monkeypatch.setattr(orch, "_resolve_gateway_bearer", lambda cfg, tok: "bearer")
    monkeypatch.setattr(orch, "_create_mcp_client", lambda url, bearer_token=None: edge)
    monkeypatch.setattr(orch, "_fetch_identity_claim_secret", lambda: None)
    token = "user-token" if identified else None
    return orch._build_gateway_client(_Cfg(), token, "sess-1")


def _stream(client, tool_name, question, tool_use_id) -> str:
    """One delegation through Strands' own MCP tool adapter; the text the
    reader is handed back."""
    tool = MCPAgentTool(MCPTool(name=tool_name, inputSchema=_SCHEMA), client)

    async def _run():
        return [e async for e in tool.stream(
            {"toolUseId": tool_use_id, "name": tool_name,
             "input": {"question": question}}, {})]

    return _tool_text(asyncio.run(_run())[-1])


def _tool_text(event) -> str:
    """The text of a Strands tool-result event (``{'type': 'tool_result',
    'tool_result': {'content': [{'text': ...}]}}``)."""
    result = event.get("tool_result", event) if isinstance(event, dict) else {}
    blocks = result.get("content") or []
    return "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict))


def _sent(edge, tool_name):
    return [args for name, args in edge.calls if name == tool_name]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    rf.reset_turn()
    ip.reset_turn_facts()
    # The route seed also remembers the turn's concept notes; each test's are
    # its own, so a later test in the same worker never builds a client with them.
    notes = cn._TURN_NOTES.set(())
    # A result carrying an IDENTIFIER-MISS block makes the gateway client's
    # miss observer count a failure signal on a background thread; no store
    # is reached from these tests.
    monkeypatch.setattr("agents.failure_signals.record_failure_signal",
                        lambda *_a, **_k: False)
    yield
    cn._TURN_NOTES.reset(notes)
    rf.reset_turn()
    ip.reset_turn_facts()


@pytest.fixture
def _capability_stores(monkeypatch):
    """A shared-table-backed capability and data store, so the real
    ``issue_provenance_arg`` records a durable capability and a tokenless write
    is refused (a memory-only store refuses to record, which is the same
    posture but leaves nothing to assert against)."""
    cap_table, data_table = SharedTable(), SharedTable()
    monkeypatch.setattr(provenance_store, "_cap_store",
                        JobStore(provenance_store._CAP_KIND, table=cap_table))
    monkeypatch.setattr(provenance_store, "_job_store",
                        JobStore(provenance_store._KIND, table=data_table))
    monkeypatch.setattr(its, "_job_store", JobStore(its._KIND, table=data_table))
    ctx = provenance_context_var.set(None)
    wtok = provenance_write_token_var.set(None)
    yield
    provenance_context_var.reset(ctx)
    provenance_write_token_var.reset(wtok)


# --------------------------------------------------------------------------- #
# (a) what a system delegation receives through the composed stack
# --------------------------------------------------------------------------- #
def test_a_delegation_receives_merged_route_capability_and_sources_request(
        monkeypatch, _capability_stores):
    """A single delegation on an identified turn carries: the merged ROUTE
    ``kg_context`` naming the target and its canonical concept, a
    capability-bearing ``_adt_provenance``, the ``_adt_identifier_sources:
    requested`` ask, and nothing else reserved beyond the chart-intent bit."""
    _seed()
    edge = _Edge({TOOL["ledger"]: f"Item for job JOB-77001: design code {DESIGN}."})
    client = _gateway(monkeypatch, edge, identified=True)

    _stream(client, TOOL["ledger"], "For job JOB-77001, give the item's design code.", "t1")

    (args,) = _sent(edge, TOOL["ledger"])
    kg = args.get("kg_context", "")
    assert "```ROUTE:" in kg and "target_system: ledger" in kg
    assert "canonical_concept: design-code" in kg
    assert args.get(ip.RESERVED_ARG) == ip.REQUESTED
    assert prov.parse_write_token(args.get(prov.RESERVED_PROVENANCE_ARG)) is not None
    # The model's two args, plus exactly the gateway-owned reserved ones (the
    # provenance capability, the identifier-sources ask, the chart-intent bit).
    assert set(args) == {"question", "kg_context", RESERVED_CHART_ARG,
                         prov.RESERVED_PROVENANCE_ARG, ip.RESERVED_ARG}


# --------------------------------------------------------------------------- #
# (b) the engine line never reaches the reader, the stored copy or the replay
# --------------------------------------------------------------------------- #
def test_b_the_identifier_sources_line_is_stripped_before_any_reader_sees_it(monkeypatch):
    """The ledger answer carries the identifier-sources engine line (the call
    asked for it); the composed stack strips it on the way up, so the reader's
    text and the replayed turn carry no engine line. The replay still carries
    the merged ``route_context``."""
    _seed()
    answer = f"Item for job JOB-77001: design code {DESIGN}."
    edge = _Edge({TOOL["ledger"]: answer},
                 ident={TOOL["ledger"]: ("items.design_code", [DESIGN], ["design-code"])})
    client = _gateway(monkeypatch, edge)

    seen = _stream(client, TOOL["ledger"], "For job JOB-77001, give the design code.", "t-led")

    assert ip.LINE_PREFIX not in seen and DESIGN in seen
    replay = rf.with_received_contexts(
        {"message": {"role": "user", "content": [{"toolResult": {
            "toolUseId": "t-led", "status": "success",
            "content": [{"text": seen}]}}]}}, rf.turn_facts())
    block = replay["message"]["content"][0]["toolResult"]
    assert block[rf.RECEIVED_KEY].startswith("```ROUTE:")  # route_context replayed
    assert ip.LINE_PREFIX not in str(replay)


def test_b_the_route_facts_record_of_the_result_has_no_engine_line(monkeypatch):
    """What route facts kept of the ledger result for the reload is the
    stripped answer: identifier provenance ran first on the way up, so the
    record never held the engine line."""
    _seed()
    answer = f"Item for job JOB-77001: design code {DESIGN}."
    edge = _Edge({TOOL["ledger"]: answer},
                 ident={TOOL["ledger"]: ("items.design_code", [DESIGN], ["design-code"])})
    client = _gateway(monkeypatch, edge)

    _stream(client, TOOL["ledger"], "For job JOB-77001, give the design code.", "t-led")

    kept = list(rf.turn_facts().results)
    assert kept, "the successful delegation was recorded for the reload"
    assert all(ip.LINE_PREFIX not in str(r) for r in kept)


# --------------------------------------------------------------------------- #
# (c) both guards still fire through the composed stack
# --------------------------------------------------------------------------- #
def test_c_an_identifier_provenance_refusal_fires_through_the_composed_stack(monkeypatch):
    """The shop-floor answer hands back the part code; a later call sends it to
    the drawing register, which the graph joins only from the ledger's design
    code. The composed stack refuses the register's call, naming the recorded
    join, and the call never reaches the edge."""
    _seed()
    edge = _Edge(
        {TOOL["shop-floor"]: f"Job JOB-77001 built part {PART} on line 4."},
        ident={TOOL["shop-floor"]: ("jobs.part_code", [PART], ["part"])})
    client = _gateway(monkeypatch, edge)

    _stream(client, TOOL["shop-floor"], "For job JOB-77001, which part was built?", "t-shop")
    refused = _stream(client, TOOL["drawings"],
                      f'List the design versions of part "{PART}".', "t-draw")

    assert _sent(edge, TOOL["drawings"]) == []
    assert ("ledger ledger_db.items.design_code -> "
            "drawings drawings_db.records.drawing_no") in refused
    assert DESIGN not in refused


def test_c_a_decline_guard_repeat_is_refused_through_the_composed_stack(monkeypatch):
    """The ledger ends an answer with a rule-decline engine line about a
    concept; a repeat delegation to the ledger that names that concept is
    refused by the composed stack and never reaches the edge."""
    _seed()
    decline = "\n\n" + dd.engine_line({"reason_kind": "not_connected", "concept": "design_code"})
    edge = _Edge({TOOL["ledger"]: "I cannot list the design codes: the source is not connected."},
                 extra={TOOL["ledger"]: decline})
    client = _gateway(monkeypatch, edge)

    _stream(client, TOOL["ledger"], "List the design codes for the open jobs.", "t-led1")
    before = len(_sent(edge, TOOL["ledger"]))
    refused = _stream(client, TOOL["ledger"], "Give the design code again please.", "t-led2")

    assert dd.REFUSAL_PREFIX in refused
    assert len(_sent(edge, TOOL["ledger"])) == before  # the repeat never reached the edge


# --------------------------------------------------------------------------- #
# (c2) a refused call leaves no routing context to show or replay
# --------------------------------------------------------------------------- #
class _Log:
    def info(self, *_a, **_k):
        pass

    def warning(self, *_a, **_k):
        pass


@pytest.fixture
def _route_store(monkeypatch):
    """The store the reopened chat reads route contexts from, in memory."""
    from agents import route_context_store
    monkeypatch.setattr(route_context_store, "_job_store",
                        JobStore(kind="route-context", table=None))


def _shown_and_replayed(tool_name, question, tool_use_id, text, status):
    """One finished delegation as the live trace and the reopened chat read
    it: the runtime forwards the kept result message
    (``route_facts.with_received_contexts``), the live chat client reads the
    frames (keeping any forwarded route context for the reload), then
    ``GET /api/memory/events`` rebuilds the stored turn. Returns (the
    forwarded tool result, the reloaded ``tool_result`` block)."""
    from agents.chat_client import ThinkingFilter, _CloudStreamState
    from tests.unit.test_memory_events_rehydrate import _event, _route
    use = {"role": "assistant", "content": [{"toolUse": {
        "toolUseId": tool_use_id, "name": tool_name, "input": {"question": question}}}]}
    kept = {"role": "user", "content": [{"toolResult": {
        "toolUseId": tool_use_id, "status": status, "content": [{"text": text}]}}]}
    forwarded = rf.with_received_contexts({"message": kept}, rf.turn_facts())
    live = _CloudStreamState(ThinkingFilter(), integrity_mode=False, log=_Log(),
                             owner_id="u1", session_id="s1", runtime_session_id="r")
    for frame in ({"message": use}, forwarded):
        live.events_for(frame)
    stored = [_event("e1", 0, "user", [{"text": QUESTION}]),
              _event("e2", 1, "assistant", use["content"]),
              _event("e3", 2, "user", kept["content"]),
              _event("e4", 3, "assistant", [{"text": "Done."}])]
    resp, _ = _route(stored)
    (block,) = [b for m in resp["messages"] for b in m["blocks"]
                if b["type"] == "tool_result" and b.get("tool_id") == tool_use_id]
    return forwarded["message"]["content"][0]["toolResult"], block


def _merge_adds_context(tool_name, question) -> bool:
    """Whether the turn's route facts merge a ROUTE block into this
    delegation, so a recorded context would have something to show."""
    args = {"question": question}
    return rf.merge(rf.turn_facts(), tool_name, args) is not args


def test_c2_an_identifier_refused_call_leaves_no_route_context_to_show_or_replay(
        monkeypatch, _route_store):
    """Reproduction (fails before the change): the identifier-provenance guard
    refuses the drawing register's call, which never reaches the edge, yet
    route facts kept the ROUTE block the merge built for it. The live trace
    then showed a Routing context, marked as added by the knowledge graph,
    for a request the register never got, and the reopened chat replayed it.
    Now nothing of the refused call is recorded: no received context, no
    result, nothing forwarded, nothing for ``/api/memory/events`` to replay."""
    _seed()
    edge = _Edge(
        {TOOL["shop-floor"]: f"Job JOB-77001 built part {PART} on line 4."},
        ident={TOOL["shop-floor"]: ("jobs.part_code", [PART], ["part"])})
    client = _gateway(monkeypatch, edge)
    question = f'List the design versions of part "{PART}".'

    _stream(client, TOOL["shop-floor"], "For job JOB-77001, which part was built?", "t-shop")
    results_before = list(rf.turn_facts().results)
    refused = _stream(client, TOOL["drawings"], question, "t-draw")

    assert _sent(edge, TOOL["drawings"]) == []
    assert _merge_adds_context(TOOL["drawings"], question)
    facts = rf.turn_facts()
    assert "t-draw" not in facts.received
    assert facts.results == results_before
    forwarded, reloaded = _shown_and_replayed(TOOL["drawings"], question, "t-draw", refused,
                                              "error")
    assert rf.RECEIVED_KEY not in forwarded
    assert "route_context" not in reloaded


def test_c2_a_decline_refused_repeat_leaves_no_route_context(monkeypatch, _route_store):
    """Guard (passes before the change too): the decline guard refuses a
    repeat delegation to the ledger, which never reaches the edge; route
    facts keep nothing of it, so nothing is shown or replayed."""
    _seed()
    decline = "\n\n" + dd.engine_line({"reason_kind": "not_connected", "concept": "design_code"})
    edge = _Edge({TOOL["ledger"]: "I cannot list the design codes: the source is not connected."},
                 extra={TOOL["ledger"]: decline})
    client = _gateway(monkeypatch, edge)
    repeat = "Give the design code again please."

    _stream(client, TOOL["ledger"], "List the design codes for the open jobs.", "t-led1")
    refused = _stream(client, TOOL["ledger"], repeat, "t-led2")

    assert dd.REFUSAL_PREFIX in refused
    assert _merge_adds_context(TOOL["ledger"], repeat)
    assert "t-led2" not in rf.turn_facts().received
    forwarded, reloaded = _shown_and_replayed(TOOL["ledger"], repeat, "t-led2", refused, "error")
    assert rf.RECEIVED_KEY not in forwarded
    assert "route_context" not in reloaded


def test_c2_a_remote_error_keeps_the_route_context_it_was_sent(monkeypatch, _route_store):
    """Guard (passes before the change too): the drawing register is sent the
    merged ROUTE block and answers with an error of its own. The call reached
    it, so the context it was sent is still recorded, forwarded and replayed;
    its result is not carried as values."""
    _seed()
    error = "The query could not run: the records relation does not exist."
    edge = _Edge({TOOL["drawings"]: error}, status={TOOL["drawings"]: "error"})
    client = _gateway(monkeypatch, edge)
    question = f"List the drawings for design code {DESIGN}."

    seen = _stream(client, TOOL["drawings"], question, "t-draw")

    (args,) = _sent(edge, TOOL["drawings"])
    facts = rf.turn_facts()
    received = facts.received.get("t-draw", "")
    assert received.startswith("```ROUTE:") and "target_system: drawings" in received
    assert args["kg_context"].startswith(received)
    assert facts.results == []
    forwarded, reloaded = _shown_and_replayed(TOOL["drawings"], question, "t-draw", seen,
                                              "error")
    assert forwarded[rf.RECEIVED_KEY] == received
    assert reloaded["route_context"] == received


#: What strands' MCP client returns for a call whose session closed under it.
_CLOSED = "Tool execution failed: Connection to the MCP server was closed"
LEDGER_QUESTION = "For job JOB-77001, give the item's design code."
DRAWINGS_QUESTION = f"List the drawings for design code {DESIGN}."


class _ClosingEdge(_Edge):
    """``_Edge`` whose MCP session can close, as strands' client behaves: while
    ``closes`` is set, the next call goes out and its session closes under it;
    a call on a closed session is refused before anything is sent
    (``MCPClientInitializationError``); ``start`` opens the session again."""

    def __init__(self, answers):
        super().__init__(answers)
        self.alive, self.closes, self.starts = True, False, 0
        self._tool_provider_started = True
        answer = self.call_tool_async

        async def _call(tool_use_id, name, arguments=None, read_timeout_seconds=None,
                        meta=None):
            from strands.types.exceptions import MCPClientInitializationError
            if not self.alive:
                raise MCPClientInitializationError("the client session is not running.")
            if self.closes:
                self.calls.append((name, dict(arguments or {})))
                self.closes, self.alive = False, False
                return {"status": "error", "toolUseId": tool_use_id,
                        "content": [{"text": _CLOSED}]}
            return await answer(tool_use_id, name, arguments, read_timeout_seconds, meta)

        self.call_tool_async = _call

    def _is_session_active(self):
        return self.alive

    def stop(self, *_args):
        self.alive, self._tool_provider_started = False, False

    def start(self):
        self.starts += 1
        self.alive = True
        return self


def _closing_turn(monkeypatch):
    """The ledger's session closes under one call and is reopened, closes
    under the next one inside the reopen cooldown, and the drawing register's
    call then finds it closed. Returns (the edge, the register's result text)."""
    monkeypatch.setattr(orch, "_GATEWAY_SESSION_RESTART_COOLDOWN_S", 3600.0)
    _seed()
    edge = _ClosingEdge({})
    client = _gateway(monkeypatch, edge)
    edge.closes = True
    _stream(client, TOOL["ledger"], LEDGER_QUESTION, "c1")
    edge.closes = True
    _stream(client, TOOL["ledger"], LEDGER_QUESTION, "c2")
    refused = _stream(client, TOOL["drawings"], DRAWINGS_QUESTION, "c3")
    return edge, refused


def test_c2_a_call_the_closed_session_refused_leaves_no_route_context(
        monkeypatch, _route_store):
    """Reproduction (fails before the change): the session keeper answers the
    drawing register's call with "This call was not sent." because the closed
    session refused it and the reopen cooldown kept it closed. The register
    got nothing, yet route facts kept the ROUTE block the merge built for it,
    so the trace and the reopened chat showed a Routing context for a request
    it never received. The keeper now says the call was not sent, and route
    facts keep nothing of it."""
    edge, refused = _closing_turn(monkeypatch)

    assert edge.starts == 1 and "was not sent" in refused
    assert [name for name, _ in edge.calls] == [TOOL["ledger"], TOOL["ledger"]]
    assert _merge_adds_context(TOOL["drawings"], DRAWINGS_QUESTION)
    facts = rf.turn_facts()
    assert "c3" not in facts.received
    assert facts.results == []
    forwarded, reloaded = _shown_and_replayed(TOOL["drawings"], DRAWINGS_QUESTION, "c3",
                                              refused, "error")
    assert rf.RECEIVED_KEY not in forwarded
    assert "route_context" not in reloaded


def test_c2_an_outcome_unknown_call_keeps_the_route_context_it_was_sent(
        monkeypatch, _route_store):
    """Guard (passes before the change too): the ledger's two calls went out
    and their session closed under them, so whether they acted is unknown.
    Each was sent its merged ROUTE block, which stays recorded, forwarded and
    replayed; neither result is carried as values."""
    edge, _refused = _closing_turn(monkeypatch)

    facts = rf.turn_facts()
    for tool_use_id, (_name, args) in zip(("c1", "c2"), edge.calls):
        received = facts.received.get(tool_use_id, "")
        assert received.startswith("```ROUTE:") and "target_system: ledger" in received
        assert args["kg_context"].startswith(received)
    assert facts.results == []
    seen = "The connection to the MCP server closed before this call returned."
    forwarded, reloaded = _shown_and_replayed(TOOL["ledger"], LEDGER_QUESTION, "c1", seen,
                                              "error")
    assert forwarded[rf.RECEIVED_KEY] == facts.received["c1"]
    assert reloaded["route_context"] == facts.received["c1"]


# --------------------------------------------------------------------------- #
# (d) a tokenless replay of a completed provenance pair changes nothing
# --------------------------------------------------------------------------- #
def test_d_a_tokenless_replay_of_a_completed_provenance_pair_is_inert(
        monkeypatch, _capability_stores):
    """A caller who later replays a completed call's public provenance pair
    (session id and query ref) without the per-call write token cannot change
    what the readers return. The issued arg carried a token and recorded the
    capability; the runtime filed its evidence under the token; a tokenless
    replay writes only the legacy key, which the capability-aware reader never
    serves, so ``get_query`` still returns the real entry."""
    _seed()
    edge = _Edge({TOOL["ledger"]: f"Item for job JOB-77001: design code {DESIGN}."})
    client = _gateway(monkeypatch, edge, identified=True)
    _stream(client, TOOL["ledger"], "For job JOB-77001, give the design code.", "t1")

    (args,) = _sent(edge, TOOL["ledger"])
    session_id, query_ref = prov.parse_provenance_context(args[prov.RESERVED_PROVENANCE_ARG])
    write_token = prov.parse_write_token(args[prov.RESERVED_PROVENANCE_ARG])
    assert write_token is not None

    # The runtime files the real evidence under the per-call token.
    real = {"system_id": "ledger", "tool_name": "query_ledger",
            "executed_query": "SELECT design_code FROM items", "kg_artifacts": [],
            "captured_at": "2026-01-01T00:00:00Z"}
    provenance_store.put_turn_queries(session_id, {query_ref: real}, write_token=write_token)
    assert provenance_store.get_query(session_id, query_ref)["executed_query"] \
        == "SELECT design_code FROM items"

    # A tokenless replay of the public pair holds no token and writes only the
    # legacy key; the capability-aware reader never serves it.
    replay = prov.provenance_context(session_id, query_ref)
    assert prov.parse_write_token(replay) is None
    forged = {**real, "executed_query": "SELECT secret FROM other"}
    provenance_store.put_turn_queries(session_id, {query_ref: forged})
    assert provenance_store.get_query(session_id, query_ref)["executed_query"] \
        == "SELECT design_code FROM items"


# --------------------------------------------------------------------------- #
# (e) a value only an orchestrator-only engine line quotes is never carried
# --------------------------------------------------------------------------- #
#: An identity rule from the shop floor's part code into the ledger's part
#: reference, with a source pattern the part code fits, and its measured join.
PART_RULE = {"id": "ir-part-1", "concept": "part", "source_system": "shop-floor",
             "source_field": "part_code", "target_system": "ledger",
             "target_field": "part_ref", "source_pattern": "AB-CD-{n}",
             "target_pattern": "", "transform": "identity", "confidence": 0.85,
             "method": "value_exact_match"}
JOIN_PART = _join("jp-3", "shop-floor", "shop_db.jobs.part_code",
                  "ledger", "ledger_db.items.part_ref")
LEDGER_PART_QUESTION = f"Look up part_ref {PART} in the ledger."


def _part_turn(monkeypatch, shop_answer):
    """The shop floor answers ``shop_answer``, then the ledger is asked about
    the part. Returns (the ledger call's kg_context, the edge)."""
    _seed(_rule_rows(identity_rules=(PART_RULE,), extra_joins=(JOIN_PART,)))
    edge = _Edge({TOOL["shop-floor"]: shop_answer, TOOL["ledger"]: "No rows."})
    client = _gateway(monkeypatch, edge)
    _stream(client, TOOL["shop-floor"], "For job JOB-77001, which part was built?", "t-shop")
    _stream(client, TOOL["ledger"], LEDGER_PART_QUESTION, "t-led")
    (args,) = _sent(edge, TOOL["ledger"])
    return args.get("kg_context", ""), edge


def test_e_a_returned_part_code_carries_the_rule_concept_and_join(monkeypatch):
    """Control: a part code the shop floor returned carries the part rule's
    concept and its join into the ledger delegation."""
    kg, _ = _part_turn(monkeypatch, f"Job JOB-77001 built part {PART} on line 4.")
    assert "canonical_concept: part" in kg
    assert "jp-3" in kg


@pytest.mark.parametrize("line_for", [answer_ids.engine_line, answer_names.engine_line],
                         ids=["identifiers-line", "names-line"])
def test_e_a_value_only_an_engine_line_quotes_carries_nothing(monkeypatch, line_for):
    """The shop floor returned no value, only the orchestrator-only line that
    its answer gave the part code without a result returning it. Route facts
    keep no record of the part code, and the ledger delegation gets neither
    the part concept nor its join. The orchestrator still reads the line."""
    line = line_for([PART])
    kg, edge = _part_turn(monkeypatch, "No job matched.\n\n" + line)
    assert "canonical_concept: part" not in kg and "jp-3" not in kg
    assert all(PART not in str(r) for r in rf.turn_facts().results)


# --------------------------------------------------------------------------- #
# (f) a retried delegation keeps its query ref but gets a fresh write token
# --------------------------------------------------------------------------- #
def test_f_each_retry_attempt_carries_a_fresh_write_capability(
        monkeypatch, _capability_stores):
    """The gateway's first answer is a retryable error. Both attempts carry
    the same deterministic query ref, and each its own non-empty write token,
    because the capability is issued inside the retry wrapper."""
    async def _no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    _seed()
    edge = _Edge({TOOL["ledger"]: f"Item for job JOB-77001: design code {DESIGN}."})
    succeed = edge.call_tool_async

    async def _flaky(tool_use_id, name, arguments=None, read_timeout_seconds=None, meta=None):
        if not edge.calls:
            edge.calls.append((name, dict(arguments or {})))
            return {"status": "error", "toolUseId": tool_use_id,
                    "content": [{"text": "endpoint not found"}]}
        return await succeed(tool_use_id, name, arguments, read_timeout_seconds, meta)

    edge.call_tool_async = _flaky
    client = _gateway(monkeypatch, edge, identified=True)

    seen = _stream(client, TOOL["ledger"], "For job JOB-77001, give the design code.", "t1")

    assert DESIGN in seen
    first, second = _sent(edge, TOOL["ledger"])
    refs = [prov.parse_provenance_context(a[prov.RESERVED_PROVENANCE_ARG])[1]
            for a in (first, second)]
    tokens = [prov.parse_write_token(a[prov.RESERVED_PROVENANCE_ARG]) for a in (first, second)]
    assert refs[0] == refs[1]
    assert all(tokens) and tokens[0] != tokens[1]


# --------------------------------------------------------------------------- #
# (g) an IDENTIFIER-MISS block takes no value off its identity rule
# --------------------------------------------------------------------------- #
def _part_miss_block(token: str) -> str:
    """The shape guard's block for ``token``, carried as the engine's
    unpaired-forms fact (``unresolved_miss``)."""
    from agents.unresolved_miss import MARKER
    return (f"{MARKER} figures were not paired.\n\n```\nIDENTIFIER-MISS:\n"
            f"token: {token}\nlooked_in: shop_db.jobs.part_code\n"
            "my_format: shop_db.jobs.part_code values are shaped like AB-CD-9999\n"
            f"context: {token} has the form of another field.\n```")


def _part_turn_seen(monkeypatch, shop_answer):
    """``_part_turn``, plus the text the orchestrator was handed back for the
    shop-floor call."""
    _seed(_rule_rows(identity_rules=(PART_RULE,), extra_joins=(JOIN_PART,)))
    edge = _Edge({TOOL["shop-floor"]: shop_answer, TOOL["ledger"]: "No rows."})
    client = _gateway(monkeypatch, edge)
    seen = _stream(client, TOOL["shop-floor"], "For job JOB-77001, which part was built?",
                   "t-shop")
    _stream(client, TOOL["ledger"], LEDGER_PART_QUESTION, "t-led")
    (args,) = _sent(edge, TOOL["ledger"])
    return args.get("kg_context", ""), seen


@pytest.mark.parametrize("data", [f"Job JOB-77001 built part {PART} on line 4.",
                                  f"| part_code | line |\n|---|---|\n| {PART} | 4 |"],
                         ids=["prose", "labelled"])
def test_g_a_part_code_a_miss_block_repeats_still_carries_its_rule(monkeypatch, data):
    """The part code is in the answer's data and the block repeats it under
    its own ``token`` label: the ledger delegation still gets the part rule's
    concept and join, and the orchestrator still reads the block."""
    kg, seen = _part_turn_seen(monkeypatch, f"{data}\n\n{_part_miss_block(PART)}")
    assert "canonical_concept: part" in kg
    assert "jp-3" in kg
    assert f"token: {PART}" in seen and "IDENTIFIER-MISS:" in seen


def test_g_a_part_code_only_a_miss_block_names_carries_nothing(monkeypatch):
    kg, seen = _part_turn_seen(monkeypatch, "No job matched.\n\n" + _part_miss_block(PART))
    assert "canonical_concept: part" not in kg and "jp-3" not in kg
    assert all(PART not in r.tokens for r in rf.turn_facts().results)
    assert f"token: {PART}" in seen


@pytest.mark.parametrize("closing", ["\n```", ""], ids=["before-the-fence-closes", "unclosed"])
def test_g_a_part_code_after_the_block_fields_carries_its_rule(monkeypatch, closing):
    """The answer writes its data after a block's fields, inside the block's
    fence or with no closing fence: the part code is still read and carries
    the part rule's concept and join."""
    opened = "\n".join(_part_miss_block("AB-CD-0001").split("\n")[:-1])
    kg, seen = _part_turn_seen(
        monkeypatch, f"{opened}\nJob JOB-77001 built part {PART} on line 4.{closing}")
    assert "canonical_concept: part" in kg
    assert "jp-3" in kg
    assert "token: AB-CD-0001" in seen
