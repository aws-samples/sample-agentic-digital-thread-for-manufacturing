# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A grouped COUNT(DISTINCT ...) result says its rows may not add up.

Measured on a rehearsal account (two of four dry runs): a system agent's SQL
returned several grouped rows with a COUNT(DISTINCT ...) column. Each row
counts distinct values inside its own group, and one value can sit in several
groups, so those rows do not add up. The agent then added such rows up, or
merged them into a coarser grouping, in its own prose: once while the result
at the coarser grouping was already in hand, once over a few dozen rows. The
EFFICIENCY rule against prose totals was in the prompt both times; it is
framed around a total the question asks for, and the merge was one the agent
built for its own table.

The engine half is a figure-free note on the result, delivered the way the
existing ``repeat_note`` and ``aggregate_note`` are: a key of the tool result
the model reads. The prompt half is one sentence in EFFICIENCY step 2:
merging rows into a coarser grouping is a total too. The note says "may
not": when the grouping splits the values (each in one group only) the rows
do add up, so it informs and never refuses or withholds anything.

Pinned offline:

1. ``aggregate_guard.grouped_distinct_counts`` reads the main query's select
   list with the module's own parser: the items computed with a top-level
   COUNT(DISTINCT ...) when the main query has a GROUP BY; nothing for
   COUNT(*), an ungrouped query, a subquery, a window, a set operation or SQL
   it cannot read.
2. ``response_integrity_query.with_distinct_note`` adds the note to a
   result with at least two rows, and to nothing else; it carries no figure.
3. The PostgreSQL and Athena tools (Mode A and Mode B) apply it, so the note
   reaches the model in the tool result, beside the existing notes.
4. The guidance sentence is in EFFICIENCY step 2 exactly once, for SQL
   engines only.
5. The note trips no outcome, outage, error, memory or fabrication classifier
   and no golden ``answer_must_not_match``, and names no dataset word.
