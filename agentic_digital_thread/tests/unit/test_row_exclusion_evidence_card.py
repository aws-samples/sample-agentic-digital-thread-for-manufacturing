# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The source owner's row exclusion on the evidence card of the call it was
applied to.

A Mode A Athena call whose query carried a table's row exclusion keeps the
rule as it was applied (the owner's label, its conditions, and who set it,
when and from what source) under that call's toolUseId. The capture of that
call, and of no other, moves it onto its sidecar row; the call's provenance
entry carries it, the store bounds it, and the turn's provenance record gets
it, so the card shows it to everyone who sees the answer. A read without the
rule, a failed call and a call without an id keep nothing; nothing about it is
added to the tool text the model reads.

Fully offline; neutral names (``table_x`` with ``status`` and ``kind``).
"""
from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest
from strands._async import run_async

import agents.athena_tools as at
import agents.system_agent as sa
from agents import record_key, record_time, state_times, storage_bounds

_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1"}
_COLUMNS = {"status": "varchar", "kind": "varchar", "amount": "double"}
_DESC = "table_x(status: varchar; kind: varchar; amount: double)"
_LABEL = "Test rows"
_CONDITIONS = [{"column": "status", "op": "in", "values": ["draft", "void"]},
               {"column": "kind", "op": "empty"}]
_BY, _AT, _SOURCE = "owner-1", "2026-11-01T10:00:00Z", "data team runbook"


def _rx():
    return importlib.import_module("agents.row_exclusion")


def _fact():
    return _rx().exclusion_from(_LABEL, _CONDITIONS)


def _missing() -> str:
    return "SELECT COUNT(*) AS n FROM table_x WHERE amount > 0"


def _present() -> str:
    return f"{_missing()} AND {_rx().predicate_sql(_fact())}"


class _Lake:
    def __init__(self, *, fail: bool = False):
        self.queries: list[str] = []
        self.fail = fail

    def get_table_metadata(self, **_kwargs):
        return {"TableMetadata": {
            "Name": "table_x",
            "Columns": [{"Name": n, "Type": t} for n, t in _COLUMNS.items()],
            "PartitionKeys": [], "Parameters": {"table_type": "ICEBERG"}}}

    def start_query_execution(self, QueryString, **_kwargs):  # noqa: N803 - boto3 spelling
        self.queries.append(QueryString)
        return {"QueryExecutionId": f"q{len(self.queries)}"}

    def get_query_execution(self, QueryExecutionId):  # noqa: N803 - boto3 spelling
        sql = self.queries[int(QueryExecutionId[1:]) - 1]
        data = "$partitions" not in sql and "information_schema" not in sql
        if self.fail and data:
            return {"QueryExecution": {"Status": {
                "State": "FAILED", "StateChangeReason": "SYNTAX_ERROR: line 1:1"}}}
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"},
                                   "Statistics": {"DataScannedInBytes": 10}}}

    def get_query_results(self, QueryExecutionId, **_kwargs):  # noqa: N803 - boto3 spelling
        sql = self.queries[int(QueryExecutionId[1:]) - 1]
        if "$partitions" in sql:
            names, rows = ["partition"], [["{}"]]
        elif "information_schema.columns" in sql:
            names = ["column_name", "data_type", "extra_info"]
            rows = [[n, t, ""] for n, t in _COLUMNS.items()]
        else:
            names, rows = ["n"], [["7"]]
        header = {"Data": [{"VarCharValue": n} for n in names]}
        return {"ResultSet": {
            "ResultSetMetadata": {"ColumnInfo": [{"Name": n, "Type": "varchar"} for n in names]},
            "Rows": [header] + [{"Data": [{"VarCharValue": v} for v in row]} for row in rows]}}

    def stop_query_execution(self, **_kwargs):
        return None


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    resets = (at.clear_schema_cache, at._PARTITION_COLUMNS_CACHE.clear, storage_bounds.reset,
              state_times.reset, record_time.reset, record_key.reset, _rx().reset)
    for reset in resets:
        reset()
    yield
    for reset in resets:
        reset()


def _stored_graph():
    """The graph a bake reads: the system's manifest lists table_x, which
    holds the stored exclusion with who set it, when and from what source."""
    from tests.unit._row_exclusion_graph import RxGraph
    g = RxGraph({"system-a": {"table_x": dict(_COLUMNS)}})
    g.list_table("system-a", "table_x")
    g.entries["system-a.table_x"]["props"] = {
        "row_exclusion": json.dumps({"label": _LABEL, "conditions": _CONDITIONS}),
        "row_exclusion_by": _BY, "row_exclusion_at": _AT, "row_exclusion_source": _SOURCE}
    return g


def _hook():
    from agents.tool_result_capture import InvocationToolResultCapture
    return InvocationToolResultCapture(system_id="system-a", boundary="athena")


def _invocation() -> dict:
    state: dict = {}
    _hook()._on_invocation_start(SimpleNamespace(invocation_state=state))
    return state


def _captured(state: dict, sql: str, call_id: str, text: str) -> list[dict]:
    """The capture hook's after-call for one call; the sidecar rows so far."""
    from agents.tool_result_capture import SIDECAR_STATEMENTS_STATE_KEY
    _hook()._on_after_tool_call(SimpleNamespace(
        invocation_state=state, selected_tool=SimpleNamespace(tool_name="athena_run_query"),
        tool_use={"name": "athena_run_query", "toolUseId": call_id, "input": {"sql": sql}},
        result={"status": "success", "content": [{"text": text}]}, exception=None))
    return list(state.get(SIDECAR_STATEMENTS_STATE_KEY) or [])


