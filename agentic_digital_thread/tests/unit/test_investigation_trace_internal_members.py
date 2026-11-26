# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The evidence card's Investigation trace never shows an internal member.

Seen on a live walk (10-27): an SQL answer's evidence card had a collapsible
Investigation trace whose query step showed the tool output as the system
agent's model saw it, including the engine's own members: ``_adt_source``
(the response-integrity capture, ``attach_source_metadata``) and
``_adt_executed_query`` (the SQL system agents). The card reads
``GET /api/admin/agents/traces/investigations/{query_ref}``
(``agents_trace_routes.get_investigation_trace``), which returned the stored
step payloads as recorded. The write side strips ``_adt_*`` keys only at the
top of a dict payload (``investigation_trace.serialize_tool_payload``), and a
tool result is a list of content blocks whose text holds the JSON, so the
members rode through.

Pinned here, through the real capture path (``investigation_trace.record_step``
on strands-shaped results), the real store and the real route:

* a recorded step's tool output and input as the route returns them hold no
  ``_adt_`` member in any form a step records: a content-block list whose
  text is JSON, a JSON object (formatting kept byte for byte), text cut at
  the size limit inside a member or inside its key, and a Python repr;
* where the text parses, only the members go and every other byte stays;
  where it does not, the text is cut before the first internal member and the
  step says its output was truncated, with the full size the writer recorded;
* text with no internal member (prose, a value that merely contains the
  letters, a key that does not start with the prefix) comes back unchanged;
* the executed SQL is not repeated in the trace: the card already shows it
  as executed under Technical detail, which redeems the same ``query_ref``;
* display only: the stored entry is never rewritten; under
  ``RESPONSE_INTEGRITY_MODE=enforce`` and to a non-admin the route returns
  nothing it did not return before.

Systems, tables, columns and ids are made up; no AWS, no graph.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import agents.agents_trace_routes as atr
import agents.investigation_trace as it
import agents.investigation_trace_store as its
import agents.provenance_store as provenance_store
from agents import db
from agents.job_store import JobStore
from agents.provenance import mint_query_ref

_SID = "sess-card.keys"
_ASKED = "SELECT lot_code, reading FROM bench_readings WHERE lot_code = 'LOT-77'"
_EXECUTED = _ASKED + " LIMIT 500"
_SOURCE = {"evidence_id": "info-0f0f0f0f0f0f0f0f", "system_id": "sys-lambda",
           "boundary": "sql", "tool_name": "", "queried_at": "2026-01-02T03:04:05Z",
           "freshness": "unknown", "completeness": "complete", "row_count": 1,
           "item_count": None}
_ANSWER = "Lot LOT-77 has one reading."


def _admin(is_admin: bool = True):
    return SimpleNamespace(state=SimpleNamespace(is_admin=is_admin))


def _payload(rows=None) -> dict:
    """An SQL tool result as the system agent's model sees it: the rows, then
    the source metadata, then the executed query, in the order they are
    attached."""
    rows = rows if rows is not None else [["LOT-77", 3]]
    return {"columns": ["lot_code", "reading"], "rows": rows, "row_count": len(rows),
            "_adt_source": dict(_SOURCE), "_adt_executed_query": _EXECUTED}


def _public(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if not k.startswith("_adt_")}


def _compact(value) -> str:
    return json.dumps(value, separators=(",", ":"))


def _record(result, tool_input=None, tool_id: str = "toolu_keys") -> dict:
    """ONE step recorded by the real capture path for a strands result."""
    state: dict = {}
    it.record_step(state, tool_use={"toolUseId": tool_id, "name": "query_sys_lambda",
                                    "input": tool_input or {"sql": _ASKED}},
                   result=result, exception=None, retry=False, messages=[])
    steps, _ = it.collect_trace_steps(state)
    return steps[0]


def _text_result(text: str) -> dict:
    return {"status": "success", "content": [{"text": text}]}


def _seed(step: dict, tool_id: str = "toolu_keys") -> str:
    ref = mint_query_ref(_SID, tool_id)
    entry = it.build_trace_entry(
        session_id=_SID, query_ref=ref, system_id="sys-lambda",
        question="Which readings does lot LOT-77 have?", kg_context_attached=False,
        steps=[step], steps_dropped=0, final_answer=_ANSWER)
    assert its.put_trace(_SID, ref, entry) is True
    return ref


def _card(ref: str) -> dict:
    return atr.get_investigation_trace(ref, _admin(), session_id=_SID)


