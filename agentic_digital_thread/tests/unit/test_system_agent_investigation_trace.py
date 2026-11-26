# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""System-agent investigation-trace writer (Wave 6, Task B2 — W6-F4 pins).

Offline pins for ``system_agent._write_investigation_trace`` and its
``_run_mode_a_query`` emit site:

* JOIN PIN (system half): the contextvar pair writes EXACTLY the key
  ``f"{sid}:{ref}"`` under the ``investigation-trace`` kind; the entry keeps
  both steps in order; ``question`` is the DELEGATED sub-question (never the
  kg-augmented text); ``final_answer`` is what actually returned;
* FAIL-SOFT byte-equal: a raising store never mutates the answer; nothing
  above WARNING; no reasoning/SQL/answer fragment in any log record;
* contextvar ``None`` ⇒ ZERO trace-store calls (legacy pin) — and the
  Wave-5 sidecar and Wave-6 trace fire INDEPENDENTLY (one failing never
  stops the other);
* LEAK PINS: the returned answer carries no ``_adt_provenance``/ref/state-
  key marker; stored ``tool_input`` carries no ``_adt_*`` key (A1 strip,
  end-to-end);
* CONCURRENCY: interleaved contexts write disjoint keys;
* ENFORCE MODE: the stored ``final_answer`` is the JSON envelope text.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

import agents.investigation_trace as it
import agents.investigation_trace_store as its
import agents.provenance_store as provenance_store
import agents.system_agent as sa
from agents.job_store import JobStore
from agents.provenance import RESERVED_PROVENANCE_ARG, mint_query_ref
from agents.runtime_asgi import provenance_context_var, provenance_write_token_var
from agents.tool_result_capture import SIDECAR_STATEMENTS_STATE_KEY
from tests.unit._shared_table import SharedTable

SID = "sid-1"
#: The real sidecar writer captured before any test patches it (so the
#: independence test can restore it without undoing the fixture's stores).
_REAL_PUT_TURN_QUERIES = provenance_store.put_turn_queries
QUESTION = "How many open orders reference part B-2847-X?"
REASONING = "Checking the orders table first."


@pytest.fixture(autouse=True)
def _memory_stores(monkeypatch):
    # The runtime write now requires a per-call capability (put_durable refuses
    # a memory-only store), so back the stores with a shared table and a
    # shared-table capability store.
    data = SharedTable()
    monkeypatch.setattr(its, "_job_store", JobStore(its._KIND, table=data))
    monkeypatch.setattr(provenance_store, "_job_store",
                        JobStore(provenance_store._KIND, table=data))
    monkeypatch.setattr(provenance_store, "_cap_store",
                        JobStore(provenance_store._CAP_KIND, table=SharedTable()))
    token = provenance_context_var.set(None)
    wtoken = provenance_write_token_var.set(None)
    yield
    provenance_context_var.reset(token)
    provenance_write_token_var.reset(wtoken)


def _issue_ctx(session_id: str, query_ref: str) -> None:
    """Set the per-request context AND a recorded write token, as a remote
    delegated call does (the orchestrator issued the capability)."""
    arg = provenance_store.issue_provenance_arg(session_id, query_ref)
    provenance_context_var.set((session_id, query_ref))
    provenance_write_token_var.set(arg.get("write_token"))


def _traced_state(step_count: int = 2) -> dict:
    state: dict = {}
    for i in range(1, step_count + 1):
        it.record_step(
            state,
            tool_use={"toolUseId": f"t-{i}", "name": "execute_sql",
                      "input": {"sql": f"SELECT {i}", "_adt_provenance": {"x": 1}}},
            result={"status": "success", "content": [{"text": f"row {i}"}]},
            exception=None, retry=False,
            messages=[{"role": "assistant", "content": [
                {"text": REASONING},
                {"toolUse": {"toolUseId": f"t-{i}", "name": "execute_sql",
                             "input": {}}}]}])
    return state


