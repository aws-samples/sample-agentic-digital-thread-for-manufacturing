# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-call write capability for the reserved provenance arg (Wave 7).

The reserved ``_adt_provenance`` arg lets any gateway caller pick the
``(session_id, query_ref)`` an entry is filed under, and the owner of a
completed answer learns its own pair from the live stream and the provenance
frame. Without a capability a caller could replay that pair in a later direct
tool call and OVERWRITE the answer's provenance/trace evidence (an
unconditional put), so an admin opening technical detail later would see the
replay filed as the original call's evidence.

These pins assert the hardened fix: the app mints a fresh per-call write token
and DURABLY records a capability (shared table only) binding the public pair to
an opaque key; the runtime writes under that key ONLY with the token; readers
resolve through the capability record and NEVER fall back to the legacy key
once a record exists; a tokenless remote call writes nothing. The base-API
replay assertion (``test_base_api.py``, imports no new symbols) fails on
da90a9c1.

Generic names only.
"""
from __future__ import annotations

import json

import pytest

import agents.provenance_store as provenance_store
import agents.runtime_asgi as runtime_asgi
import agents.system_agent as sa
from agents import investigation_trace_store as its
from agents.investigation_trace import build_trace_entry
from agents.job_store import JobStore
from agents.provenance import RESERVED_PROVENANCE_ARG, mint_query_ref, provenance_context
from agents.runtime_asgi import provenance_context_var, provenance_write_token_var
from tests.unit._shared_table import SharedTable

SID = "session-1700000000000-abcd1234"
TOOL_USE = "tooluse-legit-0001"
REF = mint_query_ref(SID, TOOL_USE)
APP_TOKEN = "0123456789abcdef0123456789abcdef"  # secrets.token_hex(16) shape
SQL = "SELECT part_no FROM orders WHERE qty > 5"


@pytest.fixture(autouse=True)
def _stores(monkeypatch):
    # The capability store MUST be shared-table-backed: put_durable refuses a
    # memory-only store, which is exactly the posture we want (no durable
    # record -> no token forwarded). The query and trace stores ride their own
    # shared table so the (sid, ref) key space is the same across the suite.
    cap_table = SharedTable()
    data_table = SharedTable()
    monkeypatch.setattr(provenance_store, "_cap_store",
                        JobStore(provenance_store._CAP_KIND, table=cap_table))
    monkeypatch.setattr(provenance_store, "_job_store",
                        JobStore(provenance_store._KIND, table=data_table))
    monkeypatch.setattr(its, "_job_store",
                        JobStore(its._KIND, table=data_table))
    token = provenance_context_var.set(None)
    wtoken = provenance_write_token_var.set(None)
    yield types_ns(cap_table=cap_table, data_table=data_table)
    provenance_context_var.reset(token)
    provenance_write_token_var.reset(wtoken)


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


def _q_entry(query_text=SQL, **extra):
    base = {"system_id": "erp", "tool_name": "query_erp",
            "executed_query": query_text, "kg_artifacts": [],
            "captured_at": "2026-08-04T17:00:00Z"}
    base.update(extra)
    return base


def _t_entry(answer="Seven open orders.", question="how many open orders",
             coverage=None):
    return build_trace_entry(
        session_id=SID, query_ref=REF, system_id="erp", question=question,
        kg_context_attached=False, steps=[], steps_dropped=0,
        final_answer=answer, telemetry_coverage=coverage)


def _legit_turn(query_text=SQL, answer="Seven open orders.", coverage=None):
    """One legitimate delegated call end to end: the app issues the arg
    (records the capability) and the runtime writes both stores with the token."""
    arg = provenance_store.issue_provenance_arg(SID, REF)
    token = arg.get("write_token")
    assert token, "capability should be recorded on a durable store"
    provenance_store.put_turn_queries(SID, {REF: _q_entry(query_text)}, write_token=token)
    its.put_trace(SID, REF, _t_entry(answer=answer, coverage=coverage), write_token=token)
    return token


# --------------------------------------------------------------------------- #
# Replay WITHOUT the token never changes a reader (even entry missing).
# --------------------------------------------------------------------------- #
def test_replay_without_token_cannot_overwrite_provenance_or_trace():
    _legit_turn()
    provenance_store.put_turn_queries(SID, {REF: _q_entry("ATTACKER")}, write_token=None)
    provenance_store.put_turn_queries(
        SID, {REF: _q_entry("ATTACKER")}, write_token="f" * 32)  # any token they invent
    its.put_trace(SID, REF, _t_entry(answer="ATTACKER ANSWER"), write_token=None)
    assert provenance_store.get_query(SID, REF)["executed_query"] == SQL
    assert its.get_trace(SID, REF)["final_answer"] == "Seven open orders."


def test_capability_present_but_entry_missing_is_a_miss_not_legacy():
    # Blocking finding 1: the real entry never landed (tool failed / write
    # failed / replication lag / expired), but the capability record exists.
    # A tokenless replay writes the legacy key; the reader must still MISS.
    arg = provenance_store.issue_provenance_arg(SID, REF)
    assert arg.get("write_token")  # capability recorded, no entry written yet
    provenance_store.put_turn_queries(SID, {REF: _q_entry("ATTACKER")}, write_token=None)
    its.put_trace(SID, REF, _t_entry(answer="ATTACKER"), write_token=None)
    assert provenance_store.get_query(SID, REF) is None
    assert its.get_trace(SID, REF) is None


def test_replay_without_token_leaves_telemetry_summary_untouched():
    _legit_turn(coverage=["coverage line one"])
    its.put_trace(SID, REF, _t_entry(answer="a", coverage=None), write_token=None)
    assert its.get_telemetry_coverage(SID, REF) == ["coverage line one"]


# --------------------------------------------------------------------------- #
# A forged write that arrives FIRST does not block the legitimate write.
# --------------------------------------------------------------------------- #
def test_forged_first_does_not_block_the_legitimate_write():
    provenance_store.put_turn_queries(SID, {REF: _q_entry("ATTACKER FIRST")}, write_token=None)
    its.put_trace(SID, REF, _t_entry(answer="ATTACKER FIRST"), write_token=None)
    _legit_turn()
    assert provenance_store.get_query(SID, REF)["executed_query"] == SQL
    assert its.get_trace(SID, REF)["final_answer"] == "Seven open orders."


# --------------------------------------------------------------------------- #
# A throttled capability write forwards no token and writes nothing.
# --------------------------------------------------------------------------- #
def test_throttled_capability_write_forwards_no_token(_stores):
    _stores.cap_table.fail_puts = True
    arg = provenance_store.issue_provenance_arg(SID, REF)
    assert "write_token" not in arg  # durable write did not land -> no token
    # The runtime, given no token, writes nothing (simulate its guard).
    provenance_write_token_var.set(arg.get("write_token"))
    provenance_context_var.set((SID, REF))
    import agents.tool_result_capture as trc
    orig = trc.collect_sidecar_statements
    trc.collect_sidecar_statements = lambda _s: [{"query_text": SQL, "table": "orders",
                                                  "columns": ["part_no"], "row_count": 1,
                                                  "queried_at": "z"}]
    try:
        sa._write_provenance_sidecar({}, "erp")
    finally:
        trc.collect_sidecar_statements = orig
    assert provenance_store.get_query(SID, REF) is None


def test_put_capability_false_when_store_is_memory_only(monkeypatch):
    monkeypatch.setattr(provenance_store, "_cap_store",
                        JobStore(provenance_store._CAP_KIND, table=None))
    assert provenance_store.put_capability(SID, REF, APP_TOKEN) is False
    # No capability recorded -> issuer returns a tokenless arg.
    assert "write_token" not in provenance_store.issue_provenance_arg(SID, REF)


# --------------------------------------------------------------------------- #
# A cross-process reader resolves the capability (shared table, no memory).
# --------------------------------------------------------------------------- #
def test_cross_process_reader_resolves_capability(_stores):
    token = _legit_turn()
    assert token
    # A second process: fresh JobStores over the SAME shared tables, no memory.
    cap_b = JobStore(provenance_store._CAP_KIND, table=_stores.cap_table)
    data_b = JobStore(provenance_store._KIND, table=_stores.data_table)
    import contextlib
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(provenance_store, "_cap_store", cap_b)
        mp.setattr(provenance_store, "_job_store", data_b)
        assert provenance_store.get_query(SID, REF)["executed_query"] == SQL
    # And a cross-process tokenless replay still cannot be served.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(provenance_store, "_cap_store", cap_b)
        mp.setattr(provenance_store, "_job_store", data_b)
        provenance_store.put_turn_queries(SID, {REF: _q_entry("ATTACKER")}, write_token=None)
        assert provenance_store.get_query(SID, REF)["executed_query"] == SQL
    contextlib.nullcontext()


def test_capability_read_error_fails_closed_not_legacy(_stores):
    _legit_turn()
    provenance_store.put_turn_queries(SID, {REF: _q_entry("ATTACKER")}, write_token=None)
    _stores.cap_table.fail_reads = True  # cannot resolve the capability
    # Fail closed: a miss, never the attacker's legacy entry.
    assert provenance_store.get_query(SID, REF) is None


def test_malformed_capability_record_fails_closed_not_legacy(_stores):
    # A durable capability record EXISTS but its suffix is missing/empty/bad.
    # That is still an existing record, so the reader must not fall back to a
    # tokenless attacker legacy entry.
    import agents.investigation_trace_store as _its
    for bad in ({}, {"suffix": ""}, {"suffix": 123}, {"other": "x"}):
        _stores.cap_table.items.clear()
        _stores.data_table.items.clear()
        caps = JobStore(provenance_store._CAP_KIND, table=_stores.cap_table)
        assert caps.put_durable(f"{SID}:{REF}", bad)
        provenance_store.put_turn_queries(SID, {REF: _q_entry("ATTACKER")}, write_token=None)
        _its.put_trace(SID, REF, _t_entry(answer="ATTACKER"), write_token=None)
        assert provenance_store.get_query(SID, REF) is None, bad
        assert _its.get_trace(SID, REF) is None, bad


# --------------------------------------------------------------------------- #
# TTL: the capability outlives the entries it protects.
# --------------------------------------------------------------------------- #
def test_capability_ttl_exceeds_entry_ttl_with_margin():
    assert (provenance_store.CAPABILITY_TTL_SECONDS
            > provenance_store.PROVENANCE_QUERY_TTL_SECONDS)
    assert (provenance_store.CAPABILITY_TTL_SECONDS
            - provenance_store.PROVENANCE_QUERY_TTL_SECONDS) >= 3600


# --------------------------------------------------------------------------- #
# A tokenless REMOTE call writes nothing (runtime guard).
# --------------------------------------------------------------------------- #
def test_tokenless_remote_call_writes_nothing(monkeypatch):
    provenance_context_var.set((SID, REF))
    provenance_write_token_var.set(None)  # remote call, no capability issued
    import agents.tool_result_capture as trc
    monkeypatch.setattr(trc, "collect_sidecar_statements",
                        lambda _s: [{"query_text": SQL, "table": "orders",
                                     "columns": ["part_no"], "row_count": 1,
                                     "queried_at": "z"}])
    sa._write_provenance_sidecar({}, "erp")
    sa._write_investigation_trace({}, "erp", "q", "an answer")
    assert provenance_store.get_query(SID, REF) is None
    assert its.get_trace(SID, REF) is None


def test_runtime_writer_uses_the_contextvar_token(monkeypatch):
    seen = {}
    monkeypatch.setattr(provenance_store, "put_turn_queries",
                        lambda sid, entries, write_token=None: seen.update(
                            write_token=write_token) or 1)
    import agents.tool_result_capture as trc
    monkeypatch.setattr(trc, "collect_sidecar_statements",
                        lambda _s: [{"query_text": SQL, "table": "orders",
                                     "columns": ["part_no"], "row_count": 1,
                                     "queried_at": "z"}])
    provenance_context_var.set((SID, REF))
    provenance_write_token_var.set(APP_TOKEN)
    sa._write_provenance_sidecar({}, "erp")
    assert seen["write_token"] == APP_TOKEN


# --------------------------------------------------------------------------- #
# Legitimate round trip for every store reader; legacy entries still read.
# --------------------------------------------------------------------------- #
def test_legitimate_round_trip_resolves_for_all_store_readers():
    _legit_turn()
    assert provenance_store.get_query(SID, REF)["executed_query"] == SQL
    assert its.get_trace(SID, REF)["final_answer"] == "Seven open orders."


def test_captured_read_resolves_through_capability():
    from agents import onboard_verify_agent as ova
    arg = provenance_store.issue_provenance_arg(SID, REF)
    provenance_store.put_turn_queries(
        SID, {REF: _q_entry(entity={"table": "orders", "columns": ["part_no"]})},
        write_token=arg["write_token"])
    assert ova._captured_read(SID, REF, "orders") is True
    assert ova._captured_read(SID, REF, "other_table") is False


def test_legacy_entries_without_capability_still_read(monkeypatch):
    # Local mode / pre-change records: memory-only cap store (no durable
    # records) -> authoritative absence -> legacy key is read.
    monkeypatch.setattr(provenance_store, "_cap_store",
                        JobStore(provenance_store._CAP_KIND, table=None))
    provenance_store.put_turn_queries(SID, {REF: _q_entry("legacy")})  # no token
    its.put_trace(SID, REF, _t_entry(answer="legacy answer"))          # no token
    assert provenance_store.get_query(SID, REF)["executed_query"] == "legacy"
    assert its.get_trace(SID, REF)["final_answer"] == "legacy answer"


# --------------------------------------------------------------------------- #
# The onboarding verifier's REAL path issues a capability.
# --------------------------------------------------------------------------- #
def test_onboarding_verifier_real_path_issues_capability():
    import types as _types

    from agents import onboard_verify_agent as ova
    run = _types.SimpleNamespace(session_id=SID, facts={})
    prov_args, query_ref = ova._provenance_args(run)
    value = prov_args[RESERVED_PROVENANCE_ARG]
    assert value["query_ref"] == query_ref
    assert "write_token" in value  # the real path recorded a durable capability
    # And that token resolves: a runtime write under it is readable, a
    # tokenless replay of the same pair is not.
    provenance_store.put_turn_queries(
        query_ref and SID, {query_ref: _q_entry("verifier")},
        write_token=value["write_token"])
    provenance_store.put_turn_queries(SID, {query_ref: _q_entry("ATTACKER")}, write_token=None)
    assert provenance_store.get_query(SID, query_ref)["executed_query"] == "verifier"


# --------------------------------------------------------------------------- #
# N is absent from the wire / only its one-way suffix is stored.
# --------------------------------------------------------------------------- #
def test_token_is_stripped_at_the_asgi_seam_and_never_reaches_tool_args():
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "query_erp", "arguments": {
            "question": "q",
            RESERVED_PROVENANCE_ARG: provenance_context(SID, REF, APP_TOKEN),
        }},
    }).encode("utf-8")
    new_body, deny, _ = runtime_asgi._pop_identity_from_mcp_body(body, "erp")
    assert deny is None
    assert APP_TOKEN.encode() not in new_body
    assert RESERVED_PROVENANCE_ARG.encode() not in new_body
    assert json.loads(new_body)["params"]["arguments"] == {"question": "q"}
    assert provenance_write_token_var.get() == APP_TOKEN
    assert provenance_context_var.get() == (SID, REF)


def test_only_the_one_way_suffix_is_stored_never_the_token(_stores):
    assert provenance_store.put_capability(SID, REF, APP_TOKEN)
    # Read the durable record straight off the shared table.
    record = JobStore(provenance_store._CAP_KIND,
                      table=_stores.cap_table).get_durable(f"{SID}:{REF}")
    assert APP_TOKEN not in json.dumps(record)
    assert record["suffix"] == provenance_store.capability_suffix(APP_TOKEN, SID, REF)


def test_capability_context_builder_omits_token_when_absent():
    assert provenance_context(SID, REF) == {"session_id": SID, "query_ref": REF}
    assert "write_token" in provenance_context(SID, REF, APP_TOKEN)