"""
from __future__ import annotations

import json
import re

import pytest

import agents.aggregate_guard as ag
import agents.athena_tools as at
import agents.chat_client as chat_client
import agents.orchestrator as orch
import agents.response_integrity_query as riq
import agents.system_agent as sa
from agents import eval_core, query_intelligence as qi

_GROUPED = ("SELECT kind, COUNT(DISTINCT item_id) AS items "
            "FROM table_x WHERE site = 'site-a' GROUP BY kind ORDER BY items DESC")
_GROUPED_COLUMNS = ["kind", "items"]
_GROUPED_ROWS = [["k-1", 7], ["k-2", 4], ["k-3", 2]]

_SENTENCE = ("Rows of a COUNT(DISTINCT ...) column add up, or merge into a coarser grouping, "
             "only when one counted value cannot belong to several of their groups")
#: The first wording of the sentence: an unconditional remedy for every merge.
_OLD_SENTENCE = "Merging rows into a coarser grouping is a total too"


def _note_of(result) -> str | None:
    return result.get(riq.DISTINCT_NOTE_KEY) if isinstance(result, dict) else None


# --------------------------------------------------------------------------- #
# 1. The detection, from the parsed SQL
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sql, items", [
    (_GROUPED, (1,)),
    # Several grouping columns, the count first, a dotted quoted name.
    ('SELECT COUNT(DISTINCT "detail.item.id") AS n, "detail.code" AS code, kind '
     'FROM table_x GROUP BY "detail.code", kind', (0,)),
    # Two distinct counts beside a COUNT(*); only the distinct ones.
    ("SELECT kind, COUNT(DISTINCT item_id) AS items, COUNT(*) AS records, "
     "COUNT(DISTINCT run_id) AS runs FROM table_x GROUP BY kind", (1, 3)),
    # A CASE inside the distinct count, a FILTER after it, GROUP BY ordinals.
    ("SELECT kind, COUNT(DISTINCT CASE WHEN state <> 'X' THEN item_id END) AS n "
     "FROM table_x GROUP BY 1", (1,)),
    ("SELECT kind, COUNT(DISTINCT item_id) FILTER (WHERE state = 'A') AS n "
     "FROM table_x GROUP BY kind", (1,)),
    # A figure computed from a distinct count is still one per group.
    ("SELECT kind, 100.0 * COUNT(DISTINCT item_id) / COUNT(*) AS share "
     "FROM table_x GROUP BY kind", (1,)),
    # The main query of a WITH, over a CTE, grouped.
    ("WITH f AS (SELECT kind, item_id FROM table_x WHERE site = 'site-a') "
     "SELECT kind, COUNT(DISTINCT item_id) AS items FROM f GROUP BY kind", (1,)),
    # SELECT DISTINCT before the list does not hide the count.
    ("SELECT DISTINCT kind, COUNT(DISTINCT item_id) AS items FROM table_x GROUP BY kind",
     (1,)),
    # Joined, and lowercase keywords.
    ("select a.kind, count(distinct b.item_id) as items from table_x a "
     "join table_y b on b.x_id = a.id group by a.kind", (1,)),
    # One trailing semicolon is the same statement.
    (_GROUPED + ";", (1,)),
    # A distinct count of a grouping term beside other terms: rows still overlap.
    ("SELECT kind, site, COUNT(DISTINCT kind) AS n FROM table_x GROUP BY kind, site", (2,)),
])
def test_grouped_distinct_counts_are_found(sql, items):
    assert ag.grouped_distinct_counts(sql) == items


@pytest.mark.parametrize("sql", [
    # Not grouped: one figure over every row.
    "SELECT COUNT(DISTINCT item_id) AS items FROM table_x WHERE site = 'site-a'",
    # Grouped, but every count is COUNT(*) or a plain COUNT.
    "SELECT kind, COUNT(*) AS n FROM table_x GROUP BY kind",
    "SELECT kind, COUNT(item_id) AS n, SUM(qty) AS q FROM table_x GROUP BY kind",
    # The distinct count is in a CTE or a subquery, not in the main select list.
    "WITH g AS (SELECT kind, COUNT(DISTINCT item_id) AS n FROM table_x GROUP BY kind) "
    "SELECT kind, n FROM g ORDER BY n DESC",
    "SELECT kind, COUNT(*) AS n FROM (SELECT DISTINCT kind, item_id FROM table_x) d "
    "GROUP BY kind",
    "SELECT kind, (SELECT COUNT(DISTINCT item_id) FROM table_y) AS n FROM table_x "
    "GROUP BY kind",
    # Only in HAVING or ORDER BY: no column carries it.
    "SELECT kind, COUNT(*) AS n FROM table_x GROUP BY kind "
    "HAVING COUNT(DISTINCT item_id) > 1",
    # A window, not an aggregate of the group.
    "SELECT kind, COUNT(DISTINCT item_id) OVER (PARTITION BY kind) AS n FROM table_x",
    # A distinct count of the one GROUP BY term: each value is its own group,
    # so the rows add up (by name, by ordinal, qualified).
    "SELECT kind, COUNT(DISTINCT kind) AS n, COUNT(*) AS records FROM table_x GROUP BY kind",
    "SELECT kind, COUNT(DISTINCT kind) AS n FROM table_x GROUP BY 1",
    "SELECT a.kind, COUNT(DISTINCT a.kind) AS n FROM table_x a GROUP BY a.kind",
    # A set operation, the words inside a string, and SQL it cannot read.
    "SELECT kind, COUNT(DISTINCT item_id) FROM table_x GROUP BY kind "
    "UNION ALL SELECT kind, COUNT(DISTINCT item_id) FROM table_y GROUP BY kind",
    "SELECT kind, 'COUNT(DISTINCT item_id)' AS label, COUNT(*) FROM table_x GROUP BY kind",
    "SELECT kind, COUNT(DISTINCT item_id FROM table_x GROUP BY kind",
    "SELECT kind, COUNT(DISTINCT item_id) AS n FROM table_x GROUP BY kind; SELECT 1",
    "SELECT 'unterminated, COUNT(DISTINCT item_id) FROM table_x GROUP BY kind",
    "", "   ", None, 7,
])
def test_anything_else_has_no_grouped_distinct_count(sql):
    assert ag.grouped_distinct_counts(sql) == ()


# A distinct count of the one GROUP BY term, written another way: SQL resolves
# both to the same column, so each value is its own group and the rows add up.
@pytest.mark.parametrize("sql", [
    # Qualified in GROUP BY, bare in the count, and the other way round.
    "SELECT t.kind, COUNT(DISTINCT kind) AS n FROM table_x t GROUP BY t.kind",
    "SELECT kind, COUNT(DISTINCT t.kind) AS n FROM table_x t GROUP BY kind",
    "SELECT table_x.kind, COUNT(DISTINCT kind) AS n FROM table_x GROUP BY table_x.kind",
    "SELECT kind, COUNT(DISTINCT table_x.kind) AS n FROM table_x GROUP BY kind",
    # Qualified on both sides of a JOIN.
    "SELECT a.kind, COUNT(DISTINCT a.kind) AS n FROM table_x a "
    "JOIN table_y b ON b.x_id = a.id GROUP BY a.kind",
    # An unquoted column in GROUP BY spelled in another case.
    "SELECT KIND, COUNT(DISTINCT kind) AS n FROM table_x GROUP BY Kind",
    # An output name that means the same as the input column of that name: either
    # reading of GROUP BY kind groups by the same column.
    "SELECT t.kind AS kind, COUNT(DISTINCT kind) AS n FROM table_x t GROUP BY kind",
    "SELECT table_x.KIND AS kind, COUNT(DISTINCT kind) AS n FROM table_x GROUP BY KIND",
    # GROUP BY kind reads the input column even under a colliding output name, and
    # the count is of that input column: one value per group.
    "SELECT lower(kind) AS kind, COUNT(DISTINCT kind) AS n FROM table_x GROUP BY kind",
    # A quoted lower-case column is the unquoted one.
    'SELECT "kind", COUNT(DISTINCT kind) AS n FROM table_x GROUP BY "kind"',
    # An ordinal naming a qualified select-list item.
    "SELECT t.kind AS k, COUNT(DISTINCT kind) AS n FROM table_x t GROUP BY 1",
    # Redundant parentheses on either side, or both.
    "SELECT kind, COUNT(DISTINCT (kind)) AS n FROM table_x GROUP BY kind",
    "SELECT kind, COUNT(DISTINCT kind) AS n FROM table_x GROUP BY (kind)",
    "SELECT t.kind, COUNT(DISTINCT ((t.kind))) AS n FROM table_x t GROUP BY ((kind))",
    # An output name that is an aggregate is not a grouping target: the input column is.
    "SELECT COUNT(DISTINCT kind) AS kind FROM table_x GROUP BY kind",
])
def test_the_one_group_by_column_is_resolved_before_it_is_compared(sql):
    assert ag.grouped_distinct_counts(sql) == ()


# Still a different column, so still noted.
@pytest.mark.parametrize("sql, items", [
    # A quoted output name stays exact: unquoted K folds to k and is not "K".
    ('SELECT kind AS "K", COUNT(DISTINCT kind) AS n FROM table_x GROUP BY K', (1,)),
    # A quoted mixed-case column is not the unquoted one.
    ('SELECT "Kind", COUNT(DISTINCT kind) AS n FROM table_x GROUP BY "Kind"', (1,)),
    # The same column name of two different FROM items, a self-join included.
    ("SELECT a.kind, COUNT(DISTINCT b.kind) AS n FROM table_x a "
     "JOIN table_y b ON b.x_id = a.id GROUP BY a.kind", (1,)),
    ("SELECT a.kind, COUNT(DISTINCT b.kind) AS n FROM table_x a "
     "JOIN table_x b ON b.parent_id = a.id GROUP BY a.kind", (1,)),
    # With several FROM items a bare column needs the schema to resolve: noted.
    ("SELECT a.kind, COUNT(DISTINCT kind) AS n FROM table_x a "
     "JOIN table_y b ON b.x_id = a.id GROUP BY a.kind", (1,)),
    # Parentheses around two grouping terms are two terms.
    ("SELECT kind, site, COUNT(DISTINCT kind) AS n FROM table_x GROUP BY (kind, site)", (2,)),
    # A GROUP BY name that is also an output name may be an input column of that
    # name, and PostgreSQL reads the input column first. Without the schema that
    # column cannot be ruled out, so an output
    # name that means something else from the input column is never taken as the
    # grouping term. A non-injective expression under a colliding name:
    ("SELECT lower(kind) AS kind, COUNT(DISTINCT lower(kind)) AS n "
     "FROM table_x GROUP BY kind", (1,)),
    ("SELECT item_id % 2 AS item_id, COUNT(DISTINCT item_id % 2) AS n "
     "FROM table_x GROUP BY item_id", (1,)),
    # A renamed column: GROUP BY k may name an input column k.
    ("SELECT kind AS K, COUNT(DISTINCT kind) AS n FROM table_x GROUP BY k", (1,)),
    ("SELECT kind AS k, COUNT(DISTINCT kind) AS n FROM table_x GROUP BY K", (1,)),
    ("SELECT t.kind AS k, COUNT(DISTINCT t.kind) AS n FROM table_x t GROUP BY K", (1,)),
    ('SELECT kind AS "K", COUNT(DISTINCT kind) AS n FROM table_x GROUP BY "K"', (1,)),
])
def test_a_different_column_is_still_noted(sql, items):
    assert ag.grouped_distinct_counts(sql) == items


# --------------------------------------------------------------------------- #
# 2. The note on the result
# --------------------------------------------------------------------------- #
def _result(rows=None, columns=None, **extra) -> dict:
    rows = _GROUPED_ROWS if rows is None else rows
    return {"columns": list(columns or _GROUPED_COLUMNS), "rows": rows,
            "row_count": len(rows), **extra}


def test_a_grouped_distinct_count_with_several_rows_carries_the_note():
    result = _result()
    out = riq.with_distinct_note(result, _GROUPED)
    assert out[riq.DISTINCT_NOTE_KEY] == riq.distinct_note(["items"])
    assert {k: v for k, v in out.items() if k != riq.DISTINCT_NOTE_KEY} == result
    assert riq.DISTINCT_NOTE_KEY not in result            # the input is not changed


_PINNED_NOTE = (
    "In this result, items is computed with COUNT(DISTINCT ...) inside each row's own "
    "group. Whether these rows add up depends on whether one counted value can belong to "
    "several of the returned groups. When each counted value has only one value of the "
    "grouping columns (a property it has once, such as one category per item), it sits in "
    "one group and the rows add up. When one counted value can carry several values of the "
    "grouping columns (several codes, reasons or states per item), it is counted in each "
    "of those groups, so these figures do not add up across rows or merge into a coarser "
    "grouping: take a figure over several groups from its own query, or say it was not "
    "computed. When you cannot tell, do not add the rows: take that figure from its own "
    "query, or say it was not computed. Each row's figure stands as returned.")


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=\.)\s+", text) if s]


def test_the_note_text_is_pinned():
    assert riq.distinct_note(["items"]) == _PINNED_NOTE
    assert riq.distinct_note(["items", "runs"]).startswith(
        "In this result, items and runs are computed with COUNT(DISTINCT ...) inside")
    assert riq.distinct_note(["a", "b", "c"]).startswith(
        "In this result, a, b and c are computed with")
    generic = riq.distinct_note([])
    assert generic.startswith(
        "In this result, a column is computed with COUNT(DISTINCT ...) inside each row's")
    assert generic.endswith(riq.distinct_note(["items"]).split(" inside", 1)[1])


def test_the_note_asks_whether_one_value_can_sit_in_several_returned_groups():
    note = riq.distinct_note(["items"])
    assert "whether one counted value can belong to several of the returned groups" in note
    # Each value in one group: the rows add up. Several per value: they do not.
    partition = next(s for s in _sentences(note) if s.startswith("When each counted value"))
    assert partition.endswith("the rows add up.")
    overlap = next(s for s in _sentences(note) if s.startswith("When one counted value can"))
    assert "do not add up" in overlap and "its own query" in overlap


def test_the_note_gives_its_remedy_only_when_values_overlap_or_it_cannot_tell():
    note = riq.distinct_note(["items"])
    remedies = [s for s in _sentences(note) if "own query" in s or "not computed" in s]
    assert len(remedies) == 2
    for sentence in remedies:
        assert sentence.startswith(("When one counted value can", "When you cannot tell")), (
            sentence)


def test_when_unsure_the_note_never_says_to_add():
    unsure = next(s for s in _sentences(riq.distinct_note(["items"]))
                  if s.startswith("When you cannot tell"))
    assert "do not add the rows" in unsure and "its own query" in unsure
    low = unsure.lower()
    for phrase in ("may add", "can add", "add them up", "rows add up", "sum them"):
        assert phrase not in low, phrase


def test_the_note_examples_are_generic():
    note = riq.distinct_note(["items"])
    assert "(a property it has once, such as one category per item)" in note
    assert "(several codes, reasons or states per item)" in note


@pytest.mark.parametrize("columns, sql, shown", [
    # Several distinct counts, named in select-list order.
    (["kind", "items", "records", "runs"],
     "SELECT kind, COUNT(DISTINCT item_id) AS items, COUNT(*) AS records, "
     "COUNT(DISTINCT run_id) AS runs FROM table_x GROUP BY kind", ["items", "runs"]),
    # A name that is not a plain identifier is not printed.
    (["kind", "item count"],
     'SELECT kind, COUNT(DISTINCT item_id) AS "item count" FROM table_x GROUP BY kind', []),
    # The result's columns do not line up with the select list.
    (["kind", "items", "extra"], _GROUPED, []),
    # Past four columns the note names none of them.
    (["k", "a", "b", "c", "d", "e"],
     "SELECT k, COUNT(DISTINCT a1) AS a, COUNT(DISTINCT b1) AS b, COUNT(DISTINCT c1) AS c, "
     "COUNT(DISTINCT d1) AS d, COUNT(DISTINCT e1) AS e FROM table_x GROUP BY k", []),
])
def test_the_note_names_the_columns_only_when_it_can_print_them(columns, sql, shown):
    rows = [["k-1"] + [1] * (len(columns) - 1), ["k-2"] + [1] * (len(columns) - 1)]
    out = riq.with_distinct_note(_result(rows, columns), sql)
    assert out[riq.DISTINCT_NOTE_KEY] == riq.distinct_note(shown)


@pytest.mark.parametrize("rows", [[], [["k-1", 7]]])
def test_a_single_row_or_an_empty_result_gets_no_note(rows):
    result = _result(rows)
    assert riq.with_distinct_note(result, _GROUPED) is result


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(DISTINCT item_id) AS items, MIN(kind) AS kind FROM table_x",
    "SELECT kind, COUNT(*) AS items FROM table_x GROUP BY kind",
    "WITH g AS (SELECT kind, COUNT(DISTINCT item_id) AS items FROM table_x GROUP BY kind) "
    "SELECT kind, items FROM g",
])
def test_ungrouped_count_star_or_subquery_results_get_no_note(sql):
    result = _result()
    assert riq.with_distinct_note(result, sql) is result


@pytest.mark.parametrize("result", [
    {"error": "boom", "columns": _GROUPED_COLUMNS, "rows": _GROUPED_ROWS},
    {"columns": _GROUPED_COLUMNS},
    {"columns": _GROUPED_COLUMNS, "rows": "not rows"},
    "not a result", None,
])
def test_anything_else_is_returned_unchanged(result):
    assert riq.with_distinct_note(result, _GROUPED) is result


def test_a_cut_result_still_carries_the_note():
    cut = riq.bounded_query_result(_GROUPED_COLUMNS, _GROUPED_ROWS, truncated=True)
    out = riq.with_distinct_note(cut, _GROUPED)
    assert out[riq.DISTINCT_NOTE_KEY] == riq.distinct_note(["items"])
    assert out["partial_note"] == cut["partial_note"]


def test_a_parser_fault_returns_the_result_unchanged(monkeypatch):
    result = _result()
    monkeypatch.setattr(ag, "grouped_distinct_counts", lambda *_a: 1 / 0)
    assert riq.with_distinct_note(result, _GROUPED) is result


#: A number standing alone, as the figure check reads one from text.
_STANDALONE_NUMBER = re.compile(r"(?<![\w.])\d")


@pytest.mark.parametrize("shown", [[], ["items"], ["items", "runs"], ["units_7d", "n2"]])
def test_the_note_carries_no_figure(shown):
    assert not _STANDALONE_NUMBER.search(riq.distinct_note(shown))


def test_the_note_carries_no_figure_from_the_rows():
    rows = [[f"k-{i}", 1000 + i] for i in range(40)]
    note = riq.with_distinct_note(_result(rows), _GROUPED)[riq.DISTINCT_NOTE_KEY]
    assert not _STANDALONE_NUMBER.search(note)


def test_the_note_informs_and_never_withholds():
    low = riq.distinct_note(["items"]).lower()
    assert "whether these rows add up depends on" in low and "stands as returned" in low
    for phrase in ("do not report", "never report", "unverified", "withheld", "refuse",
                   "wrong", "error", "invalid", "must not"):
        assert phrase not in low, phrase


# --------------------------------------------------------------------------- #
# 3. Delivery: the SQL tools put it in the result the model reads
# --------------------------------------------------------------------------- #
class _Cursor:
    def __init__(self, rows, columns):
        self.description = [(c,) for c in columns]
        self._rows = iter([tuple(r) for r in rows])

    def execute(self, *_a, **_k):
        return None

    def fetchone(self):
        return next(self._rows, None)


class _Conn:
    def __init__(self, rows, columns=_GROUPED_COLUMNS):
        self._cursor = _Cursor(rows, columns)

    def set_session(self, **_k):
        return None

    def cursor(self):
        return self._cursor

    def close(self):
        return None


def test_the_postgres_tool_result_carries_the_note(monkeypatch):
    monkeypatch.setattr(sa.pgdriver, "connect", lambda **_kw: _Conn(_GROUPED_ROWS))
    tool = sa._make_direct_query_tool("public", {"host": "h"}, "sys-a")
    payload = json.loads(tool._tool_func(sql=_GROUPED))
    assert payload["rows"] == _GROUPED_ROWS
    assert payload[riq.DISTINCT_NOTE_KEY] == riq.distinct_note(["items"])


def test_the_postgres_tool_result_has_no_note_for_one_row(monkeypatch):
    monkeypatch.setattr(sa.pgdriver, "connect", lambda **_kw: _Conn(_GROUPED_ROWS[:1]))
    tool = sa._make_direct_query_tool("public", {"host": "h"}, "sys-a")
    assert riq.DISTINCT_NOTE_KEY not in json.loads(tool._tool_func(sql=_GROUPED))


def test_mode_b_postgres_carries_the_note_too(monkeypatch):
    monkeypatch.setattr(sa, "_pg_readonly_conn", lambda _p: _Conn(_GROUPED_ROWS))
    monkeypatch.setattr(sa, "_resolve_pg_conn_params", lambda *_a: {"host": "h"})
    out = sa._mode_b_postgres_query(_GROUPED, "public", {"host": "h"}, None)
    assert out[riq.DISTINCT_NOTE_KEY] == riq.distinct_note(["items"])


_CFG = {"workgroup": "wg_a", "database": "db_a", "region": "us-east-1"}


@pytest.fixture
def athena(monkeypatch):
    """The Athena connector, stubbed: the grouped rows for whatever ran, with
    the executed-SQL record the real connector sets."""
    at._PARTITION_COLUMNS_CACHE.clear()
    at._TABLE_FORMAT_CACHE.clear()
    calls: list[str] = []

    def run(sql, _cfg, **_kwargs):
        calls.append(sql)
        rows = [list(r) for r in _GROUPED_ROWS]
        return {"columns": list(_GROUPED_COLUMNS), "rows": rows, "row_count": len(rows),
                "_adt_executed_query": sql}

    monkeypatch.setattr(at, "run_athena_query", run)
    monkeypatch.setattr(at, "run_athena_query_exact", run)
    monkeypatch.setattr(at, "list_partition_columns", lambda *a, **k: [])
    yield calls
    at._PARTITION_COLUMNS_CACHE.clear()
    at._TABLE_FORMAT_CACHE.clear()


def test_the_athena_tool_result_carries_the_note(athena):
    out = json.loads(sa._guarded_athena_call(
        _GROUPED, at._resolve_config(_CFG), {"table_x": []}, "system-a", None))
    assert "error" not in out, out
    assert athena, "the stubbed connector ran"
    assert out["rows"] == [list(r) for r in _GROUPED_ROWS]
    assert out[riq.DISTINCT_NOTE_KEY] == riq.distinct_note(["items"])


def test_the_athena_tool_result_has_no_note_for_count_star(athena):
    out = json.loads(sa._guarded_athena_call(
        "SELECT kind, COUNT(*) AS items FROM table_x GROUP BY kind",
        at._resolve_config(_CFG), {"table_x": []}, "system-a", None))
    assert "error" not in out, out
    assert riq.DISTINCT_NOTE_KEY not in out


def test_mode_b_athena_carries_the_note_too(athena):
    out = sa._mode_b_athena_query(_GROUPED, "db_a", None, dict(_CFG),
                                  "table_x(kind: varchar, item_id: varchar) "
                                  "(no partition keys)")
    assert "error" not in out, out
    assert out[riq.DISTINCT_NOTE_KEY] == riq.distinct_note(["items"])


# --------------------------------------------------------------------------- #
# 4. The guidance sentence in EFFICIENCY step 2
# --------------------------------------------------------------------------- #
def _step_two(text: str) -> str:
    start = text.index("\n2. ")
    return text[start:text.index("\n3. ", start)]


@pytest.mark.parametrize("connection_type", ["PostgreSQL", "Athena"])
def test_the_guidance_sentence_is_in_step_two_once(connection_type):
    text = sa._efficiency_prompt(connection_type)
    assert text.count(_SENTENCE) == 1
    assert _SENTENCE in _step_two(text)
    prompt = sa._compose_system_prompt("sys-a", "System A", "public", "", "",
                                       connection_type)
    assert prompt.count(_SENTENCE) == 1


@pytest.mark.parametrize("connection_type", ["SiteWise (Direct)", "MCP Server", "REST API", ""])
def test_other_types_do_not_get_the_sentence(connection_type):
    assert _SENTENCE not in sa._efficiency_prompt(connection_type)


_PINNED_SENTENCE = (
    " Rows of a COUNT(DISTINCT ...) column add up, or merge into a coarser grouping, only "
    "when one counted value cannot belong to several of their groups (each value has only "
    "one value of the grouping columns, such as one category per item); when it can "
    "(several codes, reasons or states per item), or when you cannot tell, do not add or "
    "merge those rows, also only to summarize: take that figure from its own query, or say "
    "it was not computed.")


@pytest.mark.parametrize("connection_type", ["PostgreSQL", "Athena"])
def test_the_guidance_sentence_is_conditional_and_pinned(connection_type):
    text = sa._efficiency_prompt(connection_type)
    assert _PINNED_SENTENCE in _step_two(text)
    # The first wording made every merge a total with one remedy.
    assert _OLD_SENTENCE not in text
    assert "give the rows as returned" not in text


def test_the_guidance_sentence_and_the_note_ask_the_same_question():
    note = riq.distinct_note(["items"])
    for shared in ("one counted value", "several of", "one category per item",
                   "(several codes, reasons or states per item)", "When you cannot tell",
                   "its own query, or say it was not computed"):
        assert shared.lower() in note.lower(), shared
        assert shared.lower() in _PINNED_SENTENCE.lower(), shared


# --------------------------------------------------------------------------- #
# 5. Classifiers, goldens, neutrality
# --------------------------------------------------------------------------- #
def _texts() -> list[str]:
    return [riq.distinct_note([]), riq.distinct_note(["items"]),
            riq.distinct_note(["items", "runs"]), sa._SQL_TOTALS_CLAUSE]


@pytest.mark.parametrize("index", range(4))
def test_the_new_text_trips_no_outage_error_memory_or_fabrication_signal(index):
    from pathlib import Path
    import sys
    repo = Path(__file__).resolve().parents[3]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from scripts import smoke_prompts as smoke

    text = _texts()[index]
    low = text.lower()
    hits = [m for m in orch._TRANSIENT_TOOL_ERROR_MARKERS + orch._AUTH_TOOL_ERROR_MARKERS
            if m in low]
    hits += [c for c, pattern in chat_client._ERROR_TEXT_CLASSIFIERS if pattern.search(text)]
    hits += smoke.classify_error_labels([text])
    hits += smoke.detect_outage_fallback_signals(text)
    hits += smoke.detect_memory_reference_signals(text)
    hits += smoke.detect_fabrication_signals(text)
    hits += [w for w in ("unavailable", "outage", "error", "failed", "timed out")
             if w in low]
    assert hits == []
    answer = "The query matched 7 rows across 4 groups.\n\n" + text
    assert qi.failure_marker(answer) is None and not qi.is_failed_result(answer)


def test_the_new_text_matches_no_golden_must_not():
    from pathlib import Path
    goldens = json.loads((Path(sa.__file__).parent / "demo" / "golden_evals.json")
                         .read_text(encoding="utf-8"))
    questions = goldens["questions"] if isinstance(goldens, dict) else goldens
    assert questions
    for question in questions:
        expect = question.get("expect") or {}
        for text in _texts()[:3]:
            for pattern in expect.get("answer_must_not_match") or []:
                assert not re.search(pattern, text), (question["id"], pattern)
            assert eval_core.fabrication_flags(
                expect, question.get("text", ""),
                "The query matched 7 rows.\n\n" + text) == [], question["id"]


@pytest.mark.parametrize("index", range(3))
def test_the_note_names_no_dataset_word(index):
    low = _texts()[index].lower()
    for word in ("shipment", "customer", "supplier", "machine", "batch", "lot",
                 "order", "product", "invoice", "unit", "book", "route", "request",
                 "failed", "cancelled"):
        assert not re.search(rf"\b{word}s?\b", low), word
