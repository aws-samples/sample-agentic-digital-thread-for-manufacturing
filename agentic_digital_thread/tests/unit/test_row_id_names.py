# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A SQL result that names a record only by its row id says which column names it.

Test event, 2026-10-15: asked which batches drew one material lot, the ERP
system agent read the lot usage table and returned two UUID run ids, saying
it had no business-key translation for them, and the orchestrator's
record-keys rule allows an id when "the record has no other key". Pinned
here, offline:

1. ``schema_format.declared_join_keys`` reads back the join keys
   ``with_join_keys`` bakes after the table entries, and nothing else.
2. ``row_id_names.row_id_note``: a result column holding only UUID or integer
   row ids read from the FK side of a declared join key gets one fixed
   ``[engine]`` sentence naming the referenced table's business key (a unique
   text column, else the one text column tagged with the record's identifier
   concept) and the join, and keeping the row id as the lookup key; a result
   that already has the business key, a column no declared key links, and an
   ambiguous business key get nothing.
2b. Which table a result column comes from is read from the SELECT list
   (``select_lineage.output_sources``), never from a column name or a table
   named somewhere in the SQL (``SELECT o.item_id FROM other o JOIN usages u
   ...`` reads ``other.item_id``, not ``usages.item_id``). Aliases are carried
   through; ``*``, CTEs, derived tables, set operations, expressions,
   ambiguous references and duplicate output names fail closed.
3. The PostgreSQL tools (Mode A and Mode B) carry it as ``row_id_note``; the
   unique columns are read once from pg_catalog through the views and cached,
   and a failed read is not retried during a short backoff.
4. The PostgreSQL system-agent prompt names records by their business key,
   keeps the row id as the lookup key, and never substitutes another table's
   key for a row id no declared key resolves.

