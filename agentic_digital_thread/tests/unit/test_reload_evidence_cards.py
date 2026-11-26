# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A reopened chat's Sources open the evidence cards they opened live.

A live answer's Sources chips open their evidence cards from the turn's
``provenance`` metadata frame. That frame is streamed and never stored with
the turn, so a chat reopened from Recent sessions (``GET
/api/memory/events``) rebuilt the chips with nothing to bind them to: they
rendered, but no card opened. The app now keeps the frame the browser
received (``agents.turn_evidence_store``, written at the end of the
``/api/ask`` stream) and the events route returns it on the reloaded
question, for the same actor and session only, gated exactly as it streamed.

Offline: the live turn goes through the production ``/api/ask`` response
(``server.ask`` driven over ASGI) with a chat client that parses runtime
frames through the real ``_CloudStreamState``; the frame itself comes from
the one provenance builder. The reload goes through the events route with
AWS faked (``test_memory_events_rehydrate``). The schema names are made up.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

# Make `agents...` importable when run as a script (the fixture writer below);
# a fixed memory id there keeps config from looking one up in AWS at import.
_ADT_ROOT = Path(__file__).resolve().parents[2]
if __name__ == "__main__":
    if str(_ADT_ROOT) not in sys.path:
        sys.path.insert(0, str(_ADT_ROOT))
    os.environ.setdefault("MEMORY_ID", "mem-fixture-writer")

import agents.memory_client as mc  # noqa: E402
import agents.memory_routes as routes  # noqa: E402
import agents.server as server  # noqa: E402
from agents.chat_client import ThinkingFilter, _CloudStreamState  # noqa: E402
from agents.models import AskRequest  # noqa: E402
from agents.provenance import build_provenance_records, mint_query_ref  # noqa: E402
from tests.unit.test_memory_events_rehydrate import (  # noqa: E402
    _FakeEvents,
    _client_for,
    _event,
)

#: Two made-up schemas: every test runs on either, so nothing keys on a name.
SCHEMAS = {
    "furnace": {"first": "furnace-journal", "second": "parcel-desk",
                "table": "firings", "columns": ["batch_code", "peak_temp"],
                "question": "Which firing batches ran hot last week?"},
    "brewing": {"first": "brew-log", "second": "cask-register",
                "table": "mash_runs", "columns": ["run_ref", "gravity"],
                "question": "Which mash runs missed their gravity target?"},
}
CAPTURED_AT = "2026-10-24T21:00:00Z"


class _Log:
    def info(self, *_a, **_k):
        pass

    def warning(self, *_a, **_k):
        pass


def _tool(system_id: str) -> str:
    return f"{system_id}___query_{system_id.replace('-', '_')}"


def _calls(names: dict, prefix: str) -> list[dict]:
    """One turn's calls: an uncited graph lookup, then three cited system
    calls, two of them run in parallel."""
    return [
        {"toolUseId": f"tooluse_{prefix}a", "name": "explore_graph"},
        {"toolUseId": f"tooluse_{prefix}b", "name": _tool(names["first"])},
        {"toolUseId": f"tooluse_{prefix}c", "name": _tool(names["second"])},
        {"toolUseId": f"tooluse_{prefix}d", "name": _tool(names["first"])},
    ]


def _frame(names: dict, session_id: str, calls: list[dict], *, verdict=None,
           label: str = "batch identity rule") -> dict:
    """The turn's provenance payload, from the one builder both emitters use."""
    cited = [c for c in calls if c["name"] != "explore_graph"]
    evidence = {c["toolUseId"]: {
        "table": names["table"], "columns": list(names["columns"]),
        "filters": [f"{names['columns'][0]} starts with K", f"call {i}"],
        "window": {"start": "2026-10-17T00:00:00Z", "end": "2026-10-24T00:00:00Z"},
    } for i, c in enumerate(cited)}
    metadata = {c["toolUseId"]: {"app_status": "success", "row_count": 3 + i}
                for i, c in enumerate(cited)}
    graph = calls[0]["toolUseId"]
    return build_provenance_records(
        session_id=session_id, tool_calls=calls, tool_inputs={},
        tool_results_metadata=metadata, evidence_by_tool=evidence,
        rules_applied=[{"id": "ir-77", "label": label}], verdict=verdict,
        captured_at=CAPTURED_AT,
        graph_query_refs=[{"query_ref": mint_query_ref(session_id, graph),
                           "tool_name": "explore_graph"}])


def _runtime_frames(calls: list[dict], provenance: "dict | list[dict]", *, gated: bool,
                    answer: str = "Three batches ran hot [1][2][3].",
                    seed_step: "str | None" = None) -> list[dict]:
    """The runtime's stream for one turn: calls, results, then the frame (or
    each of several frames, in order). ``seed_step``: the tool id of the
    Knowledge Graph step a seeded turn streams first, which is not one of
    the agent's stored messages."""
    if gated:
        frames = []
        for c in calls:
            frames.append({"current_tool_use": {**c, "input": {}}})
            frames.append({"tool_result_metadata": {
                **c, "metadata": {"app_status": "success", "row_count": 3}}})
    else:
        frames = [
            {"message": {"role": "assistant", "content": [{"toolUse": {**calls[0], "input": {}}}]}},
            {"message": {"role": "user", "content": [{"toolResult": {
                "toolUseId": calls[0]["toolUseId"], "status": "success",
                "content": [{"text": "the batches are kept in two systems"}]}}]}},
            {"message": {"role": "assistant", "content": [
                {"toolUse": {**calls[1], "input": {"question": "hot batches"}}},
                {"toolUse": {**calls[2], "input": {"question": "their parcels"}}}]}},
            {"message": {"role": "user", "content": [
                {"toolResult": {"toolUseId": c["toolUseId"], "status": "success",
                                "content": [{"text": "3 rows"}]}} for c in calls[1:3]]}},
            {"message": {"role": "assistant", "content": [{"toolUse": {**calls[3], "input": {}}}]}},
            {"message": {"role": "user", "content": [{"toolResult": {
                "toolUseId": calls[3]["toolUseId"], "status": "success",
                "content": [{"text": "5 rows"}]}}]}},
        ]
    provenance = provenance if isinstance(provenance, list) else [provenance]
    if seed_step:
        frames = [{"current_tool_use": {"toolUseId": seed_step, "name": "explore_graph",
                                        "input": {"question": "hot batches"}}},
                  {"message": {"role": "user", "content": [{"toolResult": {
                      "toolUseId": seed_step, "status": "success",
                      "content": [{"text": "two systems hold the batches"}]}}]}}] + frames
    return frames + [{"provenance": p} for p in provenance] + [{"data": answer}]


def _two_frames(names: dict, session_id: str, calls: list[dict]) -> list[dict]:
    """A turn that streamed two frames: the first with graph queries and two
    records, the second with all three records and no graph queries."""
    first = _frame(names, session_id, calls[:3])
    second = _frame(names, session_id, calls)
    second.pop("graph_queries")
    return [first, second]