def _calls(monkeypatch, lake, calls, *, state, graph=None):
    """Tool calls ``[(call id, sql)]`` of one Mode A call on ``system-a``, as
    the model makes them (each tool context carries its toolUseId and the
    invocation state). Returns the tool texts in order."""
    monkeypatch.setattr(_rx(), "_graph_query", (graph or _stored_graph()).seam())
    monkeypatch.setattr(record_key, "_graph_query", lambda: (lambda *_a, **_k: [{}]))
    sa._remember_turn_time_facts("system-a", [], _DESC)
    record_key.remember("system-a", {})
    monkeypatch.setattr(at, "boto3_client_for", lambda *_a, **_k: lake)
    tool = sa._make_scoped_athena_run_query(_CFG, {}, "system-a")
    texts: list[str] = []

    async def run():
        for call_id, sql in calls:
            context = SimpleNamespace(
                tool_use={"name": "athena_run_query", "toolUseId": call_id,
                          "input": {"sql": sql}},
                invocation_state=state)
            texts.append(await asyncio.to_thread(tool, sql=sql, tool_context=context))

    def invoke(_agent, _question):
        run_async(run)
        return "The count is 7.", {}

    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state", invoke)
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
    monkeypatch.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
    asyncio.run(sa._run_mode_a_query(object(), "system-a", "How many?", ""))
    return texts


def _expected_rule() -> dict:
    return {"table": "table_x", "label": _LABEL, "conditions": _CONDITIONS,
            "at": _AT, "source": _SOURCE}


# --------------------------------------------------------------------------- #
# The applied rule is kept with when it was set and from what source.
# --------------------------------------------------------------------------- #
def test_the_bake_keeps_when_the_rule_was_set_and_from_what_source(monkeypatch):
    monkeypatch.setattr(_rx(), "_graph_query", _stored_graph().seam())
    sa._remember_turn_time_facts("system-a", [], _DESC)
    kept = _rx().exclusion_for("system-a", "table_x")
    assert kept == _fact()                       # the rule itself compares as before
    assert (kept.at, kept.source) == (_AT, _SOURCE)
    found = _rx().decide_for("system-a", _present())
    assert found.applied and (found.at, found.source) == (_AT, _SOURCE)
    assert _rx().card_record(found) == _expected_rule()


