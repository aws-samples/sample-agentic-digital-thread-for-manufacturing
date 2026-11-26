# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The not-empty rewrite changes only the statement the engine receives.

The connector writes a top-level ``col <> ''`` WHERE conjunct as ``(col IS NOT
NULL AND length(col) > 0)`` so the engine skips NULL pages
(``sql_empty_string``). The result keeps ``_adt_executed_query`` as the query
as given, and the statement the engine received goes out of band
(``athena_tools.engine_statements``) to the evidence card. So the guarded
system-agent tool (``system_agent._scoped_athena_attempt``) must return the
SAME payload with and without the rewrite: same rows, same notes (the
NULL-unsafe note on an empty or zero result among them), same exact-split
record, same ``_adt_executed_query``, no extra member, so the same bytes for
the model and the integrity budgets. Each query below runs both as one query
(a one-day window, under the scan cutoff) and as an exact split (the whole
table, over it), against ``FakeLake`` (SQLite behind an Athena client); the
readers of ``_adt_executed_query`` outside the payload are then read on both
records, and the evidence card's entry is built from the real capture hook.
The statement the engine received belongs to one tool call (its toolUseId):
a failed call, or another call of the same record, never puts its statement
on a card.

Fully offline; synthetic names only.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import agents.athena_tools as at
import agents.system_agent as sa

from ._athena_split_fakes import FakeLake

_COLUMNS = {"area": "varchar", "kind": "varchar", "reason": "varchar", "qty": "bigint",
            "created": "timestamp(6)"}
_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1",
        "exact_split_max_pieces": 40}
#: One column of one row is 14 kB in the fake: a whole-table query reading two
#: columns is over this cutoff, and any one day fits.
_CUTOFF = 3_000_000
_START = datetime(2026, 9, 1)
_DAY = ("created >= TIMESTAMP '2026-09-05 00:00:00' AND "
        "created < TIMESTAMP '2026-09-06 00:00:00'")


def _rows() -> list[tuple]:
    """30 days; ``reason`` mostly NULL, sometimes empty, sometimes a code."""
    rows = []
    for day in range(30):
        for i in range(18 + day % 5):
            created = _START + timedelta(days=day, minutes=37 * i)
            reason = None if i % 4 else ("" if i % 8 == 0 else f"R{(i + day) % 3}")
            rows.append((["A", "B", None][(i + day) % 3], f"K{(i * 7 + day) % 3}", reason,
                         (i * 3 + day) % 50, created.strftime("%Y-%m-%d %H:%M:%S")))
    return rows


class _Lake(FakeLake):
    """``FakeLake`` with the catalog read the rewrite types columns from."""

    def get_table_metadata(self, **kwargs):
        if kwargs.get("TableName") != self.table:
            raise RuntimeError("EntityNotFoundException: table not found")
        return {"TableMetadata": {"Columns": [{"Name": name, "Type": dtype}
                                              for name, dtype in self.columns.items()],
                                  "PartitionKeys": []}}


def _clear_caches() -> None:
    """Every connector and split cache, so each run reads the lake afresh."""
    from agents import athena_exact_split
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    at._TABLE_FORMAT_CACHE.clear()
    at._PARTITION_FIELDS_CACHE.clear()
    athena_exact_split.clear_layout_cache()


@pytest.fixture(autouse=True)
def _fresh_caches():
    _clear_caches()
    yield
    _clear_caches()