def _padded(target: int) -> "tuple[str, int]":
    """The recorded output text of a one-row result padded so that its first
    internal member starts at ``target`` characters (all ASCII, so characters
    are bytes), and that start."""
    pad = 10
    for _ in range(2):
        text = it.serialize_tool_payload(
            [{"text": _compact(_payload([["LOT-77", "x" * pad]]))}])
        start = text.index('\\"_adt_source')
        pad += target - start
    return text, text.index('\\"_adt_source')


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(its, "_job_store", JobStore(its._KIND, table=None))
    monkeypatch.setattr(provenance_store, "_job_store",
                        JobStore(provenance_store._KIND, table=None))
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.setattr(db, "neptune_query",
                        lambda *a, **k: pytest.fail("graph read by the trace route"))


# --------------------------------------------------------------------------- #
# Reproductions
# --------------------------------------------------------------------------- #

def test_the_walk_shape_shows_the_rows_without_the_internal_members():
    """Reproduction (fails on 970238d3, which returned the members): the SQL
    step of the walk, a content block whose text is the JSON result, comes
    back as the same block with the result's own members only."""
    ref = _seed(_record(_text_result(_compact(_payload()))))
    step = _card(ref)["steps"][0]
    assert step["tool_output"] == it.serialize_tool_payload(
        [{"text": _compact(_public(_payload()))}])
    assert "_adt_" not in step["tool_output"]
    assert "output_truncated" not in step


def test_a_json_object_keeps_every_other_byte_as_recorded():
    """Reproduction (fails on 970238d3): a JSON object, here indented and with
    an internal member first, in the middle and nested, loses only those
    members; its formatting is kept as the tool wrote it."""
    payload = {"_adt_source": dict(_SOURCE), "lot_code": "LOT-77",
               "_adt_note": "engine", "detail": {"reading": 3.50, "_adt_x": 1},
               "unit": "bar"}
    expected = {"lot_code": "LOT-77", "detail": {"reading": 3.50}, "unit": "bar"}
    ref = _seed(_record(json.dumps(payload, indent=2)))
    assert _card(ref)["steps"][0]["tool_output"] == json.dumps(expected, indent=2)


def test_a_json_block_inside_the_content_list_loses_its_members():
    """Reproduction (fails on 970238d3): a ``json`` content block, whose
    object the write side does not reach, loses its internal members too."""
    result = {"status": "success", "content": [{"json": _payload()}]}
    ref = _seed(_record(result))
    assert _card(ref)["steps"][0]["tool_output"] == _compact([{"json": _public(_payload())}])


def test_output_cut_at_the_size_limit_inside_a_member_is_cut_before_it():
    """Reproduction (fails on 970238d3): the recorded output was cut at the
    4096-byte limit in the middle of ``_adt_source``, so it does not parse.
    It is cut again before the member, the trailing comma dropped, and the
    step still says the output was truncated, with the full size."""
    full, start = _padded(it.MAX_STEP_OUTPUT_BYTES - 40)
    step_in = _record(full)
    assert step_in["output_truncated"] is True
    step = _card(_seed(step_in))["steps"][0]
    assert step["tool_output"] == full[:start].rstrip(",")
    assert step["output_truncated"] is True
    assert step["output_bytes"] == len(full)
    assert "_adt_" not in step["tool_output"]


def test_output_cut_inside_a_member_key_shows_no_part_of_the_key():
    """Reproduction (fails on 970238d3): the limit fell four characters into
    the escaped key, leaving ``\\"_ad`` at the end. No part of the key is
    shown."""
    full, start = _padded(it.MAX_STEP_OUTPUT_BYTES - 5)
    step_in = _record(full)
    assert step_in["tool_output"].endswith('\\"_ad')
    step = _card(_seed(step_in))["steps"][0]
    assert step["tool_output"] == full[:start].rstrip(",")
    assert step["output_truncated"] is True
    assert not step["tool_output"].endswith(("_", "_a", "_ad"))


