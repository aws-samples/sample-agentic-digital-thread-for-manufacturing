# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Records a list expansion drops: a row whose serialized JSON list is empty or NULL.

Measured in dry runs on an analytics deployment: a grouped query by a code and
a serialized JSON list column was withheld because one list value was stored in
several spellings, and the refusal sent the agent to one row per element
(``CROSS JOIN UNNEST``). One group of the withheld rows held an empty list for
15 records. The expansion has no row for a record with no element, so its
per-element counts for that code summed to 15 fewer than the code's records,
the result carried no note, the totals line added its element rows as if they
were records, and the answer said nothing had been left out.

``json_spellings`` now counts those records in its refusal; this module
remembers the count for the Mode A call (``observe``), and a later inner
expansion of the same list column of the same table with the same WHERE
conjuncts and no HAVING carries the count (``dropped``), a note, an engine line
and a FINAL-ANSWER REQUIREMENTS block. With no such earlier count the result
gets a note without a number; with a count of zero it gets nothing; a
``LEFT JOIN UNNEST`` keeps those rows and gets nothing. The totals line labels
an expansion result's rows as elements.

Fully offline; synthetic names.
"""
from __future__ import annotations

import asyncio
import importlib
import json

import pytest
from strands._async import run_async

import agents.athena_tools as at
import agents.system_agent as sa
from agents import record_time, state_times, storage_bounds

_CFG = {"workgroup": "wg", "database": "dock_db", "region": "us-east-1"}
_TABLE = "crate_scan_log"
_LIST = '"handling.stations"'
_KIND = '"inspection.fault_kind"'
_WHERE = (f"WHERE dock = 'D4' AND \"inspection.found_at\" >= TIMESTAMP '2026-03-02 00:00:00 UTC' "
          f"AND \"inspection.found_at\" < TIMESTAMP '2026-03-14 00:00:00 UTC' "
          f"AND {_KIND} IN ('Wrap: torn film', 'Pallet: cracked board', 'Label: smeared')")
#: The grouped query, by the code and the list text, in the shape the dry run
#: sent: an alias on each output, ORDER BY the alias and LIMIT.
_GROUPED = (f"SELECT {_KIND} AS fault_code, {_LIST} AS stations_json, COUNT(*) AS scan_count "
            f"FROM {_TABLE} {_WHERE} GROUP BY {_KIND}, {_LIST} "
            "ORDER BY fault_code, scan_count DESC LIMIT 1000")
_EXPANSION = f"CROSS JOIN UNNEST(CAST(json_parse({_LIST}) AS ARRAY(JSON))) AS t(e)"
#: The remedy the refusal gives, with the same WHERE.
_EXPANDED = (f"SELECT {_KIND} AS fault_code, json_extract_scalar(e, '$.id') AS station_id, "
             f"json_extract_scalar(e, '$.name') AS station_name, COUNT(*) AS scan_count "
             f"FROM {_TABLE} {_EXPANSION} {_WHERE} "
             f"GROUP BY {_KIND}, json_extract_scalar(e, '$.id'), json_extract_scalar(e, '$.name') "
             "ORDER BY fault_code, scan_count DESC LIMIT 1000")
_S1 = '[{"id":"ST-1","name":"North","qty":1}]'
_S1_OTHER = '[{"qty":1,"id":"ST-1","name":"North"}]'
_S2 = '[{"id":"ST-2","name":"South","qty":1}]'
_S2_OTHER = '[{"name":"South","qty":1,"id":"ST-2"}]'
_GROUPED_ROWS = [["Wrap: torn film", _S1, "40"], ["Wrap: torn film", _S1_OTHER, "30"],
                 ["Pallet: cracked board", _S2, "20"], ["Pallet: cracked board", _S2_OTHER, "6"],
                 ["Label: smeared", _S1, "50"], ["Label: smeared", _S2_OTHER, "34"],
                 ["Label: smeared", "[]", "15"]]
_EXPANDED_ROWS = [["Wrap: torn film", "ST-1", "North", "70"],
                  ["Pallet: cracked board", "ST-2", "South", "26"],
                  ["Label: smeared", "ST-1", "North", "50"],
                  ["Label: smeared", "ST-2", "South", "34"]]
_QUESTION = "How many scans of each fault code were there per station?"


def _el():
    return importlib.import_module("agents.empty_lists")


def _js():
    return importlib.import_module("agents.json_spellings")


class _Lake:
    """One unpartitioned table; the grouped query answers ``grouped`` and any
    query with ``UNNEST`` answers ``expanded``."""

    def __init__(self, grouped=None, expanded=None):
        self.grouped = grouped if grouped is not None else _GROUPED_ROWS
        self.expanded = expanded if expanded is not None else _EXPANDED_ROWS
        self.queries: list[str] = []

    def get_table_metadata(self, **_kwargs):
        return {"TableMetadata": {
            "Name": _TABLE,
            "Columns": [{"Name": "dock", "Type": "varchar"},
                        {"Name": "inspection.fault_kind", "Type": "varchar"},
                        {"Name": "inspection.found_at", "Type": "timestamp"},
                        {"Name": "handling.stations", "Type": "varchar"}],
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
            names = ["fault_code", "station_id", "station_name", "scan_count"]
            rows = self.expanded
        else:
            names, rows = ["fault_code", "stations_json", "scan_count"], self.grouped
        header = {"Data": [{"VarCharValue": n} for n in names]}
        return {"ResultSet": {
            "ResultSetMetadata": {"ColumnInfo": [{"Name": n, "Type": "varchar"} for n in names]},
            "Rows": [header] + [{"Data": [{} if v is None else {"VarCharValue": v} for v in row]}
                                for row in rows]}}

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


def _ask(monkeypatch, lake, sqls, *, answer_text="Label: smeared had 84 scans."):
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
# End to end: the refusal counts them, the expansion carries the count
# --------------------------------------------------------------------------- #
def test_the_refusal_counts_the_records_the_expansion_will_drop(monkeypatch):
    (payload,), _answer = _ask(monkeypatch, _Lake(), [_GROUPED])
    assert payload["error_name"] == "JSON_SPELLINGS"
    assert payload["records_with_empty_list"] == 15
    assert payload["empty_list_rows"] == {"columns": ["fault_code", "scan_count"],
                                          "rows": [["Label: smeared", "15"]]}
    assert "15 records under this query's filters have no element" in payload["guidance"]


def test_an_expansion_after_the_refusal_carries_the_dropped_records(monkeypatch):
    el = _el()
    (first, second), answer = _ask(monkeypatch, _Lake(), [_GROUPED, _EXPANDED])
    assert first["error_name"] == "JSON_SPELLINGS"
    assert second["rows"] == _EXPANDED_ROWS
    dropped = second["empty_lists_dropped"]
    assert dropped["column"] == "handling.stations" and dropped["records"] == 15
    assert dropped["rows"] == {"columns": ["fault_code", "scan_count"],
                               "rows": [["Label: smeared", "15"]]}
    assert "15 records" in second["note"] and "empty_lists_dropped" in second["note"]
    assert el.ENGINE_PREFIX in answer and el.MARKER in answer
    assert "REQUIRED: the answer must say that 15 records" in answer
    block_at = answer.index("REQUIRED: the answer must say that 15 records")
    assert answer.index(el.ENGINE_PREFIX) < block_at


def test_the_totals_line_labels_the_element_rows(monkeypatch):
    from agents import answer_figures
    (_first, second), _answer = _ask(monkeypatch, _Lake(), [_GROUPED, _EXPANDED])
    statements = [{"query_text": second["_adt_executed_query"],
                   "totals": answer_figures.totals_record(second)}]
    line = answer_figures.totals_line(statements)
    assert "scan_count = COUNT(*) 180" in line
    assert ('one row per element of UNNEST(CAST(json_parse("handling.stations") AS ARRAY(JSON))) '
            f"in each {_TABLE} row, so 15 records with an empty or NULL list add no row") in line


def test_the_dropped_rows_are_left_out_when_the_other_keys_differ(monkeypatch):
    expanded = (f"SELECT json_extract_scalar(e, '$.id') AS station_id, COUNT(*) AS scan_count "
                f"FROM {_TABLE} {_EXPANSION} {_WHERE} GROUP BY json_extract_scalar(e, '$.id')")
    lake = _Lake(expanded=[["ST-1", "120"], ["ST-2", "60"]])
    (_first, second), answer = _ask(monkeypatch, lake, [_GROUPED, expanded])
    dropped = second["empty_lists_dropped"]
    assert dropped["records"] == 15 and "rows" not in dropped
    assert "REQUIRED: the answer must say that 15 records" in answer


def test_the_comma_form_of_the_expansion_carries_the_count(monkeypatch):
    expanded = _EXPANDED.replace(f"FROM {_TABLE} CROSS JOIN UNNEST", f"FROM {_TABLE}, UNNEST")
    (_first, second), _answer = _ask(monkeypatch, _Lake(), [_GROUPED, expanded])
    assert second["empty_lists_dropped"]["records"] == 15


@pytest.mark.parametrize("expanded", [
    # another WHERE: the earlier count is not of these records
    _EXPANDED.replace("dock = 'D4'", "dock = 'D5'"),
    # an extra conjunct
    _EXPANDED.replace(_WHERE, f"{_WHERE} AND json_extract_scalar(e, '$.id') = 'ST-1'"),
    # a HAVING on the expansion
    _EXPANDED.replace("ORDER BY", "HAVING COUNT(*) > 1 ORDER BY"),
])
def test_an_expansion_that_does_not_match_gets_a_note_without_a_count(monkeypatch, expanded):
    el = _el()
    (_first, second), answer = _ask(monkeypatch, _Lake(), [_GROUPED, expanded])
    assert "empty_lists_dropped" not in second
    assert "COALESCE(json_array_length(\"handling.stations\"), 0) = 0" in second["note"]
    assert "15" not in second["note"]
    assert el.ENGINE_PREFIX not in answer


def test_an_earlier_refusal_with_having_gives_no_count(monkeypatch):
    el = _el()
    grouped = _GROUPED.replace("ORDER BY", "HAVING COUNT(*) > 0 ORDER BY")
    (first, second), answer = _ask(monkeypatch, _Lake(), [grouped, _EXPANDED])
    assert first["error_name"] == "JSON_SPELLINGS"
    assert first["records_with_empty_list_at_least"] == 15
    assert "empty_lists_dropped" not in second and "COALESCE" in second["note"]
    assert el.ENGINE_PREFIX not in answer


def test_an_earlier_cut_refusal_gives_no_count(monkeypatch):
    el = _el()
    grouped = _GROUPED.replace("LIMIT 1000", f"LIMIT {len(_GROUPED_ROWS)}")
    (first, second), answer = _ask(monkeypatch, _Lake(), [grouped, _EXPANDED])
    assert first["records_with_empty_list_at_least"] == 15
    assert "empty_lists_dropped" not in second and "COALESCE" in second["note"]
    assert el.ENGINE_PREFIX not in answer


def test_an_expansion_with_no_earlier_finding_gets_a_note_without_a_count(monkeypatch):
    el = _el()
    (payload,), answer = _ask(monkeypatch, _Lake(), [_EXPANDED])
    assert payload["rows"] == _EXPANDED_ROWS
    assert "empty_lists_dropped" not in payload
    note = payload["note"]
    assert "one row per element" in note and "empty or NULL" in note
    assert "COALESCE(json_array_length(\"handling.stations\"), 0) = 0" in note
    assert el.ENGINE_PREFIX not in answer


def test_a_known_zero_stays_silent(monkeypatch):
    el = _el()
    grouped = [row for row in _GROUPED_ROWS if row[1] != "[]"]
    (first, second), answer = _ask(monkeypatch, _Lake(grouped=grouped), [_GROUPED, _EXPANDED])
    assert first["error_name"] == "JSON_SPELLINGS"
    assert "rows_with_empty_list" not in first
    assert "empty_lists_dropped" not in second and "note" not in second
    assert el.ENGINE_PREFIX not in answer


def test_a_left_join_expansion_gets_nothing(monkeypatch):
    el = _el()
    left = _EXPANDED.replace(_EXPANSION, "LEFT JOIN UNNEST(CAST(json_parse("
                             f"{_LIST}) AS ARRAY(JSON))) AS t(e) ON TRUE")
    (_first, second), answer = _ask(monkeypatch, _Lake(), [_GROUPED, left])
    assert "empty_lists_dropped" not in second and "note" not in second
    assert el.ENGINE_PREFIX not in answer


def test_the_resent_refusal_then_the_expansion_carries_the_count(monkeypatch):
    (first, second, third), answer = _ask(monkeypatch, _Lake(), [_GROUPED, _GROUPED, _EXPANDED])
    assert first["error_name"] == "JSON_SPELLINGS" and second["rows"] == _GROUPED_ROWS
    assert second["json_spellings"]["records_with_empty_list"] == 15
    assert third["empty_lists_dropped"]["records"] == 15
    assert answer.count("REQUIRED: the answer must say that 15 records") == 1


def test_without_a_mode_a_call_nothing_changes(monkeypatch):
    lake = _Lake()
    monkeypatch.setattr(at, "boto3_client_for", lambda *_a, **_k: lake)
    payload = sa._scoped_athena_attempt(_EXPANDED, at._resolve_config(_CFG), {}, "system-a")
    assert payload["rows"] == _EXPANDED_ROWS
    assert "empty_lists_dropped" not in payload and "note" not in payload


def test_under_enforce_the_payload_carries_it_and_no_engine_line(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    el = _el()
    (_first, second), answer = _ask(monkeypatch, _Lake(), [_GROUPED, _EXPANDED])
    assert second["empty_lists_dropped"]["records"] == 15
    assert el.ENGINE_PREFIX not in answer


def test_a_copied_engine_line_in_the_answer_is_made_inert(monkeypatch):
    el = _el()
    copied = f"{el.ENGINE_PREFIX} check, in this turn's queries: none."
    (_payload,), answer = _ask(monkeypatch, _Lake(), [_EXPANDED], answer_text=copied)
    assert el.ENGINE_PREFIX not in answer


# --------------------------------------------------------------------------- #
# Which SQL is an inner expansion of a serialized list column
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("sql", "column"), [
    (_EXPANDED, "handling.stations"),
    (f"SELECT COUNT(*) AS n FROM {_TABLE} {_EXPANSION}", "handling.stations"),
    (f"SELECT COUNT(*) AS n FROM {_TABLE} c CROSS JOIN UNNEST(CAST(json_parse(c.{_LIST}) "
     "AS ARRAY(MAP(VARCHAR, JSON)))) AS t(e)", "handling.stations"),
    (f"SELECT COUNT(*) AS n FROM {_TABLE}, UNNEST(TRY_CAST(json_parse(payload) "
     "AS ARRAY(JSON))) t(e)", "payload"),
])
def test_an_inner_expansion_is_read(sql, column):
    found = _el().expansion(sql)
    assert found is not None and found.column == column and found.table == _TABLE


@pytest.mark.parametrize("sql", [
    None, "", "SELECT 1",
    _GROUPED,
    # the rows are kept
    f"SELECT COUNT(*) FROM {_TABLE} LEFT JOIN UNNEST(CAST(json_parse({_LIST}) AS ARRAY(JSON))) "
    "AS t(e) ON TRUE",
    # not a serialized list
    f"SELECT COUNT(*) FROM {_TABLE} CROSS JOIN UNNEST(tags) AS t(tag)",
    f"SELECT COUNT(*) FROM {_TABLE} CROSS JOIN UNNEST(split(codes, ',')) AS t(code)",
    # a join
    f"SELECT COUNT(*) FROM {_TABLE} c JOIN docks d ON c.dock = d.dock {_EXPANSION}",
    # inside a CTE
    f"WITH x AS (SELECT e FROM {_TABLE} {_EXPANSION}) SELECT COUNT(*) FROM x",
    # an expression, not a column
    f"SELECT COUNT(*) FROM {_TABLE} CROSS JOIN UNNEST(CAST(json_parse(lower({_LIST})) "
    "AS ARRAY(JSON))) AS t(e)",
])
def test_other_shapes_are_not_read(sql):
    assert _el().expansion(sql) is None


def test_an_expansion_inside_a_cte_gets_the_note_without_a_count():
    el = _el()
    observed = el.ListObservation(table=_TABLE, column="handling.stations", where=frozenset(),
                                  having=False, others=frozenset(), records=15)
    wrapped = (f"WITH x AS (SELECT json_extract_scalar(e, '$.id') AS s, COUNT(*) AS n "
               f"FROM {_TABLE} {_EXPANSION} GROUP BY json_extract_scalar(e, '$.id')) "
               "SELECT s, n FROM x ORDER BY n DESC")
    found = el.dropped(wrapped, [observed])
    assert found is not None and found.records is None and found.column == "handling.stations"
    assert "COALESCE(json_array_length(\"handling.stations\"), 0) = 0" in el.note(found)
    derived = (f"SELECT COUNT(*) AS n FROM (SELECT DISTINCT json_extract_scalar(e, '$.id') AS s "
               f"FROM {_TABLE} {_EXPANSION}) d")
    assert el.dropped(derived, []).records is None


@pytest.mark.parametrize("sql", [
    # an outer join can keep the records the inner expansion drops
    f"WITH x AS (SELECT id, e FROM {_TABLE} {_EXPANSION}) "
    f"SELECT c.id, COUNT(x.e) AS n FROM {_TABLE} c LEFT JOIN x ON c.id = x.id GROUP BY c.id",
    f"SELECT COUNT(*) FROM (SELECT e FROM {_TABLE} {_EXPANSION}) a "
    f"UNION ALL SELECT COUNT(*) FROM {_TABLE}",
])
def test_a_nested_expansion_under_an_outer_join_or_union_gets_nothing(sql):
    assert _el().dropped(sql, []) is None


# --------------------------------------------------------------------------- #
# Whether a result holds every row of its query
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("result", "sql", "complete"), [
    ({"rows": [["a"], ["b"]]}, "SELECT x FROM t GROUP BY x", True),
    ({"rows": [["a"], ["b"]]}, "SELECT x FROM t GROUP BY x LIMIT 5", True),
    ({"rows": [["a"], ["b"]]}, "SELECT x FROM t GROUP BY x LIMIT 2", False),
    ({"rows": [["a"], ["b"]]}, "SELECT x FROM t GROUP BY x OFFSET 2", False),
    ({"rows": [["a"]], "truncated": True}, "SELECT x FROM t GROUP BY x", False),
    ({"rows": [["a"]], "capped_at_limit": True}, "SELECT x FROM t GROUP BY x", False),
    ({"rows": [["a"]], "partial_reason": "row_limit"}, "SELECT x FROM t GROUP BY x", False),
    ({"rows": [["a"]], "row_count": 3}, "SELECT x FROM t GROUP BY x", False),
    ({}, "SELECT x FROM t GROUP BY x", False),
])
def test_complete_result(result, sql, complete):
    assert _el().complete_result(result, sql) is complete


# --------------------------------------------------------------------------- #
# The texts: facts, names only when printable
# --------------------------------------------------------------------------- #
def _dropped(records=15, rows=None):
    el = _el()
    return el.Dropped(table=_TABLE, column="handling.stations", records=records,
                      rows=rows or (("fault_code", "scan_count"), (("Label: smeared", "15"),)))


def test_the_engine_line_and_block_state_the_count():
    el = _el()
    line = el.engine_line([_dropped()])
    assert line.startswith(el.ENGINE_PREFIX) and el.MARKER in line
    assert "15 records" in line and "handling.stations" in line and _TABLE in line
    block = el.requirement_block([_dropped()])
    assert "FINAL-ANSWER REQUIREMENTS" in block
    assert ("REQUIRED: the answer must say that 15 records under the filters of its figures by "
            "handling.stations hold no element of it and are not in those figures.") in block
    assert el.engine_line([]) == "" and el.requirement_block([]) == ""
    assert el.engine_line([_dropped(records=None)]) == ""
    assert el.engine_line(["not a record"]) == ""


def test_one_record_reads_in_the_singular():
    el = _el()
    assert "1 record under" in el.requirement_block([_dropped(records=1)])
    assert "holds no element" in el.requirement_block([_dropped(records=1)])


def test_a_copied_engine_line_is_made_inert():
    el = _el()
    copied = f"{el.engine_line([_dropped()])} and more"
    assert el.ENGINE_PREFIX not in el.neutralize(copied)
    assert el.neutralize("plain text") == "plain text"


def test_the_payload_record_is_json():
    record = _dropped().to_dict()
    assert json.loads(json.dumps(record)) == {
        "column": "handling.stations", "records": 15,
        "rows": {"columns": ["fault_code", "scan_count"], "rows": [["Label: smeared", "15"]]}}


# --------------------------------------------------------------------------- #
# The orchestrator applies the block
# --------------------------------------------------------------------------- #
def test_engine_facts_name_every_marker_that_carries_a_block():
    import agents.orchestrator as orch
    from agents import answer_figures, json_spellings, record_key
    rule = " ".join(orch._ENGINE_FACT_RULE.split())
    for marker in (_el().MARKER, json_spellings.MARKER, record_key.MARKER, record_time.MARKER,
                   state_times.MARKER, answer_figures.MARKER):
        assert marker in rule, marker
    assert "FINAL-ANSWER REQUIREMENTS block" in rule


# --------------------------------------------------------------------------- #
# Only the query's own row filter and its full source name match a count
# --------------------------------------------------------------------------- #
class _FilterCountLake(_Lake):
    """The grouped query also has a ``COUNT(*) FILTER (WHERE ...)`` output."""

    def get_query_results(self, QueryExecutionId, **kwargs):
        sql = self.queries[int(QueryExecutionId[1:]) - 1]
        if "UNNEST" in sql:
            return super().get_query_results(QueryExecutionId, **kwargs)
        names = ["fault_code", "stations_json", "scan_count", "d4_count"]
        rows = [row + [str(int(row[2]) // 4)] for row in self.grouped]
        header = {"Data": [{"VarCharValue": n} for n in names]}
        return {"ResultSet": {
            "ResultSetMetadata": {"ColumnInfo": [{"Name": n, "Type": "varchar"} for n in names]},
            "Rows": [header] + [{"Data": [{"VarCharValue": v} for v in row]} for row in rows]}}


def test_a_filter_clause_in_an_aggregate_is_not_the_row_filter(monkeypatch):
    # The grouped query's 15 counts every row; its FILTER output counts only
    # dock D4. An expansion whose own WHERE is dock D4 is a different
    # population, so the 15 is not its dropped count.
    el = _el()
    grouped = (f"SELECT {_KIND} AS fault_code, {_LIST} AS stations_json, COUNT(*) AS scan_count, "
               "COUNT(*) FILTER (WHERE dock = 'D4') AS d4_count "
               f"FROM {_TABLE} GROUP BY {_KIND}, {_LIST} ORDER BY fault_code LIMIT 1000")
    expanded = _EXPANDED.replace(_WHERE, "WHERE dock = 'D4'")
    (first, second), answer = _ask(monkeypatch, _FilterCountLake(), [grouped, expanded])
    assert first["error_name"] == "JSON_SPELLINGS"
    assert "empty_lists_dropped" not in second
    assert "COALESCE(json_array_length(\"handling.stations\"), 0) = 0" in second["note"]
    assert "15" not in second["note"]
    assert el.ENGINE_PREFIX not in answer


def test_an_expansion_whose_figure_has_its_own_filter_gets_no_count(monkeypatch):
    # The count of 15 is over every row; the expansion's figure is a FILTER
    # count, so not every one of the 15 is missing from it.
    el = _el()
    grouped = _GROUPED.replace(f"{_WHERE} ", "")
    expanded = (f"SELECT {_KIND} AS fault_code, json_extract_scalar(e, '$.id') AS station_id, "
                "json_extract_scalar(e, '$.name') AS station_name, "
                "COUNT(*) FILTER (WHERE dock = 'D4') AS scan_count "
                f"FROM {_TABLE} {_EXPANSION} "
                f"GROUP BY {_KIND}, json_extract_scalar(e, '$.id'), json_extract_scalar(e, '$.name')")
    (first, second), answer = _ask(monkeypatch, _Lake(), [grouped, expanded])
    assert first["error_name"] == "JSON_SPELLINGS" and first["records_with_empty_list"] == 15
    assert "empty_lists_dropped" not in second
    assert el.ENGINE_PREFIX not in answer


def _finding(table=_TABLE):
    """A ``json_spellings`` finding on the list column with 11 empty-list records."""
    import types
    return types.SimpleNamespace(container="list", plain=True, table=table,
                                 column="handling.stations", records_known=True,
                                 empty_records=11, empty_rows=1, empty_table=())


def _grouped_over(source):
    return (f"SELECT {_LIST} AS stations_json, COUNT(*) AS scan_count FROM {source} "
            f"WHERE dock = 'D4' GROUP BY {_LIST}")


def _expanded_over(source):
    return (f"SELECT json_extract_scalar(e, '$.id') AS station_id, COUNT(*) AS scan_count "
            f"FROM {source} {_EXPANSION} WHERE dock = 'D4' GROUP BY json_extract_scalar(e, '$.id')")


@pytest.mark.parametrize("source", [
    _TABLE, f"ops.{_TABLE}", f"catalog_a.ops.{_TABLE}", f'"Ops"."{_TABLE}"',
])
def test_the_same_source_carries_the_count(source):
    el = _el()
    observed = el.observe(_grouped_over(source), _finding())
    assert observed is not None
    assert el.dropped(_expanded_over(source.replace('"Ops"', "ops")), [observed]).records == 11


@pytest.mark.parametrize(("observed_on", "expanded_on"), [
    # two catalogs, one schema and table name
    (f"catalog_a.ops.{_TABLE}", f"catalog_b.ops.{_TABLE}"),
    # two schemas
    (f"ops.{_TABLE}", f"yard.{_TABLE}"),
    # a qualified name is not assumed to be the unqualified one
    (f"ops.{_TABLE}", _TABLE),
    # a snapshot or a sample is another population
    (f"{_TABLE} FOR VERSION AS OF 4211", _TABLE),
    (_TABLE, f"{_TABLE} FOR TIMESTAMP AS OF TIMESTAMP '2026-03-01 00:00:00 UTC'"),
    (f"{_TABLE} TABLESAMPLE BERNOULLI (10)", _TABLE),
    (_TABLE, f"{_TABLE} TABLESAMPLE SYSTEM (50)"),
    (f"{_TABLE} TABLESAMPLE BERNOULLI (10)", f"{_TABLE} TABLESAMPLE BERNOULLI (10)"),
])
def test_another_source_with_the_same_table_name_gets_no_count(observed_on, expanded_on):
    el = _el()
    observed = el.observe(_grouped_over(observed_on), _finding())
    found = el.dropped(_expanded_over(expanded_on), [observed] if observed else [])
    assert found is None or found.records is None
