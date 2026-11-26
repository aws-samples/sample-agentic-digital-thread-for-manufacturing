# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""System-agent-side provenance sidecar (Wave 5 — F1 pop/stash, F2 emit).

Offline pins for the emit site:

* JOIN PIN (system-agent half): with the contextvar carrying ``(sid, ref)``
  where ``ref = mint_query_ref(sid, tool_use_id)``, a Mode-A call whose
  capture holds SQL writes EXACTLY the key ``f"{sid}:{ref}"`` — byte-identical
  to what the D5 route reads (zero coordination, F1);
* ONE entry per call: multiple inner SQLs join with the frozen
  ``-- statement <k> --`` separator (``statement_count``, entity/row_count
  from the LAST data-bearing statement that returned rows, else the last
  data-bearing one — discovery statements skipped);
* HONEST ABSENCE: contextvar ``None`` (legacy caller) ⇒ zero store calls;
  empty capture ⇒ no entry (never fabricated);
* API READS: a call that ran no SQL but made a successful read (a telemetry
  history read that returned points, or a retained successful non-history
  result) writes ONE read entry from that capture alone: ``executed_query``
  empty with the store's API-read note, no ``row_count``; a call whose reads
  all failed, raised or returned nothing writes nothing;
* FAIL-SOFT: a raising store never mutates the answer (byte-equal pin);
  breadcrumbs stay ≤ WARNING and never carry SQL text;
* LEAK PINS: the reserved arg is popped at the ASGI seam ALWAYS (never
  reaches tool arguments); the Mode-A answer never contains the ref/sid or
  the ``_adt_provenance`` spelling;
* CONCURRENCY: two interleaved handler contexts write two disjoint keys
  (contextvar isolation).
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import agents.provenance_store as provenance_store
import agents.runtime_asgi as runtime_asgi
import agents.system_agent as sa
import agents.telemetry_coverage as tc
from agents.job_store import JobStore
from agents.provenance import RESERVED_PROVENANCE_ARG, mint_query_ref
from agents.runtime_asgi import provenance_context_var, provenance_write_token_var
from agents.tool_result_capture import (
    SIDECAR_STATEMENTS_STATE_KEY,
    TOOL_RESULTS_STATE_KEY,
    InvocationToolResultCapture,
    collect_sidecar_statements,
    record_sidecar_statement,
)
from tests.unit._shared_table import SharedTable

SID = "sid-1"
SQL_1 = "SELECT part_no FROM orders WHERE qty > 5"
SQL_2 = "SELECT count(*) FROM order_lines"


@pytest.fixture(autouse=True)
def _memory_store(monkeypatch):
    # The runtime write now requires a per-call capability. Back the stores
    # with a shared table (put_durable refuses memory-only) so a test can
    # issue a real capability and read the entry back.
    data = SharedTable()
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
    """Set the per-request provenance context AND a recorded write token, as a
    remote delegated call does: the orchestrator issued a capability for this
    pair, so the runtime writes under it and readers resolve it."""
    arg = provenance_store.issue_provenance_arg(session_id, query_ref)
    provenance_context_var.set((session_id, query_ref))
    provenance_write_token_var.set(arg.get("write_token"))


def _state_with_rows(rows: list[dict]) -> dict:
    return {SIDECAR_STATEMENTS_STATE_KEY: rows}


def _row(query_text: str, **overrides) -> dict:
    row = {"query_text": query_text, "table": "orders",
           "columns": ["part_no"], "row_count": 7,
           "queried_at": "2026-08-04T17:00:00Z"}
    row.update(overrides)
    return row


