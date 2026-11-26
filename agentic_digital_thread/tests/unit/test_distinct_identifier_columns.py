# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Onboarding measures same-named identifier columns that hold different ids (offline).

Workshop rehearsal, 2026-10-17 (Quick exact-2 and exact-6,
Module 2 Q4): one system keeps a concept under a same-named column in two
tables, both mapped as that concept's identifier, but each column holds a
different identifier: another shape, and no value in common. The system agent
was told only that both columns were the identifier, joined the two columns
to each other, looked values read from one up in the other (no rows) and
then rewrote the values into the other column's form by hand.

Pinned here with a fake ``pg`` and synthetic forms:

1. ``_dp_pg_tables_desc`` measures every such pair (``distinct_identifiers``)
   and the baked schema names each pair whose values have different shapes
   and never meet, with what the agent must and must not do about it.
2. A pair is never named when a value is shared, a side read too few
   values, a side's values have no consistent shape, the two shapes are the
   same or differ only in a segment's length, a value on one side has the
   other side's shape, or the read failed, timed out or ran out of budget.
   A pair the database's own foreign keys tie to one key is not even read.
3. The read is one deterministic, bounded query per pair under a short
   deadline and one total budget. The text leaves every table parser, the
   declared join keys and the samples as they were, names no dataset and
   leaves the IDENTIFIER-MISS shape protocol to the agent's own rules.

