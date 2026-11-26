# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Assembly-time sidecar merge (Wave-5 follow-up) — frame-level enrichment.

``agents.provenance.enrich_records_from_sidecar`` is the ONE merge home for
ALL THREE emitters (runtime legacy, runtime enforce, local): for each record
with a ``query_ref`` it reads the Wave-5 sidecar entry through an INJECTED
reader (``provenance_store.get_query`` at the emitters — injection keeps the
builder module import-pure) and fills ``entity.table``/``entity.columns``
and ``result_shape.rows``, upgrading ``unverified`` → ``partial`` (the
FROZEN D4 mapping: bounded capture success WITHOUT typed binding —
``verified`` stays reserved for enforce-mode typed evidence and is NEVER
minted by the merge).

Pinned here:
* happy path — table/columns/rows merged, verification ``partial``;
* miss ⇒ the record is EXACTLY as built (byte-identical payload);
* reader error ⇒ fail-soft, record as built, never a raise;
* D6 through the merged path — ``executed_query``/SQL from the sidecar
  entry NEVER enters the frame;
* typed evidence wins — non-empty record fields are never overwritten,
  ``verified`` is never downgraded;
* reads bounded at ``MAX_SIDECAR_READS``; invalid refs never read;
* the D1 4 KB payload cap is re-enforced after the merge (honest
  ``truncated`` flag);
* all three emitters enrich through the one helper, each fail-soft.