_QUERIES = [
    "SELECT COUNT(*) AS n FROM events WHERE reason <> ''",
    "SELECT reason, COUNT(*) AS n FROM events WHERE reason IS NOT NULL AND reason <> '' "
    "GROUP BY reason ORDER BY n DESC LIMIT 5",
    "SELECT COUNT(*) AS n FROM events WHERE kind = 'NONE_SUCH' AND reason <> ''",
    "SELECT COUNT(*) AS n FROM events WHERE kind = 'NONE_SUCH' AND reason <> '' AND area != ''",
    "SELECT COUNT(*) AS n FROM events WHERE kind = 'NONE_SUCH' AND reason <> '' "
    "AND area NOT IN ('A')",
    "SELECT COUNT(*) AS n FROM events WHERE (reason IS NOT NULL AND length(reason) > 0) "
    "AND kind <> ''",
    "SELECT COUNT(*) AS n FROM events WHERE kind = 'NONE_SUCH' "
    "AND (reason IS NOT NULL AND length(reason) > 0) AND area <> ''",
    "SELECT COUNT(*) AS n, MAX(CASE WHEN (reason IS NOT NULL AND length(reason) > 0) "
    "THEN 1 ELSE 0 END) AS flag FROM events WHERE kind = 'NONE_SUCH' AND reason <> ''",
    "SELECT kind, SUM(qty) AS q FROM events WHERE reason <> '' AND qty > 3 GROUP BY kind",
    "SELECT kind, COUNT(*) AS n FROM events WHERE kind = 'NONE_SUCH' AND '' <> reason "
    "GROUP BY kind",
    "SELECT COUNT(*) AS n FROM events WHERE reason <> '' OR kind = 'K1'",
    "SELECT COUNT(*) AS n FROM events WHERE kind = 'K1'",
]
_CASES = [pytest.param(sql.replace(" WHERE ", f" WHERE {_DAY} AND ", 1), id=f"one-{n}")
          for n, sql in enumerate(_QUERIES)] + \
         [pytest.param(sql, id=f"split-{n}") for n, sql in enumerate(_QUERIES)]


def _lake() -> _Lake:
    return _Lake(columns=_COLUMNS, rows=_rows(), part_col="created", cutoff=_CUTOFF,
                 bytes_per_row=70_000)


def _attempt(sql: str, monkeypatch, *, written: bool) -> tuple[dict, list[str], dict]:
    """The guarded tool's payload for ``sql`` on a fresh lake, with the
    connector's writing on or off, the data statements the lake got, and
    the engine statements the connector reported."""
    lake = _lake()
    _clear_caches()
    with monkeypatch.context() as patch, at.engine_statements({}) as sink:
        patch.setattr(at, "boto3_client_for",
                      lambda service, *_a, **_k: lake.glue if service == "glue" else lake)
        if not written:
            patch.setattr(at, "_written_for_null_pages", lambda _a, _c, text: text)
        payload = sa._scoped_athena_attempt(sql, at._resolve_config(_CFG), {}, "lake-a")
    return payload, lake.data_queries(), sink