@contextmanager
def _source_side_stores():
    """The provenance and trace stores on a shared table, as deployed, so a
    system agent's writes land under the capability; restored after."""
    import agents.investigation_trace_store as its
    import agents.provenance_store as provenance_store
    from agents.job_store import JobStore
    from tests.unit._shared_table import SharedTable
    saved = (provenance_store._job_store, provenance_store._cap_store, its._job_store)
    data = SharedTable()
    provenance_store._job_store = JobStore(provenance_store._KIND, table=data)
    provenance_store._cap_store = JobStore(provenance_store._CAP_KIND, table=SharedTable())
    its._job_store = JobStore(its._KIND, table=data)
    try:
        yield
    finally:
        provenance_store._job_store, provenance_store._cap_store, its._job_store = saved


class _SiteWiseSource:
    """A SiteWise client stand-in: a current value per property, 24 points
    per series (made-up readings)."""

    @staticmethod
    def _point(i: int) -> dict:
        return {"value": {"doubleValue": 1.0 + i}, "quality": "GOOD",
                "timestamp": {"timeInSeconds": 1793131200 + 60 * i}}

    def get_asset_property_value(self, **_kwargs):
        return {"propertyValue": self._point(0)}

    def batch_get_asset_property_value_history(self, entries, maxResults, nextToken=None):  # noqa: N803
        return {"successEntries": [{"entryId": e["entryId"], "assetPropertyValueHistory": [
            self._point(i) for i in range(24)]} for e in entries],
            "errorEntries": [], "skippedEntries": []}

    def get_asset_property_value_history(self, **_kwargs):
        return {"assetPropertyValueHistory": []}


#: The read each kind of API-read call makes: a history read over three
#: series, or one current value.
_READS = {
    "history": ("sitewise_get_asset_property_history", {
        "entries_json": json.dumps([{"assetId": "unit-1", "propertyId": p}
                                    for p in ("chan-a", "chan-b", "chan-c")]),
        "start_iso": "2026-10-24T20:00:00Z", "end_iso": "2026-10-24T21:00:00Z"}),
    "current_value": ("sitewise_get_asset_property_value",
                      {"asset_id": "unit-1", "property_id": "chan-a"}),
}


def _system_agent_reads(session_id: str, call: dict, read: str = "history") -> None:
    """One cited call answered by a system agent that ran no SQL: the real
    SiteWise (Direct) tool for ``read`` (``_READS``), its source faked, run
    through the system agent's real capture hook (the tool reports what it
    parsed), then its own out-of-band writes under the call's capability."""
    import agents.provenance_store as provenance_store
    import agents.system_agent as sa
    from agents import aws_session
    from agents.runtime_asgi import provenance_context_var, provenance_write_token_var
    from tests.unit._api_reads import run_tool
    name, tool_input = _READS[read]
    state: dict = {}
    with patch.object(aws_session, "boto3_client_for",
                      lambda service, cfg, default_region=None: _SiteWiseSource()):
        tools = {t.tool_name: t for t in sa._make_sitewise_direct_tool(
            region="us-east-1", connection_config={}, system_id="unit-reader")}
        hook = sa._system_agent_hooks("unit-reader", "SiteWise (Direct)", "",
                                      list(tools.values()))[1]
        run_tool(hook, state, tools[name], tool_input)
    ref = mint_query_ref(session_id, call["toolUseId"])
    token = provenance_store.issue_provenance_arg(session_id, ref).get("write_token")
    context = provenance_context_var.set((session_id, ref))
    write_token = provenance_write_token_var.set(token)
    try:
        sa._write_provenance_sidecar(state, "unit-reader")
        sa._write_investigation_trace(state, "unit-reader", "how did the units run",
                                      "Three channels read.")
    finally:
        provenance_context_var.reset(context)
        provenance_write_token_var.reset(write_token)


def _api_read_frame(names: dict, session_id: str, calls: list[dict],
                    read: str = "history") -> dict:
    """The report-only runtime's frame for a turn whose cited calls ran no SQL
    and read their source through its API (each with the ``read`` of
    ``_READS``): built as the runtime builds it (the bounded metadata of each
    call's prose answer, no typed evidence), then enriched from the read
    entries the system agents wrote."""
    from agents import runtime_app
    from agents.tool_result_capture import safe_tool_result_metadata
    cited = [c for c in calls if c["name"] != "explore_graph"]
    graph = calls[0]["toolUseId"]
    with _source_side_stores():
        for call in cited:
            _system_agent_reads(session_id, call, read)
        payload = build_provenance_records(
            session_id=session_id, tool_calls=calls, tool_inputs={},
            tool_results_metadata={c["toolUseId"]: safe_tool_result_metadata(
                {"content": [{"text": "Three channels read."}]}, tool_name=c["name"],
                max_chars=2048) for c in cited},
            evidence_by_tool={}, rules_applied=[{"id": "ir-77", "label": "unit rule"}],
            verdict=None, captured_at=CAPTURED_AT,
            graph_query_refs=[{"query_ref": mint_query_ref(session_id, graph),
                               "tool_name": "explore_graph"}])
        return runtime_app._enriched_provenance_payload(payload, session_id)


class _RuntimeClient:
    """A chat client that parses the given runtime frames the way the cloud
    client does (``_CloudStreamState``), for the caller's own actor and session."""

    def __init__(self, frames: list[dict], *, gated: bool):
        self.frames = frames
        self.gated = gated
        self.prompts: list = []

    def stream(self, prompt, session_id, user_id, cancel=None, **_kwargs):
        self.prompts.append(prompt)
        state = _CloudStreamState(ThinkingFilter(), integrity_mode=self.gated, log=_Log(),
                                  owner_id=user_id, session_id=session_id,
                                  runtime_session_id="r")
        for frame in self.frames:
            yield from state.events_for(frame)
        yield from state.completion_events()


class _Req:
    def __init__(self, user_id: str):
        self.state = types.SimpleNamespace(user=types.SimpleNamespace(user_id=user_id),
                                           session=None)


def _scope() -> dict:
    return {"type": "http", "method": "POST", "path": "/api/ask", "headers": [],
            "asgi": {"version": "3.0", "spec_version": "2.1"}}


async def _complete(response) -> bytes:
    body: list[bytes] = []
    never = asyncio.Event()

    async def receive():
        await never.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            body.append(message.get("body", b""))

    await response(_scope(), receive, send)
    return b"".join(body)


