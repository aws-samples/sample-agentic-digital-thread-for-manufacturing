# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0076 D1/D4 — the ONE binding-selection rule and column-identifier home.

Pins: selection is order-independent (reversed graph rows → same outcome);
exactly one candidate is used with basis ``single``; 2+ candidates select ONLY
through an active steward rule naming exactly one candidate (basis
``authority_rule``); a stale / ambiguous / multi-pair rule WITHHOLDS naming
every candidate; the rule read runs only for 2+ candidates; flat names render
byte-identically and dotted names as ONE quoted identifier; hostile segments
fail closed; the module is pure. Placeholder data only (``widget_*``).
"""
from __future__ import annotations

import ast
import inspect

import pytest

import agents.metric_binding as mb

_ROWS = [
    {"table": "widget_events", "column": "parent.kind_code", "role": "measure"},
    {"table": "widget_events", "column": "kind_code", "role": "identifier"},
    {"table": "widget_events", "column": "detail.trigger.kind_code"},
]


def _cands(rows=_ROWS):
    return mb.candidates_from_rows(rows)


def _never():
    raise AssertionError("rule read must not run for a single candidate")


# --- candidates ------------------------------------------------------------ #

def test_candidates_are_sorted_distinct_and_exact():
    rows = _ROWS + [dict(_ROWS[1])]
    cands = _cands(rows)
    assert [(c.table, c.column) for c in cands] == [
        ("widget_events", "detail.trigger.kind_code"),
        ("widget_events", "kind_code"),
        ("widget_events", "parent.kind_code"),
    ]


def test_candidate_provenance_is_row_order_independent():
    rows = [{"table": "t", "column": "c", "role": "measure"},
            {"table": "t", "column": "c", "role": "correlate"}]
    assert _cands(rows) == _cands(list(reversed(rows)))
    assert _cands(rows)[0].role == "correlate"


def test_confidence_only_duplicates_are_row_order_independent():
    rows = [{"table": "t", "column": "c", "role": "measure", "confidence": 0.9},
            {"table": "t", "column": "c", "role": "measure", "confidence": 0.1},
            {"table": "t", "column": "c", "role": "measure", "confidence": None},
            {"table": "t", "column": "c", "role": "measure", "confidence": "hi"}]
    forward, backward = _cands(rows), _cands(list(reversed(rows)))
    assert forward == backward
    assert forward[0].confidence == 0.1


def test_kept_provenance_is_one_real_edge_never_a_blend():
    edges = [{"role": "a", "confidence": 0.9, "source": "z", "version": "2"},
             {"role": "b", "confidence": 0.1, "source": "a", "version": "1"}]
    cand = _cands([{"table": "t", "column": "c", "edges": edges}])[0]
    assert (cand.role, cand.confidence, cand.source, cand.version) == (
        "a", 0.9, "z", "2")


def _grouped_graph(flat_edges):
    """Emulate the candidate Cypher: implicit grouping on (table, column),
    ``collect`` of provenance, ORDER BY pair, LIMIT at the PAIR grain."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for e in flat_edges:
        groups.setdefault((e["table"], e["column"]), []).append(
            {k: e.get(k) for k in ("role", "confidence", "source", "version")})
    return [{"table": t, "column": c, "edges": groups[(t, c)]}
            for t, c in sorted(groups)][:mb.BINDING_CANDIDATE_READ_LIMIT]


def test_duplicate_provenance_beyond_the_cap_cannot_hide_a_second_pair():
    """Regression: 30 provenance variants of an alphabetically earlier
    pair precede a second pair — the page still holds BOTH pairs, so the
    selection withholds instead of computing from a false singleton."""
    flat = [{"table": "a_table", "column": "first_col", "confidence": i / 100}
            for i in range(mb.BINDING_CANDIDATE_CAP + 5)]
    flat.append({"table": "a_table", "column": "second_col"})
    cands = _cands(_grouped_graph(flat))
    assert [c.column for c in cands] == ["first_col", "second_col"]
    out = mb.select_binding("widget-kind", "sys-a", cands, lambda: [])
    assert isinstance(out, mb.BindingAmbiguity)
    assert "bound to more than one column" in out.detail
    assert "'first_col'" in out.detail and "'second_col'" in out.detail


def test_empty_rows_yield_no_candidates():
    assert mb.candidates_from_rows(None) == ()
    with pytest.raises(ValueError):
        mb.select_binding("widget-kind", "sys-a", (), _never)


# --- selection ------------------------------------------------------------- #

def test_single_candidate_is_used_without_reading_rules():
    choice = mb.select_binding("widget-kind", "sys-a", _cands(_ROWS[:1]), _never)
    assert isinstance(choice, mb.BindingChoice)
    assert choice.basis == mb.BASIS_SINGLE == "single"
    assert choice.candidate.column == "parent.kind_code"