Generic table names only. The real db-init DDL: tests/integration/test_row_id_names_pg.py.
"""
from __future__ import annotations

import json

import pytest

import agents.system_agent as sa
from agents import pg_keys, row_id_names as rn, schema_format

_KEY = ("usages", "item_id", "items", "item_id")
_INT_KEY = ("members", "group_id", "groups", "group_id")
_UUID_A = "6f1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f"
_UUID_B = "0b9a8c7d-6e5f-4a3b-9c2d-1e0f2a3b4c5d"
#: ``other`` carries a same-named ``item_id`` that no declared key links.
_DESC = schema_format.with_join_keys(
    "items(item_id: uuid, item_code: character varying, label: text). "
    "usages(usage_id: uuid, item_id: uuid, other_id: uuid, quantity: numeric). "
    "other(other_id: uuid, item_id: uuid, note: text). "
    "groups(group_id: integer, group_name: text, notes: text). "
    "members(member_id: integer, group_id: integer, joined_on: date)",
    [dict(zip(("from_table", "from_field", "to_table", "to_field"), k))
     for k in (_KEY, _INT_KEY)],
    ["items", "usages", "other", "groups", "members"])
_UNIQUE = {("items", "item_code"): "character varying(50)",
           ("groups", "group_name"): "text"}
_SQL = "SELECT item_id, quantity FROM usages WHERE usage_id IS NOT NULL"
_ONE = "SELECT item_id FROM usages"
_TAIL = "and return it, with the row id labelled as the record's lookup key."
_NOTE = ("[engine] item_id is a row id; items.item_code names that record; join on "
         "usages.item_id = items.item_id " + _TAIL)


def _ctx(concepts=None):
    return rn.context_from(_DESC, concepts)


def _note(columns, rows, sql=_SQL, unique=None, ctx=None):
    return rn.row_id_note(columns, rows, sql, ctx or _ctx(),
                          _UNIQUE if unique is None else unique)


# --------------------------------------------------------------------------- #
# 1. The baked join keys read back
# --------------------------------------------------------------------------- #
def test_declared_join_keys_round_trip_the_baked_text():
    assert schema_format.declared_join_keys(_DESC) == [_KEY, _INT_KEY]


@pytest.mark.parametrize("desc", [
    "", None, "items(item_id: uuid)",
    "items(item_id: uuid). Declared keys: usages.item_id = items.item_id.",
])
def test_no_baked_keys_section_reads_as_no_keys(desc):
    assert schema_format.declared_join_keys(desc) == []


def test_a_malformed_key_entry_is_skipped_and_text_after_the_keys_is_ignored():
    desc = (f"t(a: uuid). {schema_format.JOIN_KEYS_PREFIX}"
            "usages.item_id = items.item_id; broken entry; a.b = c. Later text: x.y = z.w.")
    assert schema_format.declared_join_keys(desc) == [_KEY]


# --------------------------------------------------------------------------- #
# 2. The note
# --------------------------------------------------------------------------- #
def test_uuid_row_ids_linked_by_a_declared_key_name_the_business_key():
    assert _note(["item_id", "quantity"], [[_UUID_A, "4.00"], [_UUID_B, "2.50"]]) == _NOTE


def test_the_note_keeps_the_row_id_as_the_records_lookup_key():
    """The row id is the only key another system can resolve the record by,
    so the note never asks for it to be dropped."""
    assert _NOTE.endswith("with the row id labelled as the record's lookup key.")


def test_integer_row_ids_are_named_too():
    sql = "SELECT m.group_id, count(*) AS n FROM members m GROUP BY m.group_id"
    assert _note(["group_id", "n"], [[3, 10], [7, 2]], sql) == (
        "[engine] group_id is a row id; groups.group_name names that record; join on "
        "members.group_id = groups.group_id " + _TAIL)


def test_a_result_that_already_has_the_business_key_gets_no_note():
    sql = ("SELECT u.item_id, i.item_code, u.quantity FROM usages u "
           "JOIN items i ON i.item_id = u.item_id")
    rows = [[_UUID_A, "CODE-1", "4.00"]]
    assert _note(["item_id", "item_code", "quantity"], rows, sql) == ""


def test_a_query_that_already_reads_the_business_key_under_an_alias_gets_no_note():
    sql = ("SELECT u.item_id, u.quantity, i.item_code AS code FROM usages u "
           "JOIN items i ON i.item_id = u.item_id")
    assert _note(["item_id", "quantity", "code"], [[_UUID_A, "4.00", "CODE-1"]], sql) == ""


def test_no_declared_key_means_no_note():
    desc = "items(item_id: uuid, item_code: text). usages(usage_id: uuid, item_id: uuid)"
    assert rn.context_from(desc) is None
    plain = rn.RowIdContext(join_keys=(), column_types={}, tags={})
    assert rn.row_id_note(["item_id"], [[_UUID_A]], _ONE, plain, _UNIQUE) == ""


def test_the_key_must_belong_to_a_table_the_query_reads():
    assert _note(["item_id"], [[_UUID_A]], "SELECT item_id FROM other_table") == ""


@pytest.mark.parametrize("values", [
    ["CODE-1"], ["12"], [True], [None], [], [_UUID_A, "CODE-1"], [1.5]])
def test_values_that_are_not_row_ids_get_no_note(values):
    assert _note(["item_id"], [[_UUID_A]], _ONE) == _NOTE           # the control
    assert _note(["item_id"], [[v] for v in values], _ONE) == ""


def test_an_integer_key_must_be_named_like_a_row_id():
    desc = schema_format.with_join_keys(
        "codes(code_no: integer, title: text). refs(ref_id: integer, code_no: integer)",
        [{"from_table": "refs", "from_field": "code_no",
          "to_table": "codes", "to_field": "code_no"}], ["codes", "refs"])
    note = rn.row_id_note(["code_no"], [[12]], "SELECT code_no FROM refs",
                          rn.context_from(desc), {("codes", "title"): "text"})
    assert note == ""


def test_without_a_unique_column_the_identifier_tagged_one_is_used():
    concepts = [{"concept": "item-concept", "field": "items.item_id", "role": "identifier"},
                {"concept": "item-concept", "field": "items.label", "role": "identifier"},
                {"concept": "other-concept", "field": "items.item_code", "role": "identifier"}]
    assert _note(["item_id"], [[_UUID_A]], _ONE, unique={}, ctx=_ctx(concepts)) == (
        _NOTE.replace("items.item_code", "items.label"))


def test_any_identifier_tag_counts_when_the_row_id_carries_no_concept():
    concepts = [{"concept": "item-concept", "field": "items.label", "role": "identifier"},
                {"concept": "item-concept", "field": "items.item_code", "role": "measure"}]
    assert _note(["item_id"], [[_UUID_A]], _ONE, unique={}, ctx=_ctx(concepts)) == (
        _NOTE.replace("items.item_code", "items.label"))


def test_two_unique_text_columns_are_settled_by_the_identifier_tag_or_not_at_all():
    unique = {("items", "item_code"): "text", ("items", "label"): "text"}
    assert _note(["item_id"], [[_UUID_A]], _ONE, unique=unique) == ""
    concepts = [{"concept": "item-concept", "field": "items.label", "role": "identifier"}]
    assert _note(["item_id"], [[_UUID_A]], _ONE, unique=unique, ctx=_ctx(concepts)) == (
        _NOTE.replace("items.item_code", "items.label"))


def test_a_unique_column_that_is_not_text_or_is_the_key_itself_is_not_a_business_key():
    for unique in ({("items", "item_id"): "uuid"}, {("items", "item_code"): "integer"},
                   {("usages", "quantity"): "text"}):
        assert _note(["item_id"], [[_UUID_A]], _ONE, unique=unique) == ""


def test_several_row_id_columns_share_one_engine_line():
    desc = schema_format.with_join_keys(
        "items(item_id: uuid, item_code: text). groups(group_id: integer, group_name: text). "
        "usages(item_id: uuid, group_id: integer)",
        [{"from_table": "usages", "from_field": "item_id", "to_table": "items",
          "to_field": "item_id"},
         {"from_table": "usages", "from_field": "group_id", "to_table": "groups",
          "to_field": "group_id"}], ["items", "groups", "usages"])
    note = rn.row_id_note(["item_id", "group_id"], [[_UUID_A, 4]],
                          "SELECT item_id, group_id FROM usages", rn.context_from(desc),
                          _UNIQUE)
    assert note.count("[engine]") == 1
    assert note == ("[engine] item_id is a row id; items.item_code names that record; join on "
                    "usages.item_id = items.item_id " + _TAIL + " group_id is a row id; "
                    "groups.group_name names that record; join on usages.group_id = "
                    "groups.group_id " + _TAIL)


# --------------------------------------------------------------------------- #
# 2b. Which table a result column comes from: the SELECT list, never a name
# --------------------------------------------------------------------------- #
_JOIN = "FROM other o JOIN usages u ON u.other_id = o.other_id"


def test_a_same_named_column_from_the_other_side_of_a_join_is_not_the_key():
    assert _note(["item_id"], [[_UUID_A]], f"SELECT o.item_id {_JOIN}") == ""
    assert _note(["item_id"], [[_UUID_A]], f"SELECT u.item_id {_JOIN}") == _NOTE


def test_an_unqualified_column_two_joined_tables_carry_is_ambiguous():
    assert _note(["item_id"], [[_UUID_A]], f"SELECT item_id {_JOIN}") == ""
    # One the other table lacks resolves to the table that has it.
    assert _note(["quantity", "item_id"], [["4.00", _UUID_A]],
                 f"SELECT quantity, u.item_id {_JOIN}") == _NOTE


def test_an_alias_of_the_real_key_is_named_with_its_source():
    sql = "SELECT u.item_id AS used_item, quantity FROM usages u"
    assert _note(["used_item", "quantity"], [[_UUID_A, "1"]], sql) == (
        "[engine] used_item (usages.item_id) is a row id; items.item_code names that record; "
        "join on usages.item_id = items.item_id " + _TAIL)
    plain = "SELECT u.item_id used_item FROM usages u"
    assert _note(["used_item"], [[_UUID_A]], plain).startswith(
        "[engine] used_item (usages.item_id) is a row id;")


def test_another_column_aliased_to_the_keys_name_is_not_the_key():
    sql = f"SELECT o.other_id AS item_id {_JOIN}"
    assert _note(["item_id"], [[_UUID_A]], sql) == ""


@pytest.mark.parametrize("sql", [
    "SELECT item_id FROM other WHERE note = 'usages'",
    "SELECT item_id FROM other /* usages */",
    "SELECT item_id FROM other -- read usages later\n",
])
def test_a_table_named_only_in_a_literal_or_comment_is_not_read(sql):
    assert _note(["item_id"], [[_UUID_A]], sql) == ""


def test_duplicate_output_names_get_no_note():
    sql = f"SELECT u.item_id, o.item_id {_JOIN}"
    assert _note(["item_id", "item_id"], [[_UUID_A, _UUID_B]], sql) == ""


@pytest.mark.parametrize("sql", [
    "SELECT * FROM usages",
    "SELECT u.* FROM usages u",
    "SELECT u.*, u.item_id AS again FROM usages u",
])
def test_a_star_projection_gets_no_note(sql):
    columns = ["usage_id", "item_id", "other_id", "quantity"]
    assert _note(columns, [[_UUID_B, _UUID_A, _UUID_B, "1"]], sql) == ""


@pytest.mark.parametrize("sql", [
    "WITH x AS (SELECT item_id FROM usages) SELECT item_id FROM x",
    "SELECT item_id FROM (SELECT item_id FROM usages) s",
    "SELECT item_id FROM usages UNION SELECT item_id FROM other",
    "SELECT item_id::text FROM usages",
    "SELECT coalesce(item_id, other_id) AS item_id FROM usages",
    "SELECT u.item_id FROM another_schema.usages u",
    "SELECT item_id",
])
def test_unsupported_shapes_fail_closed(sql):
    assert _note(["item_id"], [[_UUID_A]], sql) == ""


def test_the_projection_must_line_up_with_the_result_columns():
    assert _note(["item_id"], [[_UUID_A]], _SQL) == ""               # two items, one column
    assert _note(["item", "quantity"], [[_UUID_A, "1"]], _SQL) == ""  # a name it did not select


def test_a_relation_qualified_with_the_agents_own_schema_is_read():
    sql = "SELECT u.item_id FROM app_view.usages u"
    assert rn.row_id_note(["item_id"], [[_UUID_A]], sql, _ctx(), _UNIQUE,
                          schema="app_view") == _NOTE


def test_output_sources_reads_the_select_list():
    from agents.select_lineage import output_sources
    sql = ("SELECT DISTINCT u.item_id, o.item_id AS other_item, quantity, count(*) AS n "
           f"{_JOIN} GROUP BY 1, 2, 3")
    columns = {t: set(c) for t, c in _ctx().column_types.items()}
    assert output_sources(sql, columns) == [
        ("item_id", ("usages", "item_id")), ("other_item", ("other", "item_id")),
        ("quantity", ("usages", "quantity")), ("n", None)]
    assert output_sources("SELECT * FROM usages", columns) is None
    assert output_sources("not sql at all (", columns) is None


def test_two_keys_from_one_column_name_follow_the_selected_table():
    desc = schema_format.with_join_keys(
        "items(item_id: uuid, item_code: text). parts(item_id: uuid, part_code: text). "
        "usages(item_id: uuid, move_id: uuid). moves(move_id: uuid, item_id: uuid)",
        [{"from_table": "usages", "from_field": "item_id", "to_table": "items",
          "to_field": "item_id"},
         {"from_table": "moves", "from_field": "item_id", "to_table": "parts",
          "to_field": "item_id"}], ["items", "parts", "usages", "moves"])
    unique = {("items", "item_code"): "text", ("parts", "part_code"): "text"}
    join = "FROM usages u JOIN moves m ON m.move_id = u.move_id"
    for qualifier, target in (("u", "items.item_code"), ("m", "parts.part_code")):
        note = rn.row_id_note(["item_id"], [[_UUID_A]], f"SELECT {qualifier}.item_id {join}",
                              rn.context_from(desc), unique)
        assert f"; {target} names that record;" in note, (qualifier, note)
    assert rn.row_id_note(["item_id"], [[_UUID_A]], f"SELECT item_id {join}",
                          rn.context_from(desc), unique) == ""


# --------------------------------------------------------------------------- #
# 3. The catalog read and the SQL tools
# --------------------------------------------------------------------------- #
def test_unique_columns_from_rows_keeps_proven_view_columns_only():
    rows = [("view", "items", "item_code", "character varying(50)"),
            ("view", "items", "label", "text"),
            ("table", "groups", "group_name", "text"),
            ("view", "", "x", "text"), "junk"]
    lineage = {"items": {"item_id", "item_code"}}
    assert pg_keys.unique_columns_from_rows(rows, lineage) == {
        ("items", "item_code"): "character varying(50)", ("groups", "group_name"): "text"}


class _Cursor:
    """A cursor that answers the catalog queries and returns ``rows`` for the
    user's query, recording every statement."""

    def __init__(self, columns, rows, unique_rows, lineage_rows, fail_catalog=False):
        self.columns, self.rows = columns, rows
        self.unique_rows, self.lineage_rows = unique_rows, lineage_rows
        self.fail_catalog = fail_catalog
        self.statements: list[str] = []
        self._pending: list = []
        self.description = None

    def execute(self, sql, params=None):
        text = str(sql)
        self.statements.append(text)
        if "pg_index" in text:
            if self.fail_catalog:
                raise RuntimeError("catalog read failed")
            self._pending, self.description = list(self.unique_rows), None
        elif "pg_get_viewdef" in text:
            self._pending, self.description = list(self.lineage_rows), None
        elif text.startswith("SELECT set_config("):   # the session timeout; nothing read
            pass
        elif text.lstrip().upper().startswith("SELECT"):
            self._pending = list(self.rows)
            self.description = [(c,) for c in self.columns]

    def fetchone(self):
        return self._pending.pop(0) if self._pending else None

    def fetchall(self):
        out, self._pending = self._pending, []
        return out


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def set_session(self, **_k):
        return None

    def cursor(self):
        return self._cursor

    def close(self):
        return None