Offline: no AWS, no network — every store read is a fake/monkeypatched
reader (the real ``provenance_store`` is never touched).
"""
from __future__ import annotations

import copy
import json
import queue
from types import SimpleNamespace

import pytest

from agents import runtime_app
from agents.chat_client import _build_local_provenance, _ToolResultHook
from agents.provenance import (
    MAX_ENTITY_COLUMNS,
    MAX_PAYLOAD_BYTES,
    MAX_SIDECAR_READS,
    build_provenance_records,
    enrich_records_from_sidecar,
    mint_query_ref,
)

SESSION = "sess-w5b"

#: Hostile executed-query fixture — synthetic identifiers only (no dataset
#: literals; these strings exist nowhere else in the engine).
_SQL = "SELECT hidden_col FROM restricted_ledger_table WHERE k = 1"


def _entry(**overrides) -> dict:
    """An F2-shaped stored sidecar entry (the Wave-5 system-agent writer)."""
    entry = {
        "query_ref": "q-000000000000",
        "session_id": SESSION,
        "system_id": "erp",
        "tool_name": "query_erp",
        "executed_query": _SQL,
        "kg_artifacts": [],
        "captured_at": "2026-08-04T00:00:00Z",
        "entity": {"table": "synthetic_orders", "columns": ["col_a", "col_b"]},
        "row_count": 7,
        "statement_count": 1,
        "origin": "system-agent",
    }
    entry.update(overrides)
    return entry


def _base_payload(tool_id: str = "t1", **builder_overrides) -> dict:
    """One cited call, no capture — the sparse Mode-A record this closes:
    entity empty, rows null, verification ``unverified``."""
    kwargs = dict(
        session_id=SESSION,
        tool_calls=[{"name": "erp___query_erp", "toolUseId": tool_id}],
        tool_inputs={}, tool_results_metadata={}, evidence_by_tool={},
        rules_applied=[], verdict=None, captured_at="2026-08-04T00:00:00Z",
    )
    kwargs.update(builder_overrides)
    return build_provenance_records(**kwargs)


# ---------------------------------------------------------------------------
# Merge happy path
# ---------------------------------------------------------------------------

def test_merge_fills_table_columns_rows_and_partial():
    payload = _base_payload()
    [record] = payload["records"]
    assert record["entity"] == {"table": "", "columns": []}
    assert record["result_shape"]["rows"] is None
    assert record["verification"] == "unverified"

    enriched = enrich_records_from_sidecar(
        payload, session_id=SESSION, get_entry=lambda sid, ref: _entry())
    [record] = enriched["records"]
    assert record["entity"]["table"] == "synthetic_orders"
    assert record["entity"]["columns"] == ["col_a", "col_b"]
    assert record["result_shape"]["rows"] == 7
    assert record["verification"] == "partial"


def test_merge_reader_receives_the_records_own_ref_and_session():
    seen: list[tuple[str, str]] = []

    def reader(sid, ref):
        seen.append((sid, ref))
        return None

    enrich_records_from_sidecar(
        _base_payload(), session_id=SESSION, get_entry=reader)
    assert seen == [(SESSION, mint_query_ref(SESSION, "t1"))]


def test_merge_never_mints_verified():
    """FROZEN D4: 'verified' is reserved for enforce-mode typed evidence —
    a sidecar capture corroborates at most 'partial'."""
    enriched = enrich_records_from_sidecar(
        _base_payload(), session_id=SESSION, get_entry=lambda sid, ref: _entry())
    assert enriched["records"][0]["verification"] == "partial"


def test_merge_never_downgrades_verified_or_overwrites_typed_evidence():
    payload = _base_payload(
        evidence_by_tool={"t1": {"system_id": "erp", "table": "typed_table",
                                 "columns": ["typed_col"]}},
        tool_results_metadata={"t1": {"app_status": "success", "row_count": 3,
                                      "truncated": False}},
        verdict="accepted")
    [record] = payload["records"]
    assert record["verification"] == "verified"

    enriched = enrich_records_from_sidecar(
        payload, session_id=SESSION, get_entry=lambda sid, ref: _entry())
    [record] = enriched["records"]
    assert record["verification"] == "verified"          # never downgraded
    assert record["entity"]["table"] == "typed_table"    # typed evidence wins
    assert record["entity"]["columns"] == ["typed_col"]
    assert record["result_shape"]["rows"] == 3           # capture wins


def test_merge_partial_record_stays_partial_and_fills_empty_fields():
    payload = _base_payload(
        tool_results_metadata={"t1": {"app_status": "success",
                                      "truncated": False}})
    [record] = payload["records"]
    assert record["verification"] == "partial"
    assert record["result_shape"]["rows"] is None
    enriched = enrich_records_from_sidecar(
        payload, session_id=SESSION, get_entry=lambda sid, ref: _entry())
    [record] = enriched["records"]
    assert record["verification"] == "partial"
    assert record["result_shape"]["rows"] == 7
    assert record["entity"]["table"] == "synthetic_orders"


# ---------------------------------------------------------------------------
# Miss / error — the unchanged-record pins
# ---------------------------------------------------------------------------

def test_store_miss_leaves_payload_byte_identical():
    payload = _base_payload()
    before = copy.deepcopy(payload)
    enriched = enrich_records_from_sidecar(
        payload, session_id=SESSION, get_entry=lambda sid, ref: None)
    assert enriched == before


def test_reader_error_is_fail_soft_and_leaves_payload_byte_identical():
    payload = _base_payload()
    before = copy.deepcopy(payload)

    def boom(sid, ref):
        raise RuntimeError("store exploded")

    enriched = enrich_records_from_sidecar(
        payload, session_id=SESSION, get_entry=boom)
    assert enriched == before


def test_non_dict_entry_is_ignored():
    payload = _base_payload()
    before = copy.deepcopy(payload)
    enriched = enrich_records_from_sidecar(
        payload, session_id=SESSION, get_entry=lambda sid, ref: "poison")
    assert enriched == before


def test_empty_session_or_records_is_a_no_op():
    payload = _base_payload()
    before = copy.deepcopy(payload)
    assert enrich_records_from_sidecar(
        payload, session_id="", get_entry=lambda s, r: _entry()) == before
    empty = {"records": [], "truncated": False}
    assert enrich_records_from_sidecar(
        empty, session_id=SESSION, get_entry=lambda s, r: _entry()) == empty


# ---------------------------------------------------------------------------
# D6 — no SQL on the broadcast, extended through the merged path
# ---------------------------------------------------------------------------

def test_merged_frame_never_carries_executed_query_or_sql():
    enriched = enrich_records_from_sidecar(
        _base_payload(), session_id=SESSION, get_entry=lambda sid, ref: _entry())
    serialized = json.dumps(enriched)
    assert "executed_query" not in serialized
    assert "SELECT" not in serialized
    assert "restricted_ledger_table" not in serialized
    assert "hidden_col" not in serialized


def test_merge_copies_only_the_three_surfaces():
    """A hostile entry cannot smuggle extra keys into the record: only
    entity/table+columns, rows and the verification upgrade cross over."""
    hostile = _entry(note=_SQL, origin=_SQL, statement_count=99,
                     extra_payload={"sql": _SQL})
    payload = _base_payload()
    keys_before = set(payload["records"][0])
    enriched = enrich_records_from_sidecar(
        payload, session_id=SESSION, get_entry=lambda sid, ref: hostile)
    [record] = enriched["records"]
    assert set(record) == keys_before          # no new record keys
    assert "SELECT" not in json.dumps(enriched)


# ---------------------------------------------------------------------------
# Bounds — reads, columns, and the D1 payload cap
# ---------------------------------------------------------------------------

def _fat_record(num: int) -> dict:
    return {
        "citation_num": num, "tool_id": f"t{num}", "source_system": "erp",
        "tool_name": "erp___query_erp",
        "entity": {"table": "", "columns": []},
        "scope": {"filters": [], "window": None},
        "query_ref": mint_query_ref(SESSION, f"t{num}"),
        "result_shape": {"rows": None, "truncated": False, "partial_reason": ""},
        "kg_route": {"rules_applied": [], "value_mappings": []},
        "captured_at": "2026-08-04T00:00:00Z",
        "verification": "unverified",
    }


def test_total_reads_bounded_at_max_sidecar_reads():
    payload = {"records": [_fat_record(n) for n in range(1, 41)],
               "truncated": False}
    calls = []
    enrich_records_from_sidecar(
        payload, session_id=SESSION,
        get_entry=lambda sid, ref: calls.append(ref) or None)
    assert len(calls) == MAX_SIDECAR_READS


def test_invalid_query_ref_is_never_read():
    record = _fat_record(1)
    record["query_ref"] = "not-a-ref"
    calls = []
    enrich_records_from_sidecar(
        {"records": [record], "truncated": False}, session_id=SESSION,
        get_entry=lambda sid, ref: calls.append(ref) or None)
    assert calls == []


def test_merged_columns_are_bounded():
    wide = _entry(entity={"table": "synthetic_orders",
                          "columns": [f"col_{i}_" + "x" * 300
                                      for i in range(30)]})
    enriched = enrich_records_from_sidecar(
        _base_payload(), session_id=SESSION, get_entry=lambda sid, ref: wide)
    columns = enriched["records"][0]["entity"]["columns"]
    assert len(columns) == MAX_ENTITY_COLUMNS
    assert all(len(c) <= 120 for c in columns)


def test_d1_payload_cap_reenforced_after_merge():
    """Merged columns grow records — the 4 KB cap must bite again, dropping
    trailing records with the honest ``truncated`` flag (D1)."""
    payload = {"records": [_fat_record(n) for n in range(1, 21)],
               "truncated": False}
    fat = _entry(entity={"table": "synthetic_orders",
                         "columns": [("c" * 118) + f"{i:02d}"
                                     for i in range(16)]})
    enriched = enrich_records_from_sidecar(
        payload, session_id=SESSION, get_entry=lambda sid, ref: dict(
            fat, entity=dict(fat["entity"])))
    size = len(json.dumps(enriched, separators=(",", ":"), default=str)
               .encode("utf-8"))
    assert size <= MAX_PAYLOAD_BYTES
    assert enriched["truncated"] is True
    assert 0 < len(enriched["records"]) < 20


# ---------------------------------------------------------------------------
# A rejected verdict survives the merge: the emitters pass the verdict to
# the merge, and nothing is added to the records
# ---------------------------------------------------------------------------

def _api_read_entry() -> dict:
    """The read entry a system agent that ran no SQL writes for its call."""
    import agents.system_agent as sa
    return sa._build_api_read_entry(1, "erp")


def _rejected_payload() -> dict:
    """One cited call the integrity gate rejected, its capture successful."""
    return _base_payload(
        tool_results_metadata={"t1": {"app_status": "success", "row_count": 3,
                                      "truncated": False}},
        verdict="rejected")


def test_a_rejected_record_renders_exactly_as_built_whatever_entry_exists():
    """The reviewer's probe, on a record the builder made: a rejected verdict
    is unverified (D4), and the merge used to upgrade it to partial whenever
    any entry existed. Given the turn's verdict, the merge leaves the frame as
    built, SQL entry or read entry alike, and still reads each entry (so the
    frame's capability misses are counted as before)."""
    built = _rejected_payload()
    assert built["records"][0]["verification"] == "unverified"
    assert "verdict" not in built["records"][0]
    for entry in (_entry(), _api_read_entry()):
        seen: list = []
        enriched = enrich_records_from_sidecar(
            copy.deepcopy(built), session_id=SESSION, verdict="rejected",
            get_entry=lambda sid, ref, e=entry: seen.append(ref) or e)
        assert enriched == built
        assert seen == [built["records"][0]["query_ref"]]


@pytest.mark.parametrize("verdict", [None, "accepted", "unexpected"])
def test_a_record_without_a_rejected_verdict_still_reads_partial(verdict):
    """Guard: only a rejected verdict holds a record back. With no verdict
    (report-only), an accepted one without typed evidence, or any other, a
    read entry still makes the record partial with no row count."""
    payload = _base_payload(verdict=verdict)
    [record] = enrich_records_from_sidecar(
        payload, session_id=SESSION, verdict=verdict,
        get_entry=lambda sid, ref: _api_read_entry())["records"]
    assert record["verification"] == "partial"
    assert record["result_shape"]["rows"] is None
    assert "verdict" not in record


# ---------------------------------------------------------------------------
# Emitters — all three enrich through the one helper, fail-soft
# ---------------------------------------------------------------------------

def _legacy_state() -> SimpleNamespace:
    return SimpleNamespace(
        session_id=SESSION,
        prompt="how many open work orders",
        tool_calls_log=[{"name": "erp___query_erp", "toolUseId": "t1"}],
        tool_inputs={},
        tool_results={},
        rule_ids=set(), matched_rows=[],
    )


def test_runtime_legacy_emitter_merges_sidecar(monkeypatch):
    monkeypatch.setattr("agents.provenance_store.get_query",
                        lambda sid, ref: _entry())
    [event] = runtime_app._provenance_events(_legacy_state())
    [record] = event["provenance"]["records"]
    assert record["entity"]["table"] == "synthetic_orders"
    assert record["result_shape"]["rows"] == 7
    assert record["verification"] == "partial"
    serialized = json.dumps(event)
    assert "SELECT" not in serialized
    assert "executed_query" not in serialized


def test_runtime_legacy_emitter_store_error_keeps_frame_as_today(monkeypatch):
    def boom(sid, ref):
        raise RuntimeError("store exploded")

    monkeypatch.setattr("agents.provenance_store.get_query", boom)
    [event] = runtime_app._provenance_events(_legacy_state())
    [record] = event["provenance"]["records"]
    assert record["entity"] == {"table": "", "columns": []}
    assert record["result_shape"]["rows"] is None
    assert record["verification"] == "unverified"


def _integrity_state() -> SimpleNamespace:
    return SimpleNamespace(
        session_id=SESSION,
        tool_names={"t1": "erp___query_erp"},
        tool_metadata={},
        tool_query_inputs={},
        rule_ids=set(), matched_rows=[],
        context=SimpleNamespace(system_answers=[]),
    )


def test_runtime_enforce_emitter_merges_sidecar(monkeypatch):
    monkeypatch.setattr("agents.provenance_store.get_query",
                        lambda sid, ref: _entry())
    integrity = SimpleNamespace(quality_metadata=lambda: {"verdict": None})
    [event] = runtime_app._integrity_provenance_events(
        _integrity_state(), integrity)
    [record] = event["provenance"]["records"]
    assert record["entity"]["table"] == "synthetic_orders"
    assert record["result_shape"]["rows"] == 7
    assert record["verification"] == "partial"
    assert "SELECT" not in json.dumps(event)


def test_local_emitter_merges_sidecar(monkeypatch):
    monkeypatch.setattr("agents.provenance_store.get_query",
                        lambda sid, ref: _entry())
    hook = _ToolResultHook(queue.Queue(), session_id=SESSION)
    hook.tool_calls = [{"name": "erp___query_erp", "toolUseId": "t1"}]
    payload = _build_local_provenance(hook, rules_applied=[])
    [record] = payload["records"]
    assert record["entity"]["table"] == "synthetic_orders"
    assert record["result_shape"]["rows"] == 7
    assert record["verification"] == "partial"
    assert "SELECT" not in json.dumps(payload)


def test_local_emitter_enrichment_failure_keeps_frame(monkeypatch):
    def boom(sid, ref):
        raise RuntimeError("store exploded")

    monkeypatch.setattr("agents.provenance_store.get_query", boom)
    hook = _ToolResultHook(queue.Queue(), session_id=SESSION)
    hook.tool_calls = [{"name": "erp___query_erp", "toolUseId": "t1"}]
    payload = _build_local_provenance(hook, rules_applied=[])
    assert payload is not None
    [record] = payload["records"]
    assert record["verification"] == "unverified"