@pytest.mark.parametrize("reverse", [False, True])
def test_two_plus_without_rule_withholds_naming_all_sorted(reverse):
    rows = list(reversed(_ROWS)) if reverse else _ROWS
    out = mb.select_binding("widget-kind", "sys-a", _cands(rows), lambda: [])
    assert isinstance(out, mb.BindingAmbiguity)
    assert out.detail == (
        "concept 'widget-kind' is bound to more than one column on system "
        "'sys-a' (widget_events: 'detail.trigger.kind_code', 'kind_code', "
        "'parent.kind_code'); a steward must choose one via POST "
        "/api/metrics/set-authority {field}")
    assert mb.AMBIGUOUS_BINDING_STATUS == "ambiguous_binding"


@pytest.mark.parametrize("table", [None, "widget_events"])
def test_rule_naming_one_candidate_wins(table):
    rules = [{"table": table, "field": "parent.kind_code"}]
    out = mb.select_binding("widget-kind", "sys-a", _cands(), lambda: rules)
    assert isinstance(out, mb.BindingChoice)
    assert out.basis == mb.BASIS_AUTHORITY_RULE == "authority_rule"
    assert out.candidate.column == "parent.kind_code"


def test_stale_rule_field_withholds_and_is_named():
    rules = [{"table": "widget_events", "field": "kind_cod"}]
    out = mb.select_binding("widget-kind", "sys-a", _cands(), lambda: rules)
    assert isinstance(out, mb.BindingAmbiguity)
    assert "the AuthorityRule field 'kind_cod' (table 'widget_events') names none" in out.detail


def test_rule_on_wrong_table_does_not_select():
    rules = [{"table": "widget_log", "field": "kind_code"}]
    out = mb.select_binding("widget-kind", "sys-a", _cands(), lambda: rules)
    assert isinstance(out, mb.BindingAmbiguity)


def test_tableless_rule_matching_two_tables_withholds():
    rows = [{"table": "a", "column": "kind_code"}, {"table": "b", "column": "kind_code"}]
    out = mb.select_binding("widget-kind", "sys-a", _cands(rows),
                            lambda: [{"table": None, "field": "kind_code"}])
    assert isinstance(out, mb.BindingAmbiguity)
    assert "names more than one of them" in out.detail
    assert "(a: 'kind_code'; b: 'kind_code')" in out.detail


def test_two_distinct_rule_pairs_withhold():
    rules = [{"table": "widget_events", "field": "kind_code"},
             {"table": "widget_events", "field": "parent.kind_code"}]
    out = mb.select_binding("widget-kind", "sys-a", _cands(), lambda: rules)
    assert isinstance(out, mb.BindingAmbiguity)
    assert "more than one active AuthorityRule carries a different field" in out.detail


def test_exactly_cap_candidates_is_exhaustive_and_a_rule_selects():
    rows = [{"table": "t", "column": f"c{i:02d}"} for i in range(mb.BINDING_CANDIDATE_CAP)]
    cands = _cands(rows)
    assert not mb.is_truncated(cands)
    out = mb.select_binding("widget-kind", "sys-a", cands,
                            lambda: [{"table": None, "field": "c00"}])
    assert isinstance(out, mb.BindingChoice) and out.candidate.column == "c00"


def test_sentinel_page_withholds_without_reading_rules():
    """Regression: the read returned the cap+1 sentinel, so a hidden
    same-field candidate may exist — a matching rule must NOT select."""
    assert mb.BINDING_CANDIDATE_READ_LIMIT == mb.BINDING_CANDIDATE_CAP + 1
    rows = [{"table": "t", "column": f"c{i:02d}"}
            for i in range(mb.BINDING_CANDIDATE_READ_LIMIT)]
    cands = _cands(rows)
    assert mb.is_truncated(cands)
    out = mb.select_binding("widget-kind", "sys-a", cands, _never)
    assert isinstance(out, mb.BindingAmbiguity)
    assert len(out.candidates) == mb.BINDING_CANDIDATE_CAP
    assert "bound to more than one column" in out.detail
    assert "; and more)" in out.detail
    assert "the candidate set is not exhaustive" in out.detail
    assert f"'c{mb.BINDING_CANDIDATE_CAP:02d}'" not in out.detail


def test_match_candidate_is_exact():
    cands = _cands()
    assert [c.column for c in mb.match_candidate(cands, "kind_code")] == ["kind_code"]
    assert mb.match_candidate(cands, "kind_cod") == []
    assert mb.match_candidate(cands, "kind_code", "other") == []


# --- Cypher templates ------------------------------------------------------- #

