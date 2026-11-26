# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A table's declared RECORD KEY, so a system agent stops de-duplicating records.

Measured on an analytics deployment, a live dry run ("how many
segments did C1 complete yesterday?"): the SQL system agent first ran the
right COUNT(*) of COMPLETED rows on the working day, then ran a second query
that kept one row per ``id`` with ``ROW_NUMBER() OVER (PARTITION BY id ...)``
filtered to ``rn = 1`` and answered ``COUNT(DISTINCT id)``, calling the row
count "revisions, not distinct segments". That is wrong: the source owner gave
the record key as ``id, segment_type, entity_type, revision, cell,
COALESCE(manufacturing_attempt_id, ''), COALESCE(lifecycle_id, '')`` -- each
revision row is its own record, and ``id`` is a strict subset of the key.

The feature:

* a steward declares a table's record key, stored on its ``SchemaEntry`` with
  provenance (``record_key.set_record_key``); validated against the table's
  columns, no duplicates, at most 12;
* the system agent reads it as a schema fact (``record_key.schema_lines``), a
  line baked into the prompt it sees, through the printable/withheld path;
* an engine check refuses, once, a single-table query that keeps one row per a
  STRICT SUBSET of the declared key (``record_key.decide``); the identical
  resend runs and is disclosed (``engine_line`` / ``requirement_block``);
* a PostgreSQL measured primary key wins over a declared one.

Neutral names. Fully offline.
"""
from __future__ import annotations

import importlib

import pytest


def _rk():
    return importlib.import_module("agents.record_key")


# --------------------------------------------------------------------------- #
# A fake graph: SchemaEntry + Field, and the steward record-key properties.
# --------------------------------------------------------------------------- #
class _Graph:
    """Enough of the ``(System)-[:HAS_SCHEMA]->(SchemaEntry)-[:HAS_FIELD]->
    (Field)`` shape that ``schema_fields`` and ``record_key`` read and write,
    plus the ``se.record_key*`` provenance props the steward sets. ``write``
    and ``query`` are the injected seams."""

    def __init__(self, columns):
        # columns: {table: [col, ...]}
        self.fields = {t: list(cols) for t, cols in columns.items()}
        self.rk: dict[str, dict] = {}   # seid -> record-key props

    def _seid(self, table):
        return f"sys.{table}"

    def write(self, cypher, params):
        seid = params.get("seid", "")
        table = seid.split(".", 1)[-1]
        if "SET se.record_key" in cypher:
            if params.get("record_key"):
                self.rk[seid] = {"record_key": params["record_key"],
                                 "record_key_by": params.get("by"),
                                 "record_key_at": params.get("at"),
                                 "record_key_source": params.get("source")}
            else:
                self.rk.pop(seid, None)
        return True

    def query(self, cypher, params):
        seid = params.get("seid", "")
        table = seid.split(".", 1)[-1]
        if "f.name AS name" in cypher and "HAS_FIELD" in cypher:
            return [{"name": c} for c in self.fields.get(table, [])]
        if "count(se)" in cypher:
            return [{"n": 1 if table in self.fields else 0}]
        if "se.record_key" in cypher:
            props = self.rk.get(seid)
            if props is None:
                return [{"record_key": None, "record_key_by": None,
                         "record_key_at": None, "record_key_source": None}]
            return [props]
        return []


_COLUMNS = ["id", "segment_type", "entity_type", "revision", "cell",
            "manufacturing_attempt_id", "lifecycle_id", "state", "complete_date"]
#: The declared key for the measured table, as the owner gave it.
_KEY = [{"column": "id", "coalesced": False},
        {"column": "segment_type", "coalesced": False},
        {"column": "entity_type", "coalesced": False},
        {"column": "revision", "coalesced": False},
        {"column": "cell", "coalesced": False},
        {"column": "manufacturing_attempt_id", "coalesced": True},
        {"column": "lifecycle_id", "coalesced": True}]


@pytest.fixture
def graph():
    g = _Graph({"records": _COLUMNS})
    _rk().reset()
    yield g
    _rk().reset()


# --------------------------------------------------------------------------- #
# Setting the key (valid, unknown column, duplicate, too long, auth).
# --------------------------------------------------------------------------- #
def test_setting_a_valid_key_stores_it_with_provenance(graph):
    rk = _rk()
    out = rk.set_record_key("sys", "records", _KEY, actor="alice",
                            source="owner statement 2026-10-16",
                            write=graph.write, query=graph.query)
    assert out["status"] == "ok"
    stored = rk.read_record_key("sys", "records", query=graph.query)
    assert [c["column"] for c in stored["declared_key"]] == [
        "id", "segment_type", "entity_type", "revision", "cell",
        "manufacturing_attempt_id", "lifecycle_id"]
    assert stored["declared_key"][5]["coalesced"] is True
    assert graph.rk["sys.records"]["record_key_by"] == "alice"
    assert graph.rk["sys.records"]["record_key_at"]


def test_a_bare_string_column_means_not_coalesced(graph):
    rk = _rk()
    rk.set_record_key("sys", "records", ["id", "revision"], actor="a",
                      write=graph.write, query=graph.query)
    stored = rk.read_record_key("sys", "records", query=graph.query)
    assert stored["declared_key"] == [{"column": "id", "coalesced": False},
                                      {"column": "revision", "coalesced": False}]


def test_an_unknown_column_is_refused_and_nothing_is_written(graph):
    rk = _rk()
    out = rk.set_record_key("sys", "records", ["id", "not_a_column"], actor="a",
                            write=graph.write, query=graph.query)
    assert out["status"] == "validation_failed" and out["reason"] == "unknown_column"
    assert "sys.records" not in graph.rk


def test_a_duplicate_column_is_refused(graph):
    rk = _rk()
    out = rk.set_record_key("sys", "records", ["id", "id"], actor="a",
                            write=graph.write, query=graph.query)
    assert out["status"] == "validation_failed" and out["reason"] == "duplicate_column"
    assert "sys.records" not in graph.rk


def test_an_empty_key_is_refused_when_setting(graph):
    rk = _rk()
    out = rk.set_record_key("sys", "records", [], actor="a",
                            write=graph.write, query=graph.query)
    assert out["status"] == "validation_failed" and out["reason"] == "empty_key"


def test_a_key_over_twelve_columns_is_refused(graph):
    rk = _rk()
    big = _Graph({"wide": [f"c{i}" for i in range(20)]})
    out = rk.set_record_key("sys", "wide", [f"c{i}" for i in range(13)], actor="a",
                            write=big.write, query=big.query)
    assert out["status"] == "validation_failed" and out["reason"] == "too_many_columns"
    assert "sys.wide" not in big.rk


def test_setting_a_key_on_an_unknown_table_is_refused(graph):
    rk = _rk()
    out = rk.set_record_key("sys", "ghost", ["id"], actor="a",
                            write=graph.write, query=graph.query)
    assert out["status"] == "validation_failed" and out["reason"] == "unknown_table"


def test_clearing_the_key_removes_it(graph):
    rk = _rk()
    rk.set_record_key("sys", "records", _KEY, actor="a",
                      write=graph.write, query=graph.query)
    out = rk.clear_record_key("sys", "records", actor="a",
                              write=graph.write, query=graph.query)
    assert out["status"] == "ok"
    assert rk.read_record_key("sys", "records", query=graph.query)["declared_key"] == []


def test_the_admin_route_requires_admin():
    """The PUT route is gated by the same ``_check_admin`` the other admin
    routes use (a non-admin request is 403)."""
    from fastapi import HTTPException
    from agents import admin_routes

    class _Req:
        class state:
            is_admin = False
    with pytest.raises(HTTPException) as info:
        admin_routes._check_admin(_Req())
    assert info.value.status_code == 403


# --------------------------------------------------------------------------- #
# Declaring a key on a catalog-keyed backend is refused at the route.
# --------------------------------------------------------------------------- #
def test_declaring_a_key_on_a_postgres_system_is_refused_at_the_route(monkeypatch):
    """A PostgreSQL system's keys come from its catalog, so the route refuses a
    declared key with 400 reason catalog_keyed_backend before any graph read."""
    from fastapi.responses import JSONResponse
    from agents import admin_routes

    monkeypatch.setattr(admin_routes, "_record_key_graph_seams",
                        lambda: (_fail_seam, _fail_seam))
    out = admin_routes.set_record_key_sync(
        "pg-sys", {"table": "t", "record_key": ["id"]}, "alice",
        connection_type="PostgreSQL")
    assert isinstance(out, JSONResponse)
    import json as _json
    body = _json.loads(bytes(out.body))
    assert out.status_code == 400 and body["reason"] == "catalog_keyed_backend"


def test_an_athena_system_may_declare_a_key_at_the_route(graph):
    from agents import admin_routes
    out = admin_routes.set_record_key_sync(
        "sys", {"table": "records", "record_key": ["id", "revision"]}, "alice",
        connection_type="Athena", seams=(graph.write, graph.query))
    assert out["status"] == "ok"


def _fail_seam(*_a, **_k):
    raise AssertionError("the graph must not be touched")


# --------------------------------------------------------------------------- #
# The engine check: decide() over the dedupe-by-id shape and its neighbours.
# --------------------------------------------------------------------------- #
_EFFECTIVE = [c["column"] for c in _KEY]

# The dry-run miss: partition by one key column (id), rn = 1, outer count.
_DEDUPE_BY_ID = ("WITH latest AS (SELECT id, revision, state, complete_date, "
       "ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision DESC) AS rn "
       "FROM records WHERE cell = 'C1' AND complete_date >= TIMESTAMP '2026-10-17 05:30:00 UTC' "
       "AND complete_date < TIMESTAMP '2026-10-18 05:30:00 UTC') "
       "SELECT COUNT(DISTINCT id) AS n FROM latest WHERE rn = 1 AND state = 'COMPLETED'")

# Partition by a two-column strict subset of the key.
_SUBSET_TWO = ("WITH latest AS (SELECT id, cell, "
               "ROW_NUMBER() OVER (PARTITION BY id, cell ORDER BY revision DESC) AS rn "
               "FROM records) SELECT COUNT(*) AS n FROM latest WHERE rn = 1")

# Partition by a COALESCE of a key column -- read as that key column.
_SUBSET_COALESCE = ("WITH latest AS (SELECT id, "
                    "ROW_NUMBER() OVER (PARTITION BY COALESCE(id, '') ORDER BY revision DESC) "
                    "AS rn FROM records) SELECT COUNT(*) AS n FROM latest WHERE rn = 1")

# Partition by the FULL key -- not a subset, not refused.
_FULL = ("WITH latest AS (SELECT id, "
         "ROW_NUMBER() OVER (PARTITION BY id, segment_type, entity_type, revision, cell, "
         "manufacturing_attempt_id, lifecycle_id ORDER BY complete_date DESC) AS rn "
         "FROM records) SELECT COUNT(*) AS n FROM latest WHERE rn = 1")

# A plain COUNT(DISTINCT id) alone -- a legitimate distinct-value count.
_COUNT_DISTINCT = "SELECT COUNT(DISTINCT id) AS n FROM records WHERE cell = 'C1'"

# A PARTITION BY a subset whose row number is NOT filtered to = 1 (it feeds an
# aggregate): not a dedupe, so not refused (a near-miss shape seen in a dry run).
_RN_NOT_FILTERED = ("WITH d AS (SELECT id, ROW_NUMBER() OVER (PARTITION BY id) AS rn "
                    "FROM records) SELECT APPROX_PERCENTILE(rn, 0.5) AS m FROM d")

# SELECT DISTINCT a subset feeding an outer COUNT: the same question as
# COUNT(DISTINCT id), which is deliberately not refused, so neither is this.
_DISTINCT_SUBSET = ("SELECT COUNT(*) AS n FROM "
                    "(SELECT DISTINCT id FROM records WHERE cell = 'C1') t")

# GROUP BY a subset feeding an outer COUNT: also the COUNT(DISTINCT id)
# question, not refused.
_GROUP_SUBSET = ("SELECT COUNT(*) AS n FROM "
                 "(SELECT id FROM records GROUP BY id) t")

# RANK / DENSE_RANK filtered to = 1 keep ties, so more than one row can remain:
# not read.
_RANK = ("WITH latest AS (SELECT id, RANK() OVER (PARTITION BY id ORDER BY revision DESC) "
         "AS rn FROM records) SELECT COUNT(*) AS n FROM latest WHERE rn = 1")
_DENSE_RANK = ("WITH latest AS (SELECT id, "
               "DENSE_RANK() OVER (PARTITION BY id ORDER BY revision DESC) AS rn "
               "FROM records) SELECT COUNT(*) AS n FROM latest WHERE rn = 1")


def test_the_dedupe_by_id_shape_is_decided_as_a_subset_dedupe():
    rk = _rk()
    found = rk.decide(_DEDUPE_BY_ID, "records", _EFFECTIVE)
    assert found is not None
    assert found.table == "records"
    assert list(found.subset) == ["id"]
    assert found.refuse is True


def test_a_two_column_subset_partition_is_decided():
    rk = _rk()
    found = rk.decide(_SUBSET_TWO, "records", _EFFECTIVE)
    assert found is not None and set(found.subset) == {"id", "cell"}


def test_a_coalesce_of_a_key_column_is_read_as_that_column():
    rk = _rk()
    found = rk.decide(_SUBSET_COALESCE, "records", _EFFECTIVE)
    assert found is not None and list(found.subset) == ["id"]


@pytest.mark.parametrize("sql", [
    _FULL, _COUNT_DISTINCT, _RN_NOT_FILTERED, _DISTINCT_SUBSET, _GROUP_SUBSET,
    _RANK, _DENSE_RANK,
])
def test_what_is_not_a_subset_dedupe_is_not_decided(sql):
    rk = _rk()
    assert rk.decide(sql, "records", _EFFECTIVE) is None


def test_no_declared_key_means_no_check():
    rk = _rk()
    assert rk.decide(_DEDUPE_BY_ID, "records", []) is None


# --------------------------------------------------------------------------- #
# Qualified, case-varied and schema-qualified SQL is still read.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sql,table", [
    # an alias qualifier that binds to the sole relation
    ("WITH latest AS (SELECT r.id, "
     "ROW_NUMBER() OVER (PARTITION BY r.id ORDER BY r.revision DESC) AS rn "
     "FROM records r) SELECT COUNT(*) AS n FROM latest WHERE rn = 1", "records"),
    # a database-qualified table, matched by its last name part (the terminal
    # name _record_key_rule resolves and passes in)
    ("WITH latest AS (SELECT id, "
     "ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision DESC) AS rn "
     "FROM lake.records) SELECT COUNT(*) AS n FROM latest WHERE rn = 1", "records"),
    # a quoted qualified name
    ('WITH latest AS (SELECT "id", '
     'ROW_NUMBER() OVER (PARTITION BY "id" ORDER BY revision DESC) AS rn '
     'FROM "lake"."records") SELECT COUNT(*) AS n FROM latest WHERE rn = 1', "records"),
    # a mixed-case unquoted table (case-folded)
    ("WITH latest AS (SELECT id, "
     "ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision DESC) AS rn "
     "FROM Records) SELECT COUNT(*) AS n FROM latest WHERE rn = 1", "records"),
])
def test_qualified_and_case_varied_dedupe_is_decided(sql, table):
    rk = _rk()
    found = rk.decide(sql, table, _EFFECTIVE)
    assert found is not None and list(found.subset) == ["id"]


def test_a_qualifier_that_binds_to_nothing_is_not_read():
    """A column qualifier that is neither the sole relation's alias nor its
    table name does not resolve, so the shape is not read (no refusal)."""
    rk = _rk()
    sql = ("WITH latest AS (SELECT x.id, "
           "ROW_NUMBER() OVER (PARTITION BY x.id ORDER BY revision DESC) AS rn "
           "FROM records r) SELECT COUNT(*) AS n FROM latest WHERE rn = 1")
    assert rk.decide(sql, "records", _EFFECTIVE) is None


# --------------------------------------------------------------------------- #
# The = 1 filter is bound to the window's own consuming scope.
# --------------------------------------------------------------------------- #
def test_an_unrelated_alias_filtered_to_one_is_not_a_dedupe():
    """A separate relation's rn = 1 (a cross join) does not activate the rule:
    the window alias a.rn is never filtered."""
    rk = _rk()
    sql = ("WITH a AS (SELECT id, ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision) AS rn "
           "FROM records), b AS (SELECT 1 AS rn) "
           "SELECT COUNT(*) AS n FROM a CROSS JOIN b WHERE b.rn = 1")
    assert rk.decide(sql, "records", _EFFECTIVE) is None


# --------------------------------------------------------------------------- #
# COALESCE(<key column>, '') only -- an empty-string second argument, two args.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("coalesce", [
    "COALESCE(id, 'missing')",     # a non-empty sentinel
    "COALESCE(id, 0)",             # a numeric literal
    "COALESCE(id, '', 'x')",       # three arguments
])
def test_a_coalesce_that_is_not_the_empty_two_arg_form_is_not_read(coalesce):
    rk = _rk()
    sql = (f"WITH latest AS (SELECT id, ROW_NUMBER() OVER (PARTITION BY {coalesce} "
           "ORDER BY revision DESC) AS rn FROM records) "
           "SELECT COUNT(*) AS n FROM latest WHERE rn = 1")
    assert rk.decide(sql, "records", _EFFECTIVE) is None


# --------------------------------------------------------------------------- #
# The = 1 equality counts only in a row-filtering clause (WHERE / QUALIFY).
# --------------------------------------------------------------------------- #
def test_rn_equals_one_in_order_by_is_not_a_filter():
    """ORDER BY rn = 1 orders every row and collapses none, so it is not a
    de-duplication."""
    rk = _rk()
    sql = ("WITH x AS (SELECT id, ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision) AS rn "
           "FROM records) SELECT id, rn FROM x ORDER BY rn = 1")
    assert rk.decide(sql, "records", _EFFECTIVE) is None


def test_rn_equals_one_in_the_select_list_is_not_a_filter():
    rk = _rk()
    sql = ("WITH x AS (SELECT id, ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision) AS rn "
           "FROM records) SELECT id, rn = 1 AS is_latest FROM x")
    assert rk.decide(sql, "records", _EFFECTIVE) is None


def test_rn_equals_one_in_where_is_a_filter():
    rk = _rk()
    sql = ("WITH x AS (SELECT id, ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision) AS rn "
           "FROM records) SELECT COUNT(*) AS n FROM x WHERE rn = 1")
    assert rk.decide(sql, "records", _EFFECTIVE) is not None


# --------------------------------------------------------------------------- #
# Only the statement's own WITH chain and its one top-level query are read: a
# nested WITH, a set operation or a repeated CTE name is not the grammar.
# --------------------------------------------------------------------------- #
_RANKED = ("SELECT id, ROW_NUMBER() OVER (PARTITION BY id ORDER BY revision) AS rn "
           "FROM records")


@pytest.mark.parametrize("sql", [
    # An unused nested CTE shadows the outer name: the result never reads it.
    (f"WITH x AS (SELECT 1 AS n), y AS (WITH x AS ({_RANKED}) "
     "SELECT * FROM x WHERE rn = 1) SELECT * FROM x"),
    # A used nested WITH: the de-duplication sits inside another CTE.
    (f"WITH y AS (WITH x AS ({_RANKED}) SELECT * FROM x WHERE rn = 1) "
     "SELECT COUNT(*) AS n FROM y"),
    # One branch of a set operation keeps every row.
    f"WITH x AS ({_RANKED}) SELECT * FROM x UNION ALL SELECT * FROM x WHERE rn = 1",
    f"WITH x AS ({_RANKED}) SELECT * FROM x WHERE rn = 1 UNION ALL SELECT * FROM x",
    f"WITH x AS ({_RANKED}) SELECT id FROM x WHERE rn = 1 INTERSECT SELECT id FROM x",
    f"WITH x AS ({_RANKED}) SELECT id FROM x WHERE rn = 1 EXCEPT SELECT id FROM x",
    # The same CTE name twice in one WITH chain is ambiguous.
    f"WITH x AS ({_RANKED}), x AS ({_RANKED}) SELECT COUNT(*) AS n FROM x WHERE rn = 1",
    f'WITH x AS ({_RANKED}), X AS ({_RANKED}) SELECT COUNT(*) AS n FROM x WHERE rn = 1',
    # A recursive WITH is not read.
    f"WITH RECURSIVE x AS ({_RANKED}) SELECT COUNT(*) AS n FROM x WHERE rn = 1",
])
def test_only_the_top_level_with_chain_and_its_one_query_are_read(sql):
    assert _rk().decide(sql, "records", _EFFECTIVE) is None


def test_a_second_unrelated_cte_beside_the_dedupe_is_still_read():
    sql = (f"WITH x AS ({_RANKED}), y AS (SELECT 1 AS n) "
           "SELECT COUNT(*) AS n FROM x WHERE rn = 1")
    assert _rk().decide(sql, "records", _EFFECTIVE) is not None


@pytest.mark.parametrize("sql", [
    # A statement that is not a query starting at WITH: nothing runs that the
    # disclosure could describe.
    f"EXPLAIN WITH x AS ({_RANKED}) SELECT COUNT(*) AS n FROM x WHERE rn = 1",
    f"EXPLAIN ANALYZE WITH x AS ({_RANKED}) SELECT COUNT(*) AS n FROM x WHERE rn = 1",
    # A second statement after the query.
    f"WITH x AS ({_RANKED}) SELECT COUNT(*) AS n FROM x WHERE rn = 1; DROP TABLE x",
    # Parenthesised spellings of the whole statement or of its outer query are
    # outside the one grammar this check reads: not read, today's behaviour.
    f"(WITH x AS ({_RANKED}) SELECT COUNT(*) AS n FROM x WHERE rn = 1)",
    f"WITH x AS ({_RANKED}) (SELECT COUNT(*) AS n FROM x WHERE rn = 1)",
    # A WITH chain in front of a statement that is not a query.
    f"WITH x AS ({_RANKED}) INSERT INTO target SELECT * FROM x WHERE rn = 1",
    f"WITH x AS ({_RANKED}) CREATE TABLE target AS SELECT * FROM x WHERE rn = 1",
    f"WITH x AS ({_RANKED}) DELETE FROM target WHERE id IN (SELECT id FROM x WHERE rn = 1)",
])
def test_a_statement_that_is_not_one_with_query_is_not_read(sql):
    assert _rk().decide(sql, "records", _EFFECTIVE) is None


def test_one_trailing_semicolon_is_still_read():
    sql = f"WITH x AS ({_RANKED}) SELECT COUNT(*) AS n FROM x WHERE rn = 1;"
    assert _rk().decide(sql, "records", _EFFECTIVE) is not None


# --------------------------------------------------------------------------- #
# The schema line appears for a table with a key, shows COALESCE, not without.
# --------------------------------------------------------------------------- #
def test_the_schema_line_appears_for_a_table_with_a_key():
    rk = _rk()
    line = rk.schema_lines({"records": _KEY})
    assert "records" in line
    assert "record key" in line.lower()
    assert "id" in line


def test_the_schema_line_shows_a_coalesced_column_as_coalesce():
    rk = _rk()
    line = rk.schema_lines({"records": _KEY})
    assert "COALESCE(manufacturing_attempt_id, '')" in line
    assert "COALESCE(lifecycle_id, '')" in line
    # a non-coalesced column is bare
    assert ", id," in line or "key id," in line


def test_no_schema_line_without_a_key():
    rk = _rk()
    assert rk.schema_lines({}) == ""


def test_a_non_printable_table_name_is_withheld_from_the_schema_line():
    rk = _rk()
    line = rk.schema_lines({"bad name!": [{"column": "id", "coalesced": False}]})
    assert "bad name!" not in line
    assert "withheld" in line.lower()


# --------------------------------------------------------------------------- #
# The refusal and the disclosure read as facts.
# --------------------------------------------------------------------------- #
def test_the_refusal_states_the_key_and_that_rows_are_separate_records():
    rk = _rk()
    found = rk.decide(_DEDUPE_BY_ID, "records", _EFFECTIVE)
    payload = rk.RecordKeyError(found).to_dict()
    assert payload["error_name"] == "RECORD_KEY_DEDUPE"
    assert payload["error_type"] == "invalid_request"
    assert payload["retryable"] is True
    text = payload["guidance"].lower()
    assert "record key" in text
    assert "separate records" in text or "its own record" in text
    assert "\u2014" not in payload["guidance"] and "\u2013" not in payload["guidance"]


def test_the_engine_line_and_requirement_block_disclose_the_dedupe():
    rk = _rk()
    found = rk.decide(_DEDUPE_BY_ID, "records", _EFFECTIVE)
    line = rk.engine_line([found])
    block = rk.requirement_block([found])
    assert line and "record key" in line.lower()
    assert block and "record" in block.lower()
    assert rk.engine_line([]) == "" and rk.requirement_block([]) == ""


# --------------------------------------------------------------------------- #
# The registry keeps the coalesced flag; the check reads bare column names.
# --------------------------------------------------------------------------- #
def test_the_registry_keeps_terms_and_columns_for_returns_names():
    rk = _rk()
    rk.remember("sys", {"records": _KEY})
    assert rk.columns_for("sys", "records") == [c["column"] for c in _KEY]
    assert rk.keys_for("sys")["records"][5] == {"column": "manufacturing_attempt_id",
                                                "coalesced": True}
    rk.reset()


# --------------------------------------------------------------------------- #
# The PUT body ingress cap and field types (route-level).
# --------------------------------------------------------------------------- #
class _Body:
    """A minimal ASGI-ish request carrying headers and a streamed body."""

    def __init__(self, raw: bytes, content_length=None, admin=True):
        self._raw = raw
        cl = str(len(raw)) if content_length is None else str(content_length)
        self.headers = {} if content_length == "absent" else {"content-length": cl}

        class _St:
            is_admin = admin
            user = None
        self.state = _St()

    async def stream(self):
        for i in range(0, len(self._raw), 4096):
            yield self._raw[i:i + 4096]


def _read_capped(body):
    import asyncio as _asyncio
    from agents import admin_routes

    async def _drive():
        return await admin_routes._read_capped_record_key(body)
    return _asyncio.run(_drive())


def test_an_oversized_content_length_is_refused_before_reading():
    from fastapi import HTTPException
    from agents import admin_routes
    big = admin_routes.MAX_RECORD_KEY_BYTES + 1
    body = _Body(b"{}", content_length=big)
    with pytest.raises(HTTPException) as info:
        _read_capped(body)
    assert info.value.status_code == 413


def test_a_lying_small_content_length_still_hits_the_stream_ceiling():
    from fastapi import HTTPException
    from agents import admin_routes
    raw = b"x" * (admin_routes.MAX_RECORD_KEY_BYTES + 100)
    body = _Body(raw, content_length=2)      # lies small
    with pytest.raises(HTTPException) as info:
        _read_capped(body)
    assert info.value.status_code == 413


def test_an_absent_content_length_still_hits_the_stream_ceiling():
    from fastapi import HTTPException
    from agents import admin_routes
    raw = b"x" * (admin_routes.MAX_RECORD_KEY_BYTES + 100)
    body = _Body(raw, content_length="absent")
    with pytest.raises(HTTPException) as info:
        _read_capped(body)
    assert info.value.status_code == 413


@pytest.mark.parametrize("body,reason", [
    ({"table": "t", "record_key": ["id"], "surprise": 1}, "unknown_keys"),
    ({"table": 5, "record_key": ["id"]}, "invalid_input"),
    ({"table": "t", "record_key": "id"}, "invalid_input"),
    ({"table": "t", "record_key": [{"column": "id", "coalesced": "yes"}]}, "invalid_input"),
    ({"table": "t", "record_key": [{"column": "id", "extra": 1}]}, "invalid_input"),
    ({"table": "t", "record_key": ["id"], "source": 9}, "invalid_input"),
])
def test_the_parsed_body_rejects_malformed_fields(body, reason):
    import json as _json
    from fastapi import HTTPException
    from agents import admin_routes
    raw = _json.dumps(body).encode("utf-8")
    with pytest.raises(HTTPException) as info:
        admin_routes._parse_record_key_body(raw)
    assert info.value.status_code == 422
    assert info.value.detail["reason"] == reason


def test_a_well_formed_body_parses():
    import json as _json
    from agents import admin_routes
    raw = _json.dumps({"table": "records",
                       "record_key": ["id", {"column": "revision", "coalesced": True}],
                       "source": "owner note"}).encode("utf-8")
    parsed = admin_routes._parse_record_key_body(raw)
    assert parsed["table"] == "records"
    assert parsed["record_key"][1] == {"column": "revision", "coalesced": True}


# --------------------------------------------------------------------------- #
# A failed bake leaves the system with NO key, never the prior one.
# --------------------------------------------------------------------------- #
_DESC = "records(id: varchar; revision: integer)"


def test_a_successful_bake_remembers_the_declared_key(monkeypatch):
    rk = _rk()
    monkeypatch.setattr(rk, "_graph_query",
                        lambda: (lambda *_a, **_k: [{"record_key": _json_key()}]))
    rk.remember_from_graph("sys", _DESC)
    assert rk.columns_for("sys", "records") == [c["column"] for c in _KEY]
    rk.reset()


@pytest.mark.parametrize("fault", ["import", "query"])
def test_each_bake_failure_path_leaves_no_key(monkeypatch, fault):
    rk = _rk()
    rk.remember("sys", {"records": _KEY})          # a prior key to be cleared
    if fault == "import":
        monkeypatch.setattr(rk, "_graph_query",
                            lambda: (_ for _ in ()).throw(ImportError("no db")))
    else:
        def _raises(*_a, **_k):
            raise RuntimeError("graph down")
        monkeypatch.setattr(rk, "_graph_query", lambda: _raises)
    rk.remember_from_graph("sys", _DESC)
    assert rk.columns_for("sys", "records") == []
    assert rk.schema_lines(rk.keys_for("sys")) == ""
    rk.reset()


def _json_key():
    import json as _json
    return _json.dumps(_KEY)
