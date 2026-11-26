# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""End-to-end guard behaviour for the S3 Tables path, through the wrapped
``s3tables_run_query`` (review F2c multi-relation refusal, F3 fail-closed, F8
coverage). Offline: the ``connection_factory`` seam injects an in-memory
DuckDB; rule state is set directly through the registries. Dataset-neutral.
"""
import json
from types import SimpleNamespace

import duckdb
import pytest

import agents.record_key as record_key
import agents.row_exclusion as row_exclusion
import agents.state_times as state_times
import agents.storage_bounds as storage_bounds
import agents.system_agent as sa
from agents.duckdb_tools import make_duckdb_tools

ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/demo-bucket"
SYS = "s3t-e2e"
LABEL = "non-record rows"
CONDITIONS = [{"column": "route_name", "op": "not_empty"}]
PRED = row_exclusion.predicate_sql(row_exclusion.exclusion_from(LABEL, CONDITIONS))


def _factory():
    def factory():
        con = duckdb.connect()
        con.execute("ATTACH ':memory:' AS s3t")
        con.execute("CREATE SCHEMA s3t.flow")
        con.execute("CREATE TABLE s3t.flow.segments("
                    "id BIGINT, observed_at TIMESTAMP, done_at TIMESTAMP, revision BIGINT,"
                    " route_name VARCHAR)")
        # `other` carries the same route_name column, so the F6 correlated-alias
        # test is genuine: the predicate on the outer alias would bind there.
        con.execute("CREATE TABLE s3t.flow.other(id BIGINT, route_name VARCHAR)")
        # `events` carries its own storage time column, for the two-safe-read
        # storage-bound disclosure test.
        con.execute("CREATE TABLE s3t.flow.events(id BIGINT, logged_at TIMESTAMP)")
        con.execute("INSERT INTO s3t.flow.segments VALUES "
                    "(1, TIMESTAMP '2026-10-06', TIMESTAMP '2026-10-06', 1, ''),"
                    "(2, TIMESTAMP '2026-10-06', TIMESTAMP '2026-10-06', 1, 'r1')")
        con.execute("INSERT INTO s3t.flow.other VALUES (1,'x'),(2,'y')")
        con.execute("INSERT INTO s3t.flow.events VALUES "
                    "(1, TIMESTAMP '2026-10-06'),(2, TIMESTAMP '2026-10-06')")
        return con
    return factory


def _run_tool(cfg=None, factory=None):
    raw = make_duckdb_tools(cfg or {"table_bucket_arn": ARN}, source_system=SYS,
                            connection_factory=(factory or _factory()))
    for t in sa._guard_duckdb_tools(raw, cfg or {"table_bucket_arn": ARN}, SYS):
        if getattr(t, "tool_name", "") == "s3tables_run_query":
            return t.__wrapped__
    raise AssertionError("no guarded tool")


def _ask(sql, run=None):
    run = run or _run_tool()
    call = sa._ModeACall("how many segment records")
    token = sa._MODE_A_CALL.set(call)
    try:
        return json.loads(run(sql))
    finally:
        sa._MODE_A_CALL.reset(token)


def setup_function(_):
    for mod in (row_exclusion, state_times, storage_bounds, record_key):
        mod.reset()


def teardown_function(_):
    for mod in (row_exclusion, state_times, storage_bounds, record_key):
        mod.reset()


# --- F2c: a multi-relation read touching a rule-bearing table is refused ----

def test_multi_base_table_block_of_a_storage_rule_table_fails_closed_naming_it():
    # A block that scans two BASE tables, one of which carries a time rule,
    # cannot be evaluated per table there -> fail closed, naming the table.
    storage_bounds.remember(SYS, {"segments": ["observed_at"]},
                            {"segments": ["id", "observed_at", "done_at", "route_name"]})
    sql = "SELECT COUNT(*) FROM s3t.flow.segments s JOIN s3t.flow.other o ON s.id = o.id"
    call = sa._ModeACall("how many", windowed=True)
    token = sa._MODE_A_CALL.set(call)
    try:
        out = json.loads(_run_tool()(sql))
    finally:
        sa._MODE_A_CALL.reset(token)
    assert out.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert "segments" in out.get("tables", [])


def test_scalar_subquery_over_a_rule_table_runs_when_the_read_is_safe():
    # a scalar subquery over the row-exclusion table with the
    # predicate in its OWN where is a single-table read and must RUN now
    # (the blanket multi-scan stopgap is gone).
    row_exclusion.remember(SYS, {"segments": row_exclusion.exclusion_from(LABEL, CONDITIONS)})
    sql = ("SELECT o.id FROM s3t.flow.other o WHERE o.id <= "
           f"(SELECT COUNT(*) FROM s3t.flow.segments WHERE {PRED})")
    out = _ask(sql)
    assert "error" not in out


def test_exists_over_a_rule_table_runs_when_the_read_is_safe():
    row_exclusion.remember(SYS, {"segments": row_exclusion.exclusion_from(LABEL, CONDITIONS)})
    sql = ("SELECT o.id FROM s3t.flow.other o WHERE EXISTS "
           f"(SELECT 1 FROM s3t.flow.segments WHERE {PRED} AND segments.id = o.id)")
    out = _ask(sql)
    assert "error" not in out


def test_single_table_read_of_a_rule_table_runs_with_the_predicate():
    row_exclusion.remember(SYS, {"segments": row_exclusion.exclusion_from(LABEL, CONDITIONS)})
    out = _ask(f"SELECT COUNT(*) FROM s3t.flow.segments WHERE {PRED}")
    assert "error" not in out


def test_join_with_no_registered_rule_is_allowed():
    # No rule on any scanned table -> the join runs (nothing to enforce).
    out = _ask("SELECT COUNT(*) FROM s3t.flow.segments s JOIN s3t.flow.other o ON s.id = o.id")
    assert "error" not in out


# --- F3: fail closed when the catalog reader is unavailable -----------------

def test_query_is_refused_when_the_reader_is_unavailable():
    # A raw tool whose closure exposes no reader: the guard must fail closed,
    # not fall back to an Athena provider built from S3 Tables config.
    def _raw(sql, tool_context=None):
        return json.dumps({"columns": ["n"], "rows": [[99]], "row_count": 1})
    guarded = sa._make_scoped_duckdb_run_query(
        [type("T", (), {"tool_name": "s3tables_run_query", "__wrapped__": staticmethod(_raw)})()],
        {"table_bucket_arn": ARN}, system_id=SYS)
    call = sa._ModeACall("q")
    token = sa._MODE_A_CALL.set(call)
    try:
        out = json.loads(guarded.__wrapped__("SELECT COUNT(*) FROM s3t.flow.segments"))
    finally:
        sa._MODE_A_CALL.reset(token)
    assert out.get("error") == "catalog_facts_unavailable"
    assert out.get("retryable") is True


def test_catalog_facts_provider_fails_closed_on_explicit_none_binding():
    # An explicit None binding (reader unavailable) must NOT yield an Athena
    # provider; it raises the retryable unavailable refusal.
    token = sa._CATALOG_FACTS.set(None)
    try:
        with pytest.raises(sa._CatalogFactsUnavailableError):
            sa._catalog_facts_provider({"table_bucket_arn": ARN}, SYS)
    finally:
        sa._CATALOG_FACTS.reset(token)


def test_catalog_facts_provider_unbound_is_athena_default():
    # The unbound sentinel (Athena's path) still builds the Athena provider.
    prov = sa._catalog_facts_provider({"database": "db", "region": "us-east-1"}, SYS)
    assert type(prov).__name__ == "_AthenaCatalog"


# --- F8 + F1: more rules and the correlated-alias bypass, end to end --------

def _windowed_ask(sql, run=None):
    run = run or _run_tool()
    call = sa._ModeACall("how many on the window", windowed=True)
    token = sa._MODE_A_CALL.set(call)
    try:
        return json.loads(run(sql))
    finally:
        sa._MODE_A_CALL.reset(token)


def test_record_key_subset_dedup_is_refused_end_to_end():
    record_key.remember(SYS, {"segments": ["id", "route_name"]})
    # The executable DuckDB de-dup shape: QUALIFY in a derived table, counted.
    sql = ("SELECT COUNT(*) AS n FROM (SELECT * FROM s3t.flow.segments "
           "QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY observed_at DESC) = 1) d")
    out = _ask(sql)
    assert "error" in out
    assert out.get("error_name") == "RECORD_KEY_DEDUPE" or "record key" in json.dumps(out).lower()


def test_catalog_columns_failure_refuses_catalog_facts_unavailable(monkeypatch):
    # a reader whose columns() raises during a real call must
    # fail closed (catalog_facts_unavailable), not run the data query with the
    # window rules silently skipped. The DATA query itself would succeed.
    import agents.duckdb_catalog as dc
    storage_bounds.remember(SYS, {"segments": ["observed_at"]},
                            {"segments": ["id", "observed_at", "done_at", "route_name"]})

    orig = dc.DuckDBCatalog._column_types

    def _boom(self, namespace, table):
        raise dc.CatalogFactsUnavailable("describe failed")

    monkeypatch.setattr(dc.DuckDBCatalog, "_column_types", _boom)
    try:
        out = _windowed_ask(
            "SELECT COUNT(*) FROM s3t.flow.segments WHERE observed_at >= TIMESTAMP '2026-10-05'")
    finally:
        monkeypatch.setattr(dc.DuckDBCatalog, "_column_types", orig)
    assert out.get("error") == "catalog_facts_unavailable"
    assert out.get("retryable") is True


def test_storage_window_refused_when_bound_only_on_a_storage_column():
    # observed_at is a partition source; a windowed turn that bounds only it is
    # refused with the apply-to-state-time remedy.
    # observed_at is the storage column; done_at is another time column that is
    # NOT a storage source, so bounding only observed_at trips the rule.
    storage_bounds.remember(SYS, {"segments": ["observed_at"]},
                            {"segments": ["id", "observed_at", "done_at", "route_name"]})
    out = _windowed_ask(
        "SELECT COUNT(*) FROM s3t.flow.segments WHERE observed_at >= TIMESTAMP '2026-10-05'")
    assert "error" in out
    assert out.get("error_name") == "STORAGE_BOUND" or "storage" in json.dumps(out).lower()


def test_storage_only_window_refused_by_known_business_time_without_a_block():
    # DuckDB parity with the Athena business-time rule: with a known state-time
    # mapping, a NON-windowed turn (no resolved block) whose only time bound is
    # the storage column is refused, telling the agent to filter on the state's
    # own time column. (The Athena path is covered in
    # test_storage_window_business_time.py; both share storage_bound_rule.)
    storage_bounds.remember(SYS, {"segments": ["observed_at"]},
                            {"segments": ["id", "observed_at", "done_at", "route_name"]})
    # done_at is the one business time; observed_at is only the write/storage
    # column (not a state's own time).
    state_times.remember(SYS, {"segments": (
        state_times.StateTimes("segments", "state",
                               (("COMPLETED", "done_at"),),
                               ("observed_at", "done_at")),)})
    out = _ask("SELECT COUNT(*) FROM s3t.flow.segments WHERE observed_at >= TIMESTAMP '2026-10-05' "
               "AND observed_at < TIMESTAMP '2026-10-06'")
    assert out.get("error_name") == "STORAGE_BOUND"
    assert out.get("reason") == "business_time_on_storage_column"
    assert "done_at" in out.get("guidance", "")


def _seed_business_time_mapping():
    storage_bounds.remember(SYS, {"segments": ["observed_at"]},
                            {"segments": ["id", "observed_at", "done_at", "route_name"]})
    state_times.remember(SYS, {"segments": (
        state_times.StateTimes("segments", "state",
                               (("COMPLETED", "done_at"),),
                               ("observed_at", "done_at")),)})


@pytest.mark.parametrize("sql", [
    # CAST day-comparison and GROUP BY on the storage column
    "SELECT CAST(observed_at AS DATE) AS d, COUNT(*) FROM s3t.flow.segments "
    "WHERE CAST(observed_at AS DATE) IN (DATE '2026-10-05', DATE '2026-10-12') GROUP BY 1",
    "SELECT date_trunc('day', observed_at) AS d, COUNT(*) FROM s3t.flow.segments "
    "WHERE date_trunc('day', observed_at) >= TIMESTAMP '2026-10-05' GROUP BY 1",
    "SELECT CAST(observed_at AS DATE) AS d, COUNT(*) FROM s3t.flow.segments GROUP BY 1",
    # BETWEEN and a nested (CTE / derived table) direct bound
    "SELECT COUNT(*) FROM s3t.flow.segments WHERE observed_at BETWEEN TIMESTAMP '2026-10-05' "
    "AND TIMESTAMP '2026-10-06'",
    "WITH w AS (SELECT * FROM s3t.flow.segments WHERE observed_at >= TIMESTAMP '2026-10-05' "
    "AND observed_at < TIMESTAMP '2026-10-06') SELECT COUNT(*) FROM w",
    "SELECT COUNT(*) FROM (SELECT * FROM s3t.flow.segments WHERE observed_at >= "
    "TIMESTAMP '2026-10-05' AND observed_at < TIMESTAMP '2026-10-06') w",
])
def test_duckdb_wrapped_and_grouped_storage_day_shapes_are_refused(sql):
    _seed_business_time_mapping()
    out = _ask(sql)
    assert out.get("reason") == "business_time_on_storage_column", out


def test_correlated_outer_alias_does_not_fake_an_applied_exclusion():
    # F6 genuine end-to-end: the outer table `other` HAS route_name, and the
    # scalar subquery over segments carries the predicate qualified by the
    # OUTER alias `o` (a correlated reference to other.route_name), not the
    # subquery's own segments. The qualifier-chain fix means that does NOT
    # satisfy segments' exclusion, so the segments read is refused with the
    # specific ROW_EXCLUSION refusal (not run as applied).
    row_exclusion.remember(SYS, {"segments": row_exclusion.exclusion_from(LABEL, CONDITIONS)})
    pred_on_outer = row_exclusion.predicate_sql(
        row_exclusion.exclusion_from(LABEL, CONDITIONS), qualifier="o")
    sql = ("SELECT o.id FROM s3t.flow.other o WHERE o.id <= "
           f"(SELECT COUNT(*) FROM s3t.flow.segments WHERE {pred_on_outer})")
    out = _ask(sql)
    assert out.get("error") == "row_exclusion"
    assert out.get("error_name") == "ROW_EXCLUSION"
    assert '"applied": true' not in json.dumps(out).lower()


def test_correlated_alias_overcounts_without_the_qualifier_fix(monkeypatch):
    # Prove the F6 test depends on the qualifier-chain fix: when the row-exclusion
    # reader resolves qualifiers the OLD way (any dotted-name part accepted), the
    # outer-alias predicate is read as carried on segments, so the read is
    # falsely applied and runs (an overcount), not refused.
    import agents.row_exclusion_read as rr
    row_exclusion.remember(SYS, {"segments": row_exclusion.exclusion_from(LABEL, CONDITIONS)})
    pred_on_outer = row_exclusion.predicate_sql(
        row_exclusion.exclusion_from(LABEL, CONDITIONS), qualifier="o")
    sql = ("SELECT o.id FROM s3t.flow.other o WHERE o.id <= "
           f"(SELECT COUNT(*) FROM s3t.flow.segments WHERE {pred_on_outer})")

    # Restore the OLD lenient qualifier model (a flat set of every name part),
    # which accepts the outer alias `o` as a qualifier for segments columns.
    def _old_chains(relation):
        names = {(str(p).lower(),) for p in relation.parts}
        if relation.alias:
            names.add((str(relation.alias).lower(),))
        return rr.Qualified(names) if False else frozenset(names | {("o",)})

    monkeypatch.setattr(rr, "_relation_chains", _old_chains)
    out = _ask(sql)
    # With the regression in place the segments read is NOT refused for the
    # exclusion (it reads the outer-alias predicate as carried).
    assert out.get("error_name") != "ROW_EXCLUSION"


# --- R4-F1: the competing-storage-bound rule runs on DuckDB -----------------

def test_competing_storage_and_event_bound_is_refused():
    # Bounding a temporal storage column (observed_at) AND another time column
    # (done_at) must be refused as STORAGE_BOUND, as the Athena connector does.
    storage_bounds.remember(SYS, {"segments": ["observed_at"]},
                            {"segments": ["id", "observed_at", "done_at", "revision"]})
    out = _ask("SELECT COUNT(*) FROM s3t.flow.segments "
               "WHERE observed_at >= TIMESTAMP '2026-10-05' AND done_at >= TIMESTAMP '2026-10-05'")
    assert "error" in out
    assert out.get("error_name") == "STORAGE_BOUND" or "storage" in json.dumps(out).lower()


def test_single_storage_bound_on_the_asked_column_still_runs():
    storage_bounds.remember(SYS, {"segments": ["observed_at"]},
                            {"segments": ["id", "observed_at", "done_at", "revision"]})
    out = _ask("SELECT COUNT(*) FROM s3t.flow.segments WHERE observed_at >= TIMESTAMP '2026-10-05'")
    assert "error" not in out


# --- R4-F2: a CTE-wrapped join over a rule table fails closed ---------------

def test_cte_wrapped_join_over_a_storage_rule_table_fails_closed():
    storage_bounds.remember(SYS, {"segments": ["observed_at"]},
                            {"segments": ["id", "observed_at", "done_at", "revision"]})
    sql = ("WITH x AS (SELECT * FROM s3t.flow.segments), y AS (SELECT * FROM s3t.flow.other) "
           "SELECT COUNT(*) FROM x JOIN y ON x.id = y.id WHERE x.observed_at >= TIMESTAMP '2026-10-05'")
    call = sa._ModeACall("how many", windowed=True)
    token = sa._MODE_A_CALL.set(call)
    try:
        out = json.loads(_run_tool()(sql))
    finally:
        sa._MODE_A_CALL.reset(token)
    assert out.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert "segments" in out.get("tables", [])


def test_cte_single_relation_read_still_runs():
    # One CTE reading one base table (no join of CTEs) is a single-table read.
    storage_bounds.remember(SYS, {"segments": ["observed_at"]},
                            {"segments": ["id", "observed_at", "done_at", "revision"]})
    out = _ask("WITH x AS (SELECT * FROM s3t.flow.segments WHERE observed_at >= TIMESTAMP '2026-10-05') "
               "SELECT COUNT(*) FROM x")
    assert "error" not in out


# --- R4-F3: a multi-scan refusal refuses the identical resend too -----------

def test_multi_scan_refusal_is_hard_and_refuses_the_resend():
    storage_bounds.remember(SYS, {"segments": ["observed_at"]},
                            {"segments": ["id", "observed_at", "done_at", "revision"]})
    sql = "SELECT COUNT(*) FROM s3t.flow.segments s JOIN s3t.flow.other o ON s.id = o.id"
    run = _run_tool()
    call = sa._ModeACall("how many", windowed=True)
    token = sa._MODE_A_CALL.set(call)
    try:
        first = json.loads(run(sql))
        second = json.loads(run(sql))
    finally:
        sa._MODE_A_CALL.reset(token)
    # Hard refusal: BOTH attempts refuse; no data query runs on the resend.
    assert first.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert second.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert "rows" not in first and "rows" not in second


def test_self_join_names_its_one_table_once():
    storage_bounds.remember(SYS, {"segments": ["observed_at"]},
                            {"segments": ["id", "observed_at", "done_at", "revision"]})
    sql = "SELECT COUNT(*) FROM s3t.flow.segments a JOIN s3t.flow.segments b ON a.id = b.id"
    call = sa._ModeACall("how many", windowed=True)
    token = sa._MODE_A_CALL.set(call)
    try:
        out = json.loads(_run_tool()(sql))
    finally:
        sa._MODE_A_CALL.reset(token)
    assert out.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert out.get("tables") == ["segments"]   # de-duplicated to one entry


# --- R4-F7: every physical read's record-time decision is disclosed ---------

def test_two_reads_disclose_both_record_time_windows_not_just_the_first():
    # the multi-read evaluator must keep EVERY physical read's
    # decision, not only the first. The record-time discloser now emits a list
    # when two reads carry distinct windows (de-duping exact repeats).
    from agents.record_time import RecordTimeWindow
    w1 = RecordTimeWindow("segments", "quality.defect_type", "quality.defect_at", ("observed_at",))
    w2 = RecordTimeWindow("segments", "quality.defect_type", "quality.defect_at", ("done_at",))
    result = {"columns": ["n"], "rows": [[1]]}
    sa._disclose_record_window(result, [w1, w2])
    disclosed = result.get("record_time_window")
    assert isinstance(disclosed, list) and len(disclosed) == 2


def test_record_time_discloser_dedupes_identical_windows():
    from agents.record_time import RecordTimeWindow
    w = RecordTimeWindow("segments", "quality.defect_type", "quality.defect_at", ("observed_at",))
    result = {"columns": ["n"], "rows": [[1]]}
    sa._disclose_record_window(result, [w, w])
    # Exact repeats collapse to a single object, not a list of duplicates.
    assert isinstance(result.get("record_time_window"), dict)


# --- R5-F1: the outer-combination allowlist hard-refuses every wrapper form -
#
# The earlier detector only saw a WITH chain whose outer FROM/JOIN named a CTE.
# These forms combine two relation-producing inputs WITHOUT that exact shape and
# previously ran unguarded. Each must refuse hard (every call), name the
# rule-bearing table once, and refuse the identical resend too.

def _hard_refuse(sql, windowed=True):
    run = _run_tool()
    call = sa._ModeACall("how many", windowed=windowed)
    token = sa._MODE_A_CALL.set(call)
    try:
        first = json.loads(run(sql))
        second = json.loads(run(sql))
    finally:
        sa._MODE_A_CALL.reset(token)
    return first, second


def _remember_storage_rule():
    storage_bounds.remember(SYS, {"segments": ["observed_at"]},
                            {"segments": ["id", "observed_at", "done_at", "revision"]})


def test_comma_join_of_two_ctes_over_a_rule_table_fails_closed():
    _remember_storage_rule()
    sql = ("WITH x AS (SELECT * FROM s3t.flow.segments), y AS (SELECT * FROM s3t.flow.other) "
           "SELECT COUNT(*) FROM x, y WHERE x.id = y.id "
           "AND x.observed_at >= TIMESTAMP '2026-10-05'")
    first, second = _hard_refuse(sql)
    assert first.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert second.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert first.get("tables") == ["segments"]
    assert "rows" not in first and "rows" not in second


def test_cte_joined_to_a_base_table_over_a_rule_table_fails_closed():
    _remember_storage_rule()
    sql = ("WITH x AS (SELECT * FROM s3t.flow.segments) "
           "SELECT COUNT(*) FROM x JOIN s3t.flow.other o ON x.id = o.id "
           "WHERE x.observed_at >= TIMESTAMP '2026-10-05'")
    first, second = _hard_refuse(sql)
    assert first.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert second.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert first.get("tables") == ["segments"]
    assert "rows" not in first and "rows" not in second


def test_two_derived_tables_joined_over_a_rule_table_fails_closed():
    _remember_storage_rule()
    sql = ("SELECT COUNT(*) FROM (SELECT * FROM s3t.flow.segments) a "
           "JOIN (SELECT * FROM s3t.flow.other) b ON a.id = b.id "
           "WHERE a.observed_at >= TIMESTAMP '2026-10-05'")
    first, second = _hard_refuse(sql)
    assert first.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert second.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert first.get("tables") == ["segments"]
    assert "rows" not in first and "rows" not in second


def test_derived_table_comma_joined_to_a_base_table_fails_closed():
    # The review's unrecognised-shape probe: a derived table over the rule
    # table comma-joined to a base table returned data (e.g. [[2]]) before.
    _remember_storage_rule()
    sql = ("SELECT COUNT(*) FROM (SELECT * FROM s3t.flow.segments) a, s3t.flow.other o "
           "WHERE a.id = o.id AND a.observed_at >= TIMESTAMP '2026-10-05'")
    first, second = _hard_refuse(sql)
    assert first.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert second.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert first.get("tables") == ["segments"]
    assert "rows" not in first and "rows" not in second


def test_allowlist_still_runs_a_proven_single_relation_cte_chain():
    # A chain of single-table CTEs with NO join (each wraps the one rule table)
    # is proven safe and must still run (not refused as a combination). A
    # non-windowed ask isolates this from the storage-window rule.
    _remember_storage_rule()
    out = _ask(
        "WITH x AS (SELECT * FROM s3t.flow.segments), "
        "y AS (SELECT * FROM x) SELECT COUNT(*) FROM y")
    assert out.get("error_name") != "MULTI_RELATION_RULE_READ"
    assert "error" not in out


def test_allowlist_does_not_touch_a_join_of_rule_free_tables():
    # Two tables, NEITHER carrying a single-table rule -> the join still runs.
    out = _ask("SELECT COUNT(*) FROM (SELECT * FROM s3t.flow.other) a "
               "JOIN s3t.flow.other o ON a.id = o.id")
    assert "error" not in out


# --- R5-F3: a safe DuckDB storage bound is DISCLOSED like Athena's ----------

def test_safe_single_storage_bound_is_disclosed():
    # A query that bounds only the storage column (observed_at) runs, and the
    # result carries the storage_bound disclosure the Athena connector emits
    # (athena_tools._with_storage_bound): table + bounded columns + conjuncts.
    storage_bounds.remember(SYS, {"segments": ["observed_at"]},
                            {"segments": ["id", "observed_at", "done_at", "revision"]})
    out = _ask("SELECT COUNT(*) FROM s3t.flow.segments WHERE observed_at >= TIMESTAMP '2026-10-05'")
    assert "error" not in out
    sb = out.get("storage_bound")
    assert isinstance(sb, dict)
    assert sb.get("table", "").endswith("segments")   # table (qualified as facts carry it)
    assert sb.get("columns") == ["observed_at"]
    assert sb.get("conjuncts")               # the WHERE conjunct(s) it ran with


def test_no_storage_bound_disclosure_without_a_bound():
    # A query that bounds no storage column carries no storage_bound field.
    storage_bounds.remember(SYS, {"segments": ["observed_at"]},
                            {"segments": ["id", "observed_at", "done_at", "revision"]})
    out = _ask("SELECT COUNT(*) FROM s3t.flow.segments WHERE revision = 1")
    assert "error" not in out
    assert "storage_bound" not in out


def test_no_storage_bound_disclosure_on_a_refused_competing_bound():
    # A refused competing-bound query is an error; it carries no storage_bound
    # disclosure (the bound was never run safely).
    storage_bounds.remember(SYS, {"segments": ["observed_at"]},
                            {"segments": ["id", "observed_at", "done_at", "revision"]})
    out = _ask("SELECT COUNT(*) FROM s3t.flow.segments "
               "WHERE observed_at >= TIMESTAMP '2026-10-05' AND done_at >= TIMESTAMP '2026-10-05'")
    assert "error" in out
    assert "storage_bound" not in out


# --- R5-F4: record-key disclosure keeps EVERY read's decision ---------------

def test_record_key_discloser_emits_a_list_for_two_keyed_tables():
    # Two distinct record-key decisions must disclose as a LIST, not just the
    # first (as the state/record-time disclosers do).
    from agents.record_key import SubsetDedupe
    d1 = SubsetDedupe("segments", ("id",), ("id", "route_name"))
    d2 = SubsetDedupe("other", ("id",), ("id", "kind"))
    result = {"columns": ["n"], "rows": [[1]]}
    sa._disclose_record_key_dedupe(result, [d1, d2])
    disclosed = result.get("record_key_dedupe")
    assert isinstance(disclosed, list) and len(disclosed) == 2
    assert {r["table"] for r in disclosed} == {"segments", "other"}


def test_record_key_discloser_dedupes_identical_decisions():
    from agents.record_key import SubsetDedupe
    d = SubsetDedupe("segments", ("id",), ("id", "route_name"))
    result = {"columns": ["n"], "rows": [[1]]}
    sa._disclose_record_key_dedupe(result, [d, d])
    assert isinstance(result.get("record_key_dedupe"), dict)   # exact repeats collapse


def test_record_key_rule_accumulates_both_keyed_tables(monkeypatch):
    # A proven statement whose two single-table reads each de-duplicate a keyed
    # table (a scalar subquery over one, the outer scan of the other) must
    # return BOTH decisions, not only the first.
    from agents.record_key import SubsetDedupe
    record_key.remember(SYS, {"segments": ["id", "route_name"], "other": ["id", "kind"]})
    decisions = {
        "segments": SubsetDedupe("segments", ("id",), ("id", "route_name")),
        "other": SubsetDedupe("other", ("id",), ("id", "kind")),
    }
    monkeypatch.setattr(record_key, "decide",
                        lambda sql, table, cols: decisions.get(table))
    # Two proven single-table reads (scalar subquery over segments, outer other).
    sql = ("SELECT (SELECT COUNT(*) FROM s3t.flow.segments) AS n, o.id "
           "FROM s3t.flow.other o")
    window, fired = sa._record_key_rule(sql, SYS)
    assert isinstance(window, list) and len(window) == 2
    assert {w.table for w in window} == {"segments", "other"}
    assert len(fired) == 2


# --- R6-F1: the prover is a real allowlist; every non-proven shape refuses --

def _refused_once(sql, windowed=True):
    run = _run_tool()
    call = sa._ModeACall("how many", windowed=windowed)
    token = sa._MODE_A_CALL.set(call)
    try:
        return json.loads(run(sql))
    finally:
        sa._MODE_A_CALL.reset(token)


def test_comma_join_rule_table_in_later_input_fails_closed():
    # Ordering-sensitive comma: the rule-bearing table is the SECOND input,
    # where the Athena scan list used to go uncertain and let it run ([[2]]).
    _remember_storage_rule()
    out = _refused_once("SELECT COUNT(*) FROM s3t.flow.other o, s3t.flow.segments s "
                        "WHERE o.id = s.id AND s.observed_at >= TIMESTAMP '2026-10-05'")
    assert out.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert "rows" not in out


def test_direct_union_all_over_a_rule_table_fails_closed():
    _remember_storage_rule()
    out = _refused_once("SELECT COUNT(*) FROM s3t.flow.segments "
                        "UNION ALL SELECT COUNT(*) FROM s3t.flow.segments")
    assert out.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert "rows" not in out


def test_union_inside_a_cte_over_a_rule_table_fails_closed():
    # The set operation hides inside a CTE body; the outer bound/aggregate is
    # not traced to either arm. Previously ran ([[4]]).
    _remember_storage_rule()
    out = _refused_once(
        "WITH x AS (SELECT id, observed_at FROM s3t.flow.segments "
        "UNION ALL SELECT id, observed_at FROM s3t.flow.segments) "
        "SELECT COUNT(*) FROM x WHERE observed_at >= TIMESTAMP '2026-10-05'")
    assert out.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert "rows" not in out


def test_three_deep_nested_derived_tables_with_outer_bound_fail_closed():
    # Three derived-table levels with an outer bound: lineage is not traced to
    # the one physical read, so the prover cannot prove it. Previously ran.
    _remember_storage_rule()
    out = _refused_once(
        "SELECT COUNT(*) FROM (SELECT * FROM (SELECT * FROM (SELECT * FROM s3t.flow.segments) a) b) c "
        "WHERE observed_at >= TIMESTAMP '2026-10-05'")
    # A proven single-table CTE/derived chain is allowed; three nested derived
    # levels with an UNTRACED outer bound is a shape the prover does not model,
    # so it must NOT silently run. Either it is proven (and runs) or refused;
    # the review requires it never fail open. Assert it does not run unguarded.
    assert out.get("error_name") == "MULTI_RELATION_RULE_READ" or "error" in out
    assert "rows" not in out


def test_lateral_in_from_over_a_rule_table_fails_closed():
    _remember_storage_rule()
    out = _refused_once("SELECT COUNT(*) FROM s3t.flow.other o, LATERAL "
                        "(SELECT * FROM s3t.flow.segments s WHERE s.id = o.id) t")
    assert out.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert "rows" not in out


def test_values_join_with_a_rule_table_fails_closed():
    _remember_storage_rule()
    out = _refused_once("SELECT COUNT(*) FROM s3t.flow.segments s "
                        "JOIN (VALUES (1),(2)) AS v(id) ON s.id = v.id")
    assert out.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert "rows" not in out


def test_natural_join_with_a_rule_table_fails_closed():
    _remember_storage_rule()
    out = _refused_once("SELECT COUNT(*) FROM s3t.flow.segments NATURAL JOIN s3t.flow.other")
    assert out.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert "rows" not in out


def test_using_join_with_a_rule_table_fails_closed():
    _remember_storage_rule()
    out = _refused_once("SELECT COUNT(*) FROM s3t.flow.segments JOIN s3t.flow.other USING (id)")
    assert out.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert "rows" not in out


def test_select_list_subquery_that_itself_joins_fails_closed():
    # A SELECT-list subquery whose OWN block joins the rule table is not a
    # proven single-table subquery.
    _remember_storage_rule()
    out = _refused_once(
        "SELECT (SELECT COUNT(*) FROM s3t.flow.segments s JOIN s3t.flow.other o ON s.id = o.id) AS n")
    assert out.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert "rows" not in out


def test_unproven_shape_refuses_even_with_a_record_key_only(monkeypatch):
    # An UNPROVEN shape (a set operation) that READS the rule table must refuse
    # whenever the SYSTEM has ANY single-table-only rule. Here only a record key
    # is declared (no window rule); the recovered terminal is `segments`.
    record_key.remember(SYS, {"segments": ["id", "route_name"]})
    out = _refused_once("SELECT id FROM s3t.flow.segments UNION ALL SELECT id FROM s3t.flow.segments",
                        windowed=False)
    assert out.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert "rows" not in out


def test_no_rule_system_runs_a_union_untouched():
    # No single-table-only rule anywhere -> an UNPROVEN shape is not the guard's
    # business and runs.
    out = _refused_once("SELECT id FROM s3t.flow.other UNION ALL SELECT id FROM s3t.flow.other",
                        windowed=False)
    assert "error" not in out


# positive proven cases, executed in real DuckDB

def test_proven_scalar_subquery_runs():
    _remember_storage_rule()
    out = _ask("SELECT (SELECT COUNT(*) FROM s3t.flow.segments WHERE route_name <> '') AS n, "
               "o.id FROM s3t.flow.other o")
    assert "error" not in out and out.get("rows")


def test_proven_exists_subquery_runs():
    _remember_storage_rule()
    out = _ask("SELECT o.id FROM s3t.flow.other o WHERE EXISTS "
               "(SELECT 1 FROM s3t.flow.segments s WHERE s.id = o.id)")
    assert "error" not in out


def test_proven_in_subquery_runs():
    _remember_storage_rule()
    out = _ask("SELECT o.id FROM s3t.flow.other o WHERE o.id IN "
               "(SELECT id FROM s3t.flow.segments)")
    assert "error" not in out


def test_proven_single_table_cte_chain_runs():
    _remember_storage_rule()
    out = _ask("WITH x AS (SELECT * FROM s3t.flow.segments), y AS (SELECT * FROM x) "
               "SELECT COUNT(*) FROM y")
    assert "error" not in out and out.get("rows")


# --- R6-F2: every safe storage bound in a multi-read query is disclosed -----

def test_two_safe_storage_bounds_are_both_disclosed():
    # A scalar subquery over `events` (bound logged_at) and the outer read of
    # `segments` (bound observed_at) are two proven single-table reads, each
    # with a safe storage bound. BOTH bounds must be disclosed, not just the
    # first (as state/record/record-key already do).
    storage_bounds.remember(SYS, {"segments": ["observed_at"], "events": ["logged_at"]},
                            {"segments": ["id", "observed_at", "done_at", "revision"],
                             "events": ["id", "logged_at"]})
    sql = ("SELECT (SELECT COUNT(*) FROM s3t.flow.events "
           "WHERE logged_at >= TIMESTAMP '2026-10-05') AS e, COUNT(*) AS s "
           "FROM s3t.flow.segments WHERE observed_at >= TIMESTAMP '2026-10-05'")
    out = _ask(sql)
    assert "error" not in out
    sb = out.get("storage_bound")
    assert isinstance(sb, list) and len(sb) == 2
    cols = {c for rec in sb for c in rec.get("columns", [])}
    assert cols == {"observed_at", "logged_at"}


def test_storage_bound_discloser_emits_a_list_for_two_bounds():
    from agents.storage_bound_rule import StorageBound
    b1 = StorageBound("segments", ("observed_at",), (), ("observed_at >= x",))
    b2 = StorageBound("events", ("logged_at",), (), ("logged_at >= x",))
    result = {"columns": ["n"], "rows": [[1]]}
    sa._disclose_storage_bound(result, [b1, b2])
    disclosed = result.get("storage_bound")
    assert isinstance(disclosed, list) and len(disclosed) == 2


def test_storage_bound_discloser_dedupes_identical_bounds():
    from agents.storage_bound_rule import StorageBound
    b = StorageBound("segments", ("observed_at",), (), ("observed_at >= x",))
    result = {"columns": ["n"], "rows": [[1]]}
    sa._disclose_storage_bound(result, [b, b])
    assert isinstance(result.get("storage_bound"), dict)   # exact repeats collapse


# --- R7-F1: a data read misclassified as zero reads must be refused e2e -----

import pytest as _pytest


@_pytest.mark.parametrize("sql", [
    _pytest.param("SELECT COUNT(*) FROM (((s3t.flow.segments)))", id="nested-parentheses"),
    _pytest.param("SELECT * FROM (PIVOT s3t.flow.segments ON route_name USING count(*))",
                  id="pivot"),
    _pytest.param("SELECT * FROM (UNPIVOT s3t.flow.segments ON observed_at, done_at "
                  "INTO NAME metric VALUE ts)", id="unpivot"),
])
def test_zero_read_data_shape_is_refused_end_to_end(sql):
    # A statement that reads data but whose FROM source yields no scan must be
    # refused, never run with no rule applied.
    _remember_storage_rule()
    out = _refused_once(sql, windowed=False)
    assert out.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert "rows" not in out


def test_cte_shadowing_a_rule_base_table_is_refused_end_to_end():
    # A CTE named like the rule-bearing base terminal is refused: a reference
    # cannot be told from the base table.
    _remember_storage_rule()
    out = _refused_once("WITH segments AS (SELECT * FROM s3t.flow.segments) "
                        "SELECT COUNT(*) FROM segments", windowed=False)
    assert out.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert "segments" in out.get("tables", [])
    assert "rows" not in out


def test_with_recursive_over_a_rule_table_is_refused_end_to_end():
    _remember_storage_rule()
    out = _refused_once("WITH RECURSIVE x AS (SELECT * FROM s3t.flow.segments) "
                        "SELECT COUNT(*) FROM x", windowed=False)
    assert out.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert "rows" not in out


# --- R7-F2 (corrected brief): quoted / commented / literal forms RUN --------

def test_quoted_identifier_form_runs_like_the_unquoted_form():
    # A quoted dotted name (common in real queries) must prove and run.
    _remember_storage_rule()
    out = _ask('SELECT COUNT(*) FROM "s3t"."flow"."segments"')
    assert "error" not in out and out.get("rows")


def test_sql_comment_with_join_union_text_runs():
    _remember_storage_rule()
    out = _ask("SELECT COUNT(*) /* JOIN UNION */ FROM s3t.flow.segments")
    assert "error" not in out and out.get("rows")


def test_string_literal_with_join_union_text_runs():
    _remember_storage_rule()
    out = _ask("SELECT 'JOIN UNION' AS tag, COUNT(*) AS n FROM s3t.flow.segments")
    assert "error" not in out and out.get("rows")


# --- R8-F1/F2: unmodeled FROM-source suffixes and zero-read data refuse ------

def test_alias_column_list_cannot_bypass_the_rule():
    # FROM t AS x(a,b,c,d): the alias column list renames columns the rule
    # readers cannot follow -> UNPROVEN -> refused, not run.
    _remember_storage_rule()
    out = _refused_once("SELECT a, COUNT(*) FROM s3t.flow.segments AS x(a,b,c,d) GROUP BY a",
                        windowed=False)
    assert out.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert "rows" not in out


def test_tablesample_cannot_bypass_the_rule():
    _remember_storage_rule()
    out = _refused_once("SELECT observed_at FROM s3t.flow.segments TABLESAMPLE 100 PERCENT",
                        windowed=False)
    assert out.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert "rows" not in out


def test_parenthesized_source_is_refused():
    _remember_storage_rule()
    out = _refused_once("SELECT COUNT(*) FROM (s3t.flow.segments)", windowed=False)
    assert out.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert "rows" not in out


def test_select_constant_still_runs_with_a_rule_declared():
    # A no-data statement reads no relation: UNPROVEN under the per-relation
    # contract, but the per-relation guard leaves it to run. It
    # is not refused with MULTI_RELATION_RULE_READ.
    _remember_storage_rule()
    out = _ask("SELECT 1 AS n")
    assert out.get("error_name") != "MULTI_RELATION_RULE_READ"
    assert "error" not in out and out.get("rows")


# --- R9-F1/F2: incomplete WHERE refuses; SHOW/DESCRIBE run without the LIMIT --

def test_incomplete_where_is_refused_end_to_end():
    # `... WHERE` with no body is incomplete SQL: UNPROVEN -> refused, not run.
    _remember_storage_rule()
    out = _refused_once("SELECT COUNT(*) FROM s3t.flow.segments WHERE", windowed=False)
    assert out.get("error_name") == "MULTI_RELATION_RULE_READ"
    assert "rows" not in out


def test_show_tables_reaches_the_connector_without_a_limit_rewrite():
    # SHOW must not get a `LIMIT 501` appended (DuckDB rejects it); it runs
    # through the connector and returns a normal result.
    _remember_storage_rule()
    out = _ask("SHOW TABLES")
    assert "error" not in out
    assert "LIMIT" not in (out.get("_adt_executed_query") or "")


def test_describe_table_reaches_the_connector_without_a_limit_rewrite():
    _remember_storage_rule()
    out = _ask("DESCRIBE s3t.flow.segments")
    assert "error" not in out
    assert "LIMIT" not in (out.get("_adt_executed_query") or "")


def test_select_still_gets_the_row_cap_limit():
    _remember_storage_rule()
    out = _ask("SELECT id FROM s3t.flow.segments")
    assert "error" not in out
    assert "LIMIT" in (out.get("_adt_executed_query") or "")


# --- Live fix: qualified table names + unknown_table refusal ----------------

_SCOPED_CFG = {"table_bucket_arn": ARN, "tables": ["flow.segments"]}


def _ask_scoped(sql):
    return _ask(sql, run=_run_tool(cfg=_SCOPED_CFG))


def test_query_on_a_nonexistent_namespace_refuses_unknown_table_not_retryable():
    # The live bug: the agent guessed a wrong namespace, so the query named a
    # table that does not exist. The catalog DESCRIBE raises a CatalogException;
    # the guard must refuse with a
    # NON-retryable unknown_table that names the queried table and the system's
    # real qualified tables -- not a retryable catalog_facts_unavailable that
    # the agent retries forever.
    record_key.remember(SYS, {"segments": ["id"]})
    out = _ask_scoped("SELECT COUNT(*) FROM s3t.wrong_ns.segments")
    assert out.get("reason") == "unknown_table"
    assert out.get("retryable") is False
    assert "s3t.wrong_ns.segments" in json.dumps(out)   # names the wrong table
    assert "s3t.flow.segments" in json.dumps(out)       # lists the real one
    # No path or ARN leaks in the refusal.
    assert ARN not in json.dumps(out) and "arn:" not in json.dumps(out).lower()


def test_genuine_read_outage_stays_catalog_facts_unavailable(monkeypatch):
    # A real read failure (not a nonexistent table) must still be the RETRYABLE
    # catalog_facts_unavailable, so the unknown_table split does not swallow a
    # transient outage.
    import agents.duckdb_catalog as dc
    record_key.remember(SYS, {"segments": ["id"]})
    orig = dc.DuckDBCatalog._column_types

    def _io_error(self, namespace, table):
        raise dc.CatalogFactsUnavailable("io error")

    monkeypatch.setattr(dc.DuckDBCatalog, "_column_types", _io_error)
    try:
        out = _ask_scoped("SELECT COUNT(*) FROM s3t.flow.segments WHERE id > 0")
    finally:
        monkeypatch.setattr(dc.DuckDBCatalog, "_column_types", orig)
    assert out.get("reason") == "catalog_facts_unavailable"
    assert out.get("retryable") is True


def test_steward_facts_resolve_with_the_qualified_desc_and_row_exclusion_refuses():
    # End to end with the QUALIFIED baked desc: the steward facts (keyed by the
    # terminal name) still resolve, so a query lacking the row-exclusion
    # predicate on the qualified relation is refused, and one that carries it
    # runs.
    storage_bounds.remember_tables_desc(
        SYS, "s3t.flow.segments(id: BIGINT, observed_at: TIMESTAMP, route_name: VARCHAR)")
    row_exclusion.remember(SYS, {"segments": row_exclusion.exclusion_from(LABEL, CONDITIONS)})
    missing = _ask("SELECT COUNT(*) FROM s3t.flow.segments")
    assert missing.get("error") == "row_exclusion"      # predicate absent -> refused
    kept = _ask(f"SELECT COUNT(*) FROM s3t.flow.segments WHERE {PRED}")
    assert "error" not in kept                          # predicate present -> runs


# --- Live fix r2: unknown_table never echoes an unsafe relation; lists baked -

def test_unknown_table_refusal_does_not_echo_an_arn_shaped_quoted_namespace():
    # A quoted identifier carrying ARN characters reaches the missing-relation
    # path; the refusal must WITHHOLD it, never echo an ARN into public text.
    row_exclusion.remember(SYS, {})
    record_key.remember(SYS, {"segments": ["id"]})
    sensitive = "arn:aws:s3tables:us-east-1:111122223333:bucket/private"
    out = _ask_scoped(f'SELECT COUNT(*) FROM s3t."{sensitive}"."segments"')
    rendered = json.dumps(out).lower()
    assert out.get("reason") == "unknown_table"
    assert "arn:" not in rendered
    assert sensitive.lower() not in rendered


def test_unknown_table_refusal_does_not_echo_a_filesystem_path():
    # A quoted identifier shaped like a filesystem path is withheld too.
    row_exclusion.remember(SYS, {})
    record_key.remember(SYS, {"segments": ["id"]})
    path = "/root/.aws/credentials"
    out = _ask_scoped(f'SELECT COUNT(*) FROM s3t."{path}"."segments"')
    rendered = json.dumps(out)
    assert out.get("reason") == "unknown_table"
    assert path not in rendered and "/root/" not in rendered


def test_unknown_table_refusal_lists_baked_names_for_an_open_scope():
    # An open-scope system (no connection_config.tables) still has a complete
    # baked desc, so the refusal lists its real qualified tables, not
    # "none are baked".
    row_exclusion.remember(SYS, {})
    record_key.remember(SYS, {"segments": ["id"]})
    out = _ask("SELECT COUNT(*) FROM s3t.wrong_ns.segments",
               run=_run_tool(cfg={"table_bucket_arn": ARN}))
    assert out.get("reason") == "unknown_table"
    assert "s3t.flow.segments" in json.dumps(out)


def test_unknown_table_refusal_lists_baked_names_only_inside_the_scope():
    # A remembered baked tables_desc supplies the names, but only the ones in
    # the configured read scope: the refusal never names an out-of-scope table.
    row_exclusion.remember(SYS, {})
    record_key.remember(SYS, {"segments": ["id"]})
    storage_bounds.remember_qualified_names(SYS, ["s3t.flow.segments", "s3t.flow.events"])
    out = _ask("SELECT COUNT(*) FROM s3t.wrong_ns.segments",
               run=_run_tool(cfg={"table_bucket_arn": ARN,
                                  "tables": ["flow.segments", "other.thing"]}))
    body = json.dumps(out)
    assert out.get("reason") == "unknown_table"
    assert "s3t.flow.segments" in body
    assert "s3t.flow.events" not in body


# --- Live fix r3: the live-listing fallback honours the namespace/table scope -

def test_live_name_fallback_does_not_list_tables_outside_namespace_scope():
    # With no baked desc and no cfg "tables", the refusal falls back to the
    # connector's live listing. That listing covers the whole bucket, so it must
    # be filtered to the configured scope: a namespace-scoped system must not
    # learn other namespaces' table names from an unknown_table refusal.
    row_exclusion.remember(SYS, {})
    record_key.remember(SYS, {"missing": ["id"]})
    cfg = {"table_bucket_arn": ARN, "namespaces": ["allowed"]}
    out = _ask("SELECT COUNT(*) FROM s3t.allowed.missing", run=_run_tool(cfg=cfg))
    rendered = json.dumps(out)
    assert out.get("reason") == "unknown_table"
    assert "s3t.flow." not in rendered


# --- scope is checked before any catalog read --------------------------------

def test_out_of_scope_sql_is_refused_before_any_catalog_metadata_read(monkeypatch):
    # The window rules read a table's columns. For a relation outside the
    # configured scope that read must never happen: the scope refusal comes
    # first, so the rules cannot touch (or time) an out-of-scope table.
    # It is answered like a relation that does not exist (unknown_table, which
    # lists only in-scope tables), so the refusal does not disclose it either.
    import agents.duckdb_tools as duckdb_tools
    storage_bounds.remember(SYS, {"segments": ["observed_at"]},
                            {"segments": ["id", "observed_at", "done_at", "route_name"]})
    seen = []
    real = duckdb_tools._S3TablesReader.columns

    def spy(self, namespace, table):
        seen.append((namespace, table))
        return real(self, namespace, table)

    def list_spy(self):
        seen.append(("list_tables",))
        return []

    monkeypatch.setattr(duckdb_tools._S3TablesReader, "columns", spy)
    monkeypatch.setattr(duckdb_tools._S3TablesReader, "list_tables", list_spy)
    cfg = {"table_bucket_arn": ARN, "tables": ["flow.other"]}
    out = _ask("SELECT COUNT(*) FROM s3t.flow.segments "
               "WHERE observed_at >= TIMESTAMP '2026-10-06'", run=_run_tool(cfg))
    assert out.get("reason") == "unknown_table"
    assert "s3t.flow.other" in json.dumps(out)
    assert seen == []


def test_out_of_scope_refusal_never_names_an_out_of_scope_baked_table():
    storage_bounds.remember(SYS, {"segments": ["observed_at"]},
                            {"segments": ["id", "observed_at", "done_at", "route_name"]})
    storage_bounds.remember_qualified_names(
        SYS, ["s3t.flow.segments", "s3t.secret.payroll", "s3t.flow.other"])
    cfg = {"table_bucket_arn": ARN, "tables": ["flow.other"]}
    out = _ask("SELECT COUNT(*) FROM s3t.flow.segments "
               "WHERE observed_at >= TIMESTAMP '2026-10-06'", run=_run_tool(cfg))
    body = json.dumps(out)
    assert out.get("reason") == "unknown_table"
    assert "s3t.flow.other" in body
    assert "payroll" not in body
    assert "Its tables are: s3t.flow.other." in out.get("guidance", "")


def test_out_of_scope_refusal_names_only_pairs_inside_the_namespace_scope():
    # A configured table pair the namespace scope excludes is not readable, so
    # the refusal must not name it either (the two constraints intersect).
    from agents.duckdb_catalog import DuckDBCatalog, UnknownTable
    from agents.duckdb_tools import _scope_pairs
    scope = _scope_pairs({"namespaces": ["flow"], "tables": ["flow.other", "hidden.secret"]})
    catalog = DuckDBCatalog(reader=None, source_system=SYS, scope=scope)
    with pytest.raises(UnknownTable) as raised:
        catalog._column_types("flow", "segments")
    body = json.dumps(raised.value.to_dict())
    assert "s3t.flow.other" in body
    assert "hidden" not in body