_VIEW_UNIQUE = [("view", "items", "item_code", "character varying(50)")]
_LINEAGE = [("items", "public", "items",
             " SELECT items.item_id,\n    items.item_code,\n    items.label\n   FROM public.items;",
             ["item_id", "item_code", "label"])]


@pytest.fixture(autouse=True)
def _fresh_cache():
    rn.clear_cache()
    yield
    rn.clear_cache()


def _cursor(**kw):
    return _Cursor(["item_id", "quantity"], [(_UUID_A, 4), (_UUID_B, 2)],
                   kw.pop("unique_rows", _VIEW_UNIQUE), kw.pop("lineage", _LINEAGE), **kw)


def test_mode_a_tool_result_carries_the_note(monkeypatch):
    cursor = _cursor()
    monkeypatch.setattr(sa.pgdriver, "connect", lambda **_kw: _Conn(cursor))
    tool = sa._make_direct_query_tool("app_view", {"host": "h"}, "sys-a", row_ids=_ctx())
    payload = json.loads(tool._tool_func(sql=_SQL))
    assert payload[rn.ROW_ID_NOTE_KEY] == _NOTE
    assert payload["rows"] == [[_UUID_A, 4], [_UUID_B, 2]]


def test_the_system_tools_build_the_context_from_the_baked_schema(monkeypatch):
    cursor = _cursor()
    monkeypatch.setattr(sa.pgdriver, "connect", lambda **_kw: _Conn(cursor))
    tools, _desc = sa._system_tools("sys-a", "app_view", _DESC, {"host": "h"}, "PostgreSQL",
                                    None, concept_items=[])
    payload = json.loads(tools[0]._tool_func(sql=_SQL))
    assert payload[rn.ROW_ID_NOTE_KEY] == _NOTE