@pytest.mark.parametrize("actor", ["someone@example.com", "ADT", "unknown"])
def test_who_set_the_rule_never_reaches_the_card(monkeypatch, actor):
    """The sign-in identity that set the rule stays in the admin audit trail:
    it is not kept with the fact, the call's entry or the frame record."""
    from agents import provenance_store
    from agents.provenance import enrich_records_from_sidecar
    graph = _stored_graph()
    graph.entries["system-a.table_x"]["props"]["row_exclusion_by"] = actor
    lake, state = _Lake(), _invocation()
    (text,) = _calls(monkeypatch, lake, [("t1", _present())], state=state, graph=graph)
    entry = sa._build_sidecar_entry(_captured(state, _present(), "t1", text), "system-a")
    stored = provenance_store._normalize_entry("sess-1", "q-0123456789ab", entry)
    payload = enrich_records_from_sidecar(_frame(), session_id="sess-1",
                                          get_entry=lambda _s, _r: stored)
    (rule,) = payload["records"][0]["row_exclusions"]
    assert "by" not in rule and rule == _expected_rule()
    for seen in (entry, stored, payload):
        assert f'"{actor}"' not in json.dumps(seen)


def test_a_read_without_the_rule_has_no_card_record(monkeypatch):
    monkeypatch.setattr(_rx(), "_graph_query", _stored_graph().seam())
    sa._remember_turn_time_facts("system-a", [], _DESC)
    found = _rx().decide_for("system-a", _missing())
    assert found is not None and not found.applied
    assert _rx().card_record(found) is None
    assert _rx().card_record(None) is None


# --------------------------------------------------------------------------- #
# Per call: the card shows the rule as THAT call's query ran with it.
# --------------------------------------------------------------------------- #
def test_an_applied_rule_reaches_its_own_calls_evidence_entry(monkeypatch):
    lake, state = _Lake(), _invocation()
    (text,) = _calls(monkeypatch, lake, [("t1", _present())], state=state)
    payload = json.loads(text)
    assert payload["row_exclusion"]["applied"] is True
    rows = _captured(state, _present(), "t1", text)
    assert rows[-1]["row_exclusions"] == [_expected_rule()]
    entry = sa._build_sidecar_entry(rows, "system-a")
    assert entry["row_exclusions"] == [_expected_rule()]


def test_the_tool_text_the_model_reads_gains_nothing(monkeypatch):
    lake, state = _Lake(), _invocation()
    (text,) = _calls(monkeypatch, lake, [("t1", _present())], state=state)
    payload = json.loads(text)
    assert set(payload["row_exclusion"]) == {"table", "label", "applied", "predicate"}
    for hidden in ("row_exclusions", _BY, _AT, _SOURCE):
        assert hidden not in text


def test_a_read_without_the_rule_shows_no_rule_on_its_card(monkeypatch):
    lake, state = _Lake(), _invocation()
    first, second = _calls(monkeypatch, lake, [("t1", _missing()), ("t2", _missing())],
                           state=state)
    assert json.loads(first)["error_name"] == "ROW_EXCLUSION"
    assert json.loads(second)["row_exclusion"]["applied"] is False
    rows = _captured(state, _missing(), "t2", second)
    assert "row_exclusions" not in rows[-1]
    assert "row_exclusions" not in sa._build_sidecar_entry(rows, "system-a")


def test_a_failed_call_keeps_nothing_and_drops_what_its_id_held(monkeypatch):
    from agents.tool_result_capture import APPLIED_EXCLUSIONS_STATE_KEY
    lake, state = _Lake(fail=True), _invocation()
    state[APPLIED_EXCLUSIONS_STATE_KEY] = {
        "t1": {"recorded": "SELECT 1", "rules": [_expected_rule()]}}
    (text,) = _calls(monkeypatch, lake, [("t1", _present())], state=state)
    assert "error" in json.loads(text)
    assert not state[APPLIED_EXCLUSIONS_STATE_KEY]


def test_a_call_without_an_id_keeps_nothing(monkeypatch):
    from agents.tool_result_capture import APPLIED_EXCLUSIONS_STATE_KEY
    lake, state = _Lake(), _invocation()
    (text,) = _calls(monkeypatch, lake, [("", _present())], state=state)
    assert json.loads(text)["row_exclusion"]["applied"] is True
    assert not state.get(APPLIED_EXCLUSIONS_STATE_KEY)