def test_a_python_repr_is_cut_before_the_first_internal_member():
    """Reproduction (fails on 970238d3): a result json.dumps cannot write
    (here a datetime) reaches the model as its Python repr (strands
    ``str(result)``). A repr does not parse as JSON, so the recorded output
    (the content list holding that text) is cut before the first internal
    member, the step marked truncated: what is shown is a prefix of what was
    recorded, not a re-closed list around a shortened text."""
    result = {"rows": [["LOT-9", 2]], "read_at": datetime(2026, 1, 2, 3, 4, 5),
              "_adt_source": dict(_SOURCE)}
    step_in = _record(_text_result(str(result)))
    recorded = step_in["tool_output"]
    step = _card(_seed(step_in))["steps"][0]
    assert step["tool_output"] == recorded[:recorded.index("'_adt_source'")].rstrip(", ")
    assert recorded.startswith(step["tool_output"])
    assert step["output_truncated"] is True
    assert step["output_bytes"] == step_in["output_bytes"]


@pytest.mark.parametrize(("payload", "expected"), [
    ('{ "\\u005fadt_source": {"secret": 1}, "ok": 2 }', '{ "ok": 2 }'),
    ('{"ok": 2, "inner": [{"_\\u0061dt_x": 1, "k": 3}]}', '{"ok": 2, "inner": [{"k": 3}]}'),
    ('{"ok": 2, "\\u005F\\u0061\\u0064\\u0074\\u005Fy": 1}', '{"ok": 2}'),
])
def test_an_escaped_internal_key_is_removed_too(payload, expected):
    """Reproduction (fails on d8060922, whose raw-text gate skipped text with
    no literal prefix): a key written with JSON escapes decodes to an
    internal key and is removed like a literal one, at the top level, nested,
    and with every letter escaped."""
    step = _card(_seed(_record(payload)))["steps"][0]
    assert step["tool_output"] == expected
    assert "output_truncated" not in step


def test_an_escaped_internal_key_inside_block_text_is_removed_too():
    """Reproduction (fails on d8060922): the same, inside the JSON text of a
    content block, where the escape is itself escaped in the recorded list."""
    text = '{"ok": 2, "\\u005fadt_source": {"row_count": 1}}'
    step = _card(_seed(_record(_text_result(text))))["steps"][0]
    assert step["tool_output"] == it.serialize_tool_payload([{"text": '{"ok": 2}'}])
    assert "output_truncated" not in step


def test_unparsed_text_with_an_escaped_internal_key_is_cut_before_it():
    """Reproduction (fails on d8060922): text that does not parse (a repr-like
    tail) holding an escaped internal key is cut before that key."""
    text = "{'ok': 2, '\\u005fadt_source': {'row_count': 1}"
    step = _card(_seed(_record(text)))["steps"][0]
    assert step["tool_output"] == "{'ok': 2"
    assert step["output_truncated"] is True


def test_an_internal_member_in_a_recorded_input_is_removed_too():
    """Reproduction (fails on 970238d3): the write side strips top-level
    ``_adt_*`` input keys only; a nested one is removed for display as well."""
    step_in = _record(_text_result("[]"),
                      tool_input={"sql": _ASKED, "options": {"_adt_hint": "x", "limit": 5}})
    step = _card(_seed(step_in))["steps"][0]
    assert step["tool_input"] == _compact({"sql": _ASKED, "options": {"limit": 5}})


def test_the_executed_query_is_shown_once_under_technical_detail():
    """Reproduction (fails on 970238d3): the trace step repeated the SQL as
    executed under its internal key. The card's Technical detail redeems the
    same ``query_ref`` and shows it as executed there, so the trace keeps only
    the SQL the agent sent (its tool input)."""
    ref = _seed(_record(_text_result(_compact(_payload()))))
    assert provenance_store.put_turn_queries(_SID, {ref: {
        "system_id": "sys-lambda", "tool_name": "query_sys_lambda",
        "query_text": _EXECUTED, "kg_artifact_ids": [],
        "captured_at": "2026-01-02T03:04:05Z"}}) == 1
    trace = _card(ref)
    assert _EXECUTED not in json.dumps(trace)
    assert _ASKED in trace["steps"][0]["tool_input"]
    assert atr.get_provenance_query(ref, _admin(), session_id=_SID)["executed_query"] == _EXECUTED


# --------------------------------------------------------------------------- #
# Guards (pass on 970238d3)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    "The bench reported no readings for that lot.",
    "Field _adt_source was not in the reply.",
    _compact({"note": "_adt_source", "via_adt_link": 1, "__adt_source_time__": "t"}),
])
def test_guard_text_with_no_internal_member_is_returned_as_recorded(text):
    """Guard: prose, a value that only contains the letters, and keys that do
    not start with the prefix come back byte for byte, with no flag."""
    step_in = _record(_text_result(text))
    ref = _seed(step_in)
    assert _card(ref) == its.get_trace(_SID, ref)