def _sse(body: bytes) -> list[dict]:
    """The browser's view of the stream: ``{event, data}`` per frame, without
    the clock readings (display timing only)."""
    frames = []
    for chunk in body.decode("utf-8").split("\n\n"):
        lines = chunk.strip().splitlines()
        if len(lines) != 2 or not lines[0].startswith("event: "):
            continue
        data = json.loads(lines[1][len("data: "):])
        if isinstance(data, dict):
            data = {k: v for k, v in data.items()
                    if k not in ("ts", "elapsed_ms", "elapsedSeconds")}
        frames.append({"event": lines[0][len("event: "):], "data": data})
    return frames


def _env(integrity: str) -> dict:
    return {"RESPONSE_INTEGRITY_MODE": integrity, "SYSTEM_QUERY_MODE": "agent"}


def _ask(frames: list[dict], *, user_id: str, session_id: str, question: str,
         gated: bool = False, write_memory=None, plant: str = "",
         sent: "list | None" = None, locale: "str | None" = None) -> list[dict]:
    """One /api/ask turn through the production response; the SSE it sent.
    ``sent`` receives the prompt the app sent the runtime."""
    body = AskRequest(question=question, session_id=session_id, write_memory=write_memory,
                      plant=plant, locale=locale)
    client = _RuntimeClient(frames, gated=gated)
    with patch.object(server, "get_chat_client", lambda: client), \
            patch.object(server, "_store_ask_usage", lambda *a, **k: None), \
            patch("agents.auth.forwarding.user_access_token_from_session", return_value=None), \
            patch.dict("os.environ", _env("enforce" if gated else "report-only")):
        response = asyncio.run(server.ask(body, _Req(user_id)))
        sse = _sse(asyncio.run(_complete(response)))
    if sent is not None:
        sent.extend(client.prompts)
    return sse


def _live_frame(sse: list[dict]) -> dict:
    """The provenance frame the browser received (the last one, as the chat
    reducer keeps it)."""
    frames = [f["data"]["provenance"] for f in sse
              if f["event"] == "metadata" and "provenance" in f["data"]]
    assert frames, "the live stream carried no provenance frame"
    return frames[-1]


def _stored_turn(question: str, calls: list[dict], start: int, prefix: str, *,
                 gated: bool = False, prompt: "str | None" = None) -> list:
    """The turn as the Strands session manager stores it, OLDEST first.
    ``prompt`` is the stored user text when it is not the default temporal
    context plus the question."""
    text = prompt if prompt is not None else (
        f"[Temporal context: now is 2026-10-24 21:00 UTC] {question}")
    events = [
        _event(f"{prefix}1", start, "user", [{"text": text}]),
        _event(f"{prefix}2", start + 1, "assistant", [
            {"text": "HIDDEN_NARRATION graph first" if gated else "Checking the graph first."},
            {"toolUse": {**calls[0], "input": {"question": "HIDDEN_INPUT hot"}}}]),
        _event(f"{prefix}3", start + 2, "user", [{"toolResult": {
            "toolUseId": calls[0]["toolUseId"], "status": "success",
            "content": [{"text": "the batches are kept in two systems"}]}}]),
        _event(f"{prefix}4", start + 3, "assistant", [
            {"toolUse": {**c, "input": {"question": "HIDDEN_INPUT q"}}} for c in calls[1:3]]),
        _event(f"{prefix}5", start + 4, "user", [{"toolResult": {
            "toolUseId": c["toolUseId"], "status": "success",
            "content": [{"text": "3 rows"}]}} for c in calls[1:3]]),
        _event(f"{prefix}6", start + 5, "assistant", [{"toolUse": {**calls[3], "input": {}}}]),
        _event(f"{prefix}7", start + 6, "user", [{"toolResult": {
            "toolUseId": calls[3]["toolUseId"], "status": "success",
            "content": [{"text": "5 rows"}]}}]),
    ]
    if gated:
        events.append(_event(f"{prefix}8", start + 7, "assistant", [{"toolUse": {
            "toolUseId": f"{calls[0]['toolUseId']}z", "name": "FinalAnswerDraft",
            "input": {"claims": [{"text": "HIDDEN_CLAIM three"}]}}}]))
        events.append(_event(f"{prefix}9", start + 8, "user", [{"toolResult": {
            "toolUseId": f"{calls[0]['toolUseId']}z", "status": "success",
            "content": [{"text": "HIDDEN_CLAIM accepted"}]}}]))
    else:
        events.append(_event(f"{prefix}8", start + 7, "assistant",
                             [{"text": "Three batches ran hot [1][2][3]."}]))
    return events


def _events_endpoint():
    """The handler the app registers for ``GET /api/memory/events``."""
    (route,) = [r for r in routes.router.routes if r.path == "/api/memory/events"]
    return route.endpoint


def _reload(events: list, *, user_id: str, session_id: str,
            integrity: str = "report-only") -> dict:
    """``GET /api/memory/events`` for this actor and session, AWS faked."""
    client, cfg = _client_for(_FakeEvents(events))
    with patch.object(mc, "get_config", return_value=cfg), \
            patch.object(routes, "_client", client), \
            patch.dict("os.environ", _env(integrity)):
        return asyncio.run(_events_endpoint()(_Req(user_id), session_id=session_id))


def _questions(payload: dict) -> list[dict]:
    """The question messages, picked the way the chat's reload picks them
    (``rehydrate.ts``): a user message with text and no tool result."""
    return [m for m in payload["messages"]
            if m["role"] == "user" and (m.get("content") or "").strip() and not any(
                b.get("type") == "tool_result" for b in m.get("blocks") or [])]


def _session() -> str:
    return f"s-{uuid.uuid4().hex[:12]}"


def _prefix() -> str:
    return uuid.uuid4().hex[:6]


# --------------------------------------------------------------------------- #
# Live and reload parity
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("schema", sorted(SCHEMAS))
def test_each_reloaded_answer_gets_the_frame_its_live_stream_sent(schema):
    names, session_id = SCHEMAS[schema], _session()
    stored, live = [], []
    for turn in range(2):
        calls = _calls(names, _prefix())
        frame = _frame(names, session_id, calls, label=f"rule of turn {turn}")
        sse = _ask(_runtime_frames(calls, frame, gated=False), user_id="u1",
                   session_id=session_id, question=names["question"])
        live.append(_live_frame(sse))
        stored += _stored_turn(names["question"], calls, turn * 100, f"t{turn}")
    payload = _reload(stored, user_id="u1", session_id=session_id)
    questions = _questions(payload)
    assert [q.get("provenance") for q in questions] == live
    # Three cited system calls per turn, each with its record.
    assert all(len(frame["records"]) == 3 for frame in live)