def _stable(value):
    """``value`` with the stamps of one run replaced: its time, the evidence
    id minted from it, and the engine's query ids (the split's pieces run in
    parallel, so their ids follow thread timing)."""
    if isinstance(value, dict):
        return {k: (len(v) if k == "query_ids" and isinstance(v, list) else
                    "<run>" if k in {"queried_at", "captured_at", "evidence_id"} else _stable(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def _writes(sql: str) -> bool:
    where = sql.split(" WHERE ", 1)[1]
    return ("<> ''" in where or "'' <>" in where or "!= ''" in where) and " OR " not in where


@pytest.mark.parametrize("sql", _CASES)
def test_the_payload_is_the_same_with_and_without_the_rewrite(sql, monkeypatch):
    """The whole payload, which is what the model, the integrity checks and
    their byte budgets read: identical, with no extra member."""
    plain, plain_ran, plain_sink = _attempt(sql, monkeypatch, written=False)
    written, written_ran, sink = _attempt(sql, monkeypatch, written=True)
    a, b = _stable(written), _stable(plain)
    differing = {k: (a.get(k), b.get(k)) for k in set(a) | set(b) if a.get(k) != b.get(k)}
    if isinstance(a.get("exact_split"), dict) and isinstance(b.get("exact_split"), dict):
        x, y = a["exact_split"], b["exact_split"]
        differing.update({f"exact_split.{k}": (x.get(k), y.get(k))
                          for k in set(x) | set(y) if x.get(k) != y.get(k)})
    assert not differing, differing
    assert plain_sink == {}
    own = sql.count("length(")
    if _writes(sql):
        # Not vacuous: the engine received the written form; the record and
        # the run without writing did not.
        assert any(q.count("length(") > own for q in written_ran)
        assert not any(q.count("length(") > own for q in plain_ran)
        if "error" not in written:
            assert list(sink) == [written["_adt_executed_query"]]
            assert sink[written["_adt_executed_query"]].count("length(") > own
    else:
        assert sink == {}
        assert sorted(written_ran) == sorted(plain_ran)  # pieces start in thread order


@pytest.mark.parametrize("sql", _CASES)
def test_every_reader_of_the_record_reads_the_same(sql, monkeypatch):
    """The readers of ``_adt_executed_query`` outside the payload: the
    executed-filters line, the evidence card's text filters and aggregate
    columns, the string literals the unresolved-miss check reads, and the
    NULL-unsafe tests."""
    from agents import (answer_provenance, executed_filters, null_unsafe_filters,
                        unresolved_miss)
    plain, _, _ = _attempt(sql, monkeypatch, written=False)
    written, _, _ = _attempt(sql, monkeypatch, written=True)
    if "error" in plain:
        assert "error" in written
        return
    a, b = written["_adt_executed_query"], plain["_adt_executed_query"]
    assert a == b
    for read in (executed_filters.where_clauses, answer_provenance.text_filters,
                 unresolved_miss._literals,
                 lambda s: answer_provenance._aggregate_indexes(s, 2),
                 lambda s: [(f.column, f.operator) for f in null_unsafe_filters.find(s)]):
        assert read(a) == read(b)


def test_a_zero_count_written_query_keeps_its_null_unsafe_note(monkeypatch):
    """The case three earlier designs got wrong: a zero COUNT whose WHERE held
    a written `col <> ''`, with the agent's own long form in the SELECT list."""
    sql = ("SELECT COUNT(*) AS n, MAX(CASE WHEN (reason IS NOT NULL AND length(reason) > 0) "
           "THEN 1 ELSE 0 END) AS flag FROM events WHERE " + _DAY +
           " AND kind = 'NONE_SUCH' AND reason <> ''")
    written, ran, _ = _attempt(sql, monkeypatch, written=True)
    assert written.get("rows") == [[0, None]] or written.get("rows") == [["0", None]], written
    assert any(q.count("length(reason) > 0") == 2 for q in ran)
    assert "reason" in written.get("note", "")


def _tool_call(sql: str, state: dict, call_id: str) -> str:
    """One call of the guarded tool as the model makes it: its tool context
    carries the call's toolUseId and the invocation state."""
    from types import SimpleNamespace
    tool = sa._make_scoped_athena_run_query(_CFG, {}, "lake-a")
    context = SimpleNamespace(
        tool_use={"name": "athena_run_query", "toolUseId": call_id, "input": {"sql": sql}},
        invocation_state=state)
    return tool(sql=sql, tool_context=context)


def _hook():
    from agents.tool_result_capture import InvocationToolResultCapture
    return InvocationToolResultCapture(system_id="lake-a", boundary="athena")


def _invocation() -> dict:
    """A fresh invocation state, started the way the capture hook starts one."""
    from types import SimpleNamespace
    state: dict = {}
    _hook()._on_invocation_start(SimpleNamespace(invocation_state=state))
    return state


def _captured(state: dict, sql: str, call_id: str, text: str) -> list[dict]:
    """The capture hook's after-call for one call; the sidecar rows so far."""
    from types import SimpleNamespace

    from agents.tool_result_capture import SIDECAR_STATEMENTS_STATE_KEY
    _hook()._on_after_tool_call(SimpleNamespace(
        invocation_state=state, selected_tool=SimpleNamespace(tool_name="athena_run_query"),
        tool_use={"name": "athena_run_query", "toolUseId": call_id, "input": {"sql": sql}},
        result={"status": "success", "content": [{"text": text}]}, exception=None))
    return list(state.get(SIDECAR_STATEMENTS_STATE_KEY) or [])


def _on(lake, monkeypatch) -> None:
    monkeypatch.setattr(at, "boto3_client_for",
                        lambda service, *_a, **_k: lake.glue if service == "glue" else lake)


def _card(sql: str, monkeypatch, *, written: bool) -> tuple[str, dict, list[str], str]:
    """The guarded tool call as the model makes it, its capture, and the
    evidence entry the card shows: (tool text, entry, lake statements,
    executed record)."""
    import json
    lake = _lake()
    _clear_caches()
    state = _invocation()
    with monkeypatch.context() as patch:
        _on(lake, patch)
        if not written:
            patch.setattr(at, "_written_for_null_pages", lambda _a, _c, text: text)
        text = _tool_call(sql, state, "t1")
    rows = _captured(state, sql, "t1", text)
    assert len(rows) == 1, rows
    return text, sa._build_sidecar_entry(rows, "lake-a"), lake.data_queries(), \
        json.loads(text)["_adt_executed_query"]


@pytest.mark.parametrize("sql", [_CASES[0].values[0], _CASES[1].values[0]])
def test_the_evidence_card_shows_the_statement_the_engine_received(sql, monkeypatch):
    text, entry, ran, recorded = _card(sql, monkeypatch, written=True)
    plain_text, plain_entry, _, _ = _card(sql, monkeypatch, written=False)
    assert entry["executed_query"] in ran and entry["executed_query"] != recorded
    assert "length(reason) > 0" in entry["executed_query"]
    # The tool text the model reads, and the shape fingerprint, are as before.
    assert "_adt_engine" not in text
    assert len(text) == len(plain_text)
    fingerprint = {k: v for k, v in entry.items() if k not in {"executed_query", "captured_at"}}
    assert fingerprint == {k: v for k, v in plain_entry.items()
                           if k not in {"executed_query", "captured_at"}}
    assert plain_entry["executed_query"] == recorded


def test_a_query_with_nothing_written_shows_its_own_statement(monkeypatch):
    sql = _CASES[11].values[0]
    _text, entry, ran, recorded = _card(sql, monkeypatch, written=True)
    assert entry["executed_query"] == recorded and recorded in ran


# Each call's card shows the statement that call sent: the engine statement
# belongs to one tool call (its toolUseId), never to the record's text across
# the invocation, so a failed call or another call of the same record cannot
# put its statement on a card.


def test_a_failed_written_call_gives_its_statement_to_no_later_call(monkeypatch):
    """A call whose written statement the engine failed, then a call of the
    same record the engine receives unchanged (the column is no longer
    catalogued as a string): the second card shows the statement the second
    call sent, and the failed call keeps nothing."""
    import json

    from agents.tool_result_capture import ENGINE_STATEMENTS_STATE_KEY
    lake = _lake()
    lake.fail_piece_containing = "length(reason) > 0"
    _on(lake, monkeypatch)
    sql = f"SELECT COUNT(*) AS n FROM events WHERE {_DAY} AND reason <> ''"
    state = _invocation()
    failed = _tool_call(sql, state, "t1")
    assert "error" in json.loads(failed)
    assert [q for q in lake.data_queries() if "length(reason) > 0" in q]
    assert _captured(state, sql, "t1", failed) == []
    lake.columns["reason"] = "char(8)"
    _clear_caches()
    text = _tool_call(sql, state, "t2")
    recorded = json.loads(text)["_adt_executed_query"]
    assert lake.data_queries()[-1] == recorded
    rows = _captured(state, sql, "t2", text)
    assert sa._build_sidecar_entry(rows, "lake-a")["executed_query"] == recorded
    assert len(rows) == 1 and "engine_query_text" not in rows[0], rows
    assert not state.get(ENGINE_STATEMENTS_STATE_KEY)


def test_two_calls_of_one_record_each_show_the_statement_they_sent(monkeypatch):
    """Two calls of the same record that the connector wrote differently
    (between them the catalog stopped typing one column as a string), both
    run before either is captured, as calls the model makes together are."""
    lake = _lake()
    _on(lake, monkeypatch)
    sql = f"SELECT COUNT(*) AS n FROM events WHERE {_DAY} AND reason <> '' AND kind <> ''"
    state = _invocation()
    first = _tool_call(sql, state, "t1")
    lake.columns["kind"] = "char(2)"
    _clear_caches()
    second = _tool_call(sql, state, "t2")
    sent = lake.data_queries()
    assert len(sent) == 2 and "length(kind) > 0" in sent[0] and "length(kind)" not in sent[1]
    assert all("length(reason) > 0" in q for q in sent)
    _captured(state, sql, "t1", first)
    rows = _captured(state, sql, "t2", second)
    assert [row["engine_query_text"] for row in rows] == sent
    assert [sa._build_sidecar_entry([row], "lake-a")["executed_query"] for row in rows] == sent


class _Together(_Lake):
    """A lake whose first written data statements start only once two calls
    have each reached one, so both calls report before either finishes."""

    def __init__(self, **kwargs):
        import threading
        super().__init__(**kwargs)
        self.barrier = threading.Barrier(2, timeout=20)
        self._written_starts = 0

    def start_query_execution(self, QueryString, **kwargs):  # noqa: N803 - boto3 spelling
        if "length(" in QueryString:
            with self.lock:
                self._written_starts += 1
                first_two = self._written_starts <= 2
            if first_two:
                self.barrier.wait()
        return super().start_query_execution(QueryString, **kwargs)


def test_calls_running_at_once_each_keep_their_own_statement(monkeypatch):
    import threading
    lake = _Together(columns=_COLUMNS, rows=_rows(), part_col="created", cutoff=_CUTOFF,
                     bytes_per_row=70_000)
    _on(lake, monkeypatch)
    state = _invocation()
    calls = {"t1": f"SELECT COUNT(*) AS n FROM events WHERE {_DAY} AND reason <> ''",
             "t2": f"SELECT COUNT(*) AS n FROM events WHERE {_DAY} AND kind <> ''"}
    texts: dict = {}

    def run(call_id: str) -> None:
        texts[call_id] = _tool_call(calls[call_id], state, call_id)

    threads = [threading.Thread(target=run, args=(call_id,)) for call_id in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert set(texts) == set(calls) and not lake.barrier.broken
    for call_id, column in (("t1", "reason"), ("t2", "kind")):
        rows = _captured(state, calls[call_id], call_id, texts[call_id])
        engine = rows[-1]["engine_query_text"]
        assert engine in lake.data_queries() and f"length({column}) > 0" in engine


def test_a_call_kept_under_one_id_reaches_only_that_call_and_that_record():
    """The kept statement is read by the capture of its own call only, for
    the record that call returned, and only once."""
    from agents.tool_result_capture import ENGINE_STATEMENTS_STATE_KEY, record_sidecar_statement
    recorded = "SELECT COUNT(*) AS n FROM events WHERE reason <> '' LIMIT 500"
    engine = ("SELECT COUNT(*) AS n FROM events WHERE (reason IS NOT NULL AND "
              "length(reason) > 0) LIMIT 500")
    state = {ENGINE_STATEMENTS_STATE_KEY: {"t1": {"recorded": recorded, "engine": engine}}}
    for call_id, query in (("t2", recorded), (None, recorded), ("t1", recorded + " ")):
        record_sidecar_statement(state, executed_query=query, metadata={"row_count": 1},
                                 call_id=call_id)
    state[ENGINE_STATEMENTS_STATE_KEY] = {"t1": {"recorded": recorded, "engine": engine}}
    for _ in range(2):
        record_sidecar_statement(state, executed_query=recorded, metadata={"row_count": 1},
                                 call_id="t1")
    rows = state["_adt_sidecar_statements"]
    assert [row.get("engine_query_text") for row in rows] == [None, None, None, engine, None]


def test_a_call_without_an_id_shows_the_query_as_given(monkeypatch):
    """No toolUseId (a call outside the model's loop): nothing is kept, and
    the card shows the query as given, as it did before the rewrite."""
    import json

    from agents.tool_result_capture import ENGINE_STATEMENTS_STATE_KEY
    lake = _lake()
    _on(lake, monkeypatch)
    sql = _CASES[0].values[0]
    state = _invocation()
    text = sa._guarded_athena_call(sql, at._resolve_config(_CFG), {}, "lake-a", state)
    assert not state.get(ENGINE_STATEMENTS_STATE_KEY)
    assert json.loads(text)["_adt_executed_query"] != lake.data_queries()[-1]
