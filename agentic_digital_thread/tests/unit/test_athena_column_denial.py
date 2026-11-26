# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for Lake Formation column-level denial handling.

LIVE-CONFIRMED problem: Glue/graph metadata lists columns whose SELECT is denied
by Lake Formation column-level grants; Athena fails the WHOLE query with
``COLUMN_NOT_FOUND: ... Column <x> cannot be resolved or requester is not
authorized to access requested resources``. The fix is a SHARED bounded
prune-and-retry seam (``athena_tools.run_athena_select_with_column_retry``):

* the SECTION path (``entity_render``) drops the denied column and retries —
  the rendered envelope/lineage reflect the ACTUALLY selected columns plus a
  note naming the omission;
* the SCALAR/GROUPED metric paths have nothing to drop (the denied column IS
  the measure/dimension) → a CLEAR error naming the LF restriction instead of
  the cryptic COLUMN_NOT_FOUND.

Fully offline — the Athena connector is stubbed (NO AWS); synthetic names only.
"""
from __future__ import annotations

import pytest

import agents.athena_tools as at
import agents.entity_render as er
import agents.metric_dimension as md
import agents.metric_resolver as mr
from agents.entity_render import SectionSpec
from agents.metric_layer import Aggregation
from agents.metric_resolver import MetricResolverError


_CFG = {"workgroup": "wg", "database": "lake_db"}

_DENIAL_TEXT = (
    "Athena query failed: COLUMN_NOT_FOUND: line 1:28: Column {col} cannot be "
    "resolved or requester is not authorized to access requested resources"
)


def _denial(col: str) -> RuntimeError:
    return RuntimeError(_DENIAL_TEXT.format(col=col))


# ===========================================================================
# (A) parse_denied_column — robust across quote styles
# ===========================================================================

@pytest.mark.parametrize("rendered,expected", [
    ("'op_shift_code'", "op_shift_code"),       # single-quoted
    ('"op_shift_code"', "op_shift_code"),       # double-quoted
    ("`op_shift_code`", "op_shift_code"),       # backtick
    ("op_shift_code", "op_shift_code"),         # bare
    ("m.op_shift_code", "op_shift_code"),       # alias-qualified → last segment
    ("'tbl.op_shift_code'", "op_shift_code"),   # table-qualified + quoted
])
def test_parse_denied_column_quote_styles(rendered, expected):
    assert at.parse_denied_column(_DENIAL_TEXT.format(col=rendered)) == expected


def test_parse_denied_column_requires_column_not_found_marker():
    # A resolvable-looking sentence WITHOUT the COLUMN_NOT_FOUND marker → None.
    assert at.parse_denied_column(
        "SYNTAX_ERROR: Column 'x' cannot be resolved") is None


def test_parse_denied_column_unparsable_text_is_none():
    assert at.parse_denied_column("COLUMN_NOT_FOUND: something else entirely") is None
    assert at.parse_denied_column("") is None
    assert at.parse_denied_column(None) is None


# ===========================================================================
# (B) run_athena_select_with_column_retry — the shared bounded helper
# ===========================================================================

class _DenyingRunner:
    """Athena stub that denies a configured set of columns: any SQL referencing
    one raises the LF-style COLUMN_NOT_FOUND for the FIRST such column."""

    def __init__(self, denied, result=None):
        self.denied = list(denied)
        self.result = result or {"columns": [], "rows": [["v"]], "row_count": 1}
        self.calls: list[str] = []

    def __call__(self, sql, connection_config):
        self.calls.append(sql)
        for col in self.denied:
            if f'"{col}"' in sql:
                raise _denial(col)
        return self.result


def _compose(cols):
    return "SELECT " + ",".join(f'"{c}"' for c in cols) + ' FROM "db"."t" LIMIT 5'  # nosec B608 - test fixture composing expected SQL from module constants


def test_retry_drops_exactly_the_named_column_and_succeeds():
    runner = _DenyingRunner(["c2"])
    out = at.run_athena_select_with_column_retry(
        _compose, ["c1", "c2", "c3", "c4"], _CFG, run=runner)
    assert out["columns"] == ["c1", "c3", "c4"]     # exactly c2 dropped
    assert out["denied"] == ["c2"]
    assert out["result"]["rows"] == [["v"]]
    assert len(runner.calls) == 2
    assert '"c2"' not in runner.calls[-1]


def test_each_retry_may_name_another_denied_column():
    runner = _DenyingRunner(["c1", "c4"])
    out = at.run_athena_select_with_column_retry(
        _compose, ["c1", "c2", "c3", "c4"], _CFG, run=runner)
    assert out["columns"] == ["c2", "c3"]
    assert out["denied"] == ["c1", "c4"]
    assert len(runner.calls) == 3                   # initial + 2 bounded retries


def test_retry_bound_is_half_columns_or_five_whichever_smaller():
    # 4 columns → bound min(5, 2) = 2 retries; a third denial re-raises.
    runner = _DenyingRunner(["c1", "c2", "c3"])
    with pytest.raises(RuntimeError, match="COLUMN_NOT_FOUND"):
        at.run_athena_select_with_column_retry(
            _compose, ["c1", "c2", "c3", "c4"], _CFG, run=runner)
    assert len(runner.calls) == 3                   # initial + 2 retries, then raise
    # 12 columns → bound min(5, 6) = 5 retries.
    cols = [f"c{i}" for i in range(12)]
    runner = _DenyingRunner(cols[:6])
    with pytest.raises(RuntimeError, match="COLUMN_NOT_FOUND"):
        at.run_athena_select_with_column_retry(_compose, cols, _CFG, run=runner)
    assert len(runner.calls) == 6                   # initial + 5 retries


# ---------------------------------------------------------------------------
# Nested struct paths — pruning tracks the FULL dotted composed name
# ---------------------------------------------------------------------------

def test_match_denied_column_exact_then_leaf_then_none():
    cols = ["flat", "entity_type_details.request.cell"]
    assert at._match_denied_column(cols, "flat") == "flat"
    # Exact full-dotted match wins outright.
    assert at._match_denied_column(
        cols, "entity_type_details.request.cell") == "entity_type_details.request.cell"
    # A leaf-only parse maps back to the full dotted composed name.
    assert at._match_denied_column(cols, "CELL") == "entity_type_details.request.cell"
    assert at._match_denied_column(cols, "nope") is None


def test_retry_prunes_nested_column_by_full_dotted_name():
    # The section path composes struct dereferences from FULL dotted names
    # (entity_render.build_athena_section_sql); Athena's COLUMN_NOT_FOUND may
    # name only the leaf. The prune must remove/record the FULL dotted name so
    # recomposition and the section note stay truthful.
    def compose(cols):
        items = []
        for c in cols:
            parts = c.split(".")
            deref = ".".join(f'"{p}"' for p in parts)
            items.append(deref if len(parts) == 1 else f'{deref} AS "{c}"')
        return "SELECT " + ",".join(items) + ' FROM "db"."t" LIMIT 5'  # nosec B608 - test fixture composing expected SQL from module constants

    calls: list[str] = []

    def runner(sql, connection_config):
        calls.append(sql)
        if '"cell"' in sql:
            raise _denial("'entity_type_details.request.cell'")  # leaf parse → 'cell'
        return {"columns": [], "rows": [["v"]], "row_count": 1}

    cols = ["entity_type_details.request.cell", "status"]
    out = at.run_athena_select_with_column_retry(compose, cols, _CFG, run=runner)
    assert out["columns"] == ["status"]
    assert out["denied"] == ["entity_type_details.request.cell"]   # FULL dotted name
    assert len(calls) == 2
    assert "entity_type_details" not in calls[-1]


def test_denied_column_not_in_composed_select_reraises_original():
    def runner(sql, cfg):
        raise _denial("other_col")
    with pytest.raises(RuntimeError, match="COLUMN_NOT_FOUND"):
        at.run_athena_select_with_column_retry(_compose, ["c1", "c2"], _CFG, run=runner)


def test_unparsable_error_keeps_original_path():
    def runner(sql, cfg):
        raise RuntimeError("Athena query failed: TABLE_NOT_FOUND: t")
    with pytest.raises(RuntimeError, match="TABLE_NOT_FOUND"):
        at.run_athena_select_with_column_retry(_compose, ["c1", "c2"], _CFG, run=runner)


def test_essential_column_denial_is_clear_lf_error():
    def runner(sql, cfg):
        raise _denial("qty")
    with pytest.raises(at.AthenaColumnDeniedError) as exc:
        at.run_athena_select_with_column_retry(
            lambda _c: "SELECT SUM(qty) FROM t", ["qty"], _CFG,
            essential=("qty",), run=runner)
    msg = str(exc.value)
    assert "Lake Formation" in msg and "'qty'" in msg
    assert "COLUMN_NOT_FOUND" not in msg            # the cryptic form is replaced
    assert exc.value.column == "qty"


def test_never_prunes_below_one_column():
    runner = _DenyingRunner(["c1", "c2"])
    with pytest.raises(RuntimeError, match="COLUMN_NOT_FOUND"):
        at.run_athena_select_with_column_retry(
            _compose, ["c1", "c2"], _CFG, run=runner, max_retries=5)


def test_case_insensitive_column_match():
    runner = _DenyingRunner([])
    calls = []

    def run(sql, cfg):
        calls.append(sql)
        if len(calls) == 1:
            raise _denial("C2")                     # engine upper-cases the name
        return {"rows": [["v"]], "row_count": 1}

    out = at.run_athena_select_with_column_retry(
        _compose, ["c1", "c2", "c3", "c4"], _CFG, run=run)
    assert out["columns"] == ["c1", "c3", "c4"]
    assert out["denied"] == ["c2"]


# ===========================================================================
# (C) Section path (entity_render) — envelope keeps going, lineage is honest
# ===========================================================================

_ATHENA_SYSTEM = {"id": "sys-lake", "name": "Lake System", "schema": "lake_db",
                  "connection_type": "Athena", "system_type": "MES"}


def _spec(columns):
    return SectionSpec(table="orders", entity="work-order",
                       entity_label="Work Order", columns=columns)


def _four_cols():
    return [
        {"column": "order_number", "label": "Order Number", "field_concept": "work-order"},
        {"column": "restricted_a", "label": "Restricted A", "field_concept": None},
        {"column": "status", "label": "Status", "field_concept": None},
        {"column": "qty", "label": "Qty", "field_concept": None},
    ]


def test_section_denied_column_pruned_with_note_and_honest_lineage(monkeypatch):
    monkeypatch.setattr("agents.metric_resolver._full_connection_config",
                        lambda sid: dict(_CFG))
    runner = _DenyingRunner(
        ["restricted_a"],
        result={"columns": ["order_number", "status", "qty"],
                "rows": [["WO-1", "OPEN", 5]], "row_count": 1})
    monkeypatch.setattr("agents.athena_tools.run_athena_query", runner)

    sec = er.render_section(_ATHENA_SYSTEM, _spec(_four_cols()),
                            query_fn=lambda s, q: pytest.fail("pg must not run"))
    assert sec["status"] == "ok"
    assert sec["rows"] == [["WO-1", "OPEN", 5]]
    # The envelope/lineage reflect the ACTUALLY selected columns.
    assert [c["column"] for c in sec["columns"]] == ["order_number", "status", "qty"]
    assert [ln["column"] for ln in sec["lineage"]] == ["order_number", "status", "qty"]
    # A note names the omission and the LF restriction; no error status.
    assert "restricted_a" in sec["note"]
    assert "Lake Formation" in sec["note"]
    assert "error" not in sec
    # The final executed SQL selected only the kept columns.
    assert '"restricted_a"' not in runner.calls[-1]
    assert '"order_number"' in runner.calls[-1]


def test_section_without_denials_has_no_note(monkeypatch):
    monkeypatch.setattr("agents.metric_resolver._full_connection_config",
                        lambda sid: dict(_CFG))
    monkeypatch.setattr(
        "agents.athena_tools.run_athena_query",
        lambda sql, cfg: {"rows": [["WO-1"]], "row_count": 1})
    sec = er.render_section(_ATHENA_SYSTEM, _spec(_four_cols()),
                            query_fn=lambda s, q: None)
    assert sec["status"] == "ok"
    assert "note" not in sec
    assert [c["column"] for c in sec["columns"]] == \
        ["order_number", "restricted_a", "status", "qty"]


def test_section_exhausted_retries_keeps_error_boundary(monkeypatch):
    # Every retry names another denied column until the bound trips → the section
    # degrades to today's honest error envelope (never raises, never fabricates).
    monkeypatch.setattr("agents.metric_resolver._full_connection_config",
                        lambda sid: dict(_CFG))
    runner = _DenyingRunner(["order_number", "restricted_a", "status", "qty"])
    monkeypatch.setattr("agents.athena_tools.run_athena_query", runner)
    sec = er.render_section(_ATHENA_SYSTEM, _spec(_four_cols()),
                            query_fn=lambda s, q: None)
    assert sec["status"] == "error"
    assert sec["rows"] == [] and sec["row_count"] == 0
    assert "Athena" in sec["error"]


# ===========================================================================
# (D) Scalar + grouped metric paths — nothing to drop → CLEAR LF error
# ===========================================================================

def test_scalar_measure_column_denial_is_clear_resolver_error(monkeypatch):
    plan = mr.QueryPlan(system_id="sys-lake", schema="lake_db", table="orders",
                        column="qty", aggregation=Aggregation.SUM)
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid: {"id": sid, "connection_type": "Athena"})
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: dict(_CFG))

    def boom(sql, cfg):
        raise _denial("qty")

    monkeypatch.setattr(mr.athena_tools, "run_athena_query", boom)
    with pytest.raises(MetricResolverError) as exc:
        mr._execute(plan, "SELECT SUM(qty) FROM lake_db.orders")
    msg = str(exc.value)
    assert "Lake Formation" in msg and "'qty'" in msg
    assert "COLUMN_NOT_FOUND" not in msg


def test_scalar_non_measure_denial_keeps_original_error(monkeypatch):
    plan = mr.QueryPlan(system_id="sys-lake", schema="lake_db", table="orders",
                        column="qty", aggregation=Aggregation.SUM)
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid: {"id": sid, "connection_type": "Athena"})
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: dict(_CFG))

    def boom(sql, cfg):
        raise _denial("event_ts")                   # e.g. the time-window column

    monkeypatch.setattr(mr.athena_tools, "run_athena_query", boom)
    with pytest.raises(RuntimeError, match="COLUMN_NOT_FOUND"):
        mr._execute(plan, "SELECT SUM(qty) FROM lake_db.orders")


def _grouped_plan():
    return md.GroupedQueryPlan(
        system_id="sys-lake", schema="lake_db",
        measure_table="orders", measure_column="qty",
        aggregation=Aggregation.SUM,
        dimension_table="orders", dimension_column="status",
    )


_GROUPED_SQL = ('SELECT "status", SUM("qty") FROM "lake_db"."orders" '
                'GROUP BY "status" ORDER BY "status" LIMIT 200')


@pytest.mark.parametrize("denied_col", ["qty", "status"])
def test_grouped_measure_or_dimension_denial_is_clear_resolver_error(
        monkeypatch, denied_col):
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid: {"id": sid, "connection_type": "Athena"})
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: dict(_CFG))

    def boom(sql, cfg):
        raise _denial(denied_col)

    monkeypatch.setattr(mr.athena_tools, "run_athena_query", boom)
    with pytest.raises(MetricResolverError) as exc:
        md._execute_grouped(_grouped_plan(), _GROUPED_SQL)
    msg = str(exc.value)
    assert "Lake Formation" in msg and f"'{denied_col}'" in msg
    assert "grouped query failed" not in msg        # NOT the generic wrap


def test_grouped_unrelated_denial_keeps_generic_wrap(monkeypatch):
    monkeypatch.setattr(mr.catalog, "get",
                        lambda sid: {"id": sid, "connection_type": "Athena"})
    monkeypatch.setattr(mr, "_full_connection_config", lambda sid: dict(_CFG))

    def boom(sql, cfg):
        raise _denial("some_other_col")

    monkeypatch.setattr(mr.athena_tools, "run_athena_query", boom)
    with pytest.raises(MetricResolverError, match="grouped query failed"):
        md._execute_grouped(_grouped_plan(), _GROUPED_SQL)


class TestNestedBudgetExtension:
    """Live 2026-07-25: 4 unverified struct paths exhausted the default
    prune budget — the section must survive pruning ALL of them."""

    def test_all_nested_paths_prunable_beyond_default_budget(self):
        from agents.athena_tools import (run_athena_select_with_column_retry,
                                         MAX_DENIED_COLUMN_RETRIES)
        nested = [f"a.b.col{i}" for i in range(MAX_DENIED_COLUMN_RETRIES + 2)]
        cols = ["flat_id"] + nested
        calls = {"n": 0}

        def run(sql, cfg):
            calls["n"] += 1
            for c in nested:
                if f'"{c}"' in sql or c.split(".")[-1] in sql:
                    leaf = next(x for x in nested
                                if x.split(".")[-1] in sql)
                    raise RuntimeError(
                        f"COLUMN_NOT_FOUND: line 1:5: Column '{leaf}' "
                        "cannot be resolved")
            return {"rows": [["r1"]]}

        out = run_athena_select_with_column_retry(
            lambda names: "SELECT " + ", ".join(f'"{n}"' for n in names),
            cols, {}, run=run,
            max_retries=len(nested) + MAX_DENIED_COLUMN_RETRIES)
        assert out["columns"] == ["flat_id"]
        assert sorted(out["denied"]) == sorted(nested)