def test_a_rule_cleared_after_the_call_still_shows_as_it_was_applied(monkeypatch):
    lake, state = _Lake(), _invocation()
    (text,) = _calls(monkeypatch, lake, [("t1", _present())], state=state)
    _rx().remember("system-a", {})               # the owner clears it afterwards
    rows = _captured(state, _present(), "t1", text)
    assert rows[-1]["row_exclusions"] == [_expected_rule()]


def test_a_kept_rule_reaches_only_its_own_call_and_record_once():
    from agents.tool_result_capture import (APPLIED_EXCLUSIONS_STATE_KEY,
                                            record_sidecar_statement)
    recorded = "SELECT COUNT(*) AS n FROM table_x LIMIT 500"

    def kept():
        return {"t1": {"recorded": recorded, "rules": [_expected_rule()]}}

    state = {APPLIED_EXCLUSIONS_STATE_KEY: kept()}
    for call_id, query in (("t2", recorded), (None, recorded), ("t1", recorded + " ")):
        record_sidecar_statement(state, executed_query=query, metadata={"row_count": 1},
                                 call_id=call_id)
    state[APPLIED_EXCLUSIONS_STATE_KEY] = kept()
    for _ in range(2):
        record_sidecar_statement(state, executed_query=recorded, metadata={"row_count": 1},
                                 call_id="t1")
    rows = state["_adt_sidecar_statements"]
    assert [row.get("row_exclusions") for row in rows] == [
        None, None, None, [_expected_rule()], None]


def test_an_entry_lists_each_distinct_rule_of_its_statements_once():
    other = {**_expected_rule(), "table": "table_y", "label": "Old rows"}
    rows = [{"query_text": "SELECT 1", "row_count": 1, "row_exclusions": [_expected_rule()]},
            {"query_text": "SELECT 2", "row_count": 1, "row_exclusions": [_expected_rule()]},
            {"query_text": "SELECT 3", "row_count": 1, "row_exclusions": [other]},
            {"query_text": "SELECT 4", "row_count": 1}]
    entry = sa._build_sidecar_entry(rows, "system-a")
    assert entry["row_exclusions"] == [_expected_rule(), other]


# --------------------------------------------------------------------------- #
# The store and the frame: bounded, whole rules only, never at a citation's cost.
# --------------------------------------------------------------------------- #
def _entry(rules) -> dict:
    return {"system_id": "system-a", "tool_name": "query_system_a",
            "executed_query": "SELECT 1", "captured_at": _AT, "origin": "system-agent",
            "row_exclusions": rules}


def test_the_store_keeps_valid_rules_and_drops_a_malformed_one_whole():
    from agents import provenance_store
    bad_op = {**_expected_rule(), "conditions": [{"column": "status", "op": "like",
                                                  "values": ["d%"]}]}
    long_value = {**_expected_rule(), "conditions": [{"column": "status", "op": "in",
                                                      "values": ["v" * 500]}]}
    no_values = {**_expected_rule(), "conditions": [{"column": "status", "op": "in"}]}
    stored = provenance_store._normalize_entry(
        "sess-1", "q-0123456789ab",
        _entry([bad_op, _expected_rule(), long_value, no_values, "not a rule"]))
    assert stored["row_exclusions"] == [_expected_rule()]


def _bytes(value) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _stored_rules(rules) -> dict:
    from agents import provenance_store
    return provenance_store._normalize_entry("sess-1", "q-0123456789ab", _entry(rules))


def test_the_store_bounds_the_rules_and_drops_any_actor():
    from agents import provenance
    wordy = {**_expected_rule(), "source": "s" * 5000, "by": "b" * 5000}
    stored = _stored_rules([wordy] * 3 + [_expected_rule()] * 10)
    rules = stored["row_exclusions"]
    assert len(rules) == 2 and all("by" not in r for r in rules)
    assert all(_bytes(r) <= provenance.MAX_ROW_EXCLUSION_BYTES for r in rules)


def _source_room() -> int:
    """How many ASCII source characters the neutral rule has room for."""
    from agents import provenance
    return provenance.MAX_ROW_EXCLUSION_BYTES - _bytes({**_expected_rule(), "source": ""})


def test_a_source_that_fills_the_rule_cap_exactly_is_kept_whole():
    source = "s" * _source_room()
    (rule,) = _stored_rules([{**_expected_rule(), "source": source}])["row_exclusions"]
    assert rule["source"] == source and "source_shortened" not in rule