def _run_query(answer: str, state: dict, system_id: str = "erp") -> str:
    """Drive _run_mode_a_query through the patched _with_state seam."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sa, "_invoke_mode_a_agent_with_state",
                   lambda _agent, _q: (answer, state))
        mp.setattr(sa, "_augment_question_with_kg_context",
                   lambda _s, q, _k: q)
        # These tests pin the sidecar write; the executed-filters engine line
        # (derived from the same statements) is pinned in
        # test_executed_filters_line.py.
        mp.setattr(sa, "_executed_filters_fact", lambda _state: "")
        return asyncio.run(sa._run_mode_a_query(object(), system_id, "question"))


# --------------------------------------------------------------------------- #
# Join pin (system-agent half) + F2 entry shape
# --------------------------------------------------------------------------- #
def test_writes_exact_d5_key_and_f2_entry():
    ref = mint_query_ref(SID, "tooluse_X")
    _issue_ctx(SID, ref)
    answer = _run_query("Seven open orders.", _state_with_rows([_row(SQL_1)]))
    assert answer == "Seven open orders."
    # byte-identical key: the D5 read path returns the entry
    entry = provenance_store.get_query(SID, ref)
    assert entry is not None
    assert entry["origin"] == "system-agent"
    assert entry["executed_query"] == SQL_1
    assert entry["entity"] == {"table": "orders", "columns": ["part_no"]}
    assert entry["row_count"] == 7
    assert entry["statement_count"] == 1
    assert entry["query_shape_version"] == "v1"
    assert re.fullmatch(r"[0-9a-f]{64}", entry["query_fingerprint"])
    assert entry["system_id"] == "erp"
    assert entry["tool_name"] == "query_erp"
    assert entry["captured_at"] == "2026-08-04T17:00:00Z"
    # The entry is stored under the opaque capability key, NOT the public
    # legacy pair: a tokenless replay of (SID, ref) cannot reach it.
    suffix = provenance_store._resolve_suffix(SID, ref)
    assert suffix not in (provenance_store._CAP_ABSENT, provenance_store._CAP_UNKNOWN)
    assert provenance_store._jobs().get(f"cap:{suffix}") is not None
    assert provenance_store._jobs().get(f"{SID}:{ref}") is None


def test_multi_statement_joins_into_one_entry_with_frozen_separator():
    ref = mint_query_ref(SID, "tooluse_multi")
    _issue_ctx(SID, ref)
    rows = [_row(SQL_1), _row(SQL_2, table="order_lines", columns=["count"],
                              row_count=1)]
    _run_query("ok", _state_with_rows(rows))
    entry = provenance_store.get_query(SID, ref)
    assert entry["statement_count"] == 2
    assert entry["executed_query"] == f"{SQL_1}\n\n-- statement 2 --\n{SQL_2}"
    # entity + row_count from the LAST data-bearing statement's capture
    assert entry["entity"]["table"] == "order_lines"
    assert entry["row_count"] == 1
    # captured_at stays the FIRST statement's clock (call start)
    assert entry["captured_at"] == "2026-08-04T17:00:00Z"


def test_multi_statement_skips_trailing_discovery_statement():
    """A trailing information_schema probe never becomes the entity — the
    LAST data-bearing statement wins (the live mes-det defect shape)."""
    ref = mint_query_ref(SID, "tooluse_discovery")
    _issue_ctx(SID, ref)
    discovery = ("SELECT column_name FROM information_schema.columns "
                 "WHERE table_name = 'orders'")
    rows = [_row(SQL_1),
            _row(discovery, table="", columns=[], row_count=12)]
    _run_query("ok", _state_with_rows(rows))
    entry = provenance_store.get_query(SID, ref)
    assert entry["entity"] == {"table": "orders", "columns": ["part_no"]}
    assert entry["row_count"] == 7
    assert entry["statement_count"] == 2


@pytest.mark.parametrize("answered,empty", [
    (("deliveries", "SELECT lot_id FROM deliveries WHERE batch = 'B-1'", 3),
     ("open_requests", "SELECT req_id FROM open_requests WHERE item = 'X'", 0)),
    (("ledger_lines", "SELECT line_no FROM ledger_lines WHERE ref = 9", 12),
     ("backlog", "SELECT ticket FROM backlog WHERE ref = 9", 0)),
])
def test_a_trailing_statement_with_no_rows_never_names_the_source(answered, empty):
    """Walk 2026-10-19: a containment answer's ERP chip read the table of a
    later lookup that came back empty, not the table its rows came from. The
    statement that returned rows names the source; the empty one is still in
    the joined query text."""
    ref = mint_query_ref(SID, f"tooluse_{answered[0]}")
    _issue_ctx(SID, ref)
    rows = [_row(answered[1], table=answered[0], columns=["c"], row_count=answered[2]),
            _row(empty[1], table=empty[0], columns=["c"], row_count=0)]
    _run_query("ok", _state_with_rows(rows))
    entry = provenance_store.get_query(SID, ref)
    assert entry["entity"]["table"] == answered[0]
    assert entry["row_count"] == answered[2]
    assert empty[1] in entry["executed_query"]


def test_when_no_statement_returned_rows_the_last_one_still_names_the_source():
    ref = mint_query_ref(SID, "tooluse_noneempty")
    _issue_ctx(SID, ref)
    rows = [_row(SQL_1, row_count=0),
            _row(SQL_2, table="order_lines", columns=["count"], row_count=0)]
    _run_query("ok", _state_with_rows(rows))
    entry = provenance_store.get_query(SID, ref)
    assert entry["entity"]["table"] == "order_lines"
    assert entry["row_count"] == 0


def test_all_discovery_statements_stay_honest_empty():
    ref = mint_query_ref(SID, "tooluse_alldisc")
    _issue_ctx(SID, ref)
    discovery = "SELECT table_name FROM information_schema.tables"
    _run_query("ok", _state_with_rows(
        [_row(discovery, table="", columns=[], row_count=40)]))
    entry = provenance_store.get_query(SID, ref)
    assert entry["entity"] == {"table": "", "columns": []}
    assert entry["row_count"] is None


def test_null_row_count_and_empty_entity_stay_honest():
    ref = mint_query_ref(SID, "tooluse_sparse")
    _issue_ctx(SID, ref)
    _run_query("ok", _state_with_rows(
        [_row(SQL_1, table="", columns=[], row_count=None)]))
    entry = provenance_store.get_query(SID, ref)
    assert entry["entity"] == {"table": "", "columns": []}
    assert "row_count" in entry and entry["row_count"] is None


# --------------------------------------------------------------------------- #
# Honest absence — legacy callers and empty captures
# --------------------------------------------------------------------------- #
def test_contextvar_none_makes_zero_store_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(provenance_store, "put_turn_queries",
                        lambda *a, **k: calls.append(a) or 0)
    provenance_context_var.set(None)
    answer = _run_query("legacy answer", _state_with_rows([_row(SQL_1)]))
    assert answer == "legacy answer"
    assert calls == []


def test_empty_capture_writes_no_entry(monkeypatch):
    calls = []
    monkeypatch.setattr(provenance_store, "put_turn_queries",
                        lambda *a, **k: calls.append(a) or 0)
    _issue_ctx(SID, mint_query_ref(SID, "t"))
    answer = _run_query("no sql ran", _state_with_rows([]))
    assert answer == "no sql ran"
    assert calls == []


def test_malformed_trusted_query_keeps_raw_detail_but_omits_shape():
    ref = mint_query_ref(SID, "tooluse_malformed")
    _issue_ctx(SID, ref)
    malformed = "SELECT part_no FROM orders WHERE note = 'unterminated"
    _run_query("ok", _state_with_rows([_row(malformed)]))
    entry = provenance_store.get_query(SID, ref)
    assert entry["executed_query"] == malformed
    assert "query_fingerprint" not in entry
    assert "query_shape_version" not in entry


# --------------------------------------------------------------------------- #
# Fail-soft — answer byte-equal, bounded breadcrumb, no SQL in logs
# --------------------------------------------------------------------------- #
def test_store_raising_leaves_answer_byte_equal(monkeypatch, caplog):
    def _boom(*a, **k):
        raise RuntimeError(f"ddb down while writing {SQL_1}")

    monkeypatch.setattr(provenance_store, "put_turn_queries", _boom)
    _issue_ctx(SID, mint_query_ref(SID, "t"))
    expected = "The exact answer text — byte equal."
    with caplog.at_level(logging.DEBUG):
        answer = _run_query(expected, _state_with_rows([_row(SQL_1)]))
    assert answer == expected
    breadcrumbs = [r for r in caplog.records
                   if "MODE_A_PROVENANCE_SIDECAR" in r.getMessage()]
    assert breadcrumbs, "fail-soft path must leave a logged breadcrumb"
    for record in caplog.records:
        assert record.levelno <= logging.WARNING
        message = record.getMessage()
        assert SQL_1 not in message
        assert "SELECT" not in message


def test_success_logs_never_carry_sql_or_ids(caplog):
    ref = mint_query_ref(SID, "tooluse_log")
    _issue_ctx(SID, ref)
    with caplog.at_level(logging.DEBUG):
        _run_query("ok", _state_with_rows([_row(SQL_1)]))
    for record in caplog.records:
        message = record.getMessage()
        assert SQL_1 not in message
        assert ref not in message


# --------------------------------------------------------------------------- #
# Leak pins — ASGI pop-always; answer carries no reserved spelling/ids
# --------------------------------------------------------------------------- #
def _tools_call_body(arguments: dict) -> bytes:
    return json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "query_erp", "arguments": arguments},
    }).encode("utf-8")


def test_asgi_pops_reserved_arg_always_and_sets_contextvar():
    ref = mint_query_ref(SID, "tooluse_pop")
    body = _tools_call_body({
        "question": "q",
        RESERVED_PROVENANCE_ARG: {"session_id": SID, "query_ref": ref},
    })
    new_body, deny, _ = runtime_asgi._pop_identity_from_mcp_body(body, "erp")
    assert deny is None
    assert RESERVED_PROVENANCE_ARG.encode() not in new_body
    parsed = json.loads(new_body)
    assert parsed["params"]["arguments"] == {"question": "q"}
    assert provenance_context_var.get() == (SID, ref)


def test_asgi_malformed_context_sets_none_never_denies():
    body = _tools_call_body({
        "question": "q",
        RESERVED_PROVENANCE_ARG: {"session_id": "bad session!", "query_ref": "x"},
    })
    new_body, deny, _ = runtime_asgi._pop_identity_from_mcp_body(body, "erp")
    assert deny is None
    assert RESERVED_PROVENANCE_ARG.encode() not in new_body
    assert provenance_context_var.get() is None


def test_asgi_resets_contextvar_per_tools_call():
    ref = mint_query_ref(SID, "tooluse_reset")
    runtime_asgi._pop_identity_from_mcp_body(_tools_call_body({
        RESERVED_PROVENANCE_ARG: {"session_id": SID, "query_ref": ref},
    }), "erp")
    assert provenance_context_var.get() == (SID, ref)
    # next tools/call WITHOUT the arg resets to None (no stale carry-over)
    runtime_asgi._pop_identity_from_mcp_body(_tools_call_body({"q": 1}), "erp")
    assert provenance_context_var.get() is None


def test_answer_never_contains_reserved_spelling_or_ids():
    ref = mint_query_ref(SID, "tooluse_leak")
    _issue_ctx(SID, ref)
    answer = _run_query("Plain answer about orders.",
                        _state_with_rows([_row(SQL_1)]))
    assert RESERVED_PROVENANCE_ARG not in answer
    assert ref not in answer
    assert SID not in answer
    assert SQL_1 not in answer


# --------------------------------------------------------------------------- #
# Concurrency — contextvar isolation across interleaved handler contexts
# --------------------------------------------------------------------------- #
def test_two_interleaved_contexts_write_disjoint_keys():
    ref_a = mint_query_ref("sid-a", "t1")
    ref_b = mint_query_ref("sid-b", "t1")

    async def _one(sid: str, ref: str, sql: str):
        _issue_ctx(sid, ref)
        await asyncio.sleep(0)  # interleave
        sa._write_provenance_sidecar(_state_with_rows([_row(sql)]), "erp")

    async def _main():
        await asyncio.gather(_one("sid-a", ref_a, SQL_1),
                             _one("sid-b", ref_b, SQL_2))

    asyncio.run(_main())
    assert provenance_store.get_query("sid-a", ref_a)["executed_query"] == SQL_1
    assert provenance_store.get_query("sid-b", ref_b)["executed_query"] == SQL_2
    assert provenance_store.get_query("sid-a", ref_b) is None
    assert provenance_store.get_query("sid-b", ref_a) is None


# --------------------------------------------------------------------------- #
# Capture seam — record/collect round-trip (the trusted-capture projection)
# --------------------------------------------------------------------------- #
def test_record_and_collect_round_trip_with_projection_entity():
    schema = {"orders": {"relation_kind": "base_table",
                         "columns": {"part_no": "text", "qty": "integer"}}}
    state: dict = {}
    record_sidecar_statement(
        state, executed_query="SELECT part_no FROM orders",
        metadata={"row_count": 3}, physical_schema=schema)
    rows = collect_sidecar_statements(state)
    assert len(rows) == 1
    assert rows[0]["query_text"] == "SELECT part_no FROM orders"
    assert rows[0]["table"] == "orders"
    assert rows[0]["columns"] == ["part_no"]
    assert rows[0]["row_count"] == 3
    assert rows[0]["queried_at"].endswith("Z")


def test_record_skips_empty_query_and_collect_tolerates_hostile_state():
    state: dict = {}
    record_sidecar_statement(state, executed_query="", metadata={})
    record_sidecar_statement(state, executed_query="   ", metadata={})
    assert collect_sidecar_statements(state) == []
    assert collect_sidecar_statements(None) == []
    assert collect_sidecar_statements({SIDECAR_STATEMENTS_STATE_KEY: "junk"}) == []
    assert collect_sidecar_statements(
        {SIDECAR_STATEMENTS_STATE_KEY: [{"no_query": 1}, "junk"]}) == []


def test_record_falls_back_to_sql_parse_when_projection_unprovable():
    """The live Wave-5 defect: proof-only extraction left entity ALWAYS
    empty. The bounded SQL parse now fills it; row_count counts the actual
    result rows structurally (never estimated)."""
    state: dict = {}
    result = {"status": "success", "content": [{"json": {
        "columns": ["mystery"], "rows": [[1], [2], [3]], "row_count": 3}}]}
    record_sidecar_statement(
        state, executed_query="SELECT mystery FROM unknown_tbl",
        metadata={}, physical_schema={}, result=result)
    rows = collect_sidecar_statements(state)
    assert rows[0]["table"] == "unknown_tbl"
    assert rows[0]["columns"] == ["mystery"]
    assert rows[0]["row_count"] == 3


def test_record_stays_honest_empty_on_garbage_sql_and_no_result():
    state: dict = {}
    record_sidecar_statement(
        state, executed_query="EXPLAIN whatever this is",
        metadata={}, physical_schema={})
    rows = collect_sidecar_statements(state)
    assert rows[0]["table"] == ""
    assert rows[0]["columns"] == []
    assert rows[0]["row_count"] is None


def test_record_counts_rows_from_large_text_result():
    """Connector results over the 1 KB metadata bound previously stored
    row_count None — the structural count reads the full-bound payload."""
    import json as _json
    state: dict = {}
    payload = {"columns": ["part_no"],
               "rows": [[f"P-{i:06d}" * 8] for i in range(40)]}
    result = {"status": "success",
              "content": [{"text": _json.dumps(payload)}]}
    assert len(_json.dumps(payload)) > 1024
    record_sidecar_statement(
        state, executed_query="SELECT part_no FROM orders",
        metadata={"app_status": "unknown"}, physical_schema={}, result=result)
    rows = collect_sidecar_statements(state)
    assert rows[0]["row_count"] == 40
    assert rows[0]["table"] == "orders"


# --------------------------------------------------------------------------- #
# A call that ran no SQL: its successful API reads are the source-side capture
# --------------------------------------------------------------------------- #
#: Made-up SiteWise (Direct) ids: nothing here depends on a dataset's names.
SW_SYSTEM = "line-telemetry"
SW_ASSET = "unit-7d21"
SW_PROPS = ("chan-a", "chan-b", "chan-c")
SW_START = datetime(2026, 3, 2, 8, 0, tzinfo=timezone.utc)
HISTORY_TOOL = "sitewise_get_asset_property_history"
LIST_TOOL = "sitewise_list_assets"
VALUES_TOOL = "sitewise_batch_get_property_values"
VALUES_INPUT = {"entries_json": json.dumps([{"assetId": SW_ASSET, "propertyId": p}
                                            for p in SW_PROPS[:2]])}
_TOOL_USE_IDS = itertools.count(1)


class _SeriesClient:
    """A SiteWise client stand-in serving ``points`` GOOD values per series
    on one page, and one current value per series."""

    def __init__(self, points: int):
        self.points = points

    def batch_get_asset_property_value_history(self, entries, maxResults, nextToken=None):  # noqa: N803
        base = int(SW_START.timestamp())
        values = [{"value": {"doubleValue": 1.0 + i / 1000},
                   "timestamp": {"timeInSeconds": base + i * 120}, "quality": "GOOD"}
                  for i in range(self.points)]
        return {"successEntries": [{"entryId": e["entryId"],
                                    "assetPropertyValueHistory": values} for e in entries],
                "errorEntries": [], "skippedEntries": []}

    def get_asset_property_value_history(self, **_kwargs):
        return {"assetPropertyValueHistory": []}

    def batch_get_asset_property_value(self, entries, **_kwargs):
        stamp = {"timeInSeconds": int(SW_START.timestamp())}
        return {"successEntries": [{"entryId": e["entryId"], "assetPropertyValue": {
            "value": {"doubleValue": 2.5}, "timestamp": stamp,
            "quality": "GOOD"}} for e in entries], "errorEntries": [], "skippedEntries": []}

    def get_asset_property_value(self, **_kwargs):
        return {"propertyValue": {"value": {"doubleValue": 2.5}, "quality": "GOOD",
                                  "timestamp": {"timeInSeconds": int(SW_START.timestamp())}}}


def _sitewise_tools() -> dict:
    return {t.tool_name: t for t in sa._make_sitewise_direct_tool(
        region="us-east-1", connection_config={}, system_id=SW_SYSTEM)}


def _history_call(monkeypatch, points: int = 24) -> tuple[dict, str]:
    """``(tool input, readings text)`` of one real SiteWise (Direct) history
    call over the three series, the service faked."""
    from agents import aws_session
    monkeypatch.setattr(aws_session, "boto3_client_for",
                        lambda service, cfg, default_region=None: _SeriesClient(points))
    tool = _sitewise_tools()[HISTORY_TOOL]
    tool_input = {"entries_json": json.dumps([{"assetId": SW_ASSET, "propertyId": p}
                                              for p in SW_PROPS]),
                  "start_iso": SW_START.isoformat(),
                  "end_iso": (SW_START + timedelta(hours=1)).isoformat()}
    raw = tool._tool_func(**tool_input)
    return tool_input, raw if isinstance(raw, str) else raw["content"][0]["text"]


def _values_call(monkeypatch) -> str:
    """The readings text of one real current-values read over two series."""
    from agents import aws_session
    monkeypatch.setattr(aws_session, "boto3_client_for",
                        lambda service, cfg, default_region=None: _SeriesClient(24))
    return _sitewise_tools()[VALUES_TOOL]._tool_func(**VALUES_INPUT)


def _capture(state: dict, name: str, tool_input: dict, *, text: str = "",
             status: str = "success", exception: "Exception | None" = None,
             retry: bool = False) -> None:
    """One completed tool call through the capture hook exactly as a SiteWise
    (Direct) system agent gets it (``system_agent._system_agent_hooks``)."""
    event = SimpleNamespace(
        selected_tool=None,
        tool_use={"toolUseId": f"t-{next(_TOOL_USE_IDS)}", "name": name, "input": tool_input},
        invocation_state=state, result={"status": status, "content": [{"text": text}]},
        exception=exception, retry=retry, agent=SimpleNamespace(messages=[]))
    tools = list(_sitewise_tools().values())
    hook = sa._system_agent_hooks(SW_SYSTEM, "SiteWise (Direct)", "", tools)[1]
    assert isinstance(hook, InvocationToolResultCapture)
    hook._on_after_tool_call(event)


def _read(state: dict, name: str, tool_input: dict, *, retry: bool = False) -> None:
    """One call of the real SiteWise (Direct) tool ``name`` (the source as the
    last ``_history_call`` / ``_values_call`` faked it) through the same hook,
    as the strands executor runs it: Before, the tool, After. Round 3: a read
    counts only from the count the tool reports from its own parse, so a test
    whose read must count runs the tool; a hand-made result text (``_capture``)
    can only show what does not count."""
    from tests.unit._api_reads import run_tool
    tools = _sitewise_tools()
    hook = sa._system_agent_hooks(SW_SYSTEM, "SiteWise (Direct)", "", list(tools.values()))[1]
    run_tool(hook, state, tools[name], tool_input, retry=retry)


def _listed_assets(count: int = 2) -> str:
    """A small asset list, as a non-history read returns it."""
    return json.dumps([{"id": f"a-{i}", "name": f"unit-{i}"} for i in range(count)])


def test_a_successful_api_read_writes_a_source_side_read_entry(monkeypatch):
    """Live walk: an answer read from SiteWise (Direct), 72 points over three
    series, showed its chip as unverified ("No captured evidence for this
    claim"). The call ran no SQL, so the source side recorded nothing. Its
    successful read is now that record, built from the capture alone: no
    query text (the store's API-read note says why) and no row count (points
    are not rows)."""
    tool_input, readings = _history_call(monkeypatch)
    state: dict = {}
    _read(state, HISTORY_TOOL, tool_input)
    assert collect_sidecar_statements(state) == []  # no SQL ran
    assert [line.split(", ")[-1] for line in tc.collect_coverage(state)] == [
        "24 points (samples 2026-03-02T08:00Z to 2026-03-02T08:46Z)"] * 3
    ref = mint_query_ref(SID, "tooluse_sw")
    _issue_ctx(SID, ref)
    _run_query("Three channels read.", state, system_id=SW_SYSTEM)
    entry = provenance_store.get_query(SID, ref)
    assert entry is not None
    assert entry["origin"] == "system-agent"
    assert entry["executed_query"] == ""
    assert entry["note"] == provenance_store.API_READ_NOTE
    assert "row_count" not in entry
    assert entry["entity"] == {"table": "", "columns": []}
    assert entry["statement_count"] == 1
    assert entry["system_id"] == SW_SYSTEM
    assert entry["tool_name"] == "query_line_telemetry"
    assert entry["kg_artifacts"] == []
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", entry["captured_at"])
    assert "query_fingerprint" not in entry and "query_shape_version" not in entry


def test_each_successful_read_counts_once_and_failed_ones_not_at_all(monkeypatch):
    """``statement_count`` on a read entry counts the call's successful DATA
    reads: a history read that returned points and a retained current-values
    read that returned values, once each. A read that raised is not counted,
    and neither is a discovery read (round 2: this test used to count a
    successful asset list as a read; listing what exists is discovery, not
    evidence for an answer)."""
    _values_call(monkeypatch)
    tool_input, readings = _history_call(monkeypatch)
    state: dict = {}
    _read(state, VALUES_TOOL, VALUES_INPUT)
    _capture(state, HISTORY_TOOL, tool_input, exception=RuntimeError("throttled"))
    _capture(state, LIST_TOOL, {}, text=_listed_assets())
    _read(state, HISTORY_TOOL, tool_input)
    ref = mint_query_ref(SID, "tooluse_sw_two")
    _issue_ctx(SID, ref)
    _run_query("ok", state, system_id=SW_SYSTEM)
    assert provenance_store.get_query(SID, ref)["statement_count"] == 2


def test_a_history_read_counts_only_when_it_returned_points(monkeypatch):
    """A history read decides by its own points, never by the retained
    metadata: a zero-point read's small result reads as a success there (an
    empty series is a list), and it is still not a read that returned
    anything."""
    tool_input, readings = _history_call(monkeypatch, points=0)
    state: dict = {}
    _read(state, HISTORY_TOOL, tool_input)
    assert [item.get("_has_success") for item in state[TOOL_RESULTS_STATE_KEY]] == [True]
    ref = mint_query_ref(SID, "tooluse_sw_none")
    _issue_ctx(SID, ref)
    _run_query("No values in that window.", state, system_id=SW_SYSTEM)
    assert provenance_store.get_query(SID, ref) is None


def _failed_reads(kind: str, monkeypatch) -> dict:
    """A call whose every read failed, raised or returned nothing."""
    tool_input, readings = _history_call(monkeypatch)
    error = json.dumps({"error": "window too large", "error_type": "invalid_request"})
    state: dict = {}
    if kind == "raised":
        _capture(state, HISTORY_TOOL, tool_input, exception=RuntimeError("throttled"))
        _capture(state, LIST_TOOL, {}, exception=TimeoutError("read timed out"))
    elif kind == "error_envelope":
        _capture(state, HISTORY_TOOL, tool_input, text=error)
        _capture(state, LIST_TOOL, {}, text=error)
    elif kind == "marked_error":
        _capture(state, HISTORY_TOOL, tool_input, text=readings, status="error")
        _capture(state, LIST_TOOL, {}, text=_listed_assets(), status="error")
    elif kind == "returned_nothing":
        _, empty = _history_call(monkeypatch, points=0)
        _capture(state, HISTORY_TOOL, tool_input, text=empty)
        _capture(state, LIST_TOOL, {}, text="[]")
    return state


@pytest.mark.parametrize("kind", ["raised", "error_envelope", "marked_error",
                                  "returned_nothing"])
def test_a_call_whose_reads_all_failed_writes_nothing(kind, monkeypatch):
    """Negative guard (passes before and after the fix): no successful read,
    no entry, so the record stays unverified."""
    state = _failed_reads(kind, monkeypatch)
    calls = []
    monkeypatch.setattr(provenance_store, "put_turn_queries",
                        lambda *a, **k: calls.append(a) or 0)
    _issue_ctx(SID, mint_query_ref(SID, f"tooluse_{kind}"))
    _run_query("Nothing could be read.", state, system_id=SW_SYSTEM)
    assert calls == []


def test_a_raising_store_leaves_an_api_read_answer_unchanged(monkeypatch, caplog):
    tool_input, readings = _history_call(monkeypatch)
    state: dict = {}
    _read(state, HISTORY_TOOL, tool_input)
    _issue_ctx(SID, mint_query_ref(SID, "tooluse_sw_ok"))
    expected = _run_query("Three channels read.", dict(state), system_id=SW_SYSTEM)

    def _boom(*_a, **_k):
        raise RuntimeError("ddb down")

    monkeypatch.setattr(provenance_store, "put_turn_queries", _boom)
    _issue_ctx(SID, mint_query_ref(SID, "tooluse_sw_boom"))
    with caplog.at_level(logging.DEBUG):
        assert _run_query("Three channels read.", state, system_id=SW_SYSTEM) == expected
    assert any("MODE_A_PROVENANCE_SIDECAR failed (RuntimeError)" in r.getMessage()
               for r in caplog.records)


# --------------------------------------------------------------------------- #
# Round 2: only a data read that returned data is evidence
# --------------------------------------------------------------------------- #
def _no_entry_written(monkeypatch, state: dict, label: str) -> list:
    """Run the Mode A finish path over ``state`` and return the store writes."""
    calls: list = []
    monkeypatch.setattr(provenance_store, "put_turn_queries",
                        lambda *a, **k: calls.append(a) or 0)
    _issue_ctx(SID, mint_query_ref(SID, f"tooluse_{label}"))
    _run_query("Nothing could be read.", state, system_id=SW_SYSTEM)
    return calls


def test_discovery_succeeds_and_every_data_read_fails_so_nothing_is_written(monkeypatch):
    """Reproduction: listing, describing and walking the hierarchy all
    succeeded, every data read failed, and the call still read as
    corroborated. Discovery tells the agent what exists; it is not evidence
    for an answer, so the record stays unverified."""
    tool_input, _ = _history_call(monkeypatch)
    error = json.dumps({"error": "window too large", "error_type": "invalid_request"})
    state: dict = {}
    _capture(state, "sitewise_list_asset_models", {}, text=json.dumps([{"id": "m-1"}]))
    _capture(state, LIST_TOOL, {}, text=_listed_assets())
    _capture(state, "sitewise_describe_asset", {"asset_id": "a-0"},
             text=json.dumps({"assetId": "a-0", "assetProperties": [{"id": "chan-a"}]}))
    _capture(state, "sitewise_list_associated_assets", {"asset_id": "a-0"},
             text=json.dumps({"assets": [{"id": "a-1", "depth": 1}], "truncated": False}))
    _capture(state, VALUES_TOOL, VALUES_INPUT, text=error)
    _capture(state, HISTORY_TOOL, tool_input, exception=RuntimeError("throttled"))
    assert _no_entry_written(monkeypatch, state, "discovery_only") == []


def test_a_retried_success_alone_writes_nothing(monkeypatch):
    """Reproduction: a result strands discarded (``event.retry``) was the
    only success, and it upgraded the record. The kept attempt raised."""
    _values_call(monkeypatch)
    state: dict = {}
    _read(state, VALUES_TOOL, VALUES_INPUT, retry=True)
    _capture(state, VALUES_TOOL, VALUES_INPUT, exception=RuntimeError("throttled"))
    assert _no_entry_written(monkeypatch, state, "retried_api") == []


def test_a_retried_sql_success_alone_writes_nothing(monkeypatch):
    """The same for a SQL system: a retried attempt's executed query is not a
    statement the answer rests on, so no statement entry is written."""
    result = json.dumps({"columns": ["part_no"], "rows": [["P-1"]], "row_count": 1,
                         "_adt_executed_query": SQL_1})
    state: dict = {}
    hook = InvocationToolResultCapture(system_id="erp")
    for retry, exception in ((True, None), (False, RuntimeError("timed out"))):
        hook._on_after_tool_call(SimpleNamespace(
            selected_tool=None, tool_use={"toolUseId": "t-sql", "name": "query_erp",
                                          "input": {"sql": SQL_1}},
            invocation_state=state, result={"status": "success", "content": [{"text": result}]},
            exception=exception, retry=retry, agent=SimpleNamespace(messages=[])))
    calls: list = []
    monkeypatch.setattr(provenance_store, "put_turn_queries",
                        lambda *a, **k: calls.append(a) or 0)
    _issue_ctx(SID, mint_query_ref(SID, "tooluse_retried_sql"))
    _run_query("Nothing could be read.", state)
    assert calls == []


@pytest.mark.parametrize("value", [0, 99])
@pytest.mark.parametrize("field", ["count", "row_count", "item_count", "success_count",
                                   "total"])
def test_a_count_field_in_a_result_never_makes_a_read(field, value, monkeypatch):
    """Reproduction: ``{"count": 0}`` read as a successful
    read, and a positive count field would too. No field of a result is read
    as a count any more: a read counts only by the count the tool reported
    from its own parse, so a result text alone, zero or 99, writes nothing."""
    state: dict = {}
    _capture(state, VALUES_TOOL, VALUES_INPUT, text=json.dumps({field: value}))
    assert _no_entry_written(monkeypatch, state, f"field_{field}_{value}") == []


def test_a_data_read_that_returned_values_writes_a_read_entry(monkeypatch):
    """Guard: a current-values read that returned values is a data read, so
    the call gets its read entry."""
    _values_call(monkeypatch)
    state: dict = {}
    _read(state, VALUES_TOOL, VALUES_INPUT)
    ref = mint_query_ref(SID, "tooluse_values")
    _issue_ctx(SID, ref)
    _run_query("Two channels read.", state, system_id=SW_SYSTEM)
    entry = provenance_store.get_query(SID, ref)
    assert entry["note"] == provenance_store.API_READ_NOTE
    assert entry["statement_count"] == 1


def test_a_single_current_value_read_writes_a_read_entry(monkeypatch):
    """Reproduction: one SiteWise current value is an ordinary data
    read, and its result carries no count field, so the summary-based count
    made it zero and the chip read unverified. The tool reports the value it
    parsed."""
    _values_call(monkeypatch)
    state: dict = {}
    _read(state, "sitewise_get_asset_property_value", {"asset_id": SW_ASSET,
                                                        "property_id": SW_PROPS[0]})
    ref = mint_query_ref(SID, "tooluse_one_value")
    _issue_ctx(SID, ref)
    _run_query("The current value is 2.5.", state, system_id=SW_SYSTEM)
    entry = provenance_store.get_query(SID, ref)
    assert entry is not None and entry["statement_count"] == 1
    assert entry["note"] == provenance_store.API_READ_NOTE