@pytest.mark.parametrize("read", sorted(_READS))
@pytest.mark.parametrize("schema", sorted(SCHEMAS))
def test_an_answer_read_through_a_source_api_reloads_the_same_partial_cards(schema, read):
    """A cited call that ran no SQL and read its source through the source's
    API, a history read or one current value (round 3: a single value read
    used to write no entry): its system agent's read entry makes the live
    record partial, with no row count, and the reload shows exactly that
    frame."""
    names, session_id = SCHEMAS[schema], _session()
    calls = _calls(names, _prefix())
    frame = _api_read_frame(names, session_id, calls, read)
    assert [r["verification"] for r in frame["records"]] == ["partial"] * 3
    assert [r["result_shape"]["rows"] for r in frame["records"]] == [None] * 3
    live = _live_frame(_ask(_runtime_frames(calls, frame, gated=False), user_id="u1",
                            session_id=session_id, question=names["question"]))
    assert live == frame
    (question,) = _questions(_reload(_stored_turn(names["question"], calls, 0, "api"),
                                     user_id="u1", session_id=session_id))
    assert question["provenance"] == live


def test_a_turn_that_streamed_two_frames_reloads_what_the_chat_kept():
    # The chat keeps the last frame's records and the last non-empty graph
    # queries (thread-store.ts onMetadata); the reload must hand it the same.
    names, session_id = SCHEMAS["furnace"], _session()
    calls = _calls(names, _prefix())
    first, second = _two_frames(names, session_id, calls)
    sse = _ask(_runtime_frames(calls, [first, second], gated=False), user_id="u1",
               session_id=session_id, question=names["question"])
    sent = [f["data"]["provenance"] for f in sse
            if f["event"] == "metadata" and "provenance" in f["data"]]
    assert sent == [first, second]
    (question,) = _questions(_reload(_stored_turn(names["question"], calls, 0, "w"),
                                     user_id="u1", session_id=session_id))
    assert question["provenance"] == {"records": second["records"],
                                      "truncated": second["truncated"],
                                      "graph_queries": first["graph_queries"]}


def test_a_turn_from_before_the_change_reloads_without_a_frame():
    names, session_id = SCHEMAS["furnace"], _session()
    old_calls, new_calls = _calls(names, _prefix()), _calls(names, _prefix())
    frame = _frame(names, session_id, new_calls)
    live = _live_frame(_ask(_runtime_frames(new_calls, frame, gated=False), user_id="u1",
                            session_id=session_id, question=names["question"]))
    stored = (_stored_turn(names["question"], old_calls, 0, "old")
              + _stored_turn(names["question"], new_calls, 100, "new"))
    old, new = _questions(_reload(stored, user_id="u1", session_id=session_id))
    assert "provenance" not in old  # nothing was kept for it: the old shape
    assert new["provenance"] == live


# --------------------------------------------------------------------------- #
# A frame only ever lands on the turn it came from
# --------------------------------------------------------------------------- #
#: The runtime keeps a toolUseId unique within one turn only, so two turns of
#: a session may carry the same call ids.
DUP = "dup"


def _two_turns(questions: list[str], calls_per_turn: list[list[dict]], session_id: str):
    """Ask each turn live and build its stored events: (live frames, stored
    events per turn)."""
    names = SCHEMAS["furnace"]
    live, stored = [], []
    for turn, (question, calls) in enumerate(zip(questions, calls_per_turn)):
        frame = _frame(names, session_id, calls, label=f"rule of turn {turn}")
        live.append(_live_frame(_ask(_runtime_frames(calls, frame, gated=False), user_id="u1",
                                     session_id=session_id, question=question)))
        stored.append(_stored_turn(question, calls, turn * 100, f"d{turn}"))
    assert live[0] != live[1]
    return live, stored


def _shown_frames(events: list, session_id: str) -> list:
    return [q.get("provenance") for q in
            _questions(_reload(events, user_id="u1", session_id=session_id))]


def test_a_reused_call_id_never_moves_a_frame_to_another_turn():
    names, session_id = SCHEMAS["furnace"], _session()
    calls = _calls(names, DUP)  # the same call ids in both turns
    questions = [names["question"], "Which firing batches ran cold last week?"]
    live, stored = _two_turns(questions, [calls, calls], session_id)
    assert _shown_frames(stored[0] + stored[1], session_id) == live
    # Either turn alone still gets only its own frame.
    assert _shown_frames(stored[0], session_id) == [live[0]]
    assert _shown_frames(stored[1], session_id) == [live[1]]


def test_twin_turns_that_cannot_be_told_apart_get_no_frame():
    # The same question and the same call ids: nothing either side holds tells
    # the two turns apart, so neither gets a frame, even with only one visible.
    names, session_id = SCHEMAS["furnace"], _session()
    calls = _calls(names, DUP)
    _live, stored = _two_turns([names["question"]] * 2, [calls, calls], session_id)
    assert _shown_frames(stored[0] + stored[1], session_id) == [None, None]
    assert _shown_frames(stored[0], session_id) == [None]
    assert _shown_frames(stored[1], session_id) == [None]


def test_a_frame_sharing_a_call_id_keeps_the_other_turns_frame():
    from agents import turn_evidence_store as evidence
    names, session_id = SCHEMAS["furnace"], _session()
    first, second = _calls(names, DUP), _calls(names, _prefix())
    second[1] = dict(first[1])  # one cited call id in common
    _two_turns([names["question"], "Which firing batches ran cold last week?"],
               [first, second], session_id)
    item = evidence._jobs().get(evidence.item_key("u1", session_id))
    assert len(item["frames"]) == 2


def test_unique_call_ids_attach_whatever_the_question_preambles():
    # Guard: the turn's question is read the same way at write and at reload,
    # so a plant-scoped, seeded question still gets its frame.
    names, session_id = SCHEMAS["brewing"], _session()
    calls = _calls(names, _prefix())
    frame = _frame(names, session_id, calls)
    body = AskRequest(question=names["question"], session_id=session_id, plant="North Works")
    prompt = ("[ROUTE SEED: graph-first routing context for the planner]\nconcepts: ..."
              "\n\n---\n\n" + server._prepare_question(body))
    live = _live_frame(_ask(_runtime_frames(calls, frame, gated=False,
                                            seed_step="mode-a-route-seed"),
                            user_id="u1", session_id=session_id,
                            question=names["question"], plant="North Works"))
    stored = _stored_turn(names["question"], calls, 0, "p", prompt=prompt)
    assert _shown_frames(stored, session_id) == [live]


# --------------------------------------------------------------------------- #
# The stored question is redacted; the turn identity still matches
# --------------------------------------------------------------------------- #
#: One question per structured PII shape the memory write redacts
#: (``agents.pii.PII_PATTERNS``).
PII_QUESTIONS = {
    "email": "Which orders did ops@example.com reopen?",
    "phone": "Which orders did the caller at 555-123-4567 reopen?",
    "ssn": "Which orders list the record 123-45-6789?",
    "card": "Which orders were paid with card 4111 1111 1111 1111?",
}
SEED = "[ROUTE SEED: graph-first routing context for the planner]\nconcepts: ...\n\n---\n\n"