def test_create_system_agent_hands_the_concept_mappings_to_the_tools(monkeypatch):
    seen = {}

    def _tools(*args, **kwargs):
        seen.update(kwargs)
        return [], args[2]

    monkeypatch.setattr(sa, "_system_tools", _tools)
    monkeypatch.setattr(sa, "_system_agent_model", lambda: None)
    monkeypatch.setattr(sa, "_system_agent_hooks", lambda *_a, **_k: [])
    monkeypatch.setattr(sa, "Agent", lambda **kw: kw)
    items = [{"concept": "item-concept", "field": "items.label", "role": "identifier"}]
    sa.create_system_agent("sys-a", "System A", "app_view", "", _DESC, {"host": "h"},
                           "PostgreSQL", {"concept_mappings": items})
    assert seen["concept_items"] == items


def test_mode_b_postgres_carries_the_note(monkeypatch):
    cursor = _cursor()
    monkeypatch.setattr(sa, "_pg_readonly_conn", lambda _p: _Conn(cursor))
    monkeypatch.setattr(sa, "_resolve_pg_conn_params", lambda *_a: {"host": "h"})
    out = sa._mode_b_run_query("PostgreSQL", _SQL, "app_view", {"host": "h"}, None, _DESC)
    assert out[rn.ROW_ID_NOTE_KEY] == _NOTE