def test_guard_the_stored_entry_is_never_rewritten():
    """Guard: display only. The store still holds what the capture wrote,
    and every field but the step payloads is returned as stored."""
    ref = _seed(_record(_text_result(_compact(_payload()))))
    stored = json.loads(json.dumps(its.get_trace(_SID, ref)))
    out = _card(ref)
    assert its.get_trace(_SID, ref) == stored
    assert "_adt_source" in stored["steps"][0]["tool_output"]
    assert {k: v for k, v in out.items() if k != "steps"} == \
        {k: v for k, v in stored.items() if k != "steps"}
    assert {k: v for k, v in out["steps"][0].items() if k != "tool_output"} == \
        {k: v for k, v in stored["steps"][0].items() if k != "tool_output"}


def test_guard_under_the_integrity_gate_nothing_is_added(monkeypatch):
    """Guard: under ``RESPONSE_INTEGRITY_MODE=enforce`` the route returns the
    stored fields only (no names, no lookup), and no step payload is longer
    than stored."""
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    ref = _seed(_record(_text_result(_compact(_payload()))))
    stored = its.get_trace(_SID, ref)
    out = _card(ref)
    assert set(out) == set(stored)
    for got, had in zip(out["steps"], stored["steps"]):
        assert set(got) - set(had) <= {"input_truncated", "output_truncated"}
        assert len(got["tool_output"]) <= len(had["tool_output"])
        assert len(got["tool_input"]) <= len(had["tool_input"])


def test_guard_a_non_admin_is_refused_before_any_read(monkeypatch):
    """Guard: the admin check runs first; no store read happens."""
    ref = _seed(_record(_text_result(_compact(_payload()))))
    reads: list = []
    monkeypatch.setattr(its, "get_trace", lambda *a: reads.append(a))
    with pytest.raises(HTTPException) as exc:
        atr.get_investigation_trace(ref, _admin(is_admin=False), session_id=_SID)
    assert exc.value.status_code == 403
    assert reads == []


def test_guard_logging_stays_digest_only(caplog):
    """Guard: the route logs no payload text, before or after display."""
    ref = _seed(_record(_text_result(_compact(_payload()))))
    with caplog.at_level(logging.DEBUG):
        _card(ref)
    text = "\n".join(r.getMessage() for r in caplog.records
                     if r.name.startswith("agents.agents_trace_routes"))
    for fragment in ("_adt_", "LOT-77", "SELECT", ref, _SID):
        assert fragment not in text


def test_non_ascii_text_keeps_every_other_character():
    """Reproduction (fails on 970238d3): a member is removed from text holding
    raw non-ASCII characters without re-encoding anything else."""
    text = '{"bench": "Prüfstand Nord", "_adt_source": {"row_count": 1}}'
    step = _card(_seed(_record(text)))["steps"][0]
    assert step["tool_output"] == '{"bench": "Prüfstand Nord"}'


# --------------------------------------------------------------------------- #
# The helper's edges (trace_display is new with this change, so these are
# not reproductions; imported in each test so the reproductions above still
# run against 970238d3)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(("text", "shown"), [
    ('{"_adt_a": 1}', "{}"),
    ('{"_adt_a":1,"_adt_b":2,"k":3}', '{"k":3}'),
    ('{"k":3,"_adt_a":1,"_adt_b":2}', '{"k":3}'),
    ('{"k":3,"_adt_a":1,"m":4,"_adt_b":2}', '{"k":3,"m":4}'),
    ('[{"_adt_a":1}, 2]', "[{}, 2]"),
    ('{"k": "_adt_a"}', '{"k": "_adt_a"}'),
])
def test_helper_removes_members_and_keeps_separators(text, shown):
    from agents.trace_display import public_payload
    assert public_payload(text) == (shown, False)


def test_helper_never_raises_and_cuts_text_it_cannot_walk():
    from agents.trace_display import public_entry, public_payload
    deep = "[" * 3000 + '{"_adt_a":1}' + "]" * 3000
    shown, cut = public_payload(deep)
    assert cut is True and "_adt_" not in shown and deep.startswith(shown)
    assert public_payload(None) == (None, False)
    assert public_payload(7) == (7, False)
    entry = {"steps": [{"tool_output": "rows: 3"}, "not a step"]}
    assert public_entry(entry) is entry
    assert public_entry({"steps": None}) == {"steps": None}