def test_a_source_one_character_over_is_shortened_and_says_so():
    from agents import provenance
    source = "".join(chr(97 + i % 26) for i in range(_source_room() + 1))
    (rule,) = _stored_rules([{**_expected_rule(), "source": source}])["row_exclusions"]
    assert rule["source_shortened"] is True
    assert rule["source"] and source.startswith(rule["source"]) and rule["source"] != source
    assert _bytes(rule) <= provenance.MAX_ROW_EXCLUSION_BYTES
    assert {k: v for k, v in rule.items() if k not in {"source", "source_shortened"}} == {
        k: v for k, v in _expected_rule().items() if k != "source"}


def test_a_shortened_source_stays_the_same_through_the_frame():
    from agents.provenance import enrich_records_from_sidecar
    stored = _stored_rules([{**_expected_rule(), "source": "s" * 2000}])
    payload = enrich_records_from_sidecar(_frame(), session_id="sess-1",
                                          get_entry=lambda _s, _r: stored)
    assert payload["records"][0]["row_exclusions"] == stored["row_exclusions"]
    assert stored["row_exclusions"][0]["source_shortened"] is True


def test_a_wordy_but_ordinary_rule_shows_its_whole_source():
    """Nine short values over three conditions, a short label and a source of
    about five hundred characters fit the rule cap whole."""
    source = ("Rows the data team marks as tests or drafts in the weekly cleanup; "
              "see the runbook section on test data. ") * 5
    assert 480 <= len(source) <= 560
    rule = {**_expected_rule(), "source": source, "conditions": [
        {"column": "status", "op": "in", "values": ["draft", "void", "test"]},
        {"column": "kind", "op": "not_in", "values": ["a", "b", "c"]},
        {"column": "grade", "op": "in", "values": ["x1", "x2", "x3"]}]}
    (stored,) = _stored_rules([rule])["row_exclusions"]
    assert stored == rule


def test_a_long_table_name_and_label_are_never_shortened():
    rule = {**_expected_rule(), "table": "t" * 200, "label": "L" * 80}
    (stored,) = _stored_rules([rule])["row_exclusions"]
    assert stored["table"] == "t" * 200 and stored["label"] == "L" * 80


def test_a_rule_whose_conditions_alone_overflow_the_cap_is_left_out_and_marked():
    huge = {**_expected_rule(), "source": "", "conditions": [
        {"column": f"c{i}", "op": "in", "values": [f"{i}-{j}-" + "v" * 100 for j in range(4)]}
        for i in range(8)]}
    stored = _stored_rules([huge])
    assert "row_exclusions" not in stored and stored["row_exclusions_omitted"] is True


def test_a_value_holding_a_double_quote_is_malformed_and_not_marked():
    quoted = {**_expected_rule(), "conditions": [
        {"column": "status", "op": "in", "values": ['say "draft"']}]}
    stored = _stored_rules([quoted])
    assert "row_exclusions" not in stored and "row_exclusions_omitted" not in stored


def test_rules_past_the_per_record_count_are_marked():
    from agents import provenance
    rules = [{**_expected_rule(), "table": f"table_{i}"}
             for i in range(provenance.MAX_ROW_EXCLUSIONS + 1)]
    stored = _stored_rules(rules)
    assert len(stored["row_exclusions"]) == provenance.MAX_ROW_EXCLUSIONS
    assert stored["row_exclusions_omitted"] is True


def test_an_entry_with_no_rule_stores_no_rule_key():
    from agents import provenance_store
    stored = provenance_store._normalize_entry("sess-1", "q-0123456789ab", _entry([]))
    assert "row_exclusions" not in stored
    stored = provenance_store._normalize_entry(
        "sess-1", "q-0123456789ab", {k: v for k, v in _entry([]).items()
                                     if k != "row_exclusions"})
    assert "row_exclusions" not in stored


def _frame(n: int = 1) -> dict:
    from agents.provenance import build_provenance_records
    calls = [{"name": f"system_a___query_{i}", "toolUseId": f"t{i}"} for i in range(n)]
    return build_provenance_records(
        session_id="sess-1", tool_calls=calls, tool_inputs={}, tool_results_metadata={},
        evidence_by_tool={}, rules_applied=[], verdict=None, captured_at=_AT)