def test_the_unique_columns_are_read_once_per_database(monkeypatch):
    cursor = _cursor()
    monkeypatch.setattr(sa.pgdriver, "connect", lambda **_kw: _Conn(cursor))
    tool = sa._make_direct_query_tool("app_view", {"host": "h"}, "sys-a", row_ids=_ctx())
    tool._tool_func(sql=_SQL)
    tool._tool_func(sql=_SQL)
    assert sum("pg_index" in s for s in cursor.statements) == 1


def test_a_result_with_nothing_to_name_reads_no_catalog(monkeypatch):
    cursor = _Cursor(["quantity"], [(4,)], _VIEW_UNIQUE, _LINEAGE)
    monkeypatch.setattr(sa.pgdriver, "connect", lambda **_kw: _Conn(cursor))
    tool = sa._make_direct_query_tool("app_view", {"host": "h"}, "sys-a", row_ids=_ctx())
    payload = json.loads(tool._tool_func(sql="SELECT quantity FROM usages"))
    assert rn.ROW_ID_NOTE_KEY not in payload
    assert not any("pg_index" in s for s in cursor.statements)


def test_a_failed_catalog_read_keeps_the_result_and_falls_back_to_tags(monkeypatch):
    cursor = _cursor(fail_catalog=True)
    monkeypatch.setattr(sa.pgdriver, "connect", lambda **_kw: _Conn(cursor))
    tool = sa._make_direct_query_tool("app_view", {"host": "h"}, "sys-a", row_ids=_ctx())
    payload = json.loads(tool._tool_func(sql=_SQL))
    assert rn.ROW_ID_NOTE_KEY not in payload and payload["row_count"] == 2
    concepts = [{"concept": "c", "field": "items.item_code", "role": "identifier"}]
    tagged = sa._make_direct_query_tool("app_view", {"host": "h"}, "sys-a",
                                        row_ids=_ctx(concepts))
    assert json.loads(tagged._tool_func(sql=_SQL))[rn.ROW_ID_NOTE_KEY] == _NOTE


