# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The S3 Tables system agent gets EVERY SQL engine rule Athena gets, written
in DuckDB dialect.

Live finding (customer deployment, 1ff13e64a): the S3 Tables agent received
none of the engine rules (`_policy_tail` gated each on ATHENA / PostgreSQL), so
it wrote dotted
columns unquoted and DuckDB raised `Binder Error: Referenced table "a.b" not
found` on most queries. These tests pin that every rule now reaches an S3
Tables prompt in DuckDB form, that Athena's and PostgreSQL's composed rules are
byte-identical (snapshot hash), and that the binder-split error gives the
quoting reason.
"""
import hashlib

import agents.system_agent as sa
from agents.connection_types import ATHENA, S3TABLES_DUCKDB
from agents.duckdb_tools import sanitize_engine_error

#: The engine rule headers every SQL engine's policy tail must carry.
_RULE_MARKERS = (
    "SELF-RECOVERY", "DATE FILTERS", "PERCENTILES", "STATE TIME", "CURRENT BALANCES",
    "SUBQUERY PROJECTION", "TOTALS ACROSS JOINS", "DOTTED COLUMN NAMES",
    "OBSERVED VALUE SHAPE", "IDENTIFIER FORMAT", "QUOTED VALUE", "EFFICIENCY",
    "ROW FIGURES",
)


def _s3_tail():
    return sa._policy_tail(S3TABLES_DUCKDB)


def test_s3tables_tail_contains_every_sql_engine_rule():
    tail = _s3_tail()
    missing = [m for m in _RULE_MARKERS if m not in tail]
    assert missing == [], f"S3 Tables prompt is missing engine rules: {missing}"


def test_s3tables_rules_are_in_duckdb_dialect():
    tail = _s3_tail()
    # Percentiles: DuckDB ordered-set aggregates, not Trino approx_percentile.
    assert "quantile_disc" in tail and "quantile_cont" in tail
    # Date literals/casts: DuckDB parse functions.
    assert "strptime" in tail and "TRY_CAST" in tail
    # JSON list expansion: the DuckDB form.
    assert "CAST(col AS JSON[])" in tail
    assert "json_extract_string" in tail
    # Self-recovery names the S3 Tables introspection tools.
    assert "s3tables_list_tables" in tail


def test_s3tables_dotted_column_rule_uses_the_duckdb_binder_error():
    from agents.connection_types import S3TABLES_DUCKDB
    rule = sa._dotted_column_prompt(S3TABLES_DUCKDB)
    assert "DOTTED COLUMN NAMES" in rule
    # The DuckDB variant names the binder error the live agent hit, and the
    # double-quote fix.
    assert 'Referenced table "details" not found' in rule
    assert 'SELECT "details.equipment"' in rule
    # The DuckDB rule must NOT claim Athena's COLUMN_NOT_FOUND.
    assert "COLUMN_NOT_FOUND" not in rule


def test_s3tables_percentile_rule_forbids_approx_percentile():
    # The DuckDB percentile rule explicitly steers away from Trino's spelling.
    tail = _s3_tail()
    pct = tail[tail.index("PERCENTILES"):tail.index("PERCENTILES") + 600]
    assert "quantile_disc" in pct
    assert "Do NOT use approx_percentile" in pct


# --- Athena and PostgreSQL prompts are UNCHANGED (snapshot pins) -------------
# If an engine-rule edit changes Athena's or PostgreSQL's composed policy tail,
# these fail. Update the hash ONLY with a deliberate, reviewed change to that
# engine's text.
_ATHENA_TAIL_SHA256 = "b24b23eaee1825dced40def00995f157115466aeaf3e7cd8ee311f991a366540"
_POSTGRESQL_TAIL_SHA256 = "bc830f6152de0c8512732ea2717506bc8c649ee3a30d8298da956b29a3c2ca87"


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def test_athena_policy_tail_is_unchanged():
    assert _sha(sa._policy_tail(ATHENA)) == _ATHENA_TAIL_SHA256


def test_postgresql_policy_tail_is_unchanged():
    assert _sha(sa._policy_tail("PostgreSQL")) == _POSTGRESQL_TAIL_SHA256


# --- the binder-split error gives the quoting reason ------------------------

def test_binder_referenced_table_dotted_gives_the_quoting_reason():
    # DuckDB reports a dotted column referenced unquoted as a missing table.
    out = sanitize_engine_error('Binder Error: Referenced table "a.b" not found!')
    assert out is not None
    low = out.lower()
    assert "double-quote" in low or "one identifier" in low or "one pair of quotes" in low
    assert "dotted column" in low
    assert '"a.b' in out                       # names the dotted prefix the model wrote


def test_binder_referenced_table_bare_name_is_a_plain_table_hint():
    # A bare (non-dotted) referenced table is a genuine missing-table hint, not
    # the dotted-column quoting reason.
    out = sanitize_engine_error('Binder Error: Referenced table "orders" not found!')
    assert out is not None
    assert "dotted" not in out.lower()
    assert "orders" in out


def test_binder_split_reason_leaks_no_path_or_arn():
    # A quoted identifier shaped like an ARN must not be echoed by the reason.
    arn = 'arn:aws:s3tables:us-east-1:111122223333:bucket/x.y'
    out = sanitize_engine_error(f'Binder Error: Referenced table "{arn}" not found!')
    if out is not None:
        low = out.lower()
        assert "arn:" not in low
        assert "amazonaws" not in low


# --- end to end: a dotted column referenced unquoted gets the quoting reason -
import json

import duckdb

from agents.duckdb_tools import make_duckdb_tools

_ARN = "arn:aws:s3tables:us-east-1:777788889999:bucket/demo-bucket"


def _dotted_factory():
    def factory():
        con = duckdb.connect()
        con.execute("ATTACH ':memory:' AS s3t")
        con.execute("CREATE SCHEMA s3t.flow")
        # A physical column whose NAME contains dots (a flattened nested leaf).
        con.execute('CREATE TABLE s3t.flow.t("entity.quality.defect_type" VARCHAR)')
        return con
    return factory


def test_dotted_column_unquoted_surfaces_the_quoting_reason_end_to_end():
    tools = make_duckdb_tools({"table_bucket_arn": _ARN}, connection_factory=_dotted_factory())
    run = tools[2].__wrapped__
    out = json.loads(run("SELECT entity.quality.defect_type FROM s3t.flow.t"))
    assert "error" in out
    blob = json.dumps(out).lower()
    assert "query against s3 tables failed" not in blob
    assert "dotted column" in blob and ("double-quote" in blob or "one pair of quotes" in blob)


# --- Every prescribed DuckDB form runs through the S3 Tables tool -------------
# The S3 Tables authorizer rejects table functions in FROM (so CROSS JOIN
# UNNEST is refused) and DuckDB's RE2 regex engine rejects lookahead. These run
# the prompt's own fragments through the real guarded tool and engine.

import json as _json

_ARN = "arn:aws:s3tables:us-east-1:777788889999:bucket/demo"
_SHAPE_CORPUS = (
    "abc1234567890123456", "1234567890123456", "12345678901234567890a",
    "a1234567890123456789", "deadbeefdeadbeef", "DEADBEEF00000000",
    "deadbeefdeadbee", "WN5-0042", "id 0123456789abcdef0 x 77",
    "123e4567-e89b-12d3-a456-426614174000", "pre-123e4567-e89b-12d3-a456-426614174000-9",
    "0000000000000000a0000000000000000", "ffff", "", "x9f8e7d6c5b4a39281",
    "123456789012345a", "12345678901234a", "a123456789012345", "a12345678901234",
)


def _duckdb_factory():
    import duckdb
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS s3t")
    con.execute("CREATE SCHEMA s3t.flow")
    con.execute("CREATE TABLE s3t.flow.t(id VARCHAR, col VARCHAR)")
    con.execute("INSERT INTO s3t.flow.t VALUES ('a', '[{\"id\":\"A\"},{\"id\":\"B\"}]'), "
                "('b', '[]')")
    con.execute("CREATE TABLE s3t.flow.ids(col VARCHAR)")
    con.executemany("INSERT INTO s3t.flow.ids VALUES (?)", [[v] for v in _SHAPE_CORPUS])
    return con


def _run_s3t(sql):
    from agents.duckdb_tools import make_duckdb_tools
    tools = make_duckdb_tools({"table_bucket_arn": _ARN}, connection_factory=_duckdb_factory)
    return _json.loads(tools[2].__wrapped__(sql))


def test_prescribed_duckdb_list_expansion_runs_through_the_s3tables_tool():
    expansion, element = sa._LIST_EXPANSION[S3TABLES_DUCKDB]
    sql = (f"SELECT {element} AS eid, COUNT(*) AS n "
           f"{expansion.replace('<table>', 's3t.flow.t')} GROUP BY 1 ORDER BY 1")
    out = _run_s3t(sql)
    assert "error" not in out, out
    # One row per element; the empty list drops out.
    assert out["rows"] == [["A", 1], ["B", 1]]


def test_duckdb_identifier_shape_expression_matches_the_engine_normalizer():
    from agents.answer_identifier_families import normalize_shape, shape_sql_example_duckdb
    expression = shape_sql_example_duckdb()
    assert "(?=" not in expression
    out = _run_s3t(f"SELECT col, {expression} AS shape FROM s3t.flow.ids")
    assert "error" not in out, out
    got = {row[0]: row[1] for row in out["rows"]}
    assert set(got) == set(_SHAPE_CORPUS)
    for value in _SHAPE_CORPUS:
        assert got[value] == normalize_shape(value), value


def test_s3tables_identifier_prompt_uses_the_re2_shape_expression():
    from agents.answer_identifier_families import shape_sql_example_duckdb
    rule = sa._identifier_shape_prompt(S3TABLES_DUCKDB)
    assert shape_sql_example_duckdb() in rule
    assert "(?=" not in rule
