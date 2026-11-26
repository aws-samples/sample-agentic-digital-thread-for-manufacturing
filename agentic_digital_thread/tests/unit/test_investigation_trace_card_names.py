# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The evidence card's Investigation trace names ids like the chat trace.

Seen on a live walk (10-27): a telemetry answer's evidence card showed its
final answer, the system agent's reply as sent, with bare asset and property
UUIDs in its ``[engine] Telemetry samples, not every value: ...`` line, while
the chat trace just above it showed the same reply with graph-known ids named,
``Name (idprefix...)`` (``chat_client._summarize_tool_result``,
``id_name_resolver.annotate_ids_in_text``). The card reads
``GET /api/admin/agents/traces/investigations/{query_ref}``
(``agents_trace_routes.get_investigation_trace``), which returned the stored
entry as is.

A name cannot be made literal by editing the answer's markdown: a name in
URL, ``www`` or email form, or one that reads as a UTC time, needs no escape
and is still turned into a link or a ``<time>`` when the card parses it. So
the answer text is not edited. Pinned here:

* the route returns the stored ``final_answer`` unchanged plus
  ``final_answer_names``, the graph names of the ids in it, as the graph gave
  them; the card applies them after parsing. The cases are shared with the
  UI test that renders them
  (``frontend/src/test/fixtures/investigation-trace-names-cases.json``);
* the field is bounded: at most one lookup, at most one lookup's worth of
  ids, and no name longer than ``MAX_ANSWER_NAME_CHARS``;
* ``annotate_text`` is one pass, so a name holding another known id is shown
  exactly as the graph gave it, in the chat trace as on the card;
* display only: the stored entry and every other field are returned as
  stored; under ``RESPONSE_INTEGRITY_MODE=enforce`` and to a non-admin the
  route returns exactly what it returned before, with no name lookup;
* fail-soft: an unknown id, a failed lookup or no id at all adds no field.

Ids and names are made up; no AWS, no graph (the graph read is faked).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import agents.agents_trace_routes as atr
import agents.id_name_resolver as idr
import agents.investigation_trace as it
import agents.investigation_trace_store as its
from agents import db
from agents.job_store import JobStore
from agents.provenance import mint_query_ref

_SID = "sess-card.names"
_ASSET = "5f2c8e1a-0b3d-4c6e-9a7f-1d2e3f4a5b6c"
_OTHER = "0c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f"

_CASES = json.loads((Path(__file__).resolve().parents[2] / "frontend" / "src" / "test"
                     / "fixtures" / "investigation-trace-names-cases.json")
                    .read_text(encoding="utf-8"))["cases"]
_PARITY = [c for c in _CASES if "chat_trace" in c]


def _admin(is_admin: bool = True):
    return SimpleNamespace(state=SimpleNamespace(is_admin=is_admin))


def _step(text_id: str = _ASSET) -> dict:
    tool_input = json.dumps({"asset_id": text_id})
    tool_output = json.dumps([{"asset_id": text_id, "value": 4.2}])
    return {"seq": 1, "reasoning": f"Reading asset {text_id} first.",
            "tool_name": "get_asset_property_value_history",
            "tool_input": tool_input, "input_bytes": len(tool_input),
            "tool_output": tool_output, "output_bytes": len(tool_output),
            "status": "success", "duration_ms": 40, "started_at": "2026-10-27T15:00:00Z"}


def _seed(answer: str, tool_id: str = "toolu_card") -> str:
    ref = mint_query_ref(_SID, tool_id)
    entry = it.build_trace_entry(
        session_id=_SID, query_ref=ref, system_id="sys-kappa",
        question="How did the inlet pressure move?", kg_context_attached=False,
        steps=[_step()], steps_dropped=0, final_answer=answer)
    assert its.put_trace(_SID, ref, entry) is True
    return ref


def _graph(monkeypatch, names: dict, calls: "list | None" = None) -> list:
    """Fake the graph read behind the resolver's default seam."""
    seen = [] if calls is None else calls

    def _read(cypher, parameters=None):
        asked = list((parameters or {}).get("ids", []))
        seen.append(asked)
        return [{"id": i, "name": names[i]} for i in asked if i in names]

    monkeypatch.setattr(db, "neptune_query", _read)
    return seen


def _fake_query(names: dict):
    def _q(cypher, params=None):
        return [{"id": i, "name": names[i]} for i in (params or {}).get("ids", []) if i in names]
    return _q


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(its, "_job_store", JobStore(its._KIND, table=None))
    monkeypatch.setattr(idr, "_CACHE", {})
    monkeypatch.setattr(idr, "_PAUSED_UNTIL", 0.0)
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)


def _card(ref: str) -> dict:
    return atr.get_investigation_trace(ref, _admin(), session_id=_SID)


# --------------------------------------------------------------------------- #
# Reproductions
# --------------------------------------------------------------------------- #

def test_the_route_returns_the_sample_line_names_beside_the_stored_answer(monkeypatch):
    """Reproduction (fails on fa019260, which returned no names, and on
    167a40e5, which rewrote the answer text): the route returns the stored
    answer unchanged and the graph names of its ids in their own field."""
    case = next(c for c in _CASES if c["id"] == "sample-line")
    calls = _graph(monkeypatch, case["names"])
    ref = _seed(case["answer"])
    out = _card(ref)
    assert out["final_answer"] == case["answer"]
    assert out["final_answer_names"] == case["names"]
    assert len(calls) == 1