def _run_query(answer: str, state: dict, system_id: str = "erp",
               kg_context: str = "") -> str:
    """Drive _run_mode_a_query through the patched _with_state seam."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sa, "_invoke_mode_a_agent_with_state",
                   lambda _agent, _q: (answer, state))
        return asyncio.run(sa._run_mode_a_query(object(), system_id, QUESTION,
                                                kg_context))


# --------------------------------------------------------------------------- #
# Join pin (system half) + entry shape
# --------------------------------------------------------------------------- #

def test_writes_exact_key_and_w6f2_entry():
    ref = mint_query_ref(SID, "tooluse_X")
    _issue_ctx(SID, ref)
    answer = _run_query("Seven open orders.", _traced_state())
    assert answer == "Seven open orders."
    entry = its.get_trace(SID, ref)
    assert entry is not None
    assert entry["origin"] == "system-agent"
    assert entry["system_id"] == "erp"
    assert entry["question"] == QUESTION            # the DELEGATED question
    assert entry["final_answer"] == "Seven open orders."
    assert entry["kg_context_attached"] is False
    assert [s["seq"] for s in entry["steps"]] == [1, 2]
    assert entry["steps"][0]["reasoning"] == REASONING
    # Stored under the opaque capability key, NOT the public legacy pair.
    suffix = provenance_store._resolve_suffix(SID, ref)
    assert suffix not in (provenance_store._CAP_ABSENT, provenance_store._CAP_UNKNOWN)
    assert its._jobs().get(f"cap:{suffix}") is not None
    assert its._jobs().get(f"{SID}:{ref}") is None


def test_question_is_never_the_kg_augmented_text():
    """The kg-augmented prompt goes to the agent; the stored question stays
    the raw delegated sub-question with only the bool flag riding."""
    ref = mint_query_ref(SID, "tooluse_kg")
    _issue_ctx(SID, ref)
    _run_query("ok", _traced_state(), kg_context="Advisory: orders.part_no "
                                                 "maps to parts.part_number")
    entry = its.get_trace(SID, ref)
    assert entry["question"] == QUESTION
    assert "Advisory" not in json.dumps(entry)
    assert entry["kg_context_attached"] is True


def test_zero_steps_with_real_answer_still_writes_entry():
    ref = mint_query_ref(SID, "tooluse_notools")
    _issue_ctx(SID, ref)
    _run_query("Answer without tools.", {})
    entry = its.get_trace(SID, ref)
    assert entry is not None
    assert entry["steps"] == [] and entry["steps_dropped"] == 0
    assert entry["final_answer"] == "Answer without tools."


def test_zero_steps_and_blank_answer_writes_nothing(monkeypatch):
    calls: list = []
    monkeypatch.setattr(its, "put_trace", lambda *a, **k: calls.append(a) or True)
    _issue_ctx(SID, mint_query_ref(SID, "t"))
    _run_query("   ", {})
    assert calls == []


# --------------------------------------------------------------------------- #
# Honest absence + independence of the two writers
# --------------------------------------------------------------------------- #

def test_contextvar_none_makes_zero_trace_store_calls(monkeypatch):
    calls: list = []
    monkeypatch.setattr(its, "put_trace", lambda *a, **k: calls.append(a) or True)
    provenance_context_var.set(None)
    answer = _run_query("legacy answer", _traced_state())
    assert answer == "legacy answer"
    assert calls == []


def test_sidecar_and_trace_fire_independently(monkeypatch):
    """A raising Wave-5 sidecar write never stops the Wave-6 trace write,
    and vice versa."""
    ref = mint_query_ref(SID, "tooluse_ind")
    _issue_ctx(SID, ref)
    state = _traced_state()
    state[SIDECAR_STATEMENTS_STATE_KEY] = [
        {"query_text": "SELECT 1", "table": "", "columns": [],
         "row_count": None, "queried_at": ""}]
    monkeypatch.setattr(provenance_store, "put_turn_queries",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    answer = _run_query("both writers ran", state)
    assert answer == "both writers ran"
    assert its.get_trace(SID, ref) is not None      # trace survived

    # reverse: raising trace store never stops the sidecar
    ref2 = mint_query_ref(SID, "tooluse_ind2")
    _issue_ctx(SID, ref2)
    # Restore the real sidecar writer WITHOUT undoing the fixture's
    # shared-table store/capability patches (monkeypatch.undo would revert
    # those too, leaving the capability unresolvable).
    monkeypatch.setattr(provenance_store, "put_turn_queries",
                        _REAL_PUT_TURN_QUERIES)
    monkeypatch.setattr(its, "put_trace",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    state2 = _traced_state()
    state2[SIDECAR_STATEMENTS_STATE_KEY] = [
        {"query_text": "SELECT 2", "table": "", "columns": [],
         "row_count": None, "queried_at": ""}]
    answer = _run_query("sidecar survived", state2)
    assert answer == "sidecar survived"
    assert provenance_store.get_query(SID, ref2) is not None


# --------------------------------------------------------------------------- #
# Fail-soft byte-equal + log hygiene
# --------------------------------------------------------------------------- #

def test_raising_store_leaves_answer_byte_identical_and_logs_clean(
        monkeypatch, caplog):
    ref = mint_query_ref(SID, "tooluse_fail")
    _issue_ctx(SID, ref)
    healthy = _run_query("The exact answer text.", _traced_state())

    class _TraceStoreDown(RuntimeError):
        pass

    monkeypatch.setattr(its, "put_trace",
                        lambda *a, **k: (_ for _ in ()).throw(
                            _TraceStoreDown("SELECT secret")))
    with caplog.at_level(logging.DEBUG):
        broken = _run_query("The exact answer text.", _traced_state())
    assert broken == healthy                        # byte-equal pin
    sa_records = [r for r in caplog.records if r.name == "agents.system_agent"]
    assert all(r.levelno <= logging.WARNING for r in sa_records)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "_TraceStoreDown" in text                # type name only
    for fragment in ("SELECT secret", REASONING, QUESTION,
                     "The exact answer text.", ref):
        assert fragment not in text


# --------------------------------------------------------------------------- #
# Leak pins (new path)
# --------------------------------------------------------------------------- #

def test_answer_carries_no_trace_markers():
    ref = mint_query_ref(SID, "tooluse_leak")
    _issue_ctx(SID, ref)
    answer = _run_query("Clean answer.", _traced_state())
    assert RESERVED_PROVENANCE_ARG not in answer
    assert ref not in answer
    assert it.TRACE_STATE_KEY not in answer


def test_stored_tool_input_carries_no_adt_keys():
    """End-to-end A1 strip: the recorded step's input had an _adt_provenance
    key injected — it must never reach the stored entry."""
    ref = mint_query_ref(SID, "tooluse_strip")
    _issue_ctx(SID, ref)
    _run_query("ok", _traced_state())
    entry = its.get_trace(SID, ref)
    assert "_adt_" not in json.dumps(entry["steps"])


# --------------------------------------------------------------------------- #
# Concurrency: contextvar isolation
# --------------------------------------------------------------------------- #

def test_interleaved_contexts_write_disjoint_keys():
    ref_a = mint_query_ref("sid-A", "tooluse_A")
    ref_b = mint_query_ref("sid-B", "tooluse_B")

    async def _one(sid: str, ref: str, answer: str):
        _issue_ctx(sid, ref)
        await asyncio.sleep(0)                      # force interleaving
        sa._write_investigation_trace(_traced_state(), "erp", QUESTION, answer)

    async def _both():
        await asyncio.gather(_one("sid-A", ref_a, "answer A"),
                             _one("sid-B", ref_b, "answer B"))

    asyncio.run(_both())
    assert its.get_trace("sid-A", ref_a)["final_answer"] == "answer A"
    assert its.get_trace("sid-B", ref_b)["final_answer"] == "answer B"


# --------------------------------------------------------------------------- #
# Enforce mode: the envelope IS the final answer
# --------------------------------------------------------------------------- #

def test_enforce_mode_stores_the_envelope_text(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    envelope = json.dumps({"status": "ok", "answer": "Seven.",
                           "evidence": []}, separators=(",", ":"))
    ref = mint_query_ref(SID, "tooluse_enforce")
    _issue_ctx(SID, ref)
    answer = _run_query(envelope, _traced_state())
    assert answer == envelope
    assert its.get_trace(SID, ref)["final_answer"] == envelope