def test_cypher_groups_at_pair_grain_before_the_cap_on_the_right_scope():
    """ONE row per (table, column): provenance is COLLECTED inside the group,
    never part of the row identity, so LIMIT counts pairs, not edges."""
    for cypher in (mb.MEASURE_BINDING_CYPHER, mb.PREDICATE_BINDING_CYPHER):
        assert "RETURN se.name AS table, f.name AS column, collect({" in cypher
        assert "DISTINCT" not in cypher
        assert "AS edges ORDER BY table, column LIMIT" in cypher
        assert cypher.endswith(f"LIMIT {mb.BINDING_CANDIDATE_READ_LIMIT}")
    assert "(se:SchemaEntry {name: $table})" in mb.PREDICATE_BINDING_CYPHER
    assert "{name: $table}" not in mb.MEASURE_BINDING_CYPHER
    rule = mb.BINDING_RULE_CYPHER
    assert "{system_id: $sid, concept: $concept}" in rule
    assert "coalesce(ar.status, '') <> 'deactivated'" in rule
    assert "ar.field IS NOT NULL" in rule and rule.endswith("LIMIT 2")


# --- rendering -------------------------------------------------------------- #

@pytest.mark.parametrize("name", ["quantity_on_hand", "_x", "A1"])
def test_flat_names_render_bare_and_quoted_as_before(name):
    assert mb.render_column_identifier(name) == name
    assert mb.quote_column_identifier(name) == f'"{name}"'
    assert mb.validate_column_name(name) == name


def test_dotted_names_render_as_one_identifier():
    name = "detail.trigger.kind_code"
    assert mb.render_column_identifier(name) == '"detail.trigger.kind_code"'
    assert mb.quote_column_identifier(name) == '"detail.trigger.kind_code"'
    for rendered in (mb.render_column_identifier(name), mb.quote_column_identifier(name)):
        assert '"."' not in rendered and "detail_trigger" not in rendered


@pytest.mark.parametrize("bad", [
    "", ".a", "a.", "a..b", "a.b-c", 'a."b', "a.b;drop", "1a.b", "a.1b",
    "a b", "a\n", "a.b\n", "a.b)", None, 7,
])
def test_hostile_column_names_fail_closed(bad):
    for fn in (mb.validate_column_name, mb.render_column_identifier,
               mb.quote_column_identifier):
        with pytest.raises(ValueError):
            fn(bad)


# --- purity + size ---------------------------------------------------------- #

def test_module_is_pure():
    tree = ast.parse(inspect.getsource(mb))
    imported = {
        (n.module or "") if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in n.names
    }
    assert imported <= {"__future__", "re", "dataclasses", "typing"}


def test_every_function_is_at_most_50_lines():
    tree = ast.parse(inspect.getsource(mb))
    long = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
            and n.end_lineno - n.lineno + 1 > 50]
    assert long == []


# --- the withheld reason carries no digit ---------------------------------- #
# The metric layer tells the model to state this reason verbatim, and an
# absent-metric sentence must not carry a digit.

def _letter_names(count: int) -> list[str]:
    letters = "abcdefghijklmnopqrstuvwxyz"
    return [f"col_{letters[i // 26]}{letters[i % 26]}" for i in range(count)]


def test_ambiguity_reason_names_every_candidate_without_a_digit():
    out = mb.select_binding("widget-kind", "sys-a", _cands(), lambda: [])
    assert isinstance(out, mb.BindingAmbiguity)
    assert "bound to more than one column on system 'sys-a'" in out.detail
    assert "'kind_code'" in out.detail and "'parent.kind_code'" in out.detail
    assert not any(ch.isdigit() for ch in out.detail)


def test_truncated_ambiguity_reason_carries_no_digit():
    rows = [{"table": "t", "column": name}
            for name in _letter_names(mb.BINDING_CANDIDATE_READ_LIMIT)]
    out = mb.select_binding("widget-kind", "sys-a", _cands(rows), _never)
    assert isinstance(out, mb.BindingAmbiguity)
    assert "bound to more than one column" in out.detail
    assert "and more" in out.detail
    assert "the candidate set is not exhaustive" in out.detail
    assert not any(ch.isdigit() for ch in out.detail)


def test_conflicting_rules_reason_carries_no_digit():
    rules = [{"table": "widget_events", "field": "kind_code"},
             {"table": "widget_events", "field": "parent.kind_code"}]
    out = mb.select_binding("widget-kind", "sys-a", _cands(), lambda: rules)
    assert isinstance(out, mb.BindingAmbiguity)
    assert "more than one active AuthorityRule carries a different field" in out.detail
    assert not any(ch.isdigit() for ch in out.detail)