def _persisted(text: str) -> str:
    """``text`` as the memory write stores a user turn: through the session
    manager's own redaction (``orchestrator._redact_session_message``)."""
    from strands.types.session import SessionMessage
    from agents.orchestrator import _redact_session_message
    message = SessionMessage(message={"role": "user", "content": [{"text": text}]},
                             message_id=0)
    return _redact_session_message(message).message["content"][0]["text"]


def _pii_round_trip(question: str, *, plant: str = "", seeded: bool = False,
                    seed: str = SEED, locale: "str | None" = None):
    """Ask ``question`` live, store the prompt the app sent the way the memory
    write stores it, and reload: (live frame, stored text, reloaded frames).
    ``seed`` is the seed text the runtime composes in front of the prompt
    when ``seeded``; ``locale`` is the client's locale tag."""
    names, session_id = SCHEMAS["furnace"], _session()
    calls = _calls(names, _prefix())
    frame = _frame(names, session_id, calls)
    sent: list = []
    live = _live_frame(_ask(_runtime_frames(calls, frame, gated=False,
                                            seed_step="mode-a-route-seed" if seeded else None),
                            user_id="u1", session_id=session_id, question=question,
                            plant=plant, sent=sent, locale=locale))
    (prompt,) = sent
    stored_text = _persisted((seed if seeded else "") + str(prompt))
    stored = _stored_turn(question, calls, 0, "pii", prompt=stored_text)
    return live, stored_text, _shown_frames(stored, session_id)


@pytest.mark.parametrize("shape", sorted(PII_QUESTIONS))
def test_a_question_the_memory_write_redacts_still_gets_its_frame(shape):
    question = PII_QUESTIONS[shape]
    live, stored_text, shown = _pii_round_trip(question)
    assert question not in stored_text  # the stored copy really was redacted
    assert f"[REDACTED_{shape.upper()}]" in stored_text
    assert shown == [live]


def test_a_redacted_question_with_plant_and_seed_preambles_gets_its_frame():
    live, stored_text, shown = _pii_round_trip(PII_QUESTIONS["email"], plant="North Works",
                                               seeded=True)
    assert stored_text.startswith("[ROUTE SEED") and "[Plant context: North Works" in stored_text
    assert "[REDACTED_EMAIL]" in stored_text
    assert shown == [live]


@pytest.mark.parametrize("shape", sorted(PII_QUESTIONS))
def test_redaction_is_idempotent(shape):
    # Guard: the reload reads text the memory write already redacted, so the
    # shared question helper redacting it again must change nothing.
    from agents.pii import redact_pii
    once = redact_pii(PII_QUESTIONS[shape])
    assert once != PII_QUESTIONS[shape]
    assert redact_pii(once) == once


@pytest.mark.parametrize("shape", sorted(PII_QUESTIONS))
def test_both_sides_read_the_same_question(shape):
    # The one shared helper, given the prompt the app sends and the copy the
    # memory write stores, reads the same question.
    from agents.turn_evidence_store import turn_question
    body = AskRequest(question=PII_QUESTIONS[shape], plant="North Works")
    prompt = server._prepare_question(body)
    assert turn_question(prompt) == turn_question(_persisted(SEED + prompt))
    assert turn_question(turn_question(prompt)) == turn_question(prompt)


#: One standalone value per structured PII shape, and the ones the locale
#: allowlist (``time_context._LOCALE_PATTERN``: letters, digits, ``_``, ``-``)
#: admits, written the way that allowlist allows.
PII_VALUES = {"email": "ops@example.com", "phone": "555-123-4567",
              "ssn": "123-45-6789", "card": "4111 1111 1111 1111"}
PII_LOCALES = {"phone": "555-123-4567", "ssn": "123-45-6789", "card": "4111-1111-1111-1111"}


def _seed_ending_with(value: str) -> str:
    """A seed whose last character before the separator is ``value``'s."""
    return f"[ROUTE SEED: graph-first routing context for the planner]\nlast value {value}\n\n---\n\n"


def test_pii_in_the_seed_and_in_the_question_still_gets_its_frame():
    # The stored side strips a seed the memory write redacted; the app never
    # sends the seed (the runtime adds it). Both then read the same redacted
    # question.
    seed = ("[ROUTE SEED: graph-first routing context for the planner]\n"
            f"contacts: {PII_VALUES['phone']}, {PII_VALUES['card']}\n\n---\n\n")
    live, stored_text, shown = _pii_round_trip(PII_QUESTIONS["email"], plant="North Works",
                                               seeded=True, seed=seed)
    head, _sep, tail = stored_text.partition("\n\n---\n\n")
    assert "[REDACTED_PHONE]" in head and "[REDACTED_CARD]" in head
    assert "[REDACTED_EMAIL]" in tail
    assert shown == [live]


@pytest.mark.parametrize("shape", sorted(PII_VALUES))
def test_a_pii_value_next_to_the_seed_separator_still_gets_its_frame(shape):
    # A value right before the separator, and one opening the question right
    # after the preambles: no redaction match can reach across either.
    value = PII_VALUES[shape]
    live, stored_text, shown = _pii_round_trip(f"{value} is on which orders?", seeded=True,
                                               seed=_seed_ending_with(value))
    assert f"[REDACTED_{shape.upper()}]\n\n---\n\n" in stored_text
    assert stored_text.endswith(f"] [REDACTED_{shape.upper()}] is on which orders?")
    assert shown == [live]


@pytest.mark.parametrize("shape", sorted(PII_LOCALES))
def test_a_pii_shaped_locale_still_gets_its_frame(shape):
    # The locale tag is rendered inside the temporal preamble, so the memory
    # write redacts inside that preamble and the stored copy's peel stops at
    # the token's closing bracket. Both sides read the question the same way
    # only when both redact before they peel.
    live, stored_text, shown = _pii_round_trip(PII_QUESTIONS["email"],
                                               locale=PII_LOCALES[shape])
    assert f"User locale: [REDACTED_{shape.upper()}]." in stored_text
    assert shown == [live]


def _write_and_stored_forms(prompt: str, seed: str) -> list[str]:
    """The prompt the app sent, the raw composed turn, and the copy the
    memory write stores."""
    return [prompt, seed + prompt, _persisted(seed + prompt)]


@pytest.mark.parametrize("shape", sorted(PII_VALUES))
def test_the_question_reads_the_same_from_every_form(shape):
    from agents.turn_evidence_store import turn_question
    value = PII_VALUES[shape]
    cases = [
        (AskRequest(question=f"{value} is on which orders?"), _seed_ending_with(value)),
        (AskRequest(question=f"Which orders did {value} reopen?", plant=f"Desk {value}"), SEED),
        (AskRequest(question=f"Which orders list {value}"), _seed_ending_with(value)),
    ]
    if shape in PII_LOCALES:
        cases.append((AskRequest(question=PII_QUESTIONS["email"], locale=PII_LOCALES[shape]),
                      SEED))
    for body, seed in cases:
        forms = [turn_question(text) for text in _write_and_stored_forms(
            server._prepare_question(body), seed)]
        assert forms[0] == forms[1] == forms[2], body
        assert value not in forms[0]