def _failing_reads(cursor):
    return sum("pg_index" in s for s in cursor.statements)


def test_a_failed_catalog_read_backs_off_and_keeps_the_original_result(monkeypatch, caplog):
    """A persistent catalog failure is retried at most once per backoff per
    database and schema: during it, every qualifying result comes back as it
    was and no catalog statement runs."""
    cursor = _cursor(fail_catalog=True)
    clock = [1000.0]
    monkeypatch.setattr(rn.time, "monotonic", lambda: clock[0])
    original = {"columns": ["item_id", "quantity"], "rows": [[_UUID_A, 4]], "row_count": 1}
    with caplog.at_level("WARNING", logger="agents.row_id_names"):
        first = rn.with_row_id_note(cursor, "app_view", _SQL, dict(original), _ctx(),
                                    cache_key=("h",), restore_timeout_ms=60_000)
        second = rn.with_row_id_note(cursor, "app_view", _SQL, dict(original), _ctx(),
                                     cache_key=("h",), restore_timeout_ms=60_000)
    assert first == original and second == original
    assert _failing_reads(cursor) == 1
    assert any("backing off" in r.getMessage() for r in caplog.records)
    # Another schema or database is not held back by this one's failure.
    rn.with_row_id_note(cursor, "app_view", _SQL, dict(original), _ctx(), cache_key=("h2",))
    assert _failing_reads(cursor) == 2
    # After the backoff, the read is tried again.
    clock[0] += rn.FAILURE_BACKOFF_SECONDS + 1
    rn.with_row_id_note(cursor, "app_view", _SQL, dict(original), _ctx(), cache_key=("h",))
    assert _failing_reads(cursor) == 3
    assert rn.FAILURE_BACKOFF_SECONDS < rn.CACHE_SECONDS


def test_a_failure_that_finishes_after_an_overlapping_success_keeps_the_success(monkeypatch):
    """Two reads for one database and schema overlap: the second starts while
    the first is in flight and succeeds, then the first fails. The successful
    entry stays cached, and the failing caller gets it as well."""
    clock = [5000.0]
    monkeypatch.setattr(rn.time, "monotonic", lambda: clock[0])
    found = {("items", "item_code"): "text"}
    reads = []

    def read(_cursor, _schema):
        reads.append(1)
        if len(reads) == 1:
            assert rn.unique_columns(object(), "app_view", ("h",)) == found
            raise RuntimeError("catalog read failed")
        return dict(found)

    monkeypatch.setattr(rn, "_read_unique", read)
    assert rn.unique_columns(object(), "app_view", ("h",)) == found
    assert rn.unique_columns(object(), "app_view", ("h",)) == found
    assert len(reads) == 2


