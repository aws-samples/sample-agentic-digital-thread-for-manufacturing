# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests: Athena SYSTEM-AGENT scan scoping (prompt + tool guardrail).

LIVE-CONFIRMED problem: the Athena system agent composed raw, UNPRUNED SQL over a
large partitioned table — 60-120s full-table scans (122.1s / 109.8s observed)
that tripped the 60s wall budget into 'partial' answers. The resolver's metric
paths were already pruned (agents/athena_tools.py); these tests pin the
extension of the SAME generic pruning to the agent's ad-hoc SQL:

1. PROMPT — the Athena-only QUERY EFFICIENCY (required) directive (partition
   columns named from the baked schema's "(partition key)" markers when present).
2. GUARDRAIL — the guarded athena_run_query injects the recent-partition
   predicate into UNSCOPED simple single-table SQL and notes the injection in
   the result. Unpartitioned SQL passes through; complex SQL executes unchanged
   only when every partitioned physical scan is provably constrained, otherwise
   it fails closed with scope_required. Never a silent semantics change.
3. BUDGET — the generic per-connection-type BUDGET note reaches the Athena prompt.

Fully offline — ``athena_tools.run_athena_query`` (or the pruning helpers) are
stubbed; NO AWS clients; synthetic names only.
"""
from __future__ import annotations

import json

import pytest

import agents.athena_tools as at
import agents.schema_format as schema_format
import agents.system_agent as sa

_CFG = {"workgroup": "wg", "database": "lake_db"}


@pytest.fixture(autouse=True)
def _clear_partition_cache():
    """Partition-column discovery is cached per process — isolate every test."""
    at._PARTITION_COLUMNS_CACHE.clear()
    yield
    at._PARTITION_COLUMNS_CACHE.clear()


def _compose(connection_type: str, tables_desc: str = "") -> str:
    return sa._compose_system_prompt(
        system_id="lake-athena",
        system_name="Lake Athena",
        schema="lake_db",
        system_prompt="You are the Athena agent.",
        tables_desc=tables_desc,
        connection_type=connection_type,
    )


# =========================================================================== #
# 1. PROMPT — QUERY EFFICIENCY (required), Athena-only
# =========================================================================== #
def test_athena_prompt_contains_query_efficiency_section():
    prompt = _compose("Athena")
    assert "QUERY EFFICIENCY (required)" in prompt
    # The four rules: the time bound (a partition bound only when asked for),
    # LIMIT, aggregate pushdown, no SELECT *.
    from agents.storage_bound_rule import PARTITION_RULE
    assert PARTITION_RULE in prompt
    assert "LIMIT" in prompt
    assert "GROUP BY" in prompt
    assert "SELECT *" in prompt
    # And it announces the auto-scope backstop so the note is never a surprise.
    assert "AUTO-SCOPED" in prompt


def test_query_efficiency_names_partition_columns_from_baked_schema():
    tables_desc = (
        "events(dt: date (partition key), site: varchar (partition key), qty: int). "
        "orders(order_id: varchar, amount: decimal(10, 2))"
    )
    prompt = _compose("Athena", tables_desc=tables_desc)
    assert "Known partition columns — events: dt, site" in prompt
    # The unpartitioned table is NOT listed as partitioned.
    assert "orders:" not in prompt.split("Known partition columns")[1].split(".")[0]


def test_query_efficiency_generic_fallback_when_no_partition_marker():
    prompt = _compose("Athena", tables_desc="orders(order_id: varchar)")
    assert "QUERY EFFICIENCY (required)" in prompt
    assert "Known partition columns" not in prompt
    assert "'(partition key)'" in prompt  # points at the schema marker instead


@pytest.mark.parametrize("connection_type", ["PostgreSQL", "SiteWise (Direct)"])
def test_query_efficiency_is_athena_only(connection_type):
    assert "QUERY EFFICIENCY (required)" not in _compose(connection_type)


# =========================================================================== #
# 2. PROMPT — the generic BUDGET note reaches the Athena branch
# =========================================================================== #
def test_athena_prompt_gets_generic_budget_note(monkeypatch):
    monkeypatch.delenv("SYSTEM_AGENT_TOOL_CALL_BUDGET", raising=False)
    monkeypatch.delenv("SYSTEM_AGENT_WALL_BUDGET_SECONDS", raising=False)
    prompt = _compose("Athena")
    assert "BUDGET (required)" in prompt
    # It names the soft caps and the explicit partial marker contract.
    assert "tool calls" in prompt
    assert "TOOL BUDGET EXHAUSTED" in prompt


# =========================================================================== #
# 3. Parser — partition columns from tables_desc markers
# =========================================================================== #
# Two markers, two meanings: "(partition key)" names a partition column;
# "(no partition keys)" (schema_format.PARTITION_KEYS_ABSENT_MARKER) is the
# AFFIRMATIVE completeness signal onboarding appends when a WHOLE physical
# introspection found none. Neither marker => UNKNOWN, never "unpartitioned".
_NO_PART = schema_format.PARTITION_KEYS_ABSENT_MARKER


def test_partition_columns_from_desc_parses_marked_columns_only():
    desc = (
        "events(dt: date (partition key), qty: int). "
        "orders(amount: decimal(10, 2), year: int (partition key))"
    )
    assert sa._athena_partition_columns_from_desc(desc) == {
        "events": ["dt"],
        "orders": ["year"],
    }


def test_partition_columns_from_desc_requires_the_completeness_marker():
    """A marker-free well-formed table is UNKNOWN (absent), not unpartitioned —
    a legacy/pre-marker baked schema and a genuinely unpartitioned table render
    identical text, so absence must never be read as proof."""
    assert sa._athena_partition_columns_from_desc("") == {}
    assert sa._athena_partition_columns_from_desc("t(a: int, b: varchar)") == {}
    assert sa._athena_partition_columns_from_desc(
        f"t(a: int, b: varchar) {_NO_PART}") == {"t": []}


def test_partition_columns_from_desc_marker_survives_multi_table_desc():
    """The marker is consumed as part of ITS table's entry — it must not be
    mistaken for a nameless table, and the following table must still parse."""
    desc = (
        f"plain(a: int, b: varchar) {_NO_PART}. "
        "events(dt: date (partition key), qty: int). "
        f"other(id: varchar) {_NO_PART}"
    )
    assert sa._athena_partition_columns_from_desc(desc) == {
        "plain": [], "events": ["dt"], "other": [],
    }


def test_partition_columns_from_desc_marker_works_with_sample_form():
    """The semicolon (baked-samples) rendering carries the marker identically."""
    desc = f"plain(a: int e.g. 1, 2; b: varchar e.g. x) {_NO_PART}"
    assert sa._athena_partition_columns_from_desc(desc) == {"plain": []}


def test_partition_columns_from_desc_marker_never_rescues_malformed_or_truncated():
    """Malformed / truncated entries stay ABSENT even WITH the marker — the
    marker claims completeness of introspection, not of the rendering."""
    assert sa._athena_partition_columns_from_desc(
        f"malformed(: varchar) {_NO_PART}") == {}
    assert sa._athena_partition_columns_from_desc(
        f"truncated(b: bigint {_NO_PART}") == {}
    # A names-only fallback desc has no parens at all => nothing is known.
    assert sa._athena_partition_columns_from_desc("tbl_a, tbl_b, tbl_c") == {}


def test_partition_columns_from_desc_omits_malformed_or_truncated_table():
    desc = (f"sound(a: int) {_NO_PART}. malformed(: varchar). "
            "truncated(b: bigint")
    assert sa._athena_partition_columns_from_desc(desc) == {"sound": []}


def test_partition_columns_from_desc_ignores_a_bare_marker_lookalike():
    """A stray marker with no table entry in front of it contributes nothing."""
    assert sa._athena_partition_columns_from_desc(_NO_PART) == {}


def test_partition_columns_from_desc_strips_the_relation_kind_suffix():
    """The renderer appends the connector-owned relation kind as a bracketed
    suffix during provisioning (``orders [base_table](...)``). It is presentation
    metadata; the parsed key must be the PHYSICAL table name, otherwise the scope
    layer (which matches on relation parts) can never resolve a baked entry and
    both baked paths silently go inert."""
    assert sa._athena_partition_columns_from_desc(
        f"plain [base_table](a: int) {_NO_PART}") == {"plain": []}
    assert sa._athena_partition_columns_from_desc(
        "events [base_table](dt: date (partition key))") == {"events": ["dt"]}
    assert sa._athena_partition_columns_from_desc(
        "v [materialized_view](a: int (partition key))") == {"v": ["a"]}
    # A real name is never truncated — only the normalized kind vocabulary shape
    # is stripped, and only as a trailing bracketed token.
    assert sa._athena_partition_columns_from_desc(
        "odd_name [Not A Kind](a: int (partition key))") == {
            "odd_name [Not A Kind]": ["a"]}


def test_relation_kind_rendering_round_trips_to_the_physical_table_name():
    """End-to-end with the REAL renderer under an active relation-kind scope."""
    rendered = schema_format.format_tables_desc(
        {"plain": {"relation_kind": "base_table", "columns": [("a", "int")]},
         "events": {"relation_kind": "base_table",
                    "columns": [("dt", "date (partition key)")]}},
        unpartitioned_tables={"plain"},
    )
    assert rendered == (
        f"plain [base_table](a: int) {_NO_PART}. "
        "events [base_table](dt: date (partition key))"
    )
    assert sa._athena_partition_columns_from_desc(rendered) == {
        "plain": [], "events": ["dt"],
    }


# =========================================================================== #
# 4. Predicate injection — the pure SQL rewrite helper
# =========================================================================== #
PRED = "\"dt\" IN (DATE '2026-07-15', DATE '2026-07-14')"


def test_inject_appends_where_when_none():
    out = sa._inject_where_predicate("SELECT count(*) FROM events", PRED)
    assert out == f"SELECT count(*) FROM events WHERE {PRED}"  # nosec B608 - test fixture composing expected SQL from module constants


def test_inject_wraps_existing_where_preserving_or_precedence():
    out = sa._inject_where_predicate(
        "SELECT * FROM events WHERE a = 1 OR b = 2 GROUP BY a", PRED
    )
    assert out == f"SELECT * FROM events WHERE {PRED} AND (a = 1 OR b = 2) GROUP BY a"  # nosec B608 - test fixture composing expected SQL from module constants


def test_inject_inserts_before_group_by_when_no_where():
    out = sa._inject_where_predicate(
        "SELECT site, count(*) FROM events GROUP BY site", PRED
    )
    assert out == f"SELECT site, count(*) FROM events WHERE {PRED} GROUP BY site"  # nosec B608 - test fixture composing expected SQL from module constants


def test_inject_inserts_before_limit_when_no_where():
    out = sa._inject_where_predicate("SELECT * FROM events LIMIT 10", PRED)
    assert out == f"SELECT * FROM events WHERE {PRED} LIMIT 10"  # nosec B608 - test fixture composing expected SQL from module constants


def test_inject_ignores_keywords_inside_string_literals():
    out = sa._inject_where_predicate(
        "SELECT * FROM events WHERE note = 'group by limit'", PRED
    )
    assert out == (
        f"SELECT * FROM events WHERE {PRED} AND (note = 'group by limit')"  # nosec B608 - test fixture composing expected SQL from module constants
    )




def test_inject_places_scope_before_trailing_line_comment():
    original = "SELECT count(*) FROM events -- dt is displayed above"
    out = sa._inject_where_predicate(original, PRED)
    sql_code, comment = out.split("--", 1)
    assert sql_code.rstrip().endswith(f"WHERE {PRED}")
    assert comment == " dt is displayed above"
# =========================================================================== #
# 5. _auto_scope_athena_sql — scoping decisions (pruning helpers stubbed)
# =========================================================================== #
def _stub_pruning(monkeypatch, *, pcols, predicate, calls=None):
    calls = calls if calls is not None else {}

    def fake_list(table, cfg, **kw):
        calls.setdefault("list", []).append(table)
        return pcols

    def fake_pred(table, cfg, **kw):
        calls.setdefault("pred", []).append(table)
        return predicate

    monkeypatch.setattr(at, "list_partition_columns", fake_list)
    monkeypatch.setattr(at, "build_recent_partition_predicate", fake_pred)
    return calls


def test_unscoped_sql_on_partitioned_table_gets_predicate_and_note(monkeypatch):
    _stub_pruning(
        monkeypatch,
        pcols=[{"name": "dt", "data_type": "date"}],
        predicate="\"dt\" IN (DATE '2026-07-15')",
    )
    sql, note = sa._auto_scope_athena_sql("SELECT count(*) FROM events", _CFG)
    assert sql == "SELECT count(*) FROM events WHERE \"dt\" IN (DATE '2026-07-15')"
    assert "auto-scoped" in note
    assert str(at.RECENT_PARTITIONS_LIMIT) in note
    assert "events" in note and "dt" in note
    assert "override" in note  # explicit-range override is documented


def test_already_scoped_sql_passes_through_untouched(monkeypatch):
    calls = _stub_pruning(
        monkeypatch,
        pcols=[{"name": "dt", "data_type": "date"}],
        predicate="\"dt\" IN (DATE '2026-07-15')",
    )
    original = "SELECT count(*) FROM events WHERE dt >= DATE '2026-01-01'"
    sql, note = sa._auto_scope_athena_sql(original, _CFG)
    assert sql == original
    assert note == ""
    # No partition-value metadata read is spent on an already-scoped query.
    assert "pred" not in calls


def test_unpartitioned_table_passes_through_untouched(monkeypatch):
    calls = _stub_pruning(monkeypatch, pcols=[], predicate=None)
    original = "SELECT count(*) FROM plain_table"
    sql, note = sa._auto_scope_athena_sql(original, _CFG)
    assert (sql, note) == (original, "")
    assert "pred" not in calls


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM t1 JOIN t2 ON t1.id = t2.id",                # join
        "WITH x AS (SELECT a FROM events) SELECT * FROM x",          # CTE
        "SELECT a FROM events WHERE a IN (SELECT b FROM other)",     # subquery
        "SELECT a FROM t1 UNION SELECT a FROM t2",                   # set op
        "SELECT a FROM events; SELECT b FROM events",                # multi-stmt
        "SHOW TABLES",                                               # non-select
    ],
)
def test_structurally_complex_sql_is_not_rewritten_by_compat_projection(monkeypatch, sql):
    calls = _stub_pruning(
        monkeypatch,
        pcols=[{"name": "dt", "data_type": "date"}],
        predicate="\"dt\" IN (DATE '2026-07-15')",
    )
    out_sql, note = sa._auto_scope_athena_sql(sql, _CFG)
    assert (out_sql, note) == (sql, "")
    # Complex SELECTs may perform bounded per-relation metadata reads to prove
    # scope; the compatibility projection still never rewrites them.
    assert len(calls.get("list", [])) <= 2


def test_discovery_failure_never_breaks_the_query(monkeypatch):
    def boom(table, cfg, **kw):
        raise RuntimeError("information_schema access denied")

    monkeypatch.setattr(at, "list_partition_columns", boom)
    original = "SELECT count(*) FROM events"
    assert sa._auto_scope_athena_sql(original, _CFG) == (original, "")


# =========================================================================== #
# 6. Guarded tool — end-to-end through a stubbed run_athena_query router
# =========================================================================== #
class _FakeRunQuery:
    """Router stub for ``athena_tools.run_athena_query``: answers the two
    metadata queries (information_schema.columns / ``$partitions``) and
    records + answers the final data query."""

    def __init__(self, *, partition_cols, partition_rows):
        self.partition_cols = partition_cols  # [column_name, data_type, extra_info]
        self.partition_rows = partition_rows  # one row per recent partition
        self.data_calls: list[str] = []

    def __call__(self, sql, connection_config, **kwargs):
        if "information_schema.columns" in sql:
            return {"columns": ["column_name", "data_type", "extra_info"],
                    "rows": self.partition_cols,
                    "row_count": len(self.partition_cols)}
        if "$partitions" in sql:
            return {"columns": ["dt"], "rows": self.partition_rows,
                    "row_count": len(self.partition_rows)}
        self.data_calls.append(sql)
        return {"columns": ["c"], "rows": [["1"]], "row_count": 1}


def test_guarded_tool_injects_predicate_and_notes_it(monkeypatch):
    fake = _FakeRunQuery(
        partition_cols=[["dt", "date", "partition key"],
                        ["qty", "int", ""]],
        partition_rows=[["2026-07-15"], ["2026-07-14"]],
    )
    monkeypatch.setattr(at, "run_athena_query", fake)
    guarded = sa._make_scoped_athena_run_query(_CFG)

    out = json.loads(guarded._tool_func(sql="SELECT count(*) FROM events"))

    assert len(fake.data_calls) == 1
    executed = fake.data_calls[0]
    assert 'WHERE "dt" IN (DATE \'2026-07-15\', DATE \'2026-07-14\')' in executed
    assert "auto-scoped" in out["note"]
    assert "override" in out["note"]
    assert out["row_count"] == 1  # the real result still comes back


def test_guarded_tool_scoped_sql_executes_verbatim_without_note(monkeypatch):
    fake = _FakeRunQuery(
        partition_cols=[["dt", "date", "partition key"]],
        partition_rows=[["2026-07-15"]],
    )
    monkeypatch.setattr(at, "run_athena_query", fake)
    guarded = sa._make_scoped_athena_run_query(_CFG)

    original = "SELECT count(*) FROM events WHERE dt = DATE '2026-05-01'"
    out = json.loads(guarded._tool_func(sql=original))

    assert fake.data_calls == [original]
    assert "note" not in out


def test_guarded_tool_unpartitioned_passthrough(monkeypatch):
    fake = _FakeRunQuery(partition_cols=[["c", "varchar", ""]], partition_rows=[])
    monkeypatch.setattr(at, "run_athena_query", fake)
    guarded = sa._make_scoped_athena_run_query(_CFG)

    out = json.loads(guarded._tool_func(sql="SELECT count(*) FROM plain_table"))

    assert fake.data_calls == ["SELECT count(*) FROM plain_table"]
    assert "note" not in out


def test_guarded_tool_surfaces_scan_budget_error_structured(monkeypatch):
    """The scanned-bytes budget (resolve_scan_budget) applies to the agent path:
    the guarded tool executes through the SAME run_athena_query, and a budget
    trip surfaces as the structured scan_budget_exceeded result."""

    def budget_blown(sql, connection_config, **kwargs):
        if "information_schema.columns" in sql:
            return {"columns": [], "rows": [], "row_count": 0}  # unpartitioned
        raise at.AthenaScanBudgetError(
            "over budget", scanned_bytes=999, budget_bytes=100,
            guidance=["narrow the range"],
        )

    monkeypatch.setattr(at, "run_athena_query", budget_blown)
    guarded = sa._make_scoped_athena_run_query(_CFG)

    out = json.loads(guarded._tool_func(sql="SELECT count(*) FROM events"))

    assert out["error_type"] == "scan_budget_exceeded"
    assert out["scanned_bytes"] == 999
    assert out["budget_bytes"] == 100


# =========================================================================== #
# 7. Wiring — create_system_agent's Athena branch carries the guarded tool
# =========================================================================== #
class _FakeModel:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs


class _FakeAgent:
    def __init__(self, *args, **kwargs):
        self.system_prompt = kwargs.get("system_prompt", "")
        self.tools = kwargs.get("tools") or []
        self.hooks = kwargs.get("hooks") or []


def test_create_system_agent_athena_branch_uses_guarded_run_query(monkeypatch):
    monkeypatch.setattr(sa, "Agent", _FakeAgent)
    monkeypatch.setattr("strands.models.bedrock.BedrockModel", _FakeModel)
    agent = sa.create_system_agent(
        system_id="lake-athena",
        system_name="Lake Athena",
        schema="lake_db",
        system_prompt="",
        tables_desc="events(dt: date (partition key), qty: int)",
        connection_type="Athena",
        connection_config=dict(_CFG),
    )
    names = [getattr(t, "tool_name", "") for t in agent.tools]
    assert names.count("athena_run_query") == 1
    run_tool = next(t for t in agent.tools if t.tool_name == "athena_run_query")
    # The guarded tool advertises the auto-scope contract in its spec.
    assert "auto-scoped" in run_tool.tool_spec.get("description", "")
    # And the prompt carries the Athena efficiency + budget directives.
    assert "QUERY EFFICIENCY (required)" in agent.system_prompt
    assert "Known partition columns — events: dt" in agent.system_prompt
    assert "BUDGET (required)" in agent.system_prompt


# =========================================================================== #
# 8. Hardened predicate recognition + fail-closed complex shapes
# =========================================================================== #
def test_partition_name_only_in_select_is_not_scoped(monkeypatch):
    """SELECT/MAX/GROUP/ORDER mentions are not restricting WHERE predicates."""
    _stub_pruning(
        monkeypatch,
        pcols=[{"name": "event_day", "data_type": "date"}],
        predicate='"event_day" IN (DATE \'2026-07-15\')',
    )
    sql, decision = sa._scope_athena_sql(
        "SELECT max(event_day) FROM readings GROUP BY event_day ORDER BY event_day",
        _CFG,
    )
    assert 'WHERE "event_day" IN' in sql
    assert decision == {
        "applied": True,
        "reason": "recent_partition_injected",
        "table": "readings",
        "partition_columns": ["event_day"],
    }


@pytest.mark.parametrize(
    "predicate",
    [
        "event_day >= DATE '2026-01-01'",
        "event_day IN (DATE '2026-01-01', DATE '2026-01-02')",
        "event_day BETWEEN DATE '2026-01-01' AND DATE '2026-01-02'",
    ],
)
def test_real_where_partition_predicates_pass_through(monkeypatch, predicate):
    calls = _stub_pruning(
        monkeypatch,
        pcols=[{"name": "event_day", "data_type": "date"}],
        predicate='"event_day" IN (DATE \'2026-07-15\')',
    )
    original = f"SELECT count(*) FROM readings WHERE {predicate}"  # nosec B608 - test fixture composing expected SQL from module constants
    sql, decision = sa._scope_athena_sql(original, _CFG)
    assert sql == original
    assert decision["applied"] is False
    assert decision["reason"] == "explicit_partition_predicate"
    assert decision["partition_columns"] == ["event_day"]
    assert "pred" not in calls


def test_partition_name_in_string_or_comment_is_not_scoped(monkeypatch):
    _stub_pruning(
        monkeypatch,
        pcols=[{"name": "event_day", "data_type": "date"}],
        predicate='"event_day" IN (DATE \'2026-07-15\')',
    )
    original = (
        "SELECT count(*) FROM readings "
        "WHERE note = 'event_day >= 2026-01-01' -- event_day IN (1)"
    )
    sql, decision = sa._scope_athena_sql(original, _CFG)
    assert 'WHERE "event_day" IN' in sql
    assert decision["applied"] is True


def test_unscoped_one_table_cte_fails_closed_before_execution(monkeypatch):
    fake = _FakeRunQuery(
        partition_cols=[["event_day", "date", "partition key"]],
        partition_rows=[["2026-07-15"]],
    )
    monkeypatch.setattr(at, "run_athena_query", fake)
    guarded = sa._make_scoped_athena_run_query(_CFG)
    out = json.loads(guarded._tool_func(
        sql="WITH recent AS (SELECT event_day, value FROM readings) "
            "SELECT max(value) FROM recent"
    ))
    assert out["error_type"] == "scope_required"
    assert out["scope"]["reason"] == "scope_required"
    assert out["scope"]["table"] == "readings"
    assert out["scope"]["partition_columns"] == ["event_day"]
    assert out["guidance"]
    assert fake.data_calls == []


def test_scoped_one_table_cte_executes_verbatim(monkeypatch):
    fake = _FakeRunQuery(
        partition_cols=[["event_day", "date", "partition key"]],
        partition_rows=[["2026-07-15"]],
    )
    monkeypatch.setattr(at, "run_athena_query", fake)
    guarded = sa._make_scoped_athena_run_query(_CFG)
    original = (
        "WITH recent AS (SELECT value FROM readings "
        "WHERE event_day BETWEEN DATE '2026-01-01' AND DATE '2026-01-02') "
        "SELECT max(value) FROM recent"
    )
    out = json.loads(guarded._tool_func(sql=original))
    assert fake.data_calls == [original]
    assert out["row_count"] == 1


def test_metadata_miss_with_baked_partition_hint_fails_closed(monkeypatch):
    fake = _FakeRunQuery(partition_cols=[], partition_rows=[])
    monkeypatch.setattr(at, "run_athena_query", fake)
    guarded = sa._make_scoped_athena_run_query(
        _CFG, baked_partition_columns={"readings": ["event_day"]}
    )
    out = json.loads(guarded._tool_func(
        sql="WITH x AS (SELECT value FROM readings) SELECT max(value) FROM x"
    ))
    assert out["error_type"] == "scope_required"
    assert out["scope"]["partition_columns"] == ["event_day"]
    assert fake.data_calls == []


def test_timeout_error_retains_applied_scope_decision(monkeypatch):
    calls = {"data": 0}

    def timed_out(sql, connection_config, **kwargs):
        if "information_schema.columns" in sql:
            return {
                "columns": ["column_name", "data_type", "extra_info"],
                "rows": [["event_day", "date", "partition key"]],
                "row_count": 1,
            }
        if "$partitions" in sql:
            return {"columns": ["event_day"], "rows": [["2026-07-15"]], "row_count": 1}
        calls["data"] += 1
        raise at.AthenaQueryTimeoutError(
            query_id="query-123",
            last_state="RUNNING",
            statistics={"DataScannedInBytes": 4096},
            poll_timeout=30.0,
        )

    monkeypatch.setattr(at, "run_athena_query", timed_out)
    guarded = sa._make_scoped_athena_run_query(_CFG)
    out = json.loads(guarded._tool_func(sql="SELECT count(*) FROM readings"))
    # A query whose poll window ends unfinished is run once more as a fresh
    # query (a stalled poll is a real transient); the second timeout is
    # returned and says it was retried.
    assert calls["data"] == 2
    assert out["retried_once"] is True
    assert out["error_type"] == "query_timeout"
    assert out["query_id"] == "query-123"
    assert out["last_state"] == "RUNNING"
    assert out["scanned_bytes"] == 4096
    assert out["scope"] == {
        "applied": True,
        "reason": "recent_partition_injected",
        "table": "readings",
        "partition_columns": ["event_day"],
    }


# =========================================================================== #
# 9. Adversarial relation/scope proof — every physical partitioned scan
# =========================================================================== #
class _RelationAwareRunQuery:
    def __init__(self, partitioned=("readings", "events")):
        self.partitioned = set(partitioned)
        self.metadata_calls: list[str] = []
        self.data_calls: list[str] = []

    def __call__(self, sql, connection_config, **kwargs):
        if "information_schema.columns" in sql:
            self.metadata_calls.append(sql)
            table = next((name for name in self.partitioned if f"table_name = '{name}'" in sql), "")
            rows = [["event_day", "date", "partition key"]] if table else []
            return {"columns": ["column_name", "data_type", "extra_info"], "rows": rows,
                    "row_count": len(rows)}
        if "$partitions" in sql:
            return {"columns": ["event_day"], "rows": [["2026-07-15"]], "row_count": 1}
        self.data_calls.append(sql)
        return {"columns": ["c"], "rows": [["1"]], "row_count": 1}


def _guarded_out(monkeypatch, sql, *, fake=None, baked=None):
    fake = fake or _RelationAwareRunQuery()
    monkeypatch.setattr(at, "run_athena_query", fake)
    guarded = sa._make_scoped_athena_run_query(_CFG, baked_partition_columns=baked)
    return json.loads(guarded._tool_func(sql=sql)), fake


def test_unscoped_join_fails_closed_with_zero_data_execution(monkeypatch):
    original = "SELECT count(*) FROM readings r JOIN events e ON r.id = e.id"
    out, fake = _guarded_out(monkeypatch, original)
    assert out["error_type"] == "scope_required"
    assert fake.data_calls == []
    assert len(fake.metadata_calls) == 2


def test_scoped_join_executes_verbatim_when_each_alias_is_constrained(monkeypatch):
    original = (
        "SELECT count(*) FROM readings r JOIN events e ON r.id = e.id "
        "WHERE r.event_day >= DATE '2026-07-01' "
        "AND e.event_day BETWEEN DATE '2026-07-01' AND DATE '2026-07-15'"
    )
    out, fake = _guarded_out(monkeypatch, original)
    assert out["row_count"] == 1
    assert fake.data_calls == [original]
    assert len(fake.metadata_calls) == 2


def test_quoted_qualified_partitioned_table_fails_closed(monkeypatch):
    original = 'SELECT count(*) FROM "lake_db"."readings" AS "r"'
    out, fake = _guarded_out(monkeypatch, original)
    assert out["error_type"] == "scope_required"
    assert fake.data_calls == []


def test_cte_with_optional_column_aliases_is_proved_per_physical_scan(monkeypatch):
    original = (
        "WITH recent(day, amount) AS ("
        "SELECT event_day, amount FROM readings r "
        "WHERE r.event_day >= DATE '2026-07-01') "
        "SELECT sum(amount) FROM recent"
    )
    out, fake = _guarded_out(monkeypatch, original)
    assert out["row_count"] == 1
    assert fake.data_calls == [original]


def test_unscoped_self_subquery_fails_even_when_outer_scan_is_scoped(monkeypatch):
    original = (
        "SELECT count(*) FROM readings r WHERE r.event_day = DATE '2026-07-15' "
        "AND EXISTS (SELECT 1 FROM readings r2 WHERE r2.id = r.id)"
    )
    out, fake = _guarded_out(monkeypatch, original)
    assert out["error_type"] == "scope_required"
    assert fake.data_calls == []


@pytest.mark.parametrize(
    "unsafe_predicate",
    [
        "event_day = event_day",
        "event_day != DATE '2026-07-15'",
        "event_day <> DATE '2026-07-15'",
        "event_day = DATE '2026-07-15' OR TRUE",
    ],
)
def test_nonrestricting_partition_predicates_do_not_prove_scope(monkeypatch, unsafe_predicate):
    original = f"SELECT count(*) FROM readings WHERE {unsafe_predicate}"  # nosec B608 - test fixture composing expected SQL from module constants
    out, fake = _guarded_out(monkeypatch, original)
    # A simple query may be safely auto-scoped; the unsafe predicate itself must
    # never authorize unchanged execution.
    assert fake.data_calls
    assert fake.data_calls[0] != original
    assert '"event_day" IN' in fake.data_calls[0]


def test_reversed_constant_partition_comparison_executes_verbatim(monkeypatch):
    original = "SELECT count(*) FROM readings WHERE DATE '2026-07-01' <= event_day"
    out, fake = _guarded_out(monkeypatch, original)
    assert out["row_count"] == 1
    assert fake.data_calls == [original]


def test_cross_database_qualification_uses_target_database_metadata(monkeypatch):
    fake = _RelationAwareRunQuery(partitioned=("readings",))
    original = "SELECT count(*) FROM other_db.readings WHERE event_day = DATE '2026-07-15'"
    out, fake = _guarded_out(monkeypatch, original, fake=fake)
    assert out["row_count"] == 1
    assert fake.data_calls == [original]
    assert any("table_schema = 'other_db'" in sql for sql in fake.metadata_calls)


def test_cross_database_relation_does_not_inherit_configured_database_baked_hint(monkeypatch):
    fake = _RelationAwareRunQuery(partitioned=())
    original = "SELECT count(*) FROM other_db.readings"
    out, fake = _guarded_out(
        monkeypatch,
        original,
        fake=fake,
        baked={"lake_db.readings": ["event_day"]},
    )
    assert out["row_count"] == 1
    assert fake.data_calls == [original]


def test_comma_join_fails_closed_before_data_execution(monkeypatch):
    original = "SELECT count(*) FROM readings r, events e WHERE r.id = e.id"
    out, fake = _guarded_out(monkeypatch, original)
    assert out["error_type"] == "scope_required"
    assert fake.data_calls == []


def test_self_join_requires_each_alias_to_be_partition_constrained(monkeypatch):
    original = (
        "SELECT count(*) FROM readings r1 JOIN readings r2 ON r1.id = r2.id "
        "WHERE readings.event_day = DATE '2026-07-15'"
    )
    out, fake = _guarded_out(monkeypatch, original)
    assert out["error_type"] == "scope_required"
    assert fake.data_calls == []


def test_cte_self_shadowing_does_not_hide_partitioned_physical_scan(monkeypatch):
    """A non-recursive CTE name is not in scope inside its own body."""
    original = "WITH readings AS (SELECT * FROM readings) SELECT count(*) FROM readings"
    out, fake = _guarded_out(monkeypatch, original)
    assert out["error_type"] == "scope_required"
    assert out["scope"]["table"] == "readings"
    assert out["scope"]["partition_columns"] == ["event_day"]
    assert fake.data_calls == []


def test_later_cte_alias_does_not_shadow_earlier_physical_scan(monkeypatch):
    original = (
        "WITH first_read AS (SELECT * FROM readings), "
        "readings AS (SELECT 1 AS n) SELECT count(*) FROM first_read"
    )
    out, fake = _guarded_out(monkeypatch, original)
    assert out["error_type"] == "scope_required"
    assert out["scope"]["table"] == "readings"
    assert fake.data_calls == []


def test_nested_with_alias_does_not_hide_outer_physical_scan(monkeypatch):
    original = (
        "WITH outer_read AS (SELECT * FROM readings), nested AS ("
        "WITH readings AS (SELECT 1 AS n) SELECT * FROM readings) "
        "SELECT count(*) FROM outer_read"
    )
    out, fake = _guarded_out(monkeypatch, original)
    assert out["error_type"] == "scope_required"
    assert out["scope"]["table"] == "readings"
    assert fake.data_calls == []


def test_baked_unpartitioned_table_skips_live_partition_discovery(monkeypatch):
    def unexpected_lookup(*args, **kwargs):
        raise AssertionError("baked [] must skip live partition discovery")

    monkeypatch.setattr(at, "list_partition_columns", unexpected_lookup)
    original = "SELECT count(*) FROM plain_table"
    sql, decision = sa._scope_athena_sql(
        original, _CFG, baked_partition_columns={"plain_table": []}
    )
    assert sql == original
    assert decision["reason"] == "unpartitioned"


def test_baked_unpartitioned_complex_cte_join_passes_without_live_lookup(monkeypatch):
    def unexpected_lookup(*args, **kwargs):
        raise AssertionError("known-unpartitioned scans must not introspect live metadata")

    monkeypatch.setattr(at, "list_partition_columns", unexpected_lookup)
    original = (
        "WITH recent AS (SELECT id, amount FROM plain_events WHERE amount > 0) "
        "SELECT sum(recent.amount) FROM recent "
        "JOIN plain_sites s ON recent.id = s.id"
    )
    baked = sa._athena_partition_columns_from_desc(
        f"plain_events(id: varchar, amount: decimal(10, 2)) {_NO_PART}. "
        f"plain_sites(id: varchar, name: varchar) {_NO_PART}"
    )
    assert baked == {"plain_events": [], "plain_sites": []}
    sql, decision = sa._scope_athena_sql(
        original, _CFG, baked_partition_columns=baked
    )
    assert sql == original
    assert decision["reason"] == "unpartitioned"


def test_marker_free_baked_desc_still_runs_live_discovery(monkeypatch):
    """The completeness gate: a legacy/marker-free desc yields NO baked entry, so the
    live information_schema lookup still happens — absence never masquerades as
    proof of an unpartitioned table."""
    calls: list[str] = []

    def discover(table, cfg, **kwargs):
        calls.append(table)
        return [{"name": "dt", "data_type": "date"}]

    monkeypatch.setattr(at, "list_partition_columns", discover)
    baked = sa._athena_partition_columns_from_desc(
        "legacy_events(id: varchar, amount: decimal(10, 2))"
    )
    assert baked == {}
    # An already-scoped query keeps the rewrite path out of the way, so ``calls``
    # isolates exactly the metadata discovery this test is about.
    original = "SELECT count(*) FROM legacy_events WHERE dt = DATE '2026-08-01'"
    sql, decision = sa._scope_athena_sql(
        original, _CFG, baked_partition_columns=baked
    )
    assert calls == ["legacy_events"]         # live discovery preserved
    assert sql == original
    assert decision["reason"] == "explicit_partition_predicate"
    assert decision["partition_columns"] == ["dt"]


@pytest.mark.parametrize("desc", [
    "legacy(a: int, b: varchar)",                       # marker-free (legacy)
    "truncated(a: int",                                 # truncated entry
    "name_only_a, name_only_b",                         # names-only fallback
    "malformed(: varchar)",                             # malformed column
])
def test_non_affirmative_descs_never_bake_an_empty_entry(desc):
    """Every non-affirmative descriptor shape stays ABSENT from the baked map."""
    assert sa._athena_partition_columns_from_desc(desc) == {}


def test_baked_unpartitioned_uncertain_parse_keeps_known_detail(monkeypatch):
    def unexpected_lookup(*args, **kwargs):
        raise AssertionError("baked [] must establish partition metadata")

    monkeypatch.setattr(at, "list_partition_columns", unexpected_lookup)
    sql, decision = sa._scope_athena_sql(
        "SELECT count(*) FROM plain_table;",
        _CFG,
        baked_partition_columns={"plain_table": []},
    )
    assert sql == "SELECT count(*) FROM plain_table;"
    assert decision["reason"] == "scope_required"
    assert decision["partition_metadata"] == "known_unpartitioned"


def test_baked_partition_columns_keep_live_discovery_and_merge(monkeypatch):
    calls: list[str] = []

    def discover(table, cfg, **kwargs):
        calls.append(table)
        return [{"name": "ingest_day", "data_type": "date"}]

    monkeypatch.setattr(at, "list_partition_columns", discover)
    original = (
        "SELECT count(*) FROM readings "
        "WHERE event_day = DATE '2026-08-01'"
    )
    sql, decision = sa._scope_athena_sql(
        original, _CFG,
        baked_partition_columns={"readings": ["event_day"]},
    )
    assert calls == ["readings"]
    assert sql == original
    assert decision["reason"] == "explicit_partition_predicate"
    assert decision["partition_columns"] == ["ingest_day", "event_day"]


def test_table_absent_from_baked_map_still_discovers_live(monkeypatch):
    calls: list[str] = []

    def discover(table, cfg, **kwargs):
        calls.append(table)
        return []

    monkeypatch.setattr(at, "list_partition_columns", discover)
    original = "SELECT count(*) FROM newly_added"
    sql, decision = sa._scope_athena_sql(
        original, _CFG, baked_partition_columns={"known_plain": []}
    )
    assert calls == ["newly_added"]
    assert sql == original
    assert decision["reason"] == "unpartitioned"


def _relation(*parts: str):
    """Build a minimal athena_scope.Relation for direct helper-level tests."""
    from agents import athena_scope

    return athena_scope.Relation(parts=parts, quoted=False, alias="", depth=0,
                                 select_index=0, table_token_index=0)


def test_load_partition_metadata_marks_baked_empty_as_known(monkeypatch):
    """Task I at the scope layer: a baked ``[]`` entry yields metadata_known=True
    with ZERO partition names and NO live lookup, which is what makes the
    downstream uncertain-parse detail ``known_unpartitioned`` (deterministic
    block) rather than ``unknown`` (retryable transient)."""
    from agents import athena_scope

    class ExplodingTools:
        @staticmethod
        def list_partition_columns(table, cfg, **kwargs):
            raise AssertionError("baked [] must skip live partition discovery")

    items = athena_scope._load_partition_metadata(
        [_relation("plain_table")], _CFG, {"plain_table": []}, ExplodingTools
    )
    assert len(items) == 1
    _rel, names, _cfg, metadata_known = items[0]
    assert names == []
    assert metadata_known is True
    assert athena_scope._partition_knowledge(items[0]) == "known_unpartitioned"


def test_load_partition_metadata_unknown_when_discovery_fails(monkeypatch):
    """Contrast: NO baked entry + a failing live lookup stays ``unknown`` (the
    honest retryable class) — the baked-[] skip must not swallow that signal."""
    from agents import athena_scope

    class FailingTools:
        @staticmethod
        def list_partition_columns(table, cfg, **kwargs):
            raise RuntimeError("throttled")

    items = athena_scope._load_partition_metadata(
        [_relation("other_table")], _CFG, {}, FailingTools)
    assert items[0][3] is False
    assert athena_scope._partition_knowledge(items[0]) == "unknown"


def test_baked_partition_metadata_distinguishes_empty_from_absent():
    """``[]`` (known, zero columns) and absent (unknown) must be distinguishable —
    the whole point of Task I; ``baked_partition_names`` keeps its legacy
    names-only return for existing callers."""
    from agents import athena_scope

    relation = _relation("plain_table")
    names, known = athena_scope._baked_partition_metadata(
        relation, {"plain_table": []}, _CFG)
    assert (names, known) == ([], True)
    names, known = athena_scope._baked_partition_metadata(relation, {}, _CFG)
    assert (names, known) == ([], False)
    names, known = athena_scope._baked_partition_metadata(
        relation, {"plain_table": ["dt"]}, _CFG)
    assert (names, known) == (["dt"], True)
    assert athena_scope.baked_partition_names(
        relation, {"plain_table": ["dt"]}, _CFG) == ["dt"]


def test_quoted_cte_with_optional_columns_keeps_scoped_physical_scan(monkeypatch):
    original = (
        'WITH "recent"("day", "amount") AS ('
        'SELECT event_day, amount FROM readings r '
        "WHERE r.event_day >= DATE '2026-07-01') "
        'SELECT sum("amount") FROM "recent"'
    )
    out, fake = _guarded_out(monkeypatch, original)
    assert out["row_count"] == 1
    assert fake.data_calls == [original]


def test_recursive_cte_same_name_fails_closed_without_execution(monkeypatch):
    original = (
        "WITH RECURSIVE readings AS (SELECT * FROM readings) "
        "SELECT count(*) FROM readings"
    )
    out, fake = _guarded_out(monkeypatch, original)
    assert out["error_type"] == "scope_required"
    assert out["scope"]["table"] == "readings"
    assert fake.data_calls == []


# =========================================================================== #
# 10. Partition-knowledge detail on scope decisions (failure honesty)
# =========================================================================== #
def test_known_partitioned_block_keeps_partition_guidance_without_retryable(monkeypatch):
    """Known partition columns → the deterministic partition-predicate guidance
    stands, with NO retryable label (retrying the identical SQL cannot help)."""
    fake = _FakeRunQuery(
        partition_cols=[["event_day", "date", "partition key"]],
        partition_rows=[["2026-07-15"]],
    )
    monkeypatch.setattr(at, "run_athena_query", fake)
    guarded = sa._make_scoped_athena_run_query(_CFG)
    out = json.loads(guarded._tool_func(
        sql="WITH x AS (SELECT * FROM readings) SELECT count(*) FROM x"
    ))
    assert out["error_type"] == "scope_required"
    assert out["scope"]["partition_metadata"] == "known_partitioned"
    assert "retryable" not in out
    assert "partition column" in " ".join(out["guidance"])
    assert fake.data_calls == []


def test_metadata_discovery_failure_marks_decision_unknown(monkeypatch):
    def boom(table, cfg, **kw):
        raise RuntimeError("information_schema access denied")

    monkeypatch.setattr(at, "list_partition_columns", boom)
    _sql, decision = sa._scope_athena_sql("SELECT count(*) FROM events", _CFG)
    assert decision["reason"] == "scope_required"
    assert decision["partition_metadata"] == "unknown"


def test_applied_and_passthrough_decisions_carry_no_partition_metadata_key(monkeypatch):
    """The new detail is serialized ONLY on scope_required decisions that know
    it — applied/pass-through decision shapes stay byte-identical."""
    _stub_pruning(
        monkeypatch,
        pcols=[{"name": "dt", "data_type": "date"}],
        predicate="\"dt\" IN (DATE '2026-07-15')",
    )
    _sql, applied = sa._scope_athena_sql("SELECT count(*) FROM events", _CFG)
    assert applied["applied"] is True
    assert "partition_metadata" not in applied

    original = "SELECT count(*) FROM events WHERE dt >= DATE '2026-01-01'"
    _sql2, passthrough = sa._scope_athena_sql(original, _CFG)
    assert passthrough["reason"] == "explicit_partition_predicate"
    assert "partition_metadata" not in passthrough