The same behavior on real PostgreSQL: tests/integration/test_erp_join_keys_pg.py.
"""
from __future__ import annotations

import pytest

import agents.onboard as onboard
from agents import distinct_identifiers as di
from agents import schema_format
from agents.context_placement import parse_cached_samples
from agents.response_integrity_sql import parse_physical_schema
from tests.unit._field_graph_fake import FieldGraph

_DETAILS = {
    "consumption": [("consumption_id", "uuid"), ("lot_id", "uuid"),
                    ("ref_code", "character varying")],
    "lots": [("lot_id", "uuid"), ("lot_code", "character varying")],
    "dispatch_lines": [("line_id", "uuid"), ("ref_code", "character varying"),
                       ("lot_id", "uuid")],
}
_CM = [
    {"concept": "unit", "field": "consumption.ref_code", "role": "identifier"},
    {"concept": "unit", "field": "dispatch_lines.ref_code", "role": "identifier"},
    {"concept": "material", "field": "consumption.lot_id", "role": "identifier"},
    {"concept": "material", "field": "dispatch_lines.lot_id", "role": "identifier"},
    {"concept": "material", "field": "lots.lot_id", "role": "identifier"},
]
_FKS = [("consumption", "lot_id", "lots", "lot_id"), ("dispatch_lines", "lot_id", "lots", "lot_id")]
_PAIR_TEXT = ("unit: consumption.ref_code and dispatch_lines.ref_code, "
              "40 and 37 values read, shaped like KX-9999 and QZ-9999")


def _forms(prefix, n, width=4, start=1):
    return [f"{prefix}-{i:0{width}d}" for i in range(start, start + n)]


_A = _forms("KX", 40)
_B = _forms("QZ", 37)


def _rows(a=_A, b=_B, seen_a=(), seen_b=()):
    return ([["a", v, v in seen_a] for v in a] + [["b", v, v in seen_b] for v in b])


def _ctx():
    return onboard._ProvisionState(
        body=None, system_id="sys-a", system_name="System A", plant="", schema="sys_a",
        rules=[], mappings=[], connection_type="PostgreSQL", cm_items=list(_CM),
        tables_seen=set(_DETAILS))


def _fake_pg(rows=None, fail=None, seen=None):
    """Answers the provision's column and measurement reads by shape. ``fail``
    is a result dict to return, or an exception to raise, for the measurement."""
    seen = seen if seen is not None else []
    rows = _rows() if rows is None else rows

    def pg(schema, sql, params=None, **kw):
        if "information_schema.columns WHERE table_schema = %s AND table_name" in sql:
            return {"rows": [list(c) for c in _DETAILS.get(params[1], [])]}
        if sql.startswith("WITH a AS (SELECT v FROM"):
            seen.append((sql, params, kw))
            if isinstance(fail, Exception):
                raise fail
            return fail if fail is not None else {"rows": rows}
        return {"rows": []}
    return pg


@pytest.fixture(autouse=True)
def _no_samples(monkeypatch):
    monkeypatch.setenv("SCHEMA_SAMPLE_VALUES", "off")
    monkeypatch.setattr(onboard, "_measured_foreign_keys", lambda _schema: [
        {"from_table": a, "from_field": b, "to_table": c, "to_field": d} for a, b, c, d in _FKS])


def _desc(monkeypatch, ctx=None, **kw):
    # The note is rendered from the graph record (test_distinct_identifier_graph.py):
    # a graph holding the Field nodes b2 writes for every mapped column.
    FieldGraph(auto_fields=True).install(monkeypatch)
    monkeypatch.setattr(onboard, "pg", _fake_pg(**kw))
    return onboard._dp_pg_tables_desc(ctx or _ctx())


# --------------------------------------------------------------------------- #
# The baked schema
# --------------------------------------------------------------------------- #

def test_a_measured_pair_is_named_in_the_baked_schema(monkeypatch):
    desc = _desc(monkeypatch)
    assert desc.endswith(schema_format.DISTINCT_IDENTIFIERS_PREFIX + _PAIR_TEXT + ".")


def test_the_text_tells_the_agent_what_to_do_and_not_do():
    text = schema_format.DISTINCT_IDENTIFIERS_PREFIX
    assert "the values read from the two columns have different shapes" in text
    assert "Never join the two columns to each other" in text
    assert "never look up a value of one in the other" in text
    assert "Never rewrite a value into the other column's form yourself" in text
    assert "you need that column's own identifiers" in text


def test_the_text_leaves_the_identifier_miss_shape_protocol_alone():
    """The agent's IDENTIFIER-MISS rule says a token whose shape matches the
    column's observed values is an honest zero, and the sanitizer strips a
    marker whose token matches its own format. The note claims neither."""
    text = schema_format.DISTINCT_IDENTIFIERS_PREFIX
    assert "IDENTIFIER-MISS" not in text
    assert "wrong vocabulary" not in text
    assert "look alike" not in text


def test_the_measurement_is_recorded_on_the_provision(monkeypatch):
    ctx = _ctx()
    _desc(monkeypatch, ctx=ctx)
    assert ctx.distinct_identifiers == [{
        "concept": "unit", "column": "ref_code", "table_a": "consumption",
        "table_b": "dispatch_lines", "read_a": 40, "read_b": 37,
        "signature_a": '[["-"],[["prefix","kx"],[4,"digit"]]]',
        "signature_b": '[["-"],[["prefix","qz"],[4,"digit"]]]',
        "shape_a": "KX-9999", "shape_b": "QZ-9999"}]
    assert onboard._dp_distinct_identifier_markers(ctx) == {
        "distinct_identifier_columns": ctx.distinct_identifiers}


def test_no_marker_when_nothing_was_measured():
    assert onboard._dp_distinct_identifier_markers(_ctx()) == {}


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

_NO_NOTE = {
    "a value of a is in b": _rows(seen_a={_A[3]}),
    "a value of b is in a": _rows(seen_b={_B[0]}),
    "a side read too few": _rows(a=_A[:4]),
    "the other side read too few": _rows(b=_B[:2]),
    "same shape, disjoint values": _rows(b=_forms("KX", 37, start=501)),
    "only a digit count differs": _rows(b=_forms("KX", 37, width=6)),
    "a uuid against a uuid": _rows(
        a=[f"{i:08x}-1111-2222-3333-444455556666" for i in range(40)],
        b=[f"{i:08x}-aaaa-bbbb-cccc-ddddeeeeffff" for i in range(500, 537)]),
    "a free-text side": _rows(a=[f"Station {chr(65 + i % 26)}{'x' * (i % 7)}" for i in range(40)]),
    "a mixed side, half each shape": _rows(a=_forms("KX", 20) + _forms("MW", 20)),
    "one value of a has the shape of b": _rows(a=_A[:39] + ["QZ-9999"]),
    "one value of b has the shape of a": _rows(b=_B[:36] + ["KX-9999"]),
    "a malformed row": [["a", "KX-0001"]] + _rows()[1:],
    "an unknown side": [["c", "KX-0001", False]] + _rows()[1:],
}


@pytest.mark.parametrize("rows", list(_NO_NOTE.values()), ids=list(_NO_NOTE))
def test_a_pair_is_named_only_on_different_consistent_shapes_and_no_shared_value(rows):
    assert di.verdict(rows) is None


@pytest.mark.parametrize("b", [
    _forms("QZ", 37),                                         # another prefix
    [f"KX{i:04d}" for i in range(37)],                        # another separator layout
    [f"KX-{i:04d}-01" for i in range(37)],                    # another segment count
    [f"KX-{i:03d}Z" for i in range(37)],                      # another character class
], ids=["prefix", "separators", "segments", "class"])
def test_shapes_that_differ_beyond_length_are_named(b):
    assert di.verdict(_rows(b=b)) == {"read_a": 40, "read_b": len(b)}


def test_a_few_odd_values_do_not_break_a_consistent_side():
    # 39 of 40 share one shape (over the 0.9 share); the odd one is no shape of b.
    assert di.verdict(_rows(a=_A[:39] + ["KX-ZZ"])) == {"read_a": 40, "read_b": 37}


def test_the_shape_is_the_agents_own_identifier_miss_signature():
    """A value of either column of a named pair never matches the other
    column's format in the agent's IDENTIFIER-MISS sanitizer, so its miss
    marker survives there."""
    from agents.system_agent import _shape_signature, _signatures_match
    assert not _signatures_match(_shape_signature(_A[0]), _shape_signature(_B[0]))
    assert di.side_shape(_A) == _shape_signature(_A[0])


def test_measure_reads_true_seen_flags_from_json_and_driver_rows():
    for flag in (True, "true", "t"):
        assert di.verdict([["a", _A[0], flag]] + _rows()[1:]) is None
    for flag in (False, "false", "f", None):
        assert di.verdict([["a", _A[0], flag]] + _rows()[1:]) is not None


# --------------------------------------------------------------------------- #
# The read
# --------------------------------------------------------------------------- #

def test_the_read_is_one_bounded_deterministic_query_per_pair(monkeypatch):
    seen: list = []
    _desc(monkeypatch, seen=seen)
    # Only the same-named "unit" pair is read: the material lot_id columns are
    # tied to one key by the foreign keys.
    assert len(seen) == 1
    sql, params, kw = seen[0]
    assert params == di.measure_params() == (di.SAMPLE_ROWS, di.SAMPLE_PER_END) * 4
    assert kw == {"statement_timeout_ms": di.QUERY_TIMEOUT_MS}
    assert '"sys_a"."consumption"' in sql and '"sys_a"."dispatch_lines"' in sql
    assert sql.count("LIMIT %s") == 8 and "%" not in sql.replace("%s", "")
    # Each end is read in column order, from a bounded number of rows.
    assert sql.count('ORDER BY "ref_code" ASC LIMIT %s') == 2
    assert sql.count('ORDER BY "ref_code" DESC LIMIT %s') == 2
    assert sql.count("EXISTS (SELECT 1") == 2


@pytest.mark.parametrize("fail,reason", [
    ({"rows": [], "unavailable": True, "error_category": "OperationalError"}, "failed"),
    ({"rows": [], "unavailable": True, "error_category": "QueryCanceled"}, "timeout"),
    ({"rows": _rows(), "truncated": True}, "failed"),
    (RuntimeError("down"), "failed"),
    (type("QueryCanceled", (Exception,), {})("canceling statement"), "timeout"),
], ids=["unavailable", "deadline", "truncated", "raised", "raised deadline"])
def test_a_failed_or_timed_out_read_names_nothing_and_keeps_the_schema(monkeypatch, fail, reason):
    ctx = _ctx()
    desc = _desc(monkeypatch, ctx=ctx, fail=fail)
    assert schema_format.DISTINCT_IDENTIFIERS_PREFIX not in desc
    assert set(parse_physical_schema(desc)) == set(_DETAILS)
    assert onboard._dp_distinct_identifier_markers(ctx) == {
        "distinct_identifier_not_measured": [{
            "concept": "unit", "column": "ref_code", "table_a": "consumption",
            "table_b": "dispatch_lines", "reason": reason}]}


def _many_pairs(n):
    cm, details = [], {}
    for i in range(n):
        for side in ("a", "b"):
            details[f"t{i}{side}"] = [("ref", "text")]
            cm.append({"concept": f"c{i}", "field": f"t{i}{side}.ref", "role": "identifier"})
    return cm, details


def test_the_total_budget_stops_the_reads_and_records_the_rest():
    cm, details = _many_pairs(5)
    now = [0.0]

    def clock():
        return now[0]

    seen: list = []
    base = _fake_pg(seen=seen)

    def slow_pg(schema, sql, params=None, **kw):
        now[0] += 4.0                      # every read takes 4 s
        return base(schema, sql, params, **kw)

    out = di.measure(slow_pg, "s", cm, details, [], clock=clock)
    # 10 s budget: reads at 0 s and 4 s get the full deadline, the one at 8 s
    # gets what is left, and none starts after the budget is spent.
    assert [kw["statement_timeout_ms"] for _s, _p, kw in seen] == [3000, 3000, 2000]
    assert [p["reason"] for p in out["not_measured"]] == ["budget", "budget"]
    assert len(out["distinct"]) == 3


def test_a_read_never_starts_with_less_than_the_floor_left():
    cm, details = _many_pairs(2)
    now = [0.0]
    seen: list = []
    base = _fake_pg(seen=seen)

    def pg(schema, sql, params=None, **kw):
        now[0] += di.TOTAL_BUDGET_S - 0.4  # the first read leaves 400 ms
        return base(schema, sql, params, **kw)

    out = di.measure(pg, "s", cm, details, [], clock=lambda: now[0])
    assert len(seen) == 1
    assert [p["reason"] for p in out["not_measured"]] == ["budget"]


def test_pairs_past_the_cap_are_counted_not_read(monkeypatch):
    monkeypatch.setattr(di, "MAX_PAIRS", 1)
    seen: list = []
    out = di.measure(_fake_pg(seen=seen), "sys_a", _CM, _DETAILS, [])
    assert len(seen) == 1
    assert [p["reason"] for p in out["not_measured"]] == ["pair_cap"] * 3


def test_different_declared_types_compare_as_text():
    pair = {"concept": "unit", "column": "ref_code", "table_a": "t1", "table_b": "t2"}
    assert "CAST(" not in di.measure_sql("s", pair, same_type=True)
    # The four sampled ends and the two lookups.
    assert di.measure_sql("s", pair, same_type=False).count("CAST(") == 6


@pytest.mark.parametrize("bad", ['x"; DROP TABLE t; --', "a b", ""])
def test_an_unsafe_name_is_never_read(bad):
    seen: list = []
    cm = [{"concept": "unit", "field": f"{bad}.ref_code", "role": "identifier"},
          {"concept": "unit", "field": "consumption.ref_code", "role": "identifier"}]
    details = {bad: [("ref_code", "text")], "consumption": [("ref_code", "text")]}
    out = di.measure(_fake_pg(seen=seen), "sys_a", cm, details, [])
    assert seen == [] and out["distinct"] == []


# --------------------------------------------------------------------------- #
# Candidate pairs
# --------------------------------------------------------------------------- #

def test_only_same_named_identifier_columns_of_one_concept_pair_up():
    cm = [*_CM,
          {"concept": "unit", "field": "lots.lot_code", "role": "identifier"},       # other name
          {"concept": "other", "field": "lots.ref_code", "role": "identifier"},      # other concept
          {"concept": "unit", "field": "elsewhere.ref_code", "role": "identifier"},  # not rendered
          {"concept": "unit", "field": "lots.ref_code", "role": "measure"},          # not an identifier
          {"concept": "unit", "field": "no_table", "role": "identifier"}, "junk"]
    pairs = di.candidate_pairs(cm, _DETAILS, [
        {"from_table": a, "from_field": b, "to_table": c, "to_field": d} for a, b, c, d in _FKS])
    assert pairs == [{"concept": "unit", "column": "ref_code", "table_a": "consumption",
                      "table_b": "dispatch_lines"}]


def test_without_foreign_keys_the_key_columns_are_measured_too():
    pairs = di.candidate_pairs(_CM, _DETAILS, [])
    assert [(p["concept"], p["table_a"], p["table_b"]) for p in pairs] == [
        ("material", "consumption", "dispatch_lines"), ("material", "consumption", "lots"),
        ("material", "dispatch_lines", "lots"), ("unit", "consumption", "dispatch_lines")]


def test_a_column_referencing_the_other_is_not_measured():
    pairs = di.candidate_pairs(_CM, _DETAILS, [
        {"from_table": "dispatch_lines", "from_field": "ref_code",
         "to_table": "consumption", "to_field": "ref_code"}])
    assert all(p["concept"] != "unit" for p in pairs)


# --------------------------------------------------------------------------- #
# The render
# --------------------------------------------------------------------------- #

def test_parsers_join_keys_and_samples_read_as_before():
    desc = schema_format.format_schema_with_samples(
        _DETAILS, {"consumption": {"ref_code": ["A-1", "A-2"]},
                   "dispatch_lines": {"ref_code": ["B-1"]}})
    keyed = schema_format.with_join_keys(desc, [
        {"from_table": "consumption", "from_field": "lot_id", "to_table": "lots",
         "to_field": "lot_id"}], _DETAILS)
    found = [{"concept": "unit", "column": "ref_code", "table_a": "consumption",
              "table_b": "dispatch_lines", "read_a": 40, "read_b": 37,
              "shape_a": "KX-9999", "shape_b": "QZ-9999"}]
    for before in (desc, keyed):
        out = schema_format.with_distinct_identifiers(before, found)
        assert parse_physical_schema(out) == parse_physical_schema(before)
        assert parse_cached_samples(out) == parse_cached_samples(before)
        assert schema_format.declared_join_keys(out) == schema_format.declared_join_keys(before)
        added = out[len(before):]
        assert not set(added) & set("()[]") and "e.g." not in added
        assert "\u2014" not in added and "\u2013" not in added
        assert added.endswith(_PAIR_TEXT + ".")


@pytest.mark.parametrize("found", [None, [], [{}], ["not a pair"],
                                   [{"concept": "unit", "column": "ref code", "table_a": "a",
                                     "table_b": "b", "read_a": 9, "read_b": 9}],
                                   [{"concept": "unit", "column": "c", "table_a": "a(x)",
                                     "table_b": "b", "read_a": 9, "read_b": 9}],
                                   [{"concept": "unit", "column": "c", "table_a": "a",
                                     "table_b": "b", "read_a": "nine", "read_b": 9}]])
def test_nothing_usable_leaves_the_desc_byte_identical(found):
    desc = schema_format.format_tables_desc(_DETAILS)
    assert schema_format.with_distinct_identifiers(desc, found) == desc
    assert schema_format.with_distinct_identifiers("", found or [{}]) == ""


def test_the_text_names_no_dataset():
    low = schema_format.DISTINCT_IDENTIFIERS_PREFIX.lower()
    for word in ("batch", "run", "work order", "work_order", "shipment", "lot", "order",
                 "wo-", "erp", "mes"):
        assert word not in low, word
