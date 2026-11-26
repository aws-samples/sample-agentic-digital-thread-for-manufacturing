# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A count of the records an element expansion leaves out, run in the same call.

Measured in a dry run on an analytics deployment: a grouped query by a code and
a serialized JSON list column was withheld for its spellings, and it had no
``COUNT(*)`` output, so the number of records with an empty list was not known.
The agent expanded the list into one row per element (``CROSS JOIN UNNEST``),
whose result got the note without a number, then counted those records itself
with the same WHERE and ``COALESCE(json_array_length(<list>), 0) = 0``: 15. The
expansion's engine line and FINAL-ANSWER REQUIREMENTS block never carried the
15, and the answer gave the code's per-element sum as its record count.

Pinned here:

* a later count of the records whose list is empty or NULL, over the same
  full source with the expansion's top-level WHERE conjuncts plus one test
  that selects exactly those records, reaches the earlier expansion: the call's
  engine line and REQUIRED sentence state it;
* the same count run first gives a later expansion its count, as an earlier
  grouped result's count does;
* any other count gives nothing: another WHERE, column or source, a sample, a
  GROUP BY, HAVING or FILTER, a test that misses some of those records, a
  top-level OR, a join, another aggregate, a cut result, a count of zero;
* a WHERE with a top-level OR is not read as a set of conjuncts, so no count
  reaches an expansion through it;
* the totals line keeps an expansion's list-elements label, beside the
  result's own key names, when its full label is past the entry cap.

