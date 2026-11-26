# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A declared row exclusion never disappears because a read came back empty.

Before this change the system agent decided which tables carry a row
exclusion from its baked table list and read each table's fact, so a restore
whose schema read failed (an empty table list) never read any fact, and a fact
read that matched no SchemaEntry row was taken for "no exclusion". Either way
an ordinary query ran unfiltered with no disclosure.

Now the tables come from the system's MANIFEST (``row_exclusion_manifest``),
read from the System node at bake and again before a guarded query:

* an empty table list does not matter: a listed table's fact is still read;
* a fact read that answers no SchemaEntry row is UNAVAILABLE, never absent;
* a matched row with no stored value is never absent: a pending entry (an
  interrupted set or clear, which leaves nothing stored) is held until a
  steward sets or clears the rule again, and a settled entry with no stored
  fact means the fact was removed outside the steward route; either way the
  table is unavailable;
* a manifest that cannot be read (or has no System row) holds every data read
  of the system, but not structural discovery;
* an interrupted set or clear never leaves a stored fact unenforced.

Fully offline (``_row_exclusion_graph.RxGraph``, a fake Athena lake); synthetic
names, plus one schema shaped like the measured table (``step_records_raw``).
"""
from __future__ import annotations

import asyncio
import json

import pytest
from strands._async import run_async

import agents.athena_tools as at
import agents.server as server
import agents.system_agent as sa
from agents import record_key, record_time, state_times, storage_bounds
from agents import row_exclusion as rx
from tests.unit._row_exclusion_graph import RxGraph

_SYS = "system-a"
_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1"}
#: Shaped like the measured table: a wide event table, a [base_table] marker,
#: timestamp(6) with time zone columns, and a three-condition rule whose last
#: condition keeps nine step kinds.
_WIDE = "step_records_raw"
_WIDE_COLUMNS = {
    "id": "varchar", "step_kind": "varchar", "entity_kind": "varchar", "revision": "integer",
    "cell": "varchar", "route_tag": "varchar", "cycle_ref": "varchar",
    "attempt_ref": "varchar", "state": "varchar", "fail_code": "varchar",
    "started_at": "timestamp(6) with time zone", "finished_at": "timestamp(6) with time zone",
    "source_region": "varchar", "updated_day": "date"}
_LEDGER = "ledger_lines"
_LEDGER_COLUMNS = {"batch_tag": "varchar", "amount": "double"}
_KEEP = ["ASSEMBLE", "ASSEMBLE_BUFFER", "ASSEMBLE_FEEDER", "PACK", "PACK_BUFFER", "TRIM",
         "TRIM_BUFFER", "FINISH", "FINISH_BUFFER"]
_RULE = {"label": "shadow steps", "conditions": [
    {"column": "route_tag", "op": "not_empty"}, {"column": "cycle_ref", "op": "not_empty"},
    {"column": "step_kind", "op": "not_in", "values": _KEEP}]}
_DESC = (f"{_WIDE} [base_table](" + "; ".join(f"{c}: {t}" for c, t in _WIDE_COLUMNS.items())
         + f"), {_LEDGER} [base_table](batch_tag: varchar; amount: double)")
_WIN = ("finished_at >= TIMESTAMP '2026-10-17 05:30:00 UTC' "
        "AND finished_at < TIMESTAMP '2026-10-18 05:30:00 UTC'")
_COUNT = f"SELECT COUNT(*) AS n FROM {_WIDE} WHERE state = 'FAILED' AND {_WIN}"
_LEDGER_SUM = f"SELECT SUM(amount) AS n FROM {_LEDGER}"
_DISCOVERY = ("SELECT column_name, data_type FROM information_schema.columns "
              f"WHERE table_schema = 'lake_db' AND table_name = '{_WIDE}'")


def _pred() -> str:
    return rx.predicate_sql(rx.exclusion_from(_RULE["label"], _RULE["conditions"]))


class _Lake:
    def __init__(self):
        self.queries: list[str] = []

    def get_table_metadata(self, TableName=None, **_kwargs):
        columns = _WIDE_COLUMNS if TableName == _WIDE else _LEDGER_COLUMNS
        return {"TableMetadata": {
            "Name": TableName, "Columns": [{"Name": n, "Type": t} for n, t in columns.items()],
            "PartitionKeys": [], "Parameters": {"table_type": "ICEBERG"}}}

    def start_query_execution(self, QueryString, **_kwargs):
        self.queries.append(QueryString)
        return {"QueryExecutionId": f"q{len(self.queries)}"}

    def get_query_execution(self, QueryExecutionId):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"},
                                   "Statistics": {"DataScannedInBytes": 10}}}

    def get_query_results(self, QueryExecutionId, **_kwargs):
        sql = self.queries[int(QueryExecutionId[1:]) - 1]
        if "$partitions" in sql:
            names, rows = ["partition"], [["{}"]]
        elif "information_schema.columns" in sql:
            names = ["column_name", "data_type"]
            rows = [[n, t] for n, t in _WIDE_COLUMNS.items()]
        else:
            names, rows = ["n"], [["42"]]
        header = {"Data": [{"VarCharValue": n} for n in names]}
        return {"ResultSet": {
            "ResultSetMetadata": {"ColumnInfo": [{"Name": n, "Type": "varchar"} for n in names]},
            "Rows": [header] + [{"Data": [{"VarCharValue": v} for v in row]} for row in rows]}}

    def stop_query_execution(self, **_kwargs):
        return None

    def data_queries(self) -> list[str]:
        return [q for q in self.queries
                if "$partitions" not in q and "information_schema" not in q]


def _resets():
    return (at.clear_schema_cache, at._PARTITION_COLUMNS_CACHE.clear, storage_bounds.reset,
            state_times.reset, record_time.reset, record_key.reset, rx.reset)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    monkeypatch.setattr(record_key, "_graph_query", lambda: (lambda *_a, **_k: [{}]))
    for reset in _resets():
        reset()
    yield
    for reset in _resets():
        reset()


def _graph(*, listed=(), pending=False, stored=True) -> RxGraph:
    """system-a with both tables; ``listed`` tables in its manifest, each with
    the rule stored when ``stored``."""
    g = RxGraph({_SYS: {_WIDE: _WIDE_COLUMNS, _LEDGER: _LEDGER_COLUMNS}})
    for table in listed:
        g.list_table(_SYS, table, pending=pending)
        if stored:
            g.store(_SYS, table, _RULE)
    return g


def _restored_desc(monkeypatch) -> str:
    """The table list a restore bakes when its unchecked schema read fails:
    ``neptune_query`` degrades a failure to no rows, so it is ``""``."""
    monkeypatch.setattr(server, "neptune_query", lambda *_a, **_k: [])
    desc = server._restore_direct_tables_desc(_SYS, "Athena")
    assert desc == ""
    return desc


def _ask(monkeypatch, g, sqls, *, desc=_DESC):
    """Bake system-a from ``g`` with the table list ``desc``, then run each
    SQL through the guarded Athena attempt in one Mode A call. Returns
    ``(payloads, answer, lake)``."""
    monkeypatch.setattr(rx, "_graph_query", g.seam())
    sa._remember_turn_time_facts(_SYS, [], desc, connection_type="Athena")
    lake = _Lake()
    monkeypatch.setattr(at, "boto3_client_for", lambda *_a, **_k: lake)
    cfg = at._resolve_config(_CFG)
    payloads: list[dict] = []

    async def calls():
        for sql in sqls:
            payloads.append(await asyncio.to_thread(sa._scoped_athena_attempt, sql, cfg, {}, _SYS))

    def invoke(_agent, _question):
        run_async(calls)
        return "The count is 42.", {}

    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state", invoke)
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
    monkeypatch.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
    answer = asyncio.run(sa._run_mode_a_query(object(), _SYS, "How many steps failed?", ""))
    return payloads, answer, lake


def _fact_reads(g) -> list:
    return ([p["seid"] for kind, p in g.calls if kind == "fact"]
            + [seid for kind, p in g.calls if kind == "facts" for seid in p["seids"]])


# --------------------------------------------------------------------------- #
# Restore with a table list the schema read could not rebuild.
# --------------------------------------------------------------------------- #
def test_an_empty_restored_table_list_still_applies_a_listed_tables_exclusion(monkeypatch):
    g = _graph(listed=(_WIDE,))
    (first, second), answer, lake = _ask(monkeypatch, g, [_COUNT, f"{_COUNT} AND {_pred()}"],
                                         desc=_restored_desc(monkeypatch))
    assert first["error_name"] == "ROW_EXCLUSION"
    assert f"AND {_pred()}" in first["guidance"]
    assert second["rows"] == [["42"]] and second["row_exclusion"]["applied"] is True
    assert len(lake.data_queries()) == 1
    assert "with the source owner's row exclusion" in answer


def test_an_empty_restored_table_list_and_a_failed_fact_read_hold_only_that_table(
        monkeypatch):
    g = _graph(listed=(_WIDE,))
    g.fail.update({"fact", "facts"})
    payloads, answer, lake = _ask(
        monkeypatch, g, [_COUNT, f"{_COUNT} AND {_pred()}", _DISCOVERY, _LEDGER_SUM],
        desc=_restored_desc(monkeypatch))
    assert [p.get("error_name") for p in payloads[:2]] == ["ROW_EXCLUSION_UNAVAILABLE"] * 2
    assert payloads[0]["table"] == _WIDE and payloads[0]["retryable"] is False
    assert payloads[2]["rows"]                      # structural discovery runs
    assert payloads[3]["rows"] == [["42"]]          # another table runs
    assert lake.data_queries() == [f"{_LEDGER_SUM} LIMIT 500"]
    assert "could not be confirmed" in answer


def test_an_empty_restored_table_list_on_a_confirmed_empty_manifest_lets_reads_run(
        monkeypatch):
    g = _graph()
    (first, second), answer, lake = _ask(monkeypatch, g, [_COUNT, _LEDGER_SUM],
                                         desc=_restored_desc(monkeypatch))
    assert first["rows"] == [["42"]] and second["rows"] == [["42"]]
    assert "row_exclusion" not in first and "Row exclusion" not in answer
    assert "manifest" in g.kinds() and _fact_reads(g) == []    # decided by the manifest


# --------------------------------------------------------------------------- #
# An unknown manifest: every data read held, structural discovery not.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fault", ["read_fails", "no_system_row", "unparsable"])
def test_an_unknown_manifest_holds_data_reads_but_not_structural_discovery(monkeypatch,
                                                                            fault):
    g = _graph(listed=(_WIDE,))
    if fault == "read_fails":
        g.fail.add("manifest")
    elif fault == "no_system_row":
        del g.manifests[_SYS]
    else:
        g.manifests[_SYS] = "{not json"
    payloads, answer, lake = _ask(monkeypatch, g, [_COUNT, _LEDGER_SUM, _DISCOVERY])
    assert [p.get("error_name") for p in payloads[:2]] == ["ROW_EXCLUSION_UNAVAILABLE"] * 2
    assert "this system's tables" in payloads[0]["guidance"]
    assert ["cycle_ref", "varchar"] in payloads[2]["rows"]     # structural discovery runs
    assert lake.data_queries() == []
    described = json.loads(at._describe_table_cached(at._resolve_config(_CFG), _WIDE, _SYS))
    assert ["cycle_ref", "varchar"] in described["rows"]
    assert "could not be confirmed" in answer


# --------------------------------------------------------------------------- #
# A listed table's fact read: zero rows, a null value, pending or settled.
# --------------------------------------------------------------------------- #
def test_a_zero_row_fact_read_on_a_listed_table_is_unavailable_before_athena(monkeypatch):
    g = _graph(listed=(_WIDE,))
    g.hidden.add(f"{_SYS}.{_WIDE}")
    (first, other), _answer, lake = _ask(monkeypatch, g, [_COUNT, _LEDGER_SUM])
    assert first["error_name"] == "ROW_EXCLUSION_UNAVAILABLE"
    assert first["unavailable"] == "entry_missing"
    assert other["rows"] == [["42"]]
    assert lake.data_queries() == [f"{_LEDGER_SUM} LIMIT 500"]


def test_a_pending_listed_table_with_nothing_stored_is_held(monkeypatch):
    """Listed and pending with nothing stored: a set interrupted after listing
    the table, or a clear interrupted after clearing the fact. Which rule the
    steward meant is not known, so reads are held with the repair."""
    g = _graph(listed=(_WIDE,), pending=True, stored=False)
    (payload,), _answer, lake = _ask(monkeypatch, g, [_COUNT])
    assert payload["error_name"] == "ROW_EXCLUSION_UNAVAILABLE"
    assert payload["unavailable"] == "write_pending"
    assert "set it again, or clear it" in payload["guidance"]
    assert lake.data_queries() == []


def test_a_settled_listed_table_whose_fact_is_gone_is_unavailable(monkeypatch):
    """A settled entry with no stored fact: the steward route never leaves
    that state (a schema rewrite that recreates the table's SchemaEntry does).
    The owner's rule is not dropped silently: reads are held with the repair."""
    g = _graph(listed=(_WIDE,), stored=False)
    (payload,), _answer, lake = _ask(monkeypatch, g, [_COUNT])
    assert payload["error_name"] == "ROW_EXCLUSION_UNAVAILABLE"
    assert payload["unavailable"] == "fact_missing"
    assert "set the row exclusion of step_records_raw again, or clear it" in payload["guidance"]
    assert lake.data_queries() == []


# --------------------------------------------------------------------------- #
# An interrupted set or clear never leaves a stored fact unenforced.
# --------------------------------------------------------------------------- #
def _crashed(step: str) -> RxGraph:
    """system-a after a set or clear of step_records_raw that stopped at
    ``step``."""
    g = _graph()
    write = lambda: rx.set_row_exclusion(_SYS, _WIDE, _RULE["conditions"],  # noqa: E731
                                         label=_RULE["label"], write=g.write, query=g.query)
    if step.startswith("clear"):
        write()
        write = lambda: rx.clear_row_exclusion(_SYS, _WIDE, write=g.write,  # noqa: E731
                                               query=g.query)
    stop_before_fact = step in {"set_before_fact", "clear_before_fact"}

    def crash(kind, _p):
        stored = g.stored(_SYS, _WIDE) is not None
        if stop_before_fact and kind == "set_fact":
            g.refuse.add("set_fact")
        if not stop_before_fact and kind == "swap" and (
                stored if step == "set_after_fact" else not stored):
            g.refuse.add("swap")
    g.before_write = crash
    with pytest.raises(Exception):
        write()
    g.before_write = None
    g.refuse.clear()
    g.calls.clear()
    return g


@pytest.mark.parametrize("step,stored", [
    ("set_before_fact", False), ("set_after_fact", True),
    ("clear_before_fact", True), ("clear_after_fact", False),
])
def test_an_interrupted_write_leaves_the_table_listed_and_any_stored_fact_enforced(
        monkeypatch, step, stored):
    g = _crashed(step)
    assert _WIDE in g.manifest(_SYS)
    assert (g.stored(_SYS, _WIDE) is not None) is stored
    (payload,), _answer, lake = _ask(monkeypatch, g, [_COUNT])
    if stored:
        assert payload["error_name"] == "ROW_EXCLUSION"
    else:
        assert payload["error_name"] == "ROW_EXCLUSION_UNAVAILABLE"
        assert payload["unavailable"] == "write_pending"
    assert lake.data_queries() == []


@pytest.mark.parametrize("step", ["set_before_fact", "set_after_fact", "clear_before_fact",
                                  "clear_after_fact"])
def test_an_interrupted_write_then_a_fact_read_fault_holds_the_table(monkeypatch, step):
    g = _crashed(step)
    g.fail.update({"fact", "facts"})
    (payload, other), _answer, _lake = _ask(monkeypatch, g, [_COUNT, _LEDGER_SUM])
    assert payload["error_name"] == "ROW_EXCLUSION_UNAVAILABLE"
    assert other["rows"] == [["42"]]


@pytest.mark.parametrize("sql,data", [
    ("SELECT column_name FROM information_schema.columns WHERE table_name = 'ledger_lines'",
     False),
    ("SELECT table_name FROM awsdatacatalog.information_schema.tables", False),
    ("SELECT 1 AS n", False),
    ("SELECT SUM(amount) FROM ledger_lines", True),
    ("SELECT c.column_name FROM information_schema.columns c "
     "JOIN ledger_lines l ON c.column_name = l.batch_tag", True),
    ("SELECT COUNT(*) FROM ledger_lines WHERE batch_tag = 'open", True),
    ("SELECT 1 AS n; SELECT 2 AS n", True),
], ids=["information_schema", "qualified_information_schema", "no_relation", "data_table",
        "joined_to_a_data_table", "uncertain_tokens", "two_statements"])
def test_only_structural_discovery_is_not_a_data_read(sql, data):
    from agents.row_exclusion_read import may_read_data
    assert may_read_data(sql) is data