# --------------------------------------------------------------------------- #
# Only the caller's own session
# --------------------------------------------------------------------------- #
def test_each_actor_reloads_only_its_own_frame():
    names, session_id = SCHEMAS["brewing"], _session()
    calls = _calls(names, _prefix())  # the same ids for both actors: the worst case
    live = {}
    for user_id in ("u1", "u2"):
        frame = _frame(names, session_id, calls, label=f"rule seen by {user_id}")
        live[user_id] = _live_frame(_ask(_runtime_frames(calls, frame, gated=False),
                                         user_id=user_id, session_id=session_id,
                                         question=names["question"]))
    stored = _stored_turn(names["question"], calls, 0, "x")
    for user_id in ("u1", "u2"):
        (question,) = _questions(_reload(stored, user_id=user_id, session_id=session_id))
        assert question["provenance"] == live[user_id]
    # An actor that never asked in this session gets nothing kept for it.
    (question,) = _questions(_reload(stored, user_id="u3", session_id=session_id))
    assert "provenance" not in question
    # The same actor in another session gets nothing either.
    (question,) = _questions(_reload(stored, user_id="u1", session_id=_session()))
    assert "provenance" not in question


# --------------------------------------------------------------------------- #
# Gated parity
# --------------------------------------------------------------------------- #
def test_a_gated_reload_gets_exactly_the_gated_live_frame_and_nothing_ungated():
    names, session_id = SCHEMAS["furnace"], _session()
    gated_calls, open_calls = _calls(names, _prefix()), _calls(names, _prefix())
    gated_frame = _frame(names, session_id, gated_calls, verdict="accepted")
    live_gated = _live_frame(_ask(_runtime_frames(gated_calls, gated_frame, gated=True),
                                  user_id="u1", session_id=session_id,
                                  question=names["question"], gated=True))
    # A later turn streamed with the gate off keeps an ungated frame.
    open_frame = _frame(names, session_id, open_calls)
    _live_frame(_ask(_runtime_frames(open_calls, open_frame, gated=False), user_id="u1",
                     session_id=session_id, question=names["question"]))
    stored = (_stored_turn(names["question"], gated_calls, 0, "g", gated=True)
              + _stored_turn(names["question"], open_calls, 100, "o"))
    payload = _reload(stored, user_id="u1", session_id=session_id, integrity="enforce")
    first, second = _questions(payload)
    assert first["provenance"] == live_gated
    assert {r["verification"] for r in live_gated["records"]} == {"verified"}
    # The gate is on now, so the reload shows the later turn gated: its
    # ungated frame is never shown there.
    assert "provenance" not in second
    assert "HIDDEN_" not in json.dumps(payload)


def test_a_rejected_gated_turn_reloads_unverified_with_its_verdict():
    """A turn the integrity gate rejected, whose system agents each wrote a
    read entry: the enforce emitter's merge leaves every record exactly as
    built (unverified, nothing filled in), the frame carries no per-record
    marker (round 3: one per record cost frame capacity), and the reload
    hands back exactly the frame the live stream sent."""
    from agents import runtime_app
    names, session_id = SCHEMAS["furnace"], _session()
    calls = _calls(names, _prefix())
    built = _frame(names, session_id, calls, verdict="rejected")
    with _source_side_stores():
        for call in calls[1:]:
            _system_agent_reads(session_id, call)
        frame = runtime_app._enriched_provenance_payload(
            json.loads(json.dumps(built)), session_id, verdict="rejected")
    assert frame == built
    assert [r["verification"] for r in frame["records"]] == ["unverified"] * 3
    assert all("verdict" not in r for r in frame["records"])
    live = _live_frame(_ask(_runtime_frames(calls, frame, gated=True), user_id="u1",
                            session_id=session_id, question=names["question"], gated=True))
    assert live == frame
    (question,) = _questions(_reload(
        _stored_turn(names["question"], calls, 0, "rj", gated=True), user_id="u1",
        session_id=session_id, integrity="enforce"))
    assert question["provenance"] == live


# --------------------------------------------------------------------------- #
# Bounds
# --------------------------------------------------------------------------- #
def test_a_session_keeps_only_its_newest_frames():
    from agents import turn_evidence_store as evidence
    names, session_id = SCHEMAS["brewing"], _session()
    turns = evidence.MAX_FRAMES + 3
    stored, live = [], []
    for turn in range(turns):
        calls = _calls(names, _prefix())
        frame = _frame(names, session_id, calls)
        live.append(_live_frame(_ask(_runtime_frames(calls, frame, gated=False),
                                     user_id="u1", session_id=session_id,
                                     question=names["question"])))
        stored += _stored_turn(names["question"], calls, turn * 100, f"b{turn}")
    got = [q.get("provenance") for q in
           _questions(_reload(stored, user_id="u1", session_id=session_id))]
    assert got[:3] == [None, None, None]
    assert got[3:] == live[3:]


def test_an_oversized_or_malformed_frame_is_not_kept():
    names, session_id = SCHEMAS["furnace"], _session()
    big_calls, bad_calls, ok_calls = (_calls(names, _prefix()) for _ in range(3))
    big = _frame(names, session_id, big_calls)
    big["records"][0]["scope"]["filters"] = ["x" * 5000]  # past the builder's own cap
    bad = {"records": [{"citation_num": 1}], "truncated": False}  # no tool id
    for calls, frame in ((big_calls, big), (bad_calls, bad)):
        _ask(_runtime_frames(calls, frame, gated=False), user_id="u1",
             session_id=session_id, question=names["question"])
    ok = _live_frame(_ask(_runtime_frames(ok_calls, _frame(names, session_id, ok_calls),
                                          gated=False),
                          user_id="u1", session_id=session_id, question=names["question"]))
    stored = (_stored_turn(names["question"], big_calls, 0, "big")
              + _stored_turn(names["question"], bad_calls, 100, "bad")
              + _stored_turn(names["question"], ok_calls, 200, "ok"))
    got = [q.get("provenance") for q in
           _questions(_reload(stored, user_id="u1", session_id=session_id))]
    assert got == [None, None, ok]