Fully offline; synthetic names.
"""
from __future__ import annotations

import asyncio
import importlib
import re
import types

import pytest
from strands._async import run_async

import agents.athena_tools as at
import agents.system_agent as sa
from agents import record_time, state_times, storage_bounds

_CFG = {"workgroup": "wg", "database": "yard_db", "region": "us-east-1"}
_TABLE = "tote_pick_record_details_raw"
_LIST = '"routing_actuals_details.bin_list"'
_LABELS = '"routing_actuals_details.label_list"'
_REASON = '"handling_record_details.pick_data.miss_reason"'
_DATE = '"handling_record_details.pick_data.miss_flagged_date"'
_CONJUNCTS = ("zone = 'Z2'",
              f"{_DATE} >= TIMESTAMP '2031-04-03 00:00:00 UTC'",
              f"{_DATE} < TIMESTAMP '2031-04-15 00:00:00 UTC'",
              f"{_REASON} IN (\n    'Scanner: ScannerNoReadAtInduction',\n"
              "    'Tote: ToteWrongSizeForOrder',\n    'Bin: BinEmptyAtPick'\n  )")
_WHERE = "WHERE " + " AND ".join(_CONJUNCTS)
_ELEMENT = "bin_list_element_ref"
_EXPANSION = f"CROSS JOIN UNNEST(CAST(json_parse({_LIST}) AS ARRAY(JSON))) AS t({_ELEMENT})"
#: The expansion in the shape the dry run sent: a long element alias, the
#: element's id and name grouped as expressions, ORDER BY an alias.
_EXPANDED = (f"SELECT {_REASON} AS miss_reason, json_extract_scalar({_ELEMENT}, '$.id') AS bin_id, "
             f"json_extract_scalar({_ELEMENT}, '$.name') AS bin_name, COUNT(*) AS pick_count "
             f"FROM {_TABLE} {_EXPANSION} {_WHERE} "
             f"GROUP BY {_REASON}, json_extract_scalar({_ELEMENT}, '$.id'), "
             f"json_extract_scalar({_ELEMENT}, '$.name') ORDER BY {_REASON}, bin_id")
_EMPTY = f"COALESCE(json_array_length({_LIST}), 0) = 0"
#: The count the agent ran: the same WHERE and one test, written with two
#: narrower tests beside the exact one.
_COUNT_OR = (f"SELECT COUNT(*) AS picks_with_no_bin FROM {_TABLE} {_WHERE} "
             f"AND ({_EMPTY}\n    OR {_LIST} IS NULL\n    OR {_LIST} = '[]')")
_COUNT_PLAIN = f"SELECT COUNT(*) AS n FROM {_TABLE} {_WHERE} AND {_EMPTY}"
_EXPANDED_ROWS = [["Bin: BinEmptyAtPick", "B-1", "North", "52"],
                  ["Bin: BinEmptyAtPick", "B-2", "South", "37"],
                  ["Scanner: ScannerNoReadAtInduction", "B-1", "North", "730"],
                  ["Scanner: ScannerNoReadAtInduction", "B-3", "East", "617"],
                  ["Tote: ToteWrongSizeForOrder", "B-2", "South", "297"]]
_QUESTION = "How many picks of each miss reason were there per bin?"
_REQUIRED = ("REQUIRED: the answer must say that 15 records under the filters of its figures by "
             "routing_actuals_details.bin_list hold no element of it and are not in those figures.")


def _el():
    return importlib.import_module("agents.empty_lists")


class _Lake:
    """One unpartitioned table: an expansion answers its element rows, a
    count answers ``count``, any other query one grouped row."""

    def __init__(self, count="15"):
        self.count = count
        self.queries: list[str] = []

    def get_table_metadata(self, **_kwargs):
        return {"TableMetadata": {
            "Name": _TABLE,
            "Columns": [{"Name": "zone", "Type": "varchar"},
                        {"Name": "shift", "Type": "varchar"},
                        {"Name": "handling_record_details.pick_data.miss_reason",
                         "Type": "varchar"},
                        {"Name": "handling_record_details.pick_data.miss_flagged_date",
                         "Type": "timestamp"},
                        {"Name": "routing_actuals_details.bin_list", "Type": "varchar"},
                        {"Name": "routing_actuals_details.label_list", "Type": "varchar"}],
            "PartitionKeys": [], "Parameters": {}}}

    def start_query_execution(self, QueryString, **_kwargs):
        self.queries.append(QueryString)
        return {"QueryExecutionId": f"q{len(self.queries)}"}

    def get_query_execution(self, QueryExecutionId):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"},
                                   "Statistics": {"DataScannedInBytes": 10}}}

    def get_query_results(self, QueryExecutionId, **_kwargs):
        sql = self.queries[int(QueryExecutionId[1:]) - 1]
        if "UNNEST" in sql:
            names, rows = ["miss_reason", "bin_id", "bin_name", "pick_count"], _EXPANDED_ROWS
        elif "COUNT(*)" in sql and "GROUP BY" not in sql:
            names, rows = ["n"], [[self.count]]
        else:
            names, rows = ["miss_reason", "n"], [["Bin: BinEmptyAtPick", "15"]]
        header = {"Data": [{"VarCharValue": n} for n in names]}
        return {"ResultSet": {
            "ResultSetMetadata": {"ColumnInfo": [{"Name": n, "Type": "varchar"} for n in names]},
            "Rows": [header] + [{"Data": [{"VarCharValue": v} for v in row]} for row in rows]}}

    def stop_query_execution(self, **_kwargs):
        return None


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()
    state_times.reset()
    record_time.reset()
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()
    state_times.reset()
    record_time.reset()


def _ask(monkeypatch, lake, sqls, *, answer_text="Bin: BinEmptyAtPick had 89 picks."):
    """One Mode A call. Returns ``(tool payloads, final answer)``."""
    monkeypatch.setattr(at, "boto3_client_for", lambda *_a, **_k: lake)
    payloads: list[dict] = []
    cfg = at._resolve_config(_CFG)

    async def calls():
        for sql in sqls:
            payloads.append(await asyncio.to_thread(
                sa._scoped_athena_attempt, sql, cfg, {}, "system-a"))

    def invoke(_agent, _question):
        run_async(calls)
        return answer_text, {}

    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state", invoke)
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
    monkeypatch.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
    answer = asyncio.run(sa._run_mode_a_query(object(), "system-a", _QUESTION, ""))
    return payloads, answer


# --------------------------------------------------------------------------- #
# End to end: the later count reaches the expansion's engine line and block
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("count", [_COUNT_OR, _COUNT_PLAIN], ids=["or_form", "plain_form"])
def test_a_later_count_reaches_the_earlier_expansion(monkeypatch, count):
    el = _el()
    (expanded, counted), answer = _ask(monkeypatch, _Lake(), [_EXPANDED, count])
    assert expanded["rows"] == _EXPANDED_ROWS
    assert "empty_lists_dropped" not in expanded
    assert f"count those records under the same filters with {_EMPTY}" in expanded["note"]
    assert counted["rows"] == [["15"]]
    assert el.ENGINE_PREFIX in answer and el.MARKER in answer
    assert (f"a result in {_TABLE} expanded the list in routing_actuals_details.bin_list into "
            "one row per element, "
            "so 15 records under its filters whose list is empty or NULL are not in its rows"
            ) in answer
    assert _REQUIRED in answer
    assert answer.index(el.ENGINE_PREFIX) < answer.index(_REQUIRED)


def test_a_count_run_first_gives_the_later_expansion_its_count(monkeypatch):
    el = _el()
    (counted, expanded), answer = _ask(monkeypatch, _Lake(), [_COUNT_OR, _EXPANDED])
    assert counted["rows"] == [["15"]]
    assert expanded["empty_lists_dropped"] == {"column": "routing_actuals_details.bin_list",
                                             "records": 15}
    assert "the 15 records under the same filters" in expanded["note"]
    assert el.ENGINE_PREFIX in answer and answer.count(_REQUIRED) == 1


def test_the_count_and_a_resent_expansion_carry_it_once(monkeypatch):
    (_e, _c, again), answer = _ask(monkeypatch, _Lake(), [_EXPANDED, _COUNT_OR, _EXPANDED])
    assert again["empty_lists_dropped"]["records"] == 15
    assert answer.count(_REQUIRED) == 1


def test_a_count_of_zero_reaches_nothing(monkeypatch):
    el = _el()
    (_expanded, counted), answer = _ask(monkeypatch, _Lake(count="0"), [_EXPANDED, _COUNT_OR])
    assert counted["rows"] == [["0"]]
    assert el.ENGINE_PREFIX not in answer and "REQUIRED: the answer must say that" not in answer


#: Counts of these records that are not the expansion's: another WHERE or
#: column, or another source with the same table name.
_UNMATCHED_COUNTS = {
    "another_value": _COUNT_OR.replace("zone = 'Z2'", "zone = 'Z3'"),
    "a_conjunct_left_out": _COUNT_OR.replace("zone = 'Z2' AND ", ""),
    "a_conjunct_added": _COUNT_OR.replace("zone = 'Z2'", "zone = 'Z2' AND shift = 'night'"),
    "another_column": _COUNT_PLAIN.replace(f"json_array_length({_LIST})",
                                           f"json_array_length({_LABELS})"),
    "another_schema": _COUNT_PLAIN.replace(f"FROM {_TABLE}", f"FROM yard.{_TABLE}"),
    # the NULL lists alone: a count under another WHERE
    "null_lists_alone": f"{_COUNT_PLAIN} AND {_LIST} IS NULL",
}
#: Queries that are not a count of every record with an empty or NULL list.
_UNREAD_COUNTS = {
    "another_columns_null": _COUNT_OR.replace(f"{_LIST} IS NULL", f"{_LABELS} IS NULL"),
    "misses_a_spelling": (f"SELECT COUNT(*) AS n FROM {_TABLE} {_WHERE} "
                          f"AND ({_LIST} IS NULL OR {_LIST} = '[]')"),
    "misses_null": (f"SELECT COUNT(*) AS n FROM {_TABLE} {_WHERE} "
                    f"AND json_array_length({_LIST}) = 0"),
    "negated": f"SELECT COUNT(*) AS n FROM {_TABLE} {_WHERE} AND NOT ({_EMPTY})",
    "complement": (f"SELECT COUNT(*) AS n FROM {_TABLE} {_WHERE} "
                   f"AND COALESCE(json_array_length({_LIST}), 0) > 0"),
    "inner_and": f"SELECT COUNT(*) AS n FROM {_TABLE} {_WHERE} AND ({_EMPTY} AND {_LIST} IS NULL)",
    "sample": _COUNT_PLAIN.replace(f"FROM {_TABLE}", f"FROM {_TABLE} TABLESAMPLE BERNOULLI (10)"),
    "top_level_or": _COUNT_PLAIN.replace("zone = 'Z2' AND", "zone = 'Z2' OR"),
    "grouped": f"{_COUNT_PLAIN} GROUP BY zone",
    "having": f"{_COUNT_PLAIN} HAVING COUNT(*) > 0",
    "filter": _COUNT_PLAIN.replace("COUNT(*) AS n", f"COUNT(*) FILTER (WHERE {_EMPTY}) AS n"
                                   ).replace(f" AND {_EMPTY}", ""),
    "distinct": _COUNT_PLAIN.replace("COUNT(*)", "COUNT(DISTINCT zone)"),
    "two_outputs": _COUNT_PLAIN.replace("COUNT(*) AS n", "COUNT(*) AS n, MAX(zone) AS z"),
    "join": _COUNT_PLAIN.replace(f"FROM {_TABLE}",
                                 f"FROM {_TABLE} p JOIN zones z ON p.zone = z.zone"),
    "cte": (f"WITH x AS (SELECT zone FROM {_TABLE} {_WHERE} AND {_EMPTY}) "
            "SELECT COUNT(*) AS n FROM x"),
}
_ONE = {"columns": ["n"], "rows": [["15"]], "row_count": 1}


@pytest.mark.parametrize("count", [*_UNMATCHED_COUNTS.values(), *_UNREAD_COUNTS.values()],
                         ids=[*_UNMATCHED_COUNTS, *_UNREAD_COUNTS])
def test_another_count_reaches_nothing(monkeypatch, count):
    el = _el()
    (_expanded, _counted), answer = _ask(monkeypatch, _Lake(), [_EXPANDED, count])
    assert el.ENGINE_PREFIX not in answer and "REQUIRED: the answer must say that" not in answer


@pytest.mark.parametrize("count", _UNMATCHED_COUNTS.values(), ids=_UNMATCHED_COUNTS)
def test_a_count_of_other_records_is_read_and_reaches_no_expansion(count):
    el = _el()
    observed = el.counted(count, _ONE)
    assert observed is not None and observed.records == 15
    assert el.reached(observed, [el.expansion(_EXPANDED)]) == ()
    assert el.dropped(_EXPANDED, [observed]).records is None


@pytest.mark.parametrize("count", _UNREAD_COUNTS.values(), ids=_UNREAD_COUNTS)
def test_another_query_is_not_read_as_a_count(count):
    assert _el().counted(count, _ONE) is None


def test_the_count_is_read_with_its_conjuncts():
    found = _el().counted(_COUNT_OR, {"columns": ["picks_with_no_bin"], "rows": [["15"]]})
    assert found is not None
    assert (found.table, found.column, found.records, found.source) == (
        _TABLE, "routing_actuals_details.bin_list", 15, _TABLE)
    assert found.where == _el().expansion(_EXPANDED).where and len(found.where) == 4
    assert found.having is False and found.others is None


@pytest.mark.parametrize("result", [
    {"columns": ["n"], "rows": [["15"]], "truncated": True},
    {"columns": ["n"], "rows": [["15"], ["4"]]},
    {"columns": ["n", "m"], "rows": [["15", "1"]]},
    {"columns": ["n"], "rows": [["fifteen"]]},
    {"columns": ["n"], "rows": [[None]]},
    {"columns": ["n"], "rows": []},
    {"error": "query_failed"},
    None,
])
def test_a_count_whose_result_is_not_one_whole_count_is_not_read(result):
    assert _el().counted(_COUNT_PLAIN, result) is None


def test_a_count_reaches_only_a_matching_earlier_expansion():
    el = _el()
    other = _EXPANDED.replace("zone = 'Z2'", "zone = 'Z3'")
    waiting = [el.expansion(other), el.expansion(_EXPANDED), "not a record"]
    observed = el.counted(_COUNT_OR, {"columns": ["n"], "rows": [["15"]]})
    assert el.reached(observed, waiting) == (
        el.Dropped(table=_TABLE, column="routing_actuals_details.bin_list", records=15),)
    assert el.reached(None, waiting) == ()
    zero = el.counted(_COUNT_OR, {"columns": ["n"], "rows": [["0"]]})
    assert zero is not None and el.reached(zero, waiting) == ()


def test_an_expansion_with_a_having_is_not_reached():
    el = _el()
    having = _EXPANDED.replace(" ORDER BY", " HAVING COUNT(*) > 1 ORDER BY")
    observed = el.counted(_COUNT_OR, {"columns": ["n"], "rows": [["15"]]})
    assert el.reached(observed, [el.expansion(having)]) == ()


# --------------------------------------------------------------------------- #
# A top-level OR is not a set of conjuncts
# --------------------------------------------------------------------------- #
def test_a_top_level_or_is_not_read_as_conjuncts():
    el = _el()
    where = "WHERE zone = 'Z2' AND shift = 'day' OR zone = 'Z9'"
    grouped = (f"SELECT {_LIST} AS bins_json, COUNT(*) AS n FROM {_TABLE} {where} "
               f"GROUP BY {_LIST}")
    expanded = (f"SELECT json_extract_scalar(e, '$.id') AS bin_id, COUNT(*) AS n FROM {_TABLE} "
                f"CROSS JOIN UNNEST(CAST(json_parse({_LIST}) AS ARRAY(JSON))) AS t(e) "
                f"WHERE shift = 'day' OR zone = 'Z9' AND zone = 'Z2' "
                "GROUP BY json_extract_scalar(e, '$.id')")
    finding = types.SimpleNamespace(container="list", plain=True, table=_TABLE,
                                    column="routing_actuals_details.bin_list", records_known=True,
                                    empty_records=11, empty_rows=1, empty_table=())
    observed = el.observe(grouped, finding)
    assert observed is not None and observed.where is None
    assert el.expansion(expanded).where is None
    found = el.dropped(expanded, [observed])
    assert found is not None and found.records is None


# --------------------------------------------------------------------------- #
# The totals line keeps the list-elements label past the entry cap
# --------------------------------------------------------------------------- #
def _af():
    return importlib.import_module("agents.answer_figures")


def _statement(sql: str, rows=_EXPANDED_ROWS) -> dict:
    payload = {"columns": ["miss_reason", "bin_id", "bin_name", "pick_count"], "rows": rows,
               "row_count": len(rows), "_adt_executed_query": sql}
    record = _af().totals_record(payload)
    assert record is not None
    return {"query_text": sql, "totals": record}


def test_a_long_expansion_label_keeps_its_list_elements_with_the_key_names():
    af = _af()
    line = af.totals_line([_statement(_EXPANDED)])
    body = line.split("numbered as above: ", 1)[1]
    assert len(body) <= af._MAX_ENTRY_CHARS + 1
    assert body == ("Q1 (5 rows by miss_reason, bin_id, bin_name, of list elements: one row per "
                    "element of UNNEST(CAST(json_parse(\"routing_actuals_details.bin_list\") AS "
                    f"ARRAY(JSON))) in each {_TABLE} row, so a record whose list is empty adds "
                    "no row, with handling_record_details.pick_data.miss_flagged_date in "
                    "[2031-04-03 00:00:00 UTC, 2031-04-15 00:00:00 UTC), where zone = 'Z2' AND "
                    "\"handling_record_details.pick_data.miss_reason\" IN (<3 values>)): "
                    "pick_count 1,733.")


def test_a_long_expansion_with_its_count_keeps_the_label_too():
    af = _af()
    payload = {"columns": ["miss_reason", "bin_id", "bin_name", "pick_count"],
               "rows": _EXPANDED_ROWS, "row_count": len(_EXPANDED_ROWS),
               "_adt_executed_query": _EXPANDED,
               "empty_lists_dropped": {"column": "routing_actuals_details.bin_list",
                                       "records": 15}}
    line = af.totals_line([{"query_text": _EXPANDED, "totals": af.totals_record(payload)}])
    body = line.split("numbered as above: ", 1)[1]
    assert len(body) <= af._MAX_ENTRY_CHARS + 1
    assert re.match(r"Q1 \(5 rows by miss_reason, bin_id, bin_name, of list elements: ", body)
    assert body.endswith("): pick_count 1,733.")
