# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Investigation-trace store (Wave 6, Task A2 — W6-F2 store pins).

Offline pins for ``agents/investigation_trace_store.py``:

* round-trip: ``put_trace`` then ``get_trace`` returns the normalized W6-F2
  entry; the raw backend key is EXACTLY ``investigation-trace:{sid}:{ref}``;
* invalid ref/session shapes and non-dict entries ⇒ ``False``/``None``,
  never a raise;
* ABSENT-KEY LEGACY PIN: a never-written key reads ``None`` (the route's
  404 source);
* KIND-ISOLATION PIN: a Wave-5 ``provenance-query`` entry under the same
  ``(sid, ref)`` on the SAME table does NOT satisfy ``get_trace``;
* write-side defense-in-depth: hostile oversize/malformed entries are
  re-bounded through the A1 caps home (flags preserved, 64 KB ratchet);
* caplog hygiene: no reasoning/input/output/answer fragment in any record —
  digests and counts only.
"""
from __future__ import annotations

import logging

import pytest

import agents.investigation_trace as it
import agents.investigation_trace_store as its
import agents.provenance_store as provenance_store
from agents.job_store import JobStore
from agents.provenance import mint_query_ref

SID = "sess-w6.1"
REF = mint_query_ref(SID, "tooluse_W6")

REASONING = "Looking at the open orders table first."
SQL_INPUT = '{"sql":"SELECT part_no FROM orders"}'
OUTPUT = '[{"part_no":"B-2847-X"}]'
ANSWER = "Seven open orders reference that part."
QUESTION = "How many open orders reference part B-2847-X?"


class _FakeTable:
    """Minimal DynamoDB Table stand-in (shared across kinds — the isolation
    pin needs both stores on ONE table)."""

    def __init__(self):
        self.items: dict[str, dict] = {}

    def put_item(self, Item):
        self.items[Item["system_id"]] = Item

    def get_item(self, Key):
        item = self.items.get(Key["system_id"])
        return {"Item": item} if item else {}

    def delete_item(self, Key):
        self.items.pop(Key["system_id"], None)


@pytest.fixture()
def fake_table(monkeypatch):
    table = _FakeTable()
    monkeypatch.setattr(its, "_job_store", JobStore(its._KIND, table=table))
    monkeypatch.setattr(
        provenance_store, "_job_store",
        JobStore(provenance_store._KIND, table=table))
    return table


@pytest.fixture(autouse=True)
def _memory_store(monkeypatch):
    monkeypatch.setattr(its, "_job_store", JobStore(its._KIND, table=None))


def _step(seq: int = 1, **overrides) -> dict:
    step = {"seq": seq, "reasoning": REASONING, "tool_name": "execute_sql",
            "tool_input": SQL_INPUT, "input_bytes": len(SQL_INPUT),
            "tool_output": OUTPUT, "output_bytes": len(OUTPUT),
            "status": "success", "duration_ms": 42,
            "started_at": "2026-08-04T18:00:00Z"}
    step.update(overrides)
    return step


def _entry(**overrides) -> dict:
    entry = it.build_trace_entry(
        session_id=SID, query_ref=REF, system_id="erp", question=QUESTION,
        kg_context_attached=True, steps=[_step()], steps_dropped=0,
        final_answer=ANSWER)
    entry.update(overrides)
    return entry


# --------------------------------------------------------------------------- #
# Round-trip + raw key spelling
# --------------------------------------------------------------------------- #

def test_round_trip_returns_normalized_entry():
    assert its.put_trace(SID, REF, _entry()) is True
    out = its.get_trace(SID, REF)
    assert out is not None
    assert out["origin"] == "system-agent"
    assert out["question"] == QUESTION
    assert out["kg_context_attached"] is True
    assert out["final_answer"] == ANSWER
    assert out["steps_dropped"] == 0
    assert len(out["steps"]) == 1
    step = out["steps"][0]
    assert step["reasoning"] == REASONING
    assert step["tool_input"] == SQL_INPUT
    assert step["tool_output"] == OUTPUT
    assert step["duration_ms"] == 42


def test_backend_key_is_kind_prefixed_sid_colon_ref(fake_table):
    assert its.put_trace(SID, REF, _entry()) is True
    # JobStore spells backend keys "__job__:{kind}:{key}" — the key half is
    # EXACTLY f"{sid}:{ref}" under the investigation-trace kind (W6-F1).
    assert f"__job__:investigation-trace:{SID}:{REF}" in fake_table.items


# --------------------------------------------------------------------------- #
# Invalid shapes — False/None, never a raise
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad_ref", ["", "q-XYZ", "q-" + "a" * 13,
                                     "notaref", None, 42])
def test_invalid_ref_rejected(bad_ref):
    assert its.put_trace(SID, bad_ref, _entry()) is False
    assert its.get_trace(SID, bad_ref) is None


@pytest.mark.parametrize("bad_sid", ["", "a b", "s|1", None, "s" * 257])
def test_invalid_session_rejected(bad_sid):
    assert its.put_trace(bad_sid, REF, _entry()) is False
    assert its.get_trace(bad_sid, REF) is None


@pytest.mark.parametrize("bad_entry", [None, "text", 42, ["x"]])
def test_non_dict_entry_rejected(bad_entry):
    assert its.put_trace(SID, REF, bad_entry) is False
    assert its.get_trace(SID, REF) is None


# --------------------------------------------------------------------------- #
# Absent-key legacy pin + kind isolation
# --------------------------------------------------------------------------- #

def test_never_written_key_reads_none():
    assert its.get_trace(SID, mint_query_ref(SID, "never-written")) is None


def test_wave5_query_entry_does_not_satisfy_trace_read(fake_table):
    """Kind isolation: same (sid, ref), same table — the Wave-5 entry lives
    under 'provenance-query:', the trace read misses honestly."""
    stored = provenance_store.put_turn_queries(SID, {REF: {
        "system_id": "erp", "tool_name": "query_erp",
        "query_text": "SELECT 1", "kg_artifact_ids": [],
        "captured_at": "2026-08-04T18:00:00Z"}})
    assert stored == 1
    assert provenance_store.get_query(SID, REF) is not None
    assert its.get_trace(SID, REF) is None
    # and the reverse: a trace write never satisfies the Wave-5 read
    assert its.put_trace(SID, REF, _entry()) is True
    assert provenance_store.get_query(SID, REF)["executed_query"] == "SELECT 1"


# --------------------------------------------------------------------------- #
# Write-side defense-in-depth (the A1 caps home, re-enforced)
# --------------------------------------------------------------------------- #

def test_hostile_oversize_fields_rebounded_with_flags():
    entry = _entry(question="q" * (it.MAX_QUESTION_BYTES * 2),
                   final_answer="a" * (it.MAX_FINAL_ANSWER_BYTES * 2),
                   steps=[_step(tool_output="z" * (it.MAX_STEP_OUTPUT_BYTES * 2),
                                tool_input="i" * (it.MAX_STEP_INPUT_BYTES * 2),
                                reasoning="r" * (it.MAX_REASONING_BYTES * 2))])
    assert its.put_trace(SID, REF, entry) is True
    out = its.get_trace(SID, REF)
    assert len(out["question"].encode()) <= it.MAX_QUESTION_BYTES
    assert out["question_truncated"] is True
    assert len(out["final_answer"].encode()) <= it.MAX_FINAL_ANSWER_BYTES
    assert out["final_answer_truncated"] is True
    step = out["steps"][0]
    assert len(step["tool_output"].encode()) <= it.MAX_STEP_OUTPUT_BYTES
    assert step["output_truncated"] is True
    assert len(step["tool_input"].encode()) <= it.MAX_STEP_INPUT_BYTES
    assert step["input_truncated"] is True
    assert len(step["reasoning"].encode()) <= it.MAX_REASONING_BYTES
    assert step["reasoning_truncated"] is True


def test_writer_truncated_flags_survive_rewrite():
    """An entry the builder already bounded keeps its honest flags even
    though re-bounding at the store is a no-op."""
    entry = it.build_trace_entry(
        session_id=SID, query_ref=REF, system_id="erp",
        question="q" * (it.MAX_QUESTION_BYTES * 2), kg_context_attached=False,
        steps=[_step()], steps_dropped=0, final_answer=ANSWER)
    assert entry["question_truncated"] is True         # builder set it
    assert its.put_trace(SID, REF, entry) is True
    assert its.get_trace(SID, REF)["question_truncated"] is True


def test_malformed_steps_dropped_and_fields_coerced():
    entry = _entry(steps=["junk", None, _step(status="bogus", duration_ms=-5,
                                              seq="x", tool_name=None)])
    assert its.put_trace(SID, REF, entry) is True
    out = its.get_trace(SID, REF)
    assert len(out["steps"]) == 1
    step = out["steps"][0]
    assert step["status"] == "unknown"
    assert step["duration_ms"] is None
    assert step["seq"] == 0
    assert step["tool_name"] == ""


def test_64kb_ratchet_reapplied_at_write():
    huge = [_step(seq=i, tool_output="z" * it.MAX_STEP_OUTPUT_BYTES,
                  output_bytes=it.MAX_STEP_OUTPUT_BYTES)
            for i in range(1, it.MAX_TRACE_STEPS + 1)]
    entry = _entry()
    entry["steps"] = huge                 # bypass the builder's ratchet
    assert its.put_trace(SID, REF, entry) is True
    out = its.get_trace(SID, REF)
    assert it._entry_bytes(out) <= it.MAX_TRACE_ENTRY_BYTES
    assert out["steps_dropped"] > 0


def test_ttl_imported_from_the_one_home(monkeypatch):
    """A freshly-built store carries the Wave-5 24 h TTL (one home). No
    store operation runs — JobStore resolves its table lazily."""
    monkeypatch.setattr(its, "_job_store", None)
    assert its._jobs()._ttl_seconds == provenance_store.PROVENANCE_QUERY_TTL_SECONDS


# --------------------------------------------------------------------------- #
# Log hygiene — digests and counts only
# --------------------------------------------------------------------------- #

def test_logs_carry_no_payload_fragments(caplog):
    with caplog.at_level(logging.DEBUG):
        its.put_trace(SID, REF, _entry())
        its.get_trace(SID, REF)
        its.put_trace("bad sid", REF, _entry())
    text = "\n".join(r.getMessage() for r in caplog.records
                     if r.name.startswith("agents.investigation_trace_store"))
    for fragment in (REASONING, SQL_INPUT, OUTPUT, ANSWER, QUESTION,
                     "SELECT", SID, REF):
        assert fragment not in text