def test_the_frame_record_carries_the_rules_its_entry_kept():
    from agents.provenance import enrich_records_from_sidecar
    payload = enrich_records_from_sidecar(
        _frame(), session_id="sess-1", get_entry=lambda _s, _r: _entry([_expected_rule()]))
    assert payload["records"][0]["row_exclusions"] == [_expected_rule()]


def test_a_rejected_turn_shows_no_rule():
    from agents.provenance import enrich_records_from_sidecar
    payload = enrich_records_from_sidecar(
        _frame(), session_id="sess-1", verdict="rejected",
        get_entry=lambda _s, _r: _entry([_expected_rule()]))
    assert "row_exclusions" not in payload["records"][0]


def test_an_entry_with_a_malformed_rule_shows_no_such_rule_on_the_record():
    from agents.provenance import enrich_records_from_sidecar
    bad = {**_expected_rule(), "conditions": [{"column": "status", "op": "drop"}]}
    payload = enrich_records_from_sidecar(
        _frame(), session_id="sess-1", get_entry=lambda _s, _r: _entry([bad]))
    assert "row_exclusions" not in payload["records"][0]


def test_rules_never_push_a_citation_off_the_frame():
    """A frame whose every record has a large rule: rules are left off the
    records they would push past the frame's byte cap, and every citation
    record stays."""
    from agents.provenance import MAX_PAYLOAD_BYTES, enrich_records_from_sidecar
    wide = {**_expected_rule(), "conditions": [
        {"column": f"c{i}", "op": "in", "values": [f"value-{i}-{j}" for j in range(6)]}
        for i in range(5)]}
    payload = enrich_records_from_sidecar(
        _frame(8), session_id="sess-1", get_entry=lambda _s, _r: _entry([wide]))
    assert len(payload["records"]) == 8 and payload["truncated"] is False
    assert len(json.dumps(payload, separators=(",", ":")).encode()) <= MAX_PAYLOAD_BYTES
    shown = [r for r in payload["records"] if "row_exclusions" in r]
    assert shown and len(shown) < 8
    assert all(r["row_exclusions"] == [wide] for r in shown)


# --------------------------------------------------------------------------- #
# Reload: the kept frame replays the same card.
# --------------------------------------------------------------------------- #
def test_a_reloaded_turn_gets_the_same_rule_on_its_record(monkeypatch):
    from agents import turn_evidence_store as tes
    from agents.job_store import JobStore
    from agents.provenance import enrich_records_from_sidecar
    monkeypatch.setattr(tes, "_job_store", JobStore(
        kind="turn-evidence", table=None, ttl_seconds=tes.TTL_SECONDS))
    payload = enrich_records_from_sidecar(
        _frame(), session_id="sess-1", get_entry=lambda _s, _r: _entry([_expected_rule()]))
    turn = tes.turn_identity("How many?", ["t0"])
    assert tes.keep("user-1", "sess-1", tes.fold(None, payload), turn=turn, gated=False)
    frames = tes.for_turns("user-1", "sess-1", {0: (turn, False)})
    assert frames[0]["records"][0]["row_exclusions"] == [_expected_rule()]


# --------------------------------------------------------------------------- #
# The omission marker: a captured rule whose details do not fit the frame.
# --------------------------------------------------------------------------- #
def _wide() -> dict:
    return {**_expected_rule(), "conditions": [
        {"column": f"c{i}", "op": "in", "values": [f"value-{i}-{j}" for j in range(6)]}
        for i in range(5)]}


def test_a_record_whose_rule_does_not_fit_the_frame_says_a_rule_left_rows_out():
    from agents.provenance import MAX_PAYLOAD_BYTES, enrich_records_from_sidecar
    payload = enrich_records_from_sidecar(
        _frame(8), session_id="sess-1", get_entry=lambda _s, _r: _entry([_wide()]))
    records = payload["records"]
    assert len(records) == 8 and payload["truncated"] is False
    assert _bytes(payload) <= MAX_PAYLOAD_BYTES
    marked = [r for r in records if r.get("row_exclusions_omitted") is True]
    assert marked and all("row_exclusions" not in r for r in marked)
    assert all("row_exclusions" in r or r in marked for r in records)