def test_the_session_item_stays_within_its_byte_bound():
    from agents import turn_evidence_store as evidence
    from agents.provenance import MAX_PAYLOAD_BYTES
    session_id = _session()
    turns = [evidence.turn_identity("q", [f"tooluse_{turn}"])
             for turn in range(evidence.MAX_FRAMES)]
    for turn in range(evidence.MAX_FRAMES):
        records = [{"citation_num": 1, "tool_id": f"tooluse_{turn}", "source_system": "s",
                    "captured_at": CAPTURED_AT, "verification": "partial",
                    "scope": {"filters": ["y" * (MAX_PAYLOAD_BYTES - 400)]}}]
        assert evidence.keep("u1", session_id, {"records": records, "truncated": False},
                             turn=turns[turn], gated=False)
    item = evidence._jobs().get(evidence.item_key("u1", session_id))
    size = len(json.dumps(item["frames"], separators=(",", ":")).encode("utf-8"))
    assert size <= evidence.MAX_SESSION_BYTES
    assert 0 < len(item["frames"]) < evidence.MAX_FRAMES
    # The newest frame is always among those kept.
    assert item["frames"][-1]["turn"] == turns[-1]


class _CountingTable:
    """A DynamoDB Table stand-in that counts its reads."""

    def __init__(self):
        self.items: dict = {}
        self.reads = 0

    def get_item(self, Key):
        self.reads += 1
        item = self.items.get(Key["system_id"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, **_condition):
        self.items[Item["system_id"]] = dict(Item)


def test_a_reload_reads_the_kept_frames_once(monkeypatch):
    from agents import turn_evidence_store as evidence
    from agents.job_store import JobStore
    table = _CountingTable()
    monkeypatch.setattr(evidence, "_job_store", JobStore(
        kind="turn-evidence", table=table, ttl_seconds=evidence.TTL_SECONDS))
    names, session_id = SCHEMAS["brewing"], _session()
    stored = []
    for turn in range(3):
        calls = _calls(names, _prefix())
        _ask(_runtime_frames(calls, _frame(names, session_id, calls), gated=False),
             user_id="u1", session_id=session_id, question=names["question"])
        stored += _stored_turn(names["question"], calls, turn * 100, f"r{turn}")
    table.reads = 0
    questions = _questions(_reload(stored, user_id="u1", session_id=session_id))
    assert all("provenance" in q for q in questions)
    assert table.reads == 1


def test_nothing_is_kept_for_a_turn_that_is_not_stored():
    names, session_id = SCHEMAS["furnace"], _session()
    skipped, kept = _calls(names, _prefix()), _calls(names, _prefix())
    _ask(_runtime_frames(skipped, _frame(names, session_id, skipped), gated=False),
         user_id="u1", session_id=session_id, question=names["question"], write_memory=False)
    live = _live_frame(_ask(_runtime_frames(kept, _frame(names, session_id, kept),
                                            gated=False),
                            user_id="u1", session_id=session_id, question=names["question"]))
    # Even if the skipped turn's events existed, nothing was kept for it.
    stored = (_stored_turn(names["question"], skipped, 0, "s")
              + _stored_turn(names["question"], kept, 100, "k"))
    got = [q.get("provenance") for q in
           _questions(_reload(stored, user_id="u1", session_id=session_id))]
    assert got == [None, live]


# --------------------------------------------------------------------------- #
# Charts: a reopened answer replays the charts its stream sent, and only those
# --------------------------------------------------------------------------- #
CHART_A = ('<chart-block type="line" title="lot-a: peak" labels=\'["t1","t2"]\' '
           'datasets=\'[{"label":"peak","data":[1,2]}]\' />')
CHART_B = ('<chart-block type="line" title="lot-a: dwell" labels=\'["t1","t2"]\' '
           'datasets=\'[{"label":"dwell","data":[3,4]}]\' />')


def _chart_stream(frames: list[dict], text: str) -> list[dict]:
    """``frames`` with the runtime's chart text (``runtime_app``
    ``_wrap_stream_with_chart_injection``) right after the last call's result."""
    last_result = max(i for i, f in enumerate(frames)
                      if "toolResult" in json.dumps(f.get("message", {})))
    return frames[:last_result + 1] + [{"data": text}] + frames[last_result + 1:]


def _stored_chart_turn(question: str, calls: list[dict], start: int, prefix: str,
                       tags: list[str], *, gated: bool = False) -> list:
    """The stored turn with the last call's result holding ``tags``, as the
    session manager stores it (before the hook redacts the live copy)."""
    events = _stored_turn(question, calls, start, prefix, gated=gated)
    events[6] = _event(f"{prefix}7", start + 6, "user", [{"toolResult": {
        "toolUseId": calls[3]["toolUseId"], "status": "success",
        "content": [{"text": "\n".join(["5 rows", *tags])}]}}])
    return events


def _reloaded_charts(payload: dict) -> tuple[list, list]:
    """(each tool result's ``charts``, the questions marked charts_not_shown)."""
    charts = [b.get("charts") for m in payload["messages"] for b in m["blocks"]
              if b["type"] == "tool_result"]
    return charts, [q["content"] for q in _questions(payload) if q.get("charts_not_shown")]


def test_a_reopened_answer_replays_the_charts_its_stream_sent():
    from agents import turn_evidence_store as evidence
    names, session_id = SCHEMAS["furnace"], _session()
    calls = _calls(names, _prefix())
    frames = _chart_stream(_runtime_frames(calls, _frame(names, session_id, calls), gated=False),
                           f"\n\n{CHART_A}\n\n{CHART_B}\n\n")
    sse = _ask(frames, user_id="u1", session_id=session_id, question=names["question"])
    assert CHART_A in "".join(f["data"]["text"] for f in sse if f["event"] == "token")
    stored = _stored_chart_turn(names["question"], calls, 0, "c", [CHART_A, CHART_B])
    charts, marked = _reloaded_charts(_reload(stored, user_id="u1", session_id=session_id))
    assert [c for c in charts if c] == [[CHART_A, CHART_B]] and marked == []
    # The app kept short fingerprints of what it sent, never the tags.
    item = evidence._jobs().get(evidence.item_key("u1", session_id))
    (record,) = item["charts"]
    assert len(record["charts"]) == 2 and "<chart-block" not in json.dumps(item)


def test_a_turn_whose_stream_sent_one_of_two_charts_replays_that_one():
    """A turn stopped after its first chart reached the page: the stored
    result holds both tags, the reload replays the one the page got."""
    names, session_id = SCHEMAS["brewing"], _session()
    calls = _calls(names, _prefix())
    frames = _chart_stream(_runtime_frames(calls, _frame(names, session_id, calls), gated=False),
                           f"\n\n{CHART_A}\n\n")
    _ask(frames, user_id="u1", session_id=session_id, question=names["question"])
    stored = _stored_chart_turn(names["question"], calls, 0, "p", [CHART_A, CHART_B])
    charts, marked = _reloaded_charts(_reload(stored, user_id="u1", session_id=session_id))
    assert [c for c in charts if c] == [[CHART_A]] and marked == []


@pytest.mark.parametrize("integrity", ["report-only", "enforce"])
def test_an_ungated_turn_whose_stream_sent_no_chart_reloads_with_no_notice(integrity):
    """Reproduction (fails on 434f9f0d): a Mode B turn under enforce is not
    gated, and its stream sends no chart (the runtime drops them). The app
    kept no chart record for a stream that sent none, so the reload could not
    tell it from a missing record and showed the 'charts not shown' notice,
    which the live answer never had. A completed turn with calls now keeps
    an empty record, through the production stream and store."""
    from agents import turn_evidence_store as evidence
    names, session_id = SCHEMAS["furnace"], _session()
    calls = _calls(names, _prefix())
    _ask(_runtime_frames(calls, _frame(names, session_id, calls), gated=False),
         user_id="u1", session_id=session_id, question=names["question"])
    item = evidence._jobs().get(evidence.item_key("u1", session_id))
    assert [r["charts"] for r in item.get("charts", [])] == [[]]
    stored = _stored_chart_turn(names["question"], calls, 0, "e", [CHART_A])
    charts, marked = _reloaded_charts(_reload(stored, user_id="u1", session_id=session_id,
                                              integrity=integrity))
    assert all(c is None for c in charts) and marked == []


def test_guard_a_turn_with_no_record_still_says_its_charts_are_not_shown():
    """Guard (passes on 434f9f0d): a stored turn the app kept nothing for (an
    older chat, or a stream stopped before its end) replays no chart, and its
    answer says its charts are not shown."""
    names, session_id = SCHEMAS["furnace"], _session()
    calls = _calls(names, _prefix())
    stored = _stored_chart_turn(names["question"], calls, 0, "n", [CHART_A])
    charts, marked = _reloaded_charts(_reload(stored, user_id="u1", session_id=session_id))
    assert all(c is None for c in charts) and marked == [names["question"]]


@pytest.mark.parametrize("integrity", ["report-only", "enforce"])
def test_a_gated_turn_replays_no_chart_and_no_notice(integrity):
    """The gate's stream sends no chart (the runtime drops them under
    enforce), so the app records none; the reopened turn shows none, with or
    without the gate on now, and no chart notice either: the gated stream
    never mentioned charts, so a notice would say its hidden results held
    some. This test first expected the notice."""
    names, session_id = SCHEMAS["furnace"], _session()
    calls = _calls(names, _prefix())
    frame = _frame(names, session_id, calls, verdict="accepted")
    _ask(_runtime_frames(calls, frame, gated=True), user_id="u1", session_id=session_id,
         question=names["question"], gated=True)
    stored = _stored_chart_turn(names["question"], calls, 0, "g", [CHART_A], gated=True)
    payload = _reload(stored, user_id="u1", session_id=session_id, integrity=integrity)
    charts, marked = _reloaded_charts(payload)
    assert all(c is None for c in charts) and marked == []
    assert "<chart-block" not in json.dumps(payload)


def test_the_newest_turn_keeps_its_chart_within_the_response_bound():
    """Two answered turns, each with one 600 KB chart its stream sent: the
    reload's chart bytes stay within its bound, the newest turn keeps its
    chart and the earlier turn says its charts are not shown."""
    pad = "x" * 600_000
    names, session_id = SCHEMAS["brewing"], _session()
    stored, tags = [], []
    for turn, question in enumerate([names["question"], "Which mash runs ran long?"]):
        calls = _calls(names, _prefix())
        tag = f'<chart-block type="line" title="turn-{turn}" pad="{pad}" />'
        frames = _chart_stream(
            _runtime_frames(calls, _frame(names, session_id, calls), gated=False), tag)
        _ask(frames, user_id="u1", session_id=session_id, question=question)
        stored += _stored_chart_turn(question, calls, turn * 100, f"n{turn}", [tag])
        tags.append(tag)
    charts, marked = _reloaded_charts(_reload(stored, user_id="u1", session_id=session_id))
    assert [c for c in charts if c] == [[tags[1]]]
    assert marked == [names["question"]]


# --------------------------------------------------------------------------- #
# Shared fixture: the live stream and the reload payload of four sessions,
# read by the chat's test (frontend/src/test/session-reload-evidence.test.tsx)
# so the two halves test one payload. Regenerate after a deliberate change with
#   python tests/unit/test_reload_evidence_cards.py --write-fixture
# --------------------------------------------------------------------------- #
_FIXTURE = (_ADT_ROOT / "frontend" / "src" / "test" / "fixtures"
            / "session-reload-evidence.json")
_FIXTURE_ABOUT = ("The /api/ask SSE frames of one live turn and the GET /api/memory/events "
                  "output for its reopened session; written by "
                  "tests/unit/test_reload_evidence_cards.py, read by "
                  "src/test/session-reload-evidence.test.tsx.")


def _scenario(schema: str, *, gated: bool = False, kept: bool = True,
              two_frames: bool = False, api_read: bool = False) -> dict:
    """One session's live SSE and reload payload. ``kept=False`` is a session
    stored before the app kept frames: its turn was never asked through this
    code, so there is no live stream to show and nothing kept. ``two_frames``
    streams the turn's frame in two parts (:func:`_two_frames`); ``api_read``
    streams the frame of a turn whose calls read their source through its API
    (:func:`_api_read_frame`)."""
    names = SCHEMAS[schema]
    label = ("api" if api_read else "two" if two_frames
             else ("gated" if gated else "open"))
    session_id = f"sess-{schema}-{label}-{'kept' if kept else 'legacy'}"
    calls = _calls(names, f"{schema[:3]}{label[0]}{'k' if kept else 'l'}")
    sse = None
    if kept:
        if api_read:
            frame = _api_read_frame(names, session_id, calls)
        elif two_frames:
            frame = _two_frames(names, session_id, calls)
        else:
            frame = _frame(names, session_id, calls, verdict="accepted" if gated else None)
        sse = _ask(_runtime_frames(calls, frame, gated=gated), user_id="u-fixture",
                   session_id=session_id, question=names["question"], gated=gated)
    payload = _reload(_stored_turn(names["question"], calls, 0, "f", gated=gated),
                      user_id="u-fixture", session_id=session_id,
                      integrity="enforce" if gated else "report-only")
    return {"question": names["question"], "live": sse, "payload": payload}


def _fixture_payloads() -> dict:
    # Fixed session ids: a re-run keeps each turn's frame again, replacing
    # the one kept before, so the output is the same on every run.
    return {"about": _FIXTURE_ABOUT, "scenarios": {
        "open": _scenario("furnace"),
        "renamed": _scenario("brewing"),
        "two_frames": _scenario("brewing", two_frames=True),
        "gated": _scenario("furnace", gated=True),
        "api_read": _scenario("furnace", api_read=True),
        "legacy": _scenario("brewing", kept=False),
    }}


def test_shared_fixture_is_the_live_and_reload_output():
    assert json.loads(_FIXTURE.read_text()) == _fixture_payloads()


if __name__ == "__main__" and "--write-fixture" in sys.argv:
    _FIXTURE.write_text(json.dumps(_fixture_payloads(), indent=2) + "\n")