@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_every_case_gets_its_names_as_the_graph_gave_them(monkeypatch, case):
    """Reproduction (fails on fa019260 and 167a40e5): for every shared case,
    URL, ``www``, email and UTC-time names included, the answer is returned
    as stored and each name exactly as the graph gave it, so the card can
    apply it after parsing, where it cannot become markup, a link or a time."""
    _graph(monkeypatch, case["names"])
    ref = _seed(case["answer"])
    out = _card(ref)
    assert out["final_answer"] == case["answer"]
    assert out["final_answer_names"] == {
        i: n for i, n in case["names"].items() if i in case["answer"]}


@pytest.mark.parametrize("case", _PARITY, ids=[c["id"] for c in _PARITY])
def test_the_chat_trace_names_the_shared_cases_as_the_card_shows_them(case):
    """Reproduction (``name-holding-another-id`` fails on fa019260 and
    167a40e5): ``annotate_text`` replaced ids one after another, so when one
    id's name held another known id the chat trace named that id inside the
    name too. It is one pass now. The UI test asserts the card shows the same
    ``chat_trace`` text for each of these cases."""
    q = _fake_query(case["names"])
    assert idr.annotate_ids_in_text(case["answer"], query=q) == case["chat_trace"]


def test_the_names_field_is_bounded(monkeypatch):
    """Reproduction (fails on fa019260 and 167a40e5, which have no field):
    one lookup at most, at most one lookup's worth of ids in the field, and
    a name longer than ``MAX_ANSWER_NAME_CHARS`` is left out (its id stays)."""
    ids = [f"{n:08x}-0b3d-4c6e-9a7f-1d2e3f4a5b6c" for n in range(60)]
    names = {i: f"Rig {k}" for k, i in enumerate(ids)}
    names[ids[1]] = "R" * (atr.MAX_ANSWER_NAME_CHARS + 1)
    calls = _graph(monkeypatch, names)
    ref = _seed(" ".join(ids))
    out = _card(ref)["final_answer_names"]
    assert len(calls) == 1
    assert list(out) == [ids[0], *ids[2:idr._MAX_IDS_PER_LOOKUP]]


def test_names_in_text_is_one_lookup_and_fail_soft():
    """Reproduction (fails on fa019260 and 167a40e5, which have no such
    helper): the ids' names in first-appearance order, ``{}`` on a failed
    lookup or a non-string."""
    q = _fake_query({_ASSET: "Test Rig 4"})
    assert idr.names_in_text(f"{_OTHER} then {_ASSET}", query=q) == {_ASSET: "Test Rig 4"}

    def _boom(cypher, params=None):
        raise RuntimeError("graph down")

    idr._CACHE.clear()
    assert idr.names_in_text(f"asset {_ASSET}", query=_boom) == {}
    assert idr.names_in_text(None) == {}  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Guards (pass on fa019260; the first also failed on 167a40e5, which
# returned a rewritten answer)
# --------------------------------------------------------------------------- #

def test_guard_the_stored_entry_and_the_steps_are_never_rewritten(monkeypatch):
    """Guard: display only. The store keeps the entry as written, and every
    stored field, the answer and the steps' raw ids included, is returned
    as stored."""
    case = next(c for c in _CASES if c["id"] == "sample-line")
    _graph(monkeypatch, case["names"])
    ref = _seed(case["answer"])
    stored = json.loads(json.dumps(its.get_trace(_SID, ref)))
    out = _card(ref)
    assert its.get_trace(_SID, ref) == stored
    assert {k: v for k, v in out.items() if k != "final_answer_names"} == stored
    assert _ASSET in out["steps"][0]["reasoning"]


def test_guard_under_the_integrity_gate_the_route_returns_the_stored_entry(monkeypatch):
    """Guard: under ``RESPONSE_INTEGRITY_MODE=enforce`` the route returns the
    stored entry unchanged and sends no name lookup."""
    case = next(c for c in _CASES if c["id"] == "sample-line")
    calls = _graph(monkeypatch, case["names"])
    ref = _seed(case["answer"])
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    assert _card(ref) == its.get_trace(_SID, ref)
    assert calls == []


def test_guard_a_non_admin_is_refused_before_any_name_lookup(monkeypatch):
    """Guard: the admin check still runs first; a non-admin gets 403 and no
    lookup or store read happens."""
    case = next(c for c in _CASES if c["id"] == "sample-line")
    calls = _graph(monkeypatch, case["names"])
    ref = _seed(case["answer"])
    reads: list = []
    monkeypatch.setattr(its, "get_trace", lambda *a: reads.append(a))
    with pytest.raises(HTTPException) as exc:
        atr.get_investigation_trace(ref, _admin(is_admin=False), session_id=_SID)
    assert exc.value.status_code == 403
    assert calls == [] and reads == []


def test_guard_an_unknown_id_or_a_failed_lookup_adds_no_names(monkeypatch):
    """Guard: fail-soft. An id the graph does not know, and a lookup that
    fails, both return the stored entry as is."""
    _graph(monkeypatch, {_ASSET: "Test Rig 4"})
    ref = _seed(f"Asset {_OTHER} reported a fault.", "toolu_unknown")
    assert _card(ref) == its.get_trace(_SID, ref)

    def _down(cypher, parameters=None):
        raise RuntimeError("graph down")

    monkeypatch.setattr(idr, "_CACHE", {})
    monkeypatch.setattr(db, "neptune_query", _down)
    ref = _seed(f"Asset {_ASSET} reported a fault.", "toolu_down")
    assert _card(ref) == its.get_trace(_SID, ref)


def test_guard_an_answer_with_no_id_sends_no_lookup(monkeypatch):
    """Guard: an answer with no id is returned as stored and costs no lookup."""
    calls = _graph(monkeypatch, {_ASSET: "Test Rig 4"})
    ref = _seed("Seven open orders reference that part.")
    assert _card(ref) == its.get_trace(_SID, ref)
    assert calls == []