def test_a_rule_left_out_at_the_store_is_marked_on_its_record():
    from agents.provenance import enrich_records_from_sidecar
    stored = {**_entry([]), "row_exclusions_omitted": True}
    payload = enrich_records_from_sidecar(_frame(), session_id="sess-1",
                                          get_entry=lambda _s, _r: stored)
    assert payload["records"][0]["row_exclusions_omitted"] is True


def test_no_marker_when_the_marker_itself_would_not_fit(monkeypatch):
    import copy

    from agents import provenance
    frame = _frame(3)
    plain = provenance.enrich_records_from_sidecar(
        copy.deepcopy(frame), session_id="sess-1", get_entry=lambda _s, _r: _entry([]))
    monkeypatch.setattr(provenance, "MAX_PAYLOAD_BYTES", _bytes(plain))
    payload = provenance.enrich_records_from_sidecar(
        frame, session_id="sess-1", get_entry=lambda _s, _r: _entry([_wide()]))
    assert payload == plain


def test_no_marker_without_a_captured_rule_or_on_a_rejected_turn():
    from agents.provenance import enrich_records_from_sidecar
    for entry, verdict in ((_entry([]), None),
                           ({**_entry([]), "row_exclusions_omitted": True}, "rejected"),
                           (_entry([{**_expected_rule(), "conditions": [
                               {"column": "status", "op": "drop"}]}]), None)):
        payload = enrich_records_from_sidecar(_frame(), session_id="sess-1", verdict=verdict,
                                              get_entry=lambda _s, _r, e=entry: e)
        assert "row_exclusions_omitted" not in payload["records"][0]


# --------------------------------------------------------------------------- #
# Joins and unions: each read is proven on its own; one rule per table per card.
# --------------------------------------------------------------------------- #
_Y_COLUMNS = {"grade": "varchar", "note": "varchar"}
_Y_CONDITIONS = [{"column": "grade", "op": "not_in", "values": ["a"]}]


def _two_table_graph():
    from tests.unit._row_exclusion_graph import RxGraph
    g = RxGraph({"system-a": {"table_x": dict(_COLUMNS), "table_y": dict(_Y_COLUMNS)}})
    for table, label, conditions in (("table_x", _LABEL, _CONDITIONS),
                                     ("table_y", "Old rows", _Y_CONDITIONS)):
        g.list_table("system-a", table)
        g.entries[f"system-a.{table}"]["props"] = {
            "row_exclusion": json.dumps({"label": label, "conditions": conditions}),
            "row_exclusion_by": _BY, "row_exclusion_at": _AT, "row_exclusion_source": _SOURCE}
    return g


def _join(x: bool, y: bool) -> str:
    """A join of the two rule-bearing tables, with each read's own predicate
    (bound to its alias) when asked."""
    sql = "SELECT COUNT(*) AS n FROM table_x x JOIN table_y y ON x.status = y.grade"
    preds = ([_rx().predicate_sql(_fact(), qualifier="x")] if x else []) + (
        [_rx().predicate_sql(_rx().exclusion_from("Old rows", _Y_CONDITIONS), qualifier="y")]
        if y else [])
    return f"{sql} WHERE {' AND '.join(preds)}" if preds else sql


def _y_rule() -> dict:
    return {"table": "table_y", "label": "Old rows", "conditions": _Y_CONDITIONS,
            "at": _AT, "source": _SOURCE}


def test_a_join_whose_every_read_carries_its_rule_records_each_rule_once(monkeypatch):
    """Each read of a join is decided on its own alias: both proven, so the
    call's card records both tables' rules, once each."""
    lake, state = _Lake(), _invocation()
    sql = _join(True, True)
    (text,) = _calls(monkeypatch, lake, [("t1", sql)], state=state, graph=_two_table_graph())
    payload = json.loads(text)
    assert "error" not in payload
    assert [r["applied"] for r in payload["row_exclusion"]] == [True, True]
    rows = _captured(state, sql, "t1", text)
    assert rows[-1]["row_exclusions"] == [_expected_rule(), _y_rule()]