def test_the_tool_makes_one_catalog_attempt_during_the_backoff(monkeypatch):
    cursor = _cursor(fail_catalog=True)
    monkeypatch.setattr(sa.pgdriver, "connect", lambda **_kw: _Conn(cursor))
    tool = sa._make_direct_query_tool("app_view", {"host": "h"}, "sys-a", row_ids=_ctx())
    payloads = [json.loads(tool._tool_func(sql=_SQL)) for _ in range(2)]
    assert _failing_reads(cursor) == 1
    for payload in payloads:
        assert rn.ROW_ID_NOTE_KEY not in payload
        assert payload["rows"] == [[_UUID_A, 4], [_UUID_B, 2]]


def test_a_view_column_the_lineage_does_not_prove_is_not_unique(monkeypatch):
    """The view renames its ``label`` from another base column, so the base
    table's unique ``label`` says nothing about the view's."""
    lineage = [("items", "public", "items",
                " SELECT items.item_id,\n    items.item_code,\n    items.notes AS label\n"
                "   FROM public.items;", ["item_id", "item_code", "label"])]
    cursor = _cursor(unique_rows=[("view", "items", "label", "text")], lineage=lineage)
    monkeypatch.setattr(sa.pgdriver, "connect", lambda **_kw: _Conn(cursor))
    tool = sa._make_direct_query_tool("app_view", {"host": "h"}, "sys-a", row_ids=_ctx())
    assert rn.ROW_ID_NOTE_KEY not in json.loads(tool._tool_func(sql=_SQL))


def test_a_tool_without_a_context_is_unchanged(monkeypatch):
    cursor = _cursor()
    monkeypatch.setattr(sa.pgdriver, "connect", lambda **_kw: _Conn(cursor))
    payload = json.loads(sa._make_direct_query_tool("app_view", {"host": "h"}, "sys-a")
                         ._tool_func(sql=_SQL))
    assert rn.ROW_ID_NOTE_KEY not in payload
    assert not any("pg_index" in s for s in cursor.statements)


def test_the_catalog_read_runs_under_its_own_short_timeout_and_restores_it(monkeypatch):
    cursor = _cursor()
    monkeypatch.setattr(sa.pgdriver, "connect", lambda **_kw: _Conn(cursor))
    sa._make_direct_query_tool("app_view", {"host": "h"}, "sys-a",
                               row_ids=_ctx())._tool_func(sql=_SQL)
    index = next(i for i, s in enumerate(cursor.statements) if "pg_index" in s)
    # A utility SET cannot bind a server-side parameter: set_config instead.
    timeout_sql = "SELECT set_config('statement_timeout', %s, false)"
    assert cursor.statements[index - 1] == timeout_sql
    assert cursor.statements[-1] == timeout_sql


# --------------------------------------------------------------------------- #
# 4. The prompt
# --------------------------------------------------------------------------- #
_RULE = "RECORD NAMES (required)"


def test_the_postgres_prompt_names_records_by_their_business_key():
    rule = sa._record_names_prompt("PostgreSQL")
    assert _RULE in rule
    assert "business key" in rule and "row id" in rule
    assert "declared join key" in rule and '"row_id_note"' in rule
    tail = sa._policy_tail("PostgreSQL")
    assert tail.count(_RULE) == 1
    prompt = sa._compose_system_prompt("sys-a", "System A", "app_view",
                                       "Custom prompt.", _DESC, "PostgreSQL")
    assert prompt.count(_RULE) == 1 and prompt.index(_RULE) > len("Custom prompt.")


def test_with_no_declared_key_the_rule_keeps_the_row_id_and_never_substitutes():
    """A row id no declared key in this schema resolves (its record lives in
    another system) stays the answer's key, labelled as such. Another table
    linked through the same parent record is not proof of the same record:
    on the ERP views one lot's two uses each pair with both of the lot's MES
    work orders (tests/integration/test_row_id_names_pg.py)."""
    rule = sa._record_names_prompt("PostgreSQL")
    assert "keep the row id in your answer, labelled as the record's lookup key" in rule
    assert "When no declared join key reaches a table that names it" in rule
    assert "labelled as the id of a record named in another system" in rule
    assert "never substitute another table's key for it" in rule
    for removed in ("same parent", "another table that links the same records",
                    "and return that key; if none does"):
        assert removed not in rule, removed


@pytest.mark.parametrize("connection_type",
                         ["Athena", "MCP Server", "SiteWise (Direct)", "REST API", ""])
def test_other_connection_types_do_not_get_the_rule(connection_type):
    assert sa._record_names_prompt(connection_type) == ""
    assert _RULE not in sa._policy_tail(connection_type)