def test_a_join_with_one_read_missing_its_rule_records_only_the_proven_one(monkeypatch):
    """table_y's read lacks its predicate: refused once; the identical resend
    runs under the "may include" disclosure for table_y, so the card records
    table_x's rule only."""
    lake, state = _Lake(), _invocation()
    sql = _join(True, False)
    first, second = _calls(monkeypatch, lake, [("t1", sql), ("t2", sql)], state=state,
                           graph=_two_table_graph())
    refused = json.loads(first)
    assert refused["error_name"] == "ROW_EXCLUSION" and refused["table"] == "table_y"
    ran = json.loads(second)
    assert {r["table"]: r["applied"] for r in ran["row_exclusion"]} == {
        "table_x": True, "table_y": False}
    rows = _captured(state, sql, "t2", second)
    assert rows[-1]["row_exclusions"] == [_expected_rule()]


def test_a_join_whose_reads_all_lack_their_rules_records_nothing(monkeypatch):
    from agents.tool_result_capture import APPLIED_EXCLUSIONS_STATE_KEY
    lake, state = _Lake(), _invocation()
    sql = _join(False, False)
    first, second = _calls(monkeypatch, lake, [("t1", sql), ("t2", sql)], state=state,
                           graph=_two_table_graph())
    assert json.loads(first)["error_name"] == "ROW_EXCLUSION"
    assert all(r["applied"] is False for r in json.loads(second)["row_exclusion"])
    assert not state.get(APPLIED_EXCLUSIONS_STATE_KEY)
    assert "row_exclusions" not in _captured(state, sql, "t2", second)[-1]


def test_a_union_of_proven_reads_records_its_table_once(monkeypatch):
    lake, state = _Lake(), _invocation()
    sql = f"{_present()} UNION ALL {_present()}"
    (text,) = _calls(monkeypatch, lake, [("t1", sql)], state=state)
    assert json.loads(text)["row_exclusion"]["applied"] is True
    assert _captured(state, sql, "t1", text)[-1]["row_exclusions"] == [_expected_rule()]


def _applied_read(table: str, label: str, conditions):
    fact = _rx().exclusion_from(label, conditions)
    return _rx().ExclusionRead(table, label, fact.conditions, True, at=_AT, source=_SOURCE)


def test_every_proven_read_of_one_call_is_recorded_for_that_call_only():
    """Two reads the guard proved applied in one call each give their own
    entry, under that call's id, and reach that call's citation only."""
    from agents.provenance import enrich_records_from_sidecar
    from agents.tool_result_capture import (APPLIED_EXCLUSIONS_STATE_KEY,
                                            record_sidecar_statement)
    reads = [_applied_read("table_x", _LABEL, _CONDITIONS),
             _applied_read("table_y", "Old rows", _Y_CONDITIONS)]
    recorded = "SELECT 1"
    payload = {"rows": [["7"]], "_adt_executed_query": recorded,
               "row_exclusion": reads[-1].to_dict()}
    state: dict = {}
    sa._keep_applied_exclusion(state, "t1", payload, list(reads))
    expected = [_rx().card_record(r) for r in reads]
    assert state[APPLIED_EXCLUSIONS_STATE_KEY]["t1"]["rules"] == expected
    for call_id in ("t2", "t1"):
        record_sidecar_statement(state, executed_query=recorded, metadata={"row_count": 1},
                                 call_id=call_id)
    rows = state["_adt_sidecar_statements"]
    assert "row_exclusions" not in rows[0] and rows[1]["row_exclusions"] == expected
    entries = {"q": sa._build_sidecar_entry([rows[1]], "system-a"),
               "other": sa._build_sidecar_entry([rows[0]], "system-a")}
    frame = _frame(2)
    by_ref = dict(zip((r["query_ref"] for r in frame["records"]), ("other", "q")))
    merged = enrich_records_from_sidecar(frame, session_id="sess-1",
                                         get_entry=lambda _s, ref: entries[by_ref[ref]])
    assert "row_exclusions" not in merged["records"][0]
    assert merged["records"][1]["row_exclusions"] == expected
